"""ONTA-274 acceptance: first-class, lineage-preserving merge/split ops with A6 receipts.

Identity drift is domain reality ("Twitter Inc." + "X Corp" → same entity). When two
nodes carry half the facts each, the answer layer answers with half the picture at
full confidence. ``merge_entities`` unifies them onto ONE canonical node (re-keyed via
``kg_writer.rewrite_subject`` — one re-key event, never delete+insert) with an
alias/``sameAs`` lineage record and a reversible snapshot; ``split_entity`` reads that
snapshot back to separate them again with facts correctly re-attributed.

Two layers:

1. Pure unit tests of the lineage builders + the canonical-form helper (no store).
2. Real end-to-end over a PRE-POPULATED in-process pyoxigraph store (the acceptance
   bar): seed ``Company/twitter_inc`` and ``Company/x_corp`` with disjoint facts, and
   prove:
     (a) MERGE unifies both fact sets onto the canonical, records a ``sameAs`` lineage
         edge tying the merged-away URI to the canonical, emits an A6 receipt whose
         delta carries ``fan_in`` (merged → canonical), and loses NO fact — the
         merged-away subject's triples now resolve under the canonical URI;
     (b) SPLIT reverses the merge — the node separates back into two with facts
         correctly re-attributed (merged-exclusive facts leave the canonical, shared
         facts stay) and lineage intact.
   The LOAD-BEARING control seeds the two nodes WITHOUT merging and shows the canonical
   does NOT see the other node's facts — so a no-op merge fails the acceptance test
   (the re-key, not the query, is what unifies).
"""
from __future__ import annotations

import json

import pytest

from cograph_client.graph.kg_writer import GraphDelta, insert_facts
from cograph_client.graph.ontology_queries import entity_uri
from cograph_client.graph.provenance import (
    EVENT_MERGE,
    LIN_ORIGIN,
    ORIGIN_CANONICAL,
    ORIGIN_MERGED,
    PROV_EVENT,
    PROV_REWRITTEN_TO,
    PROV_SUBJECT,
    build_merge_lineage_triples,
)
from cograph_client.graph.queries import kg_graph_uri
from cograph_client.pipeline.mutations import (
    SAME_AS,
    MergeReceipt,
    SplitReceipt,
    _to_canonical_form,
    merge_entities,
    split_entity,
)

TENANT, KG = "onta274", "corp"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)

TWITTER = entity_uri("Company", "twitter_inc")
XCORP = entity_uri("Company", "x_corp")
INVESTOR = entity_uri("Investor", "vanguard")

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
COMPANY = "https://cograph.tech/types/Company"
HAS_CEO = "https://cograph.tech/onto/hasCEO"
LEGAL_NAME = "https://cograph.tech/onto/legalName"
HAS_PRODUCT = "https://cograph.tech/onto/hasProduct"
OWNER = "https://cograph.tech/onto/owner"
EMPLOYEE_COUNT = "https://cograph.tech/onto/employeeCount"
INVESTED_IN = "https://cograph.tech/onto/invested_in"
XSD_INT = "http://www.w3.org/2001/XMLSchema#integer"

# Disjoint fact sets — this is the whole point (half the facts on each node).
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
# 1. Pure unit tests — lineage builders + canonical-form helper (no store)
# --------------------------------------------------------------------------- #
def test_to_canonical_form_rewrites_both_positions():
    """A merged node's triple as it lands on the canonical: the merged URI in the
    subject OR object slot becomes the canonical (exactly the rewrite_subject move)."""
    assert _to_canonical_form((TWITTER, HAS_CEO, "Jack"), TWITTER, XCORP) == (XCORP, HAS_CEO, "Jack")
    assert _to_canonical_form((INVESTOR, INVESTED_IN, TWITTER), TWITTER, XCORP) == (
        INVESTOR, INVESTED_IN, XCORP,
    )
    # An untouched triple is unchanged.
    assert _to_canonical_form((XCORP, OWNER, "Musk"), TWITTER, XCORP) == (XCORP, OWNER, "Musk")


def test_merge_lineage_builder_records_event_and_reified_snapshot():
    """The always-on reversible record: a ``merge`` event (merged → canonical) plus a
    reified snapshot of BOTH sides' facts, tagged by origin."""
    triples = build_merge_lineage_triples(
        XCORP, TWITTER,
        merged_facts=[(TWITTER, HAS_CEO, "Jack")],
        canonical_facts=[(XCORP, OWNER, "Musk")],
        graph_uri=INSTANCE_GRAPH, reason="SEC filing", timestamp="2026-07-13T00:00:00+00:00",
        touched_types=["Company"],
    )
    # The merge event: PROV_EVENT="merge", subject=merged, rewrittenTo=canonical.
    assert any(p == PROV_EVENT and o == EVENT_MERGE for _s, p, o in triples)
    assert any(p == PROV_SUBJECT and o == TWITTER for _s, p, o in triples)
    assert any(p == PROV_REWRITTEN_TO and o == XCORP for _s, p, o in triples)
    # Both origins are represented in the reified snapshot.
    origins = {o for _s, p, o in triples if p == LIN_ORIGIN}
    assert origins == {ORIGIN_MERGED, ORIGIN_CANONICAL}


# --------------------------------------------------------------------------- #
# 2. Real end-to-end over a pyoxigraph store (the acceptance bar)
# --------------------------------------------------------------------------- #
pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — async
    query()/update() returning SPARQL-1.1 JSON, union-of-named-graphs default
    (identical to tests/test_supersession.py)."""

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
    stats recompute) so the tests isolate the merge/split mechanism — exactly as
    tests/test_supersession.py does. The ops STILL call refresh_after_write; only its
    best-effort downstreams are no-ops here. The spatiotemporal index defaults to the
    zero-config in-memory backend, so the merge's rewritten_subjects re-key is a
    harmless no-op over facts with no geometry."""
    import cograph_client.api.routes.explore as explore_mod
    import cograph_client.nlp.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda g: None)
    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)


def _plain(obj: str) -> str:
    """The plain object VALUE a ``?o`` binding reports (datatype dropped) — so a
    write-convention typed term ``7500^^…integer`` compares against the ``7500`` a
    read returns. Term-faithfulness of the typed literal is asserted separately via
    :func:`_employee_count_datatype`."""
    return obj.split("^^", 1)[0]


async def _facts_of(n: PyoxiNeptune, subject: str) -> set[tuple[str, str]]:
    """The (predicate, object-value) set of a subject's INSTANCE triples — the
    projection an answer-layer read sees for one node."""
    raw = await n.query(
        f"SELECT ?p ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ <{subject}> ?p ?o }} }}"
    )
    return {(b["p"]["value"], b["o"]["value"]) for b in raw["results"]["bindings"]}


async def _employee_count_datatype(n: PyoxiNeptune, subject: str) -> str | None:
    """Read the raw ``employeeCount`` binding's datatype (to prove a typed literal
    survives the snapshot round-trip — the ONTA-247 lesson)."""
    raw = await n.query(
        f"SELECT ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ <{subject}> <{EMPLOYEE_COUNT}> ?o }} }}"
    )
    rows = raw["results"]["bindings"]
    return rows[0]["o"].get("datatype") if rows else None


async def _seed(n: PyoxiNeptune, subject: str, facts: list[tuple[str, str]]):
    """Pre-populate a node's facts via the shared insert primitive, exactly as ingest
    would land them."""
    await insert_facts(n, INSTANCE_GRAPH, [(subject, p, o) for p, o in facts])


@pytest.mark.asyncio
async def test_merge_unifies_two_nodes_onto_canonical_with_lineage_and_receipt():
    """THE acceptance bar (a). Pre-populated store: twitter_inc + x_corp carry disjoint
    facts. merge_entities → the two unify onto ONE canonical (x_corp) carrying BOTH
    sets, a sameAs lineage edge ties the merged-away URI to the canonical, an A6
    receipt with fan_in is emitted, and NO fact is lost."""
    n = PyoxiNeptune()
    await _seed(n, TWITTER, TWITTER_FACTS)
    await _seed(n, XCORP, XCORP_FACTS)

    twitter_before = await _facts_of(n, TWITTER)
    xcorp_before = await _facts_of(n, XCORP)
    # Sanity: before the merge x_corp does NOT carry twitter's CEO fact.
    assert (HAS_CEO, "Jack") not in xcorp_before

    receipt = await merge_entities(
        n, INSTANCE_GRAPH,
        a=TWITTER, b=XCORP, canonical=XCORP, type_name="Company",
        reason="SEC filing: Twitter Inc renamed X Corp", run_id="run-274",
    )

    # (a1) The canonical now carries BOTH fact sets — the answer layer sees the whole
    #      picture. This is the assertion a no-op merge FAILS (see the control below).
    xcorp_after = await _facts_of(n, XCORP)
    assert (HAS_CEO, "Jack") in xcorp_after
    assert (LEGAL_NAME, "Twitter Inc") in xcorp_after
    assert (HAS_PRODUCT, "X") in xcorp_after and (OWNER, "Musk") in xcorp_after
    # No fact lost: every original fact of either node resolves under the canonical.
    for pred, obj in TWITTER_FACTS + XCORP_FACTS:
        assert (pred, _plain(obj)) in xcorp_after, f"merge lost {(pred, obj)!r}"

    # (a2) The merged-away subject's triples now resolve UNDER the canonical — nothing
    #      is left on the old URI (its identity redirected, not duplicated).
    assert await _facts_of(n, TWITTER) == set(), "merged-away subject must have no triples of its own"

    # (a3) An alias/sameAs lineage edge ties the canonical to the merged-away URI.
    assert (SAME_AS, TWITTER) in xcorp_after, "a sameAs lineage edge must record the unification"

    # (a4) A typed literal survived the re-key (ONTA-247): employeeCount is still int.
    assert await _employee_count_datatype(n, XCORP) == XSD_INT

    # (a5) The A6 receipt records the merge via fan_in (merged → canonical).
    assert isinstance(receipt, MergeReceipt)
    assert isinstance(receipt.graph_delta, GraphDelta)
    assert receipt.canonical == XCORP and receipt.merged == TWITTER
    assert receipt.same_as == (XCORP, SAME_AS, TWITTER)
    assert receipt.graph_delta.fan_in, "the A6 delta must record the merge fan_in"
    assert receipt.graph_delta.run_id == "run-274"
    # The unified facts recorded on the receipt include the re-keyed CEO fact.
    assert (XCORP, HAS_CEO, "Jack") in set(receipt.unified_facts)

    # Guard against a trivially-true test: the two nodes really did differ pre-merge.
    assert twitter_before and xcorp_before and twitter_before != xcorp_before


@pytest.mark.asyncio
async def test_control_without_merge_canonical_does_not_see_other_facts():
    """LOAD-BEARING control: seed twitter_inc + x_corp as two separate nodes and do
    NOT merge. The canonical does NOT see the other node's facts — so the mechanism
    (the rewrite_subject re-key), not the query, is what unifies them in the acceptance
    test. If merge_entities were a no-op, this {no Jack under x_corp} state would
    persist and the acceptance test's (a1) assertion would fail."""
    n = PyoxiNeptune()
    await _seed(n, TWITTER, TWITTER_FACTS)
    await _seed(n, XCORP, XCORP_FACTS)

    xcorp = await _facts_of(n, XCORP)
    assert (HAS_CEO, "Jack") not in xcorp, (
        "with no merge, the canonical must NOT carry the other node's facts — proves "
        "the re-key (not the query) is what unifies"
    )
    assert (LEGAL_NAME, "Twitter Inc") not in xcorp
    assert (SAME_AS, TWITTER) not in xcorp, "no merge → no sameAs lineage edge"
    # And the merged-away URI still holds its OWN facts (it was never redirected).
    assert (HAS_CEO, "Jack") in await _facts_of(n, TWITTER)


@pytest.mark.asyncio
async def test_split_reverses_a_merge_reattributing_facts():
    """THE acceptance bar (b). After a merge, split_entity separates the node back into
    two with facts correctly re-attributed and lineage intact — a merge followed by a
    split returns to the exact two original nodes."""
    n = PyoxiNeptune()
    await _seed(n, TWITTER, TWITTER_FACTS)
    await _seed(n, XCORP, XCORP_FACTS)
    twitter_before = await _facts_of(n, TWITTER)
    xcorp_before = await _facts_of(n, XCORP)

    await merge_entities(
        n, INSTANCE_GRAPH, a=TWITTER, b=XCORP, canonical=XCORP, type_name="Company",
        reason="SEC filing", run_id="run-274",
    )
    # (precondition) the merge really unified them.
    assert (HAS_CEO, "Jack") in await _facts_of(n, XCORP)

    receipt = await split_entity(
        n, INSTANCE_GRAPH, canonical=XCORP, merged=TWITTER, type_name="Company",
        reason="court-ordered spinoff", run_id="run-split",
    )

    # (b1) The two nodes are back — each carrying EXACTLY its original facts.
    assert await _facts_of(n, TWITTER) == twitter_before, "merged node restored to its original facts"
    assert await _facts_of(n, XCORP) == xcorp_before, "canonical restored to its original facts"

    # (b2) The merged-exclusive facts left the canonical; the shared rdf:type stayed;
    #      the sameAs lineage edge is gone.
    xcorp_after = await _facts_of(n, XCORP)
    assert (HAS_CEO, "Jack") not in xcorp_after and (LEGAL_NAME, "Twitter Inc") not in xcorp_after
    assert (RDF_TYPE, COMPANY) in xcorp_after, "a genuinely shared fact stays on the canonical"
    assert (SAME_AS, TWITTER) not in xcorp_after, "the sameAs edge is withdrawn on split"

    # (b3) The restored typed literal is still an int (round-tripped through lineage).
    assert await _employee_count_datatype(n, TWITTER) == XSD_INT

    # (b4) The A6 split receipt records the re-materialized node.
    assert isinstance(receipt, SplitReceipt)
    assert receipt.canonical == XCORP and receipt.restored == TWITTER
    assert receipt.removed > 0
    assert (TWITTER, HAS_CEO, "Jack") in set(receipt.restored_facts)


@pytest.mark.asyncio
async def test_split_restores_incoming_edges_re_pointed_by_the_merge():
    """A merge re-points INCOMING edges too (``investor invested_in twitter`` →
    ``… x_corp``); a clean split must re-attribute them back to the restored node."""
    n = PyoxiNeptune()
    await _seed(n, TWITTER, TWITTER_FACTS)
    await _seed(n, XCORP, XCORP_FACTS)
    await insert_facts(n, INSTANCE_GRAPH, [(INVESTOR, INVESTED_IN, TWITTER)])

    await merge_entities(
        n, INSTANCE_GRAPH, a=TWITTER, b=XCORP, canonical=XCORP, type_name="Company",
        reason="merger", run_id="r-in",
    )
    # After merge the investor's edge points at the canonical.
    assert (INVESTED_IN, XCORP) in await _facts_of(n, INVESTOR)
    assert (INVESTED_IN, TWITTER) not in await _facts_of(n, INVESTOR)

    await split_entity(
        n, INSTANCE_GRAPH, canonical=XCORP, merged=TWITTER, type_name="Company",
        reason="spinoff", run_id="r-in-split",
    )
    # After split the incoming edge is re-attributed back to the restored node.
    investor_facts = await _facts_of(n, INVESTOR)
    assert (INVESTED_IN, TWITTER) in investor_facts, "incoming edge restored to the split-out node"
    assert (INVESTED_IN, XCORP) not in investor_facts, "and no longer points at the canonical"


@pytest.mark.asyncio
async def test_split_with_explicit_partition_overrides_lineage():
    """An operator can supply the partition explicitly (the over-merge case) instead of
    relying on the recorded lineage."""
    n = PyoxiNeptune()
    await _seed(n, TWITTER, TWITTER_FACTS)
    await _seed(n, XCORP, XCORP_FACTS)
    await merge_entities(
        n, INSTANCE_GRAPH, a=TWITTER, b=XCORP, canonical=XCORP, type_name="Company",
        reason="merger", run_id="r-p",
    )

    receipt = await split_entity(
        n, INSTANCE_GRAPH, canonical=XCORP, merged=TWITTER, type_name="Company",
        reason="manual re-partition",
        partition=(
            [(TWITTER, p, o) for p, o in TWITTER_FACTS],
            [(XCORP, p, o) for p, o in XCORP_FACTS],
        ),
    )
    assert isinstance(receipt, SplitReceipt)
    assert (HAS_CEO, "Jack") in await _facts_of(n, TWITTER)
    assert (HAS_CEO, "Jack") not in await _facts_of(n, XCORP)


@pytest.mark.asyncio
async def test_split_without_lineage_or_partition_raises():
    """Split needs to know what to separate out: with no recorded merge lineage and no
    explicit partition, it refuses rather than silently doing nothing."""
    n = PyoxiNeptune()
    await _seed(n, XCORP, XCORP_FACTS)
    with pytest.raises(ValueError, match="no merge lineage"):
        await split_entity(
            n, INSTANCE_GRAPH, canonical=XCORP, merged=TWITTER, type_name="Company",
        )


@pytest.mark.asyncio
async def test_merge_rejects_self_merge():
    """Merging an entity into itself is a caller error, not a silent no-op."""
    n = PyoxiNeptune()
    await _seed(n, XCORP, XCORP_FACTS)
    with pytest.raises(ValueError, match="into itself"):
        await merge_entities(
            n, INSTANCE_GRAPH, a=XCORP, b=XCORP, type_name="Company", reason="x",
        )


@pytest.mark.asyncio
async def test_merge_records_a_run_manifest_item():
    """A9 wiring (low-cost, mirroring supersede): a merge handed a RunManifest records
    the op as a completed item."""
    from cograph_client.pipeline.manifest import RunManifest

    n = PyoxiNeptune()
    await _seed(n, TWITTER, TWITTER_FACTS)
    await _seed(n, XCORP, XCORP_FACTS)
    manifest = RunManifest(run_id="run-mani", stage="merge").start(total=1)

    await merge_entities(
        n, INSTANCE_GRAPH, a=TWITTER, b=XCORP, canonical=XCORP, type_name="Company",
        reason="merger", run_id="run-mani", manifest=manifest,
    )
    cov = manifest.complete().coverage()
    assert cov.completed == 1 and cov.complete is True
