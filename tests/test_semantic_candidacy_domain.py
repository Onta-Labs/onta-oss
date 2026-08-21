"""Candidacy domains: one unclassifiable attribute must not abort a reconcile.

The regression: ``_apply_default_candidacy`` fell back to the literal type name
``"Entity"`` for any literal predicate whose leaf the ontology catalog does not
declare. ``Entity`` is a RESERVED SYSTEM LABEL (the Neo4j label every instance
node carries), so the catalog's B2 gate raised

    GraphScopeError: Domain label 'Entity' collides with a reserved system label

out of ``commit_ontology`` — in the MIDDLE of ``reconcile_kg``, before the
upsert and ghost passes. Every scheduled run of a KG carrying one such leaf
(``rdfs:label`` dual-writes an Entity ``name`` property, so most of them) died
there and the KG's semantic index never refreshed again.

Two halves are pinned here, matching the two ways a bad domain can arrive:

* **synthesized** — the fallback above, on the GraphStore Assertion path
  (``…/properties/<leaf>`` predicates carry no type segment at all);
* **stored** — a ``types/Entity/attrs/<a>`` predicate genuinely present in the
  instance graph, which the ``_ATTR_URI_RE`` branch would parse a reserved type
  name straight out of.

Plus the backstop: a verdict commit that fails for any OTHER reason is logged
and skipped, never allowed to take the run down with it.
"""

from __future__ import annotations

import asyncio
import re

import pytest
import structlog

import infona_client.graph.ontology_commit as oc
import infona_client.graph.text_markers as tm
import infona_client.semantic.reconciler as rec
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import upsert_attribute, upsert_type
from infona_client.graph.ontology_queries import attr_uri
from infona_client.graph.queries import kg_graph_uri
from infona_client.graph.store import configure_graph_store
from infona_client.models.ontology import OntologyMutation, OntologyOpKind
from infona_client.scheduling.store import reset_schedule_store
from infona_client.semantic.memory import InMemorySemanticIndex
from infona_client.semantic.registry import reset_semantic_index

TENANT = "t1"
KG = "kg1"
GRAPH = kg_graph_uri(TENANT, KG)
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
DOC_TYPE = "https://graph.infona.ai/types/Doc"
DESC_PRED = attr_uri("Doc", "description")
#: The stored half: a predicate whose TYPE segment is a reserved system label.
ENTITY_PRED = attr_uri("Entity", "note")
ENTITY = "https://graph.infona.ai/entities/Doc/e1"

PROSE = (
    "The committee heard extensive testimony about the proposed changes to the "
    "watershed management plan and debated the funding formula for well over "
    "two hours before adjourning without a final vote on the matter."
)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    reset_semantic_index()
    tm.reset_for_tests()
    rec.reset_for_tests()
    reset_schedule_store()
    monkeypatch.setenv("INFONA_SEMANTIC_INDEX_ENABLED", "true")
    yield
    reset_semantic_index()
    tm.reset_for_tests()
    rec.reset_for_tests()
    reset_schedule_store()


@pytest.fixture
def store():
    st = MemoryGraphStore()
    configure_graph_store(st)
    return st


@pytest.fixture
def index():
    return InMemorySemanticIndex()


class SparqlKg:
    """Minimal SPARQL stub for the reconciler's four query shapes.

    Only the stored-predicate half needs one: ``types/{T}/attrs/{a}`` predicates
    reach ``_apply_default_candidacy`` intact via SPARQL, whereas the GraphStore
    Assertion path reports property IRIs that carry no type segment.
    ``commit_ontology`` still takes its GraphStore branch (a store is always
    configured), so no ``update`` handler is needed.
    """

    def __init__(self, entities: dict[str, dict[str, list[str]]]) -> None:
        self.entities = entities

    @staticmethod
    def _rows(rows: list[dict[str, str]], variables: list[str]) -> dict:
        return {
            "head": {"vars": variables},
            "results": {
                "bindings": [
                    {k: {"type": "literal", "value": v} for k, v in row.items()}
                    for row in rows
                ]
            },
        }

    async def query(self, sparql: str) -> dict:
        if "SELECT DISTINCT ?p" in sparql:
            preds = sorted(
                {
                    p
                    for pv in self.entities.values()
                    for p, vals in pv.items()
                    if p != RDF_TYPE and vals
                }
            )
            return self._rows([{"p": p} for p in preds], ["p"])
        if sparql.startswith("SELECT ?o FROM"):
            pred = re.search(r"\?e <([^>]+)> \?o", sparql).group(1)
            limit = int(re.search(r"LIMIT (\d+)", sparql).group(1))
            values = [v for pv in self.entities.values() for v in pv.get(pred, [])]
            return self._rows([{"o": v} for v in values[:limit]], ["o"])
        if "SELECT ?e ?p ?o" in sparql and "VALUES ?p" in sparql:
            block = re.search(r"VALUES \?p \{ ([^}]*)\}", sparql).group(1)
            preds = set(re.findall(r"<([^>]+)>", block))
            limit = int(re.search(r"LIMIT (\d+)", sparql).group(1))
            m = re.search(r'FILTER\(STR\(\?e\) > "((?:[^"\\]|\\.)*)"\)', sparql)
            after = m.group(1) if m else ""
            triples = sorted(
                (e, p, v)
                for e, pv in self.entities.items()
                if e > after
                for p, vals in pv.items()
                if p in preds
                for v in vals
            )
            return self._rows(
                [{"e": e, "p": p, "o": o} for e, p, o in triples[:limit]],
                ["e", "p", "o"],
            )
        # Marker / fingerprint / revision reads — no SPARQL-side markers here.
        return self._rows([], [])


def _capture_verdicts(monkeypatch) -> dict[tuple[str, str], str]:
    """``{(type, attr): kind}`` for every SET_TEXT_KIND actually committed."""
    seen: dict[tuple[str, str], str] = {}
    real = oc.commit_ontology

    async def recording(neptune, graph_uri, mutations, **kwargs):
        for mut in mutations:
            if mut.op is OntologyOpKind.SET_TEXT_KIND:
                seen[(mut.type_name, mut.slot_name)] = mut.text_kind
        return await real(neptune, graph_uri, mutations, **kwargs)

    monkeypatch.setattr(oc, "commit_ontology", recording)
    return seen


async def _mark_free_text(type_name: str, attr_name: str) -> None:
    """Declare one attribute free-text in the catalog (the marker source the
    reconciler reads — it deliberately bypasses the request-path cache)."""
    await oc.commit_ontology(
        None,
        f"https://graph.infona.ai/graphs/{TENANT}",
        [
            OntologyMutation(
                op=OntologyOpKind.SET_TEXT_KIND,
                type_name=type_name,
                slot_name=attr_name,
                text_kind="free_text",
            )
        ],
    )


def test_an_undeclared_literal_leaf_does_not_abort_the_reconcile(
    store, index, monkeypatch
):
    """The production abort, end to end.

    ``rdfs:label`` dual-writes an Entity ``name`` property, so the KG's literal
    predicates include ``…/properties/name`` — a leaf no ontology declares. The
    run must skip it (loudly, counted) and go on to refresh the index; before
    the fix it raised ``Domain label 'Entity' collides with a reserved system
    label`` and wrote nothing.
    """
    verdicts = _capture_verdicts(monkeypatch)

    async def run():
        await _mark_free_text("Doc", "description")
        await insert_facts(
            None,
            GRAPH,
            [
                (ENTITY, RDF_TYPE, DOC_TYPE),
                (ENTITY, DESC_PRED, PROSE),
                (ENTITY, RDFS_LABEL, "Session one"),
            ],
            store=store,
        )
        with structlog.testing.capture_logs() as logs:
            counters = await rec.reconcile_kg(None, TENANT, KG, index=index)

        # The marked attribute was indexed — the run reached its actual duty.
        assert counters["chunks_written"] >= 1
        assert (ENTITY, "description") in {
            (uri, attr) for uri, attr, *_ in await index.list_docs(TENANT, kg_name=KG)
        }
        # The undeclared leaf got no invented domain — least of all a reserved one.
        assert ("Entity", "name") not in verdicts
        assert not any(t == "Entity" for t, _ in verdicts)
        [skipped] = [
            e for e in logs if e["event"] == "semantic_candidacy_skipped_no_domain"
        ]
        assert skipped["attrs"] >= 1

    asyncio.run(run())


def test_a_declared_attribute_is_still_classified_beside_the_skipped_one(
    store, index, monkeypatch
):
    """The skip is scoped to leaves with no home type — it must not disarm
    candidacy for a declared attribute sharing the same run."""
    verdicts = _capture_verdicts(monkeypatch)

    async def run():
        await upsert_type(name="Doc", tenant_id=TENANT)
        await upsert_attribute(
            type_name="Doc",
            attr_name="description",
            datatype="string",
            tenant_id=TENANT,
        )
        await insert_facts(
            None,
            GRAPH,
            [
                (ENTITY, RDF_TYPE, DOC_TYPE),
                (ENTITY, DESC_PRED, PROSE),
                (ENTITY, RDFS_LABEL, "Session one"),
            ],
            store=store,
        )
        counters = await rec.reconcile_kg(None, TENANT, KG, index=index)

        # Declared → classified under its real domain, and durably.
        assert verdicts == {("Doc", "description"): "free_text"}
        assert counters["attrs_marked_free_text"] == 1
        # Undeclared ``name`` → still undecided, never minted under a fake type.
        assert await rec._catalog_domain_for_attr(TENANT, "name") is None

    asyncio.run(run())


def test_a_reserved_type_segment_in_the_data_is_skipped_not_fatal(index, monkeypatch):
    """The DATA half: a ``types/Entity/attrs/note`` predicate in the instance
    graph. Nothing can be declared under a reserved label, so that attribute is
    skipped — while the healthy ``Doc`` attribute in the same run is classified
    and indexed."""
    verdicts = _capture_verdicts(monkeypatch)
    neptune = SparqlKg(
        {
            f"https://graph.infona.ai/entities/Doc/e{i}": {
                RDF_TYPE: [DOC_TYPE],
                DESC_PRED: [f"{PROSE} Session {i}."],
                ENTITY_PRED: [f"{PROSE} Stray note {i}."],
            }
            for i in range(1, 3)
        }
    )

    async def run():
        with structlog.testing.capture_logs() as logs:
            counters = await rec.reconcile_kg(neptune, TENANT, KG, index=index)

        assert verdicts == {("Doc", "description"): "free_text"}
        assert counters["attrs_marked_free_text"] == 1
        assert counters["chunks_written"] >= 2  # the Doc attr was indexed
        [skipped] = [
            e for e in logs if e["event"] == "semantic_candidacy_skipped_no_domain"
        ]
        assert skipped["attrs"] == 1

    asyncio.run(run())


def test_a_failing_verdict_commit_is_logged_and_the_run_continues(
    store, index, monkeypatch
):
    """Backstop for a domain the screen does not know about: the verdict is
    dropped (the attr stays undecided and is re-sampled next run) and the KG's
    index still refreshes. Contrast the marker FETCH, which must still abort —
    an empty map there would ghost-delete the whole index."""

    async def boom(neptune, graph_uri, mutations, **kwargs):
        raise RuntimeError("catalog write rejected")

    async def run():
        # Declared, undecided → the candidacy pass reaches the commit for it.
        await upsert_type(name="Doc", tenant_id=TENANT)
        await upsert_attribute(
            type_name="Doc",
            attr_name="description",
            datatype="string",
            tenant_id=TENANT,
        )
        monkeypatch.setattr(oc, "commit_ontology", boom)
        await insert_facts(
            None,
            GRAPH,
            [(ENTITY, RDF_TYPE, DOC_TYPE), (ENTITY, DESC_PRED, PROSE)],
            store=store,
        )
        with structlog.testing.capture_logs() as logs:
            counters = await rec.reconcile_kg(None, TENANT, KG, index=index)

        assert counters["attrs_marked_free_text"] == 0
        [failed] = [
            e for e in logs if e["event"] == "semantic_candidacy_verdict_failed"
        ]
        assert failed["attr"] == "description"
        assert "catalog write rejected" in failed["error"]
        # The run reached its summary — it did not die on the failed verdict.
        assert any(e["event"] == "semantic_reconcile" for e in logs)

    asyncio.run(run())
