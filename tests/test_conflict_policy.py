"""ONTA-276 acceptance: write-time conflict resolution on a FUNCTIONAL attribute
picks a DETERMINISTIC winner and keeps the LOSER deprecated-but-queryable (never
silently dropped), with its provenance intact.

Three layers:

1. Pure unit tests of the policy (no store) — the precedence is total &
   deterministic (authority > confidence > recency > value), the winner flips when
   the trust signals flip, same-value re-assertions are not conflicts, and the A4
   ``ValidatedTriple`` carries authority + confidence through to a ``FactClaim``.
2. THE acceptance bar over a PRE-POPULATED in-process pyoxigraph store: seed
   ``Company/acme revenue "10000000"`` from source A (authority=authoritative,
   confidence=0.8); apply a contradicting ``"12000000"`` from source B
   (authority=source_of_truth, confidence=0.9) and prove:
     (a) a DETERMINISTIC winner — the current-facts query returns ONLY $12M (the
         higher-authority source);
     (b) the LOSER stays queryable — the history/full query still returns $10M WITH
         a closed/deprecated interval, AND its provenance (source A, confidence 0.8)
         is still readable;
     (e) an A6 GraphDelta receipt is emitted.
3. The LOAD-BEARING control: flip the authorities/confidences so the OTHER fact
   wins, and assert the winner flips deterministically (proves the policy actually
   arbitrates, not a fixed pick) — the loser (now the incoming $12M) is written
   deprecated-but-queryable with ITS provenance.
"""
from __future__ import annotations

import json

import pytest

from cograph_client.api_registry.spec import AuthorityLevel
from cograph_client.graph.kg_writer import GraphDelta, insert_facts
from cograph_client.graph.provenance import fetch_provenance
from cograph_client.graph.queries import kg_graph_uri
from cograph_client.graph.validity import (
    STATUS_DEPRECATED,
    current_objects_query,
    fetch_history,
)
from cograph_client.pipeline.conflict import (
    REASON_AUTHORITY,
    REASON_CONFIDENCE,
    REASON_RECENCY,
    ConflictPolicy,
    FactClaim,
    resolve,
)
from cograph_client.pipeline.mutations import (
    ConflictReceipt,
    write_with_conflict_resolution,
)
from cograph_client.resolver.models import ValidatedTriple

TENANT, KG = "onta276", "corp"
INSTANCE_GRAPH = kg_graph_uri(TENANT, KG)
ACME = "https://cograph.tech/entities/Company/acme"
REVENUE = "https://cograph.tech/onto/revenue"

REV_10M = "10000000"
REV_12M = "12000000"


# --------------------------------------------------------------------------- #
# 1. Pure unit tests — the policy is total & deterministic (no store)
# --------------------------------------------------------------------------- #
def test_authority_decides_and_is_the_top_axis():
    """Higher AUTHORITY wins even when it also has higher confidence — and the
    deciding axis is reported as ``authority``."""
    existing = FactClaim(REV_10M, authority=AuthorityLevel.authoritative, confidence=0.8, source="A")
    incoming = FactClaim(REV_12M, authority=AuthorityLevel.source_of_truth, confidence=0.9, source="B")
    d = resolve(existing, incoming)
    assert d.conflict is True
    assert d.winner.value == REV_12M and d.loser.value == REV_10M
    assert d.reason == REASON_AUTHORITY
    assert d.winner_is_incoming is True


def test_winner_flips_when_authority_flips():
    """LOAD-BEARING: swap the authorities and the winner flips deterministically —
    the policy arbitrates, it is not a fixed pick."""
    existing = FactClaim(REV_10M, authority=AuthorityLevel.source_of_truth, confidence=0.9, source="A")
    incoming = FactClaim(REV_12M, authority=AuthorityLevel.authoritative, confidence=0.8, source="B")
    d = resolve(existing, incoming)
    assert d.winner.value == REV_10M and d.loser.value == REV_12M
    assert d.reason == REASON_AUTHORITY
    assert d.winner_is_incoming is False  # the existing value wins


def test_confidence_breaks_equal_authority():
    d = resolve(
        FactClaim("lo", authority=AuthorityLevel.authoritative, confidence=0.6),
        FactClaim("hi", authority=AuthorityLevel.authoritative, confidence=0.95),
    )
    assert d.winner.value == "hi" and d.reason == REASON_CONFIDENCE


def test_recency_breaks_equal_authority_and_confidence():
    from datetime import datetime, timezone

    old = FactClaim("stale", authority=AuthorityLevel.authoritative, confidence=0.8,
                    observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    new = FactClaim("fresh", authority=AuthorityLevel.authoritative, confidence=0.8,
                    observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    d = resolve(old, new)
    assert d.winner.value == "fresh" and d.reason == REASON_RECENCY


def test_total_order_no_unbroken_tie_on_distinct_values():
    """Two DISTINCT-valued claims identical on every trust axis still resolve to a
    single deterministic winner (the value tiebreak) — never a tie."""
    a = FactClaim("aaa", authority=AuthorityLevel.authoritative, confidence=0.8)
    b = FactClaim("bbb", authority=AuthorityLevel.authoritative, confidence=0.8)
    d1 = resolve(a, b)
    d2 = resolve(a, b)
    assert d1.conflict is True and d1.winner.value == d2.winner.value  # deterministic


def test_same_value_is_a_reassertion_not_a_conflict():
    d = resolve(
        FactClaim(REV_10M, authority=AuthorityLevel.authoritative),
        FactClaim(REV_10M, authority=AuthorityLevel.source_of_truth),
    )
    assert d.conflict is False and d.loser is None


def test_no_existing_value_is_not_a_conflict():
    d = resolve(None, FactClaim(REV_12M, authority=AuthorityLevel.source_of_truth))
    assert d.conflict is False and d.winner.value == REV_12M


def test_configurable_precedence_changes_the_winner():
    """The precedence is injectable: recency-first flips an authority-vs-recency
    case — still totally ordered, just a different (documented) policy."""
    from datetime import datetime, timezone

    strong_old = FactClaim("strong_old", authority=AuthorityLevel.source_of_truth,
                           observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    weak_new = FactClaim("weak_new", authority=AuthorityLevel.supplementary,
                         observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    default = ConflictPolicy()  # authority first
    recency_first = ConflictPolicy(precedence=("recency", "authority", "confidence"))
    assert default.resolve(strong_old, weak_new).winner.value == "strong_old"
    assert recency_first.resolve(strong_old, weak_new).winner.value == "weak_new"


def test_a4_validated_triple_carries_authority_and_confidence():
    """The A4 Verified model carries source authority + verification confidence, and
    adapts to a FactClaim the policy reads — the carrier that keeps P1 priority
    alive to the write-time conflict point."""
    vt = ValidatedTriple(
        subject=ACME, predicate=REVENUE, object=REV_12M,
        authority=AuthorityLevel.source_of_truth, confidence=0.9, source="B",
    )
    claim = FactClaim.from_verified(vt)
    assert claim.value == REV_12M
    assert claim.authority is AuthorityLevel.source_of_truth
    assert claim.effective_confidence == 0.9
    assert claim.source == "B"
    # Back-compat: a triple constructed without the new fields still parses.
    plain = ValidatedTriple(subject=ACME, predicate=REVENUE, object=REV_10M)
    assert plain.authority is None and plain.confidence is None and plain.source == ""


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
    stats recompute) so the end-to-end tests isolate the conflict mechanism — as
    tests/test_supersession.py does. The op STILL calls refresh_after_write."""
    import cograph_client.api.routes.explore as explore_mod
    import cograph_client.nlp.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod.NLQueryPipeline, "invalidate_cache", lambda g: None)
    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
    monkeypatch.setattr(explore_mod, "schedule_recompute", lambda *a, **k: None)


async def _current(n: PyoxiNeptune, subject: str, predicate: str) -> set[str]:
    """The "current facts" projection — objects with no CLOSED validity interval."""
    raw = await n.query(current_objects_query(INSTANCE_GRAPH, subject, predicate))
    return {b["o"]["value"] for b in raw["results"]["bindings"]}


async def _seed_fact(
    n: PyoxiNeptune, subject: str, predicate: str, value: str,
    *, authority: AuthorityLevel, confidence: float, source: str,
) -> None:
    """Seed an initial current fact (open interval) WITH its trust signals persisted
    in provenance — via the SAME conflict-resolving op (no existing value, so it is
    a plain current write). This is exactly how an upstream A4 fact lands, and it is
    what makes the seeded fact's authority/confidence readable when the next
    contradicting fact arrives."""
    await insert_facts(
        n,
        INSTANCE_GRAPH,
        [(subject, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
          "https://cograph.tech/types/Company")],
    )
    await write_with_conflict_resolution(
        n, INSTANCE_GRAPH,
        subject=subject, predicate=predicate, type_name="Company", value=value,
        authority=authority, confidence=confidence, source=source, run_id="seed",
    )


@pytest.mark.asyncio
async def test_conflict_deterministic_winner_and_queryable_deprecated_loser():
    """THE acceptance bar. Pre-populated store: acme revenue $10M from source A
    (authoritative, 0.8). Apply a contradicting $12M from source B (source_of_truth,
    0.9). Assert (a) current = ONLY $12M; (b) $10M stays queryable, deprecated, WITH
    its provenance; (e) an A6 receipt is emitted."""
    n = PyoxiNeptune()
    await _seed_fact(n, ACME, REVENUE, REV_10M,
                     authority=AuthorityLevel.authoritative, confidence=0.8, source="source_A")

    # Sanity: before the conflicting write $10M is the only current revenue.
    assert await _current(n, ACME, REVENUE) == {REV_10M}

    receipt = await write_with_conflict_resolution(
        n, INSTANCE_GRAPH,
        subject=ACME, predicate=REVENUE, type_name="Company", value=REV_12M,
        authority=AuthorityLevel.source_of_truth, confidence=0.9, source="source_B",
        run_id="run-276",
    )

    # (a) DETERMINISTIC winner: the current-facts query cites ONLY $12M now.
    assert await _current(n, ACME, REVENUE) == {REV_12M}
    assert receipt.conflict is True and receipt.reason == REASON_AUTHORITY
    assert receipt.winner == (ACME, REVENUE, REV_12M)
    assert receipt.loser == (ACME, REVENUE, REV_10M)

    # (b) The LOSER stays queryable: history returns BOTH; $10M carries a CLOSED,
    #     DEPRECATED interval pointing at the winner; $12M is the open/current fact.
    history = await fetch_history(n, INSTANCE_GRAPH, ACME, REVENUE)
    by_obj = {h.obj: h for h in history}
    assert set(by_obj) == {REV_10M, REV_12M}, "the losing fact must remain in the graph"
    assert not by_obj[REV_10M].is_current and by_obj[REV_10M].valid_to, "$10M must be CLOSED"
    assert by_obj[REV_10M].status == STATUS_DEPRECATED, "$10M closed with the deprecated status"
    assert by_obj[REV_10M].superseded_by, "$10M must point at the winning fact"
    assert by_obj[REV_12M].is_current, "$12M must be the current (open) fact"

    # (b, provenance) The loser's provenance (source A, confidence 0.8) is STILL
    # readable — proving it was retired WITH its provenance, not silently dropped.
    prov = await fetch_provenance(n, INSTANCE_GRAPH, ACME, REVENUE)
    by_val = {p.obj: p for p in prov}
    assert REV_10M in by_val, "the loser's provenance must still be queryable"
    assert by_val[REV_10M].source == "source_A"
    assert by_val[REV_10M].confidence == 0.8
    assert by_val[REV_10M].authority == "authoritative"
    # The winner's provenance is recorded too (source B, source_of_truth, 0.9).
    assert by_val[REV_12M].source == "source_B" and by_val[REV_12M].authority == "source_of_truth"

    # (b, stronger) The $10M INSTANCE triple is byte-for-byte still present — the
    # conflict closed an interval, it did NOT delete the loser's edge.
    raw = await n.query(
        f'SELECT ?o WHERE {{ GRAPH <{INSTANCE_GRAPH}> {{ <{ACME}> <{REVENUE}> ?o }} }}'
    )
    assert {b["o"]["value"] for b in raw["results"]["bindings"]} == {REV_10M, REV_12M}

    # (e) An A6 GraphDelta receipt was produced.
    assert isinstance(receipt.graph_delta, GraphDelta)
    assert receipt.graph_delta.run_id == "run-276"
    delta_spo = {(s, p, o) for _fid, s, p, o in receipt.graph_delta.facts}
    assert (ACME, REVENUE, REV_12M) in delta_spo, "the A6 delta must record the winning fact"
    assert receipt.deprecated == ((ACME, REVENUE, REV_10M),)


@pytest.mark.asyncio
async def test_control_flipped_authorities_flip_the_winner_deterministically():
    """LOAD-BEARING control: flip the authorities/confidences so the OTHER fact wins.
    Seed $10M as source_of_truth/0.9 and apply $12M as authoritative/0.8 — now $10M
    (the existing value) must WIN, so current = {$10M} and the DEPRECATED loser is
    the incoming $12M, still queryable WITH its provenance. Same code path, opposite
    outcome — proves the policy arbitrates rather than always picking incoming."""
    n = PyoxiNeptune()
    await _seed_fact(n, ACME, REVENUE, REV_10M,
                     authority=AuthorityLevel.source_of_truth, confidence=0.9, source="source_A")

    receipt = await write_with_conflict_resolution(
        n, INSTANCE_GRAPH,
        subject=ACME, predicate=REVENUE, type_name="Company", value=REV_12M,
        authority=AuthorityLevel.authoritative, confidence=0.8, source="source_B",
        run_id="run-flip",
    )

    # The winner FLIPPED: the existing $10M wins; only it is current now.
    assert await _current(n, ACME, REVENUE) == {REV_10M}
    assert receipt.winner == (ACME, REVENUE, REV_10M)
    assert receipt.loser == (ACME, REVENUE, REV_12M)
    assert receipt.reason == REASON_AUTHORITY

    # The loser is now the INCOMING $12M — written deprecated-but-queryable.
    history = await fetch_history(n, INSTANCE_GRAPH, ACME, REVENUE)
    by_obj = {h.obj: h for h in history}
    assert set(by_obj) == {REV_10M, REV_12M}
    assert by_obj[REV_12M].status == STATUS_DEPRECATED and not by_obj[REV_12M].is_current
    assert by_obj[REV_10M].is_current

    # The deprecated incoming loser retains ITS provenance (source B, 0.8, authoritative).
    prov = await fetch_provenance(n, INSTANCE_GRAPH, ACME, REVENUE)
    by_val = {p.obj: p for p in prov}
    assert by_val[REV_12M].source == "source_B"
    assert by_val[REV_12M].confidence == 0.8
    assert by_val[REV_12M].authority == "authoritative"

    assert isinstance(receipt.graph_delta, GraphDelta)
    assert receipt.deprecated == ((ACME, REVENUE, REV_12M),)


@pytest.mark.asyncio
async def test_no_existing_value_writes_incoming_current_no_conflict():
    """A first fact on a fresh functional attribute is written current with no
    arbitration (conflict=False), so a normal write is unaffected."""
    n = PyoxiNeptune()
    receipt = await write_with_conflict_resolution(
        n, INSTANCE_GRAPH,
        subject=ACME, predicate=REVENUE, type_name="Company", value=REV_10M,
        authority=AuthorityLevel.authoritative, confidence=0.8, source="source_A",
        run_id="run-fresh",
    )
    assert await _current(n, ACME, REVENUE) == {REV_10M}
    assert receipt.conflict is False and receipt.loser is None
    assert receipt.reason == "no_conflict"
