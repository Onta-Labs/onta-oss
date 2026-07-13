"""ONTA-277 acceptance: supersession & retraction close a fact's validity interval
WITHOUT deleting or re-pointing the edge, so a superseded fact stops being cited
by a "current facts" query while staying present in a full-history query.

Two layers:

1. Pure unit tests of the recency policy + the validity/provenance builders (no
   store) — the policy branches (functional supersedes, multi-valued coexists) and
   the closed-interval / governance-event shapes.
2. Real end-to-end over a PRE-POPULATED in-process pyoxigraph store (the acceptance
   bar): seed ``Company/acme hasCEO "Alice"`` (open interval), apply a supersede
   with ``"Bob"``, and prove:
     (c) the "current facts" query returns ONLY Bob;
     (d) the full/history query still returns Alice WITH a CLOSED interval
         (superseded_by set) — proving it was retired, not deleted/re-pointed;
     (e) an A6 GraphDelta receipt was produced for the new fact.
   The load-bearing control seeds Alice + Bob as two plain facts with NO closure
   and shows the SAME "current" query returns BOTH — so the acceptance test fails
   if supersession were a no-op (the closed interval is what removes Alice, not the
   query).
"""
from __future__ import annotations

import json

import pytest

from cograph_client.graph.kg_writer import GraphDelta, insert_facts
from cograph_client.graph.queries import kg_graph_uri
from cograph_client.graph.validity import (
    STATUS_SUPERSEDED,
    build_closed_interval_triples,
    fetch_history,
    history_objects_query,
    validity_graph_uri,
)
from cograph_client.pipeline.mutations import (
    DEFAULT_RECENCY_POLICY,
    RecencyPolicy,
    retract_fact,
    supersede_fact,
)

TENANT, KG = "onta277", "corp"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
ACME = "https://cograph.tech/entities/Company/acme"
HAS_CEO = "https://cograph.tech/onto/hasCEO"
HAS_EMPLOYEE = "https://cograph.tech/onto/hasEmployee"


# --------------------------------------------------------------------------- #
# 1. Pure unit tests — recency policy + builders (no store)
# --------------------------------------------------------------------------- #
def test_recency_policy_default_is_single_valued():
    """The sensible default: recency wins (functional) for any (type, attr)."""
    assert DEFAULT_RECENCY_POLICY.supersedes("Company", "hasCEO") is True
    assert DEFAULT_RECENCY_POLICY.supersedes("Anything", "whatever") is True


def test_recency_policy_multivalued_override_coexists():
    """A multi-valued attribute appends (coexists) instead of superseding, and an
    explicit single_valued entry overrides even a multivalued-by-default policy."""
    policy = RecencyPolicy(multivalued=frozenset({("Company", "hasEmployee")}))
    assert policy.supersedes("Company", "hasCEO") is True  # default single-valued
    assert policy.supersedes("Company", "hasEmployee") is False  # multi-valued

    flipped = RecencyPolicy(
        default_multivalued=True, single_valued=frozenset({("Company", "hasCEO")})
    )
    assert flipped.supersedes("Company", "hasCEO") is True  # explicit override wins
    assert flipped.supersedes("Company", "title") is False  # default multi-valued


def test_closed_interval_builder_carries_valid_to_and_superseded_by():
    """The load-bearing shape: a closed interval carries validTo (the CLOSED
    marker) + superseded_by + status, keyed to the exact (s, p, o) fact."""
    from cograph_client.graph.validity import (
        VAL_STATUS,
        VAL_SUPERSEDED_BY,
        VAL_VALID_TO,
    )

    triples = build_closed_interval_triples(
        ACME, HAS_CEO, "Alice",
        valid_to="2026-07-13T00:00:00+00:00",
        superseded_by="new-stmt-id",
        status=STATUS_SUPERSEDED,
        graph_uri=INSTANCE_GRAPH,
    )
    preds = {p for _s, p, _o in triples}
    assert VAL_VALID_TO in preds and VAL_SUPERSEDED_BY in preds and VAL_STATUS in preds
    # All triples share ONE interval node keyed by (s, p, o).
    nodes = {s for s, _p, _o in triples}
    assert len(nodes) == 1


def test_supersession_provenance_event_shape():
    from cograph_client.graph.provenance import (
        EVENT_SUPERSEDE,
        PROV_EVENT,
        PROV_SUPERSEDED_BY,
        build_supersession_triples,
    )

    triples = build_supersession_triples(
        ACME, HAS_CEO, "Alice", "Bob", graph_uri=INSTANCE_GRAPH,
        timestamp="2026-07-13T00:00:00+00:00",
    )
    assert any(p == PROV_EVENT and o == EVENT_SUPERSEDE for _s, p, o in triples)
    assert any(p == PROV_SUPERSEDED_BY for _s, p, _o in triples)


# --------------------------------------------------------------------------- #
# 2. Real end-to-end over a pyoxigraph store (the acceptance bar)
# --------------------------------------------------------------------------- #
pyoxigraph = pytest.importorskip("pyoxigraph")
from pyoxigraph import QueryResultsFormat, Store  # noqa: E402


class PyoxiNeptune:
    """Minimal NeptuneClient shim over an in-process pyoxigraph Store — async
    query()/update() returning SPARQL-1.1 JSON, union-of-named-graphs default."""

    def __init__(self) -> None:
        self.store = Store()

    async def query(self, sparql: str) -> dict:
        results = self.store.query(sparql, use_default_graph_as_union=True)
        return json.loads(results.serialize(format=QueryResultsFormat.JSON))

    async def update(self, sparql: str) -> None:
        self.store.update(sparql)


@pytest.fixture(autouse=True)
def _quiet_housekeeping(monkeypatch):
    """Silence the shared refresh_after_write internals (cache-invalidate / embed /
    stats recompute) so the end-to-end tests isolate the supersede/retract
    mechanism — exactly as tests/test_kg_writer.py does. The op STILL calls
    refresh_after_write; only its best-effort downstreams are no-ops here."""
    import cograph_client.api.routes.explore as explore_mod
    import cograph_client.nlp.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda g: None)
    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)


async def _current(n: PyoxiNeptune, subject: str, predicate: str) -> set[str]:
    """The "current facts" projection — objects with no CLOSED validity interval."""
    from cograph_client.graph.validity import current_objects_query

    raw = await n.query(current_objects_query(INSTANCE_GRAPH, subject, predicate))
    return {b["o"]["value"] for b in raw["results"]["bindings"]}


async def _seed_open_fact(n: PyoxiNeptune, subject: str, predicate: str, value: str):
    """Pre-populate an initial current fact (open interval) via the shared insert
    primitive — the fact plus its rdf:type, exactly as ingest would land it."""
    await insert_facts(
        n,
        INSTANCE_GRAPH,
        [
            (subject, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
             "https://cograph.tech/types/Company"),
            (subject, predicate, value),
        ],
    )


@pytest.mark.asyncio
async def test_supersede_retires_old_fact_but_keeps_it_queryable():
    """THE acceptance bar. Pre-populated store: acme hasCEO Alice (current). Apply a
    supersede with Bob. The current query returns ONLY Bob; the history query still
    returns Alice WITH a closed interval (superseded_by set); an A6 delta is
    emitted."""
    n = PyoxiNeptune()
    await _seed_open_fact(n, ACME, HAS_CEO, "Alice")

    # Sanity: before the supersede Alice is the only current CEO.
    assert await _current(n, ACME, HAS_CEO) == {"Alice"}

    receipt = await supersede_fact(
        n, INSTANCE_GRAPH,
        subject=ACME, predicate=HAS_CEO, new_value="Bob", type_name="Company",
        observed_at=None, run_id="run-277",
    )

    # (c) The "current facts" query cites ONLY Bob now.
    assert await _current(n, ACME, HAS_CEO) == {"Bob"}

    # (d) History still returns BOTH; Alice carries a CLOSED interval (validTo +
    #     superseded_by), Bob is open — proving Alice was retired, not deleted.
    history = await fetch_history(n, INSTANCE_GRAPH, ACME, HAS_CEO)
    by_obj = {h.obj: h for h in history}
    assert set(by_obj) == {"Alice", "Bob"}, "the superseded fact must remain in the graph"
    assert not by_obj["Alice"].is_current and by_obj["Alice"].valid_to, "Alice must be CLOSED"
    assert by_obj["Alice"].superseded_by, "Alice must point at its replacement"
    assert by_obj["Bob"].is_current, "Bob must be the current (open) fact"

    # (d, stronger) The Alice INSTANCE triple is byte-for-byte still there — the
    # supersede closed an interval, it did not delete or re-point the edge.
    raw = await n.query(
        f'SELECT ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ <{ACME}> <{HAS_CEO}> ?o }} }}'
    )
    assert {b["o"]["value"] for b in raw["results"]["bindings"]} == {"Alice", "Bob"}

    # (e) An A6 GraphDelta receipt was produced for the NEW fact.
    assert isinstance(receipt.graph_delta, GraphDelta)
    assert receipt.graph_delta.run_id == "run-277"
    delta_spo = {(s, p, o) for _fid, s, p, o in receipt.graph_delta.facts}
    assert (ACME, HAS_CEO, "Bob") in delta_spo, "the A6 delta must record the new fact"
    assert receipt.superseded == ((ACME, HAS_CEO, "Alice"),)
    assert receipt.coexisted is False


@pytest.mark.asyncio
async def test_control_without_supersede_current_query_returns_both():
    """LOAD-BEARING control: seed Alice AND Bob as two plain facts (NO closure). The
    SAME "current facts" query returns BOTH — so the mechanism (closing Alice's
    interval), not the query, is what removes Alice in the acceptance test. If
    supersession were a no-op, that test would see this {Alice, Bob} result and
    fail."""
    n = PyoxiNeptune()
    await _seed_open_fact(n, ACME, HAS_CEO, "Alice")
    # Append Bob directly, with NO validity closure of Alice.
    await insert_facts(n, INSTANCE_GRAPH, [(ACME, HAS_CEO, "Bob")])

    assert await _current(n, ACME, HAS_CEO) == {"Alice", "Bob"}, (
        "with no interval closed, BOTH values are current — proves the current "
        "query is not trivially filtering"
    )


@pytest.mark.asyncio
async def test_multivalued_attribute_coexists_instead_of_superseding():
    """The policy branch: a multi-valued attribute APPENDS — the newer fact
    coexists, the older stays current (no interval closed)."""
    n = PyoxiNeptune()
    await _seed_open_fact(n, ACME, HAS_EMPLOYEE, "Ann")
    policy = RecencyPolicy(multivalued=frozenset({("Company", "hasEmployee")}))

    receipt = await supersede_fact(
        n, INSTANCE_GRAPH,
        subject=ACME, predicate=HAS_EMPLOYEE, new_value="Ben", type_name="Company",
        run_id="run-mv", policy=policy,
    )

    # Both employees are current — nothing was retired.
    assert await _current(n, ACME, HAS_EMPLOYEE) == {"Ann", "Ben"}
    assert receipt.coexisted is True
    assert receipt.superseded == ()


@pytest.mark.asyncio
async def test_retract_closes_currency_but_keeps_history():
    """Explicit retraction (interval-close, the preferred path): the fact stops
    being current but stays in the graph, marked retracted."""
    n = PyoxiNeptune()
    await _seed_open_fact(n, ACME, HAS_CEO, "Alice")
    assert await _current(n, ACME, HAS_CEO) == {"Alice"}

    receipt = await retract_fact(
        n, INSTANCE_GRAPH,
        subject=ACME, predicate=HAS_CEO, value="Alice", type_name="Company",
        run_id="run-retract",
    )

    # No longer current, but still present in history with a retracted interval.
    assert await _current(n, ACME, HAS_CEO) == set()
    history = await fetch_history(n, INSTANCE_GRAPH, ACME, HAS_CEO)
    assert [h.obj for h in history] == ["Alice"]
    assert history[0].status == "retracted" and not history[0].is_current
    # The instance triple is untouched.
    raw = await n.query(
        f'SELECT ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ <{ACME}> <{HAS_CEO}> ?o }} }}'
    )
    assert {b["o"]["value"] for b in raw["results"]["bindings"]} == {"Alice"}
    assert receipt.op == "retract" and receipt.removed == 0


@pytest.mark.asyncio
async def test_retract_hard_delete_removes_triple_via_delete_facts():
    """The opt-in hard-delete path genuinely removes the instance triple (through
    kg_writer.delete_facts) — the escape hatch, distinct from interval-close."""
    n = PyoxiNeptune()
    await _seed_open_fact(n, ACME, HAS_CEO, "Alice")

    receipt = await retract_fact(
        n, INSTANCE_GRAPH,
        subject=ACME, predicate=HAS_CEO, value="Alice", type_name="Company",
        run_id="run-hard", hard_delete=True,
    )

    raw = await n.query(
        f'SELECT ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ <{ACME}> <{HAS_CEO}> ?o }} }}'
    )
    assert raw["results"]["bindings"] == [], "hard delete removes the instance triple"
    assert receipt.removed == 1


@pytest.mark.asyncio
async def test_supersede_auto_discovers_current_value_when_old_not_given():
    """The op discovers the current value to close from the graph when the caller
    doesn't name it — and a chained supersede (Alice→Bob→Carol) leaves exactly one
    current value with the earlier two closed in history."""
    n = PyoxiNeptune()
    await _seed_open_fact(n, ACME, HAS_CEO, "Alice")

    await supersede_fact(n, INSTANCE_GRAPH, subject=ACME, predicate=HAS_CEO,
                         new_value="Bob", type_name="Company", run_id="r1")
    await supersede_fact(n, INSTANCE_GRAPH, subject=ACME, predicate=HAS_CEO,
                         new_value="Carol", type_name="Company", run_id="r2")

    assert await _current(n, ACME, HAS_CEO) == {"Carol"}
    history = await fetch_history(n, INSTANCE_GRAPH, ACME, HAS_CEO)
    closed = {h.obj for h in history if not h.is_current}
    assert closed == {"Alice", "Bob"}
    assert {h.obj for h in history} == {"Alice", "Bob", "Carol"}


@pytest.mark.asyncio
async def test_supersede_closes_a_typed_literal_value():
    """Regression (ONTA-247 class): the current value is auto-discovered and closed
    even when it is a TYPED literal — the op reconstructs the exact term
    (``value^^datatype``) so the "current" FILTER excludes it. A plain-string close
    would never match a typed original."""
    n = PyoxiNeptune()
    count_pred = "https://cograph.tech/onto/employeeCount"
    xsd_int = "http://www.w3.org/2001/XMLSchema#integer"
    await insert_facts(n, INSTANCE_GRAPH, [(ACME, count_pred, f"100^^{xsd_int}")])

    await supersede_fact(
        n, INSTANCE_GRAPH, subject=ACME, predicate=count_pred,
        new_value=f"250^^{xsd_int}", type_name="Company", run_id="r-typed",
    )

    assert await _current(n, ACME, count_pred) == {"250"}
    history = await fetch_history(n, INSTANCE_GRAPH, ACME, count_pred)
    by_obj = {h.obj: h.is_current for h in history}
    assert by_obj == {"100": False, "250": True}


@pytest.mark.asyncio
async def test_mutation_records_a_run_manifest_item():
    """A9 wiring (low-cost): a supersede handed a RunManifest records the op as a
    completed item, so a mutation run has honest coverage."""
    from cograph_client.pipeline.manifest import RunManifest

    n = PyoxiNeptune()
    await _seed_open_fact(n, ACME, HAS_CEO, "Alice")
    manifest = RunManifest(run_id="run-mani", stage="supersede").start(total=1)

    await supersede_fact(
        n, INSTANCE_GRAPH, subject=ACME, predicate=HAS_CEO, new_value="Bob",
        type_name="Company", run_id="run-mani", manifest=manifest,
    )

    cov = manifest.complete().coverage()
    assert cov.completed == 1 and cov.complete is True


def test_history_query_is_scoped_to_the_two_companion_graphs():
    """Guard: the history query reads the instance graph + its validity companion
    graph (never a bare union), so it can't leak another KG's facts."""
    q = history_objects_query(INSTANCE_GRAPH, ACME, HAS_CEO)
    assert f"GRAPH <{INSTANCE_GRAPH}>" in q
    assert f"GRAPH <{validity_graph_uri(INSTANCE_GRAPH)}>" in q
