"""ONTA-278 acceptance: entailment-aware NL→SPARQL query expansion.

Two entailment axes the read path must follow so a query returns what it logically
should — proven END-TO-END over a real pyoxigraph store, not only string-tested:

  (1) SUPERTYPE → SUBTYPE. A query over a parent type must return subtype
      instances. The subclass-closure rewrite (``rdf:type/rdfs:subClassOf*``) was
      only string-tested before; here it is asserted against a real store: seed a
      ``Clinic ⊑ Organization`` edge + a Clinic instance, run the closure-rewritten
      "all Organizations" query, and prove the Clinic instance comes back.

  (2) MERGED entity under EITHER alias (Wave-3 ONTA-274). A merge re-keys the
      merged node's facts onto the canonical and records ``(canonical, onto/sameAs,
      merged)``. A query that PINS the merged-away IRI directly would otherwise find
      nothing. ``rewrite_entity_ref_to_sameas_closure`` routes a pinned entity IRI
      through the bidirectional walk ``(<sameAs>|^<sameAs>)*`` so BOTH aliases
      resolve the identical unified fact set. The LOAD-BEARING control shows the raw
      (un-rewritten) merged-away query returns nothing — so the rewrite, not the
      store, is what unifies the aliases.

Plus string-level unit tests of the rewriter: subject/object position gains the
sameAs path, idempotency, and a negative control (a non-``entities/`` URI is left
untouched — the rewrite never disturbs the type-closure/attribute rewrites).
"""
from __future__ import annotations

import json

import pytest

from cograph_client.graph.kg_writer import insert_facts
from cograph_client.graph.ontology_queries import (
    SAME_AS,
    entity_uri,
    rewrite_entity_ref_to_sameas_closure,
)
from cograph_client.graph.queries import kg_graph_uri

TENANT, KG = "onta278", "corp"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)

TWITTER = entity_uri("Company", "twitter_inc")
XCORP = entity_uri("Company", "x_corp")

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDFS_SUBCLASSOF = "http://www.w3.org/2000/01/rdf-schema#subClassOf"

ORGANIZATION = "https://cograph.tech/types/Organization"
CLINIC = "https://cograph.tech/types/Clinic"
CLINIC1 = entity_uri("Clinic", "sunrise")

COMPANY = "https://cograph.tech/types/Company"
HAS_CEO = "https://cograph.tech/onto/hasCEO"
LEGAL_NAME = "https://cograph.tech/onto/legalName"
HAS_PRODUCT = "https://cograph.tech/onto/hasProduct"
OWNER = "https://cograph.tech/onto/owner"
EMPLOYEE_COUNT = "https://cograph.tech/onto/employeeCount"
XSD_INT = "http://www.w3.org/2001/XMLSchema#integer"

# The exact bidirectional walk the rewriter injects (full-URI form of the ticket's
# `(<onto/sameAs>|^<onto/sameAs>)*` shorthand).
SAMEAS_PATH = f"(<{SAME_AS}>|^<{SAME_AS}>)*"

TWITTER_FACTS = [
    (RDF_TYPE, COMPANY),
    (HAS_CEO, "Jack"),
    (LEGAL_NAME, "Twitter Inc"),
    (EMPLOYEE_COUNT, f"7500^^{XSD_INT}"),
]
XCORP_FACTS = [
    (RDF_TYPE, COMPANY),
    (HAS_PRODUCT, "X"),
    (OWNER, "Musk"),
]


# --------------------------------------------------------------------------- #
# 1. Pure string-level unit tests of the rewriter (no store)
# --------------------------------------------------------------------------- #
def test_subject_entity_ref_gains_sameas_path():
    q = f"SELECT ?p ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ <{TWITTER}> ?p ?o }} }}"
    out = rewrite_entity_ref_to_sameas_closure(q)
    # The pinned subject is routed through the bidirectional walk into a fresh var.
    assert f"<{TWITTER}> {SAMEAS_PATH} ?_sa0 . ?_sa0 ?p ?o" in out
    # The original bare `<E> ?p ?o` triple is gone.
    assert f"<{TWITTER}> ?p ?o" not in out


def test_object_entity_ref_gains_sameas_path():
    q = f"SELECT ?s WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ ?s <{OWNER}> <{XCORP}> }} }}"
    out = rewrite_entity_ref_to_sameas_closure(q)
    # The pinned object is reached through the walk from a fresh intermediate var.
    assert f"?s <{OWNER}> ?_sa0 . ?_sa0 {SAMEAS_PATH} <{XCORP}>" in out


def test_rewrite_is_idempotent_subject():
    q = f"SELECT ?p ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ <{TWITTER}> ?p ?o }} }}"
    once = rewrite_entity_ref_to_sameas_closure(q)
    twice = rewrite_entity_ref_to_sameas_closure(once)
    assert once == twice
    # Exactly one walk was introduced (no double-wrapping).
    assert once.count("sameAs>)*") == 1


def test_rewrite_is_idempotent_object():
    q = f"SELECT ?s WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ ?s <{OWNER}> <{XCORP}> }} }}"
    once = rewrite_entity_ref_to_sameas_closure(q)
    twice = rewrite_entity_ref_to_sameas_closure(once)
    assert once == twice
    assert once.count("sameAs>)*") == 1


def test_non_entity_uris_are_left_untouched():
    # A `types/` object (subclass closure) and an `onto/` predicate + `attrs/`
    # reference carry no `entities/` IRI — the sameAs rewrite must be a pure no-op,
    # so it can never disturb the type-closure or attribute rewrites that run in
    # the same _fix_common_sparql_issues pass.
    q = (
        f"SELECT ?x WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ "
        f"?x <{RDF_TYPE}>/<{RDFS_SUBCLASSOF}>* <{ORGANIZATION}> . "
        f"?x <{OWNER}> ?o . "
        f"?x <https://cograph.tech/types/Clinic/attrs/name> ?n "
        f"}} }}"
    )
    assert rewrite_entity_ref_to_sameas_closure(q) == q


# --------------------------------------------------------------------------- #
# 2. Real end-to-end over a pyoxigraph store (the acceptance bar)
# --------------------------------------------------------------------------- #
pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402

from cograph_client.nlp.pipeline import NLQueryPipeline  # noqa: E402
from cograph_client.pipeline.mutations import merge_entities  # noqa: E402


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — async
    query()/update() returning SPARQL-1.1 JSON, union-of-named-graphs default
    (identical to tests/test_merge_split.py)."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)


@pytest.fixture(autouse=True)
def _quiet_housekeeping(monkeypatch):
    """Silence the shared refresh_after_write downstreams (cache-invalidate / embed /
    stats recompute) so the tests isolate the entailment mechanism — exactly as
    tests/test_merge_split.py does."""
    import cograph_client.api.routes.explore as explore_mod
    import cograph_client.nlp.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda g: None)
    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)


async def _seed(n: PyoxiNeptune, subject: str, facts: list[tuple[str, str]]):
    await insert_facts(n, INSTANCE_GRAPH, [(subject, p, o) for p, o in facts])


async def _facts_of(n: PyoxiNeptune, subject: str) -> set[tuple[str, str]]:
    raw = await n.query(
        f"SELECT ?p ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ <{subject}> ?p ?o }} }}"
    )
    return {(b["p"]["value"], b["o"]["value"]) for b in raw["results"]["bindings"]}


async def _run_facts(n: PyoxiNeptune, sparql: str) -> set[tuple[str, str]]:
    """Run a projected-?p/?o query and return its (predicate, object-value) set."""
    raw = await n.query(sparql)
    return {(b["p"]["value"], b["o"]["value"]) for b in raw["results"]["bindings"]}


def _fix(sparql: str) -> str:
    """Route a query through the real read-path fixer — this is what proves the ONE
    wiring line in _fix_common_sparql_issues is live (covers CLI/MCP/webapp/agent)."""
    return NLQueryPipeline._fix_common_sparql_issues(sparql, "")


# --------------------- (1) supertype → subtype, end-to-end -------------------- #
@pytest.mark.asyncio
async def test_supertype_query_returns_subtype_instance_end_to_end():
    """Closes the currently string-only closure gap over a REAL store: a query for
    `Organization` instances returns a `Clinic` instance because `Clinic ⊑
    Organization` and the closure rewrite walks `rdf:type/subClassOf*`."""
    n = PyoxiNeptune()
    # Seed the subclass edge + a Clinic instance (all in the instance graph so the
    # property path resolves within one GRAPH scope).
    await insert_facts(n, INSTANCE_GRAPH, [(CLINIC, RDFS_SUBCLASSOF, ORGANIZATION)])
    await _seed(n, CLINIC1, [(RDF_TYPE, CLINIC), (RDFS_LABEL, "Sunrise Clinic")])

    # Sanity: a NON-closured query for Organizations finds nothing (the instance is
    # a Clinic, not a direct Organization) — so the closure is load-bearing.
    plain = (
        f"SELECT ?x WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ "
        f"?x <{RDF_TYPE}> <{ORGANIZATION}> }} }}"
    )
    plain_rows = (await n.query(plain))["results"]["bindings"]
    assert plain_rows == [], "direct rdf:type query must NOT see the subtype instance"

    # The user's "all Organizations" query, run through the real read-path fixer.
    q = f"SELECT ?x WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ ?x a <{ORGANIZATION}> }} }}"
    fixed = _fix(q)
    assert "subClassOf>*" in fixed, "the fixer must apply the subclass closure"
    rows = (await n.query(fixed))["results"]["bindings"]
    got = {b["x"]["value"] for b in rows}
    assert CLINIC1 in got, "supertype query must return the subtype instance"


# --------------------- (2) merged entity under either alias ------------------- #
@pytest.mark.asyncio
async def test_merged_entity_resolves_under_either_alias_end_to_end():
    """THE acceptance bar: after merge_entities(canonical=x_corp), a query pinned to
    the merged-away twitter_inc IRI AND a query pinned to canonical x_corp both
    resolve the SAME unified fact set once routed through the sameAs-expanded
    builder."""
    n = PyoxiNeptune()
    await _seed(n, TWITTER, TWITTER_FACTS)
    await _seed(n, XCORP, XCORP_FACTS)

    await merge_entities(
        n, INSTANCE_GRAPH, a=TWITTER, b=XCORP, canonical=XCORP, type_name="Company",
        reason="SEC filing: Twitter Inc renamed X Corp", run_id="run-278",
    )

    # Post-merge, the merged-away IRI carries NO facts of its own (they were re-keyed
    # onto the canonical). This is the state the rewrite must see through.
    assert await _facts_of(n, TWITTER) == set()

    # LOAD-BEARING control: the RAW (un-rewritten) merged-away query finds nothing —
    # so the sameAs rewrite, not the store, is what unifies the aliases.
    raw_twitter = f"SELECT ?p ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ <{TWITTER}> ?p ?o }} }}"
    assert await _run_facts(n, raw_twitter) == set()

    # Now route BOTH alias queries through the real read-path fixer.
    twitter_fixed = _fix(raw_twitter)
    xcorp_fixed = _fix(f"SELECT ?p ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ <{XCORP}> ?p ?o }} }}")
    assert "sameAs>)*" in twitter_fixed and "sameAs>)*" in xcorp_fixed

    twitter_result = await _run_facts(n, twitter_fixed)
    xcorp_result = await _run_facts(n, xcorp_fixed)

    # Both aliases resolve the IDENTICAL unified fact set.
    assert twitter_result == xcorp_result
    assert twitter_result, "the merged-away alias must now resolve the canonical's facts"

    # And that set is the union of both original nodes' facts.
    for pred, obj in [
        (HAS_CEO, "Jack"),          # merged-exclusive
        (LEGAL_NAME, "Twitter Inc"),  # merged-exclusive
        (HAS_PRODUCT, "X"),         # canonical-exclusive
        (OWNER, "Musk"),            # canonical-exclusive
        (RDF_TYPE, COMPANY),        # shared
        (EMPLOYEE_COUNT, "7500"),   # typed literal, lexical form
    ]:
        assert (pred, obj) in twitter_result, f"unified set missing {(pred, obj)!r}"
