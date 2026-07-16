"""P1 "Find" component bar — offline coverage & precision fixture-eval (ONTA-343).

This IS the P1 discovery-quality bar. For each fixture goal in
``tests/fixtures/p1_goals/*.json`` it:

  1. registers a SCRIPTED web-source provider built from the fixture's row-set
     (some true members, some off-membership / near-dup / fabricated rows),
  2. drives the PUBLIC ``WebIngestCapability.plan`` / ``execute`` interface —
     deterministically, with NO live LLM or network: the entity/attribute spec is
     injected via ``parsed=`` (the LLM resolver never runs) and the commit takes
     the deterministic ``ingest_structured_rows`` fast-path (asserted: the
     open-ended ``ingest`` LLM detour is never taken),
  3. collects the rows the discovery run SURFACED (recorded at the provider
     boundary, so the metric is immune to whatever internal bundle/dedupe the
     rail does — a sibling P1 ticket is refactoring those internals), and
  4. scores them with ``cograph_client.pipeline.find_metrics`` and asserts the
     metric bundle against the fixture's expected verdict.

Load-bearing control: ``padded_gadgets.json`` is deliberately gamed (near-dup
padding + off-membership + fabricated keys + expensive spend) and MUST FAIL the
bar — asserted via ``pytest.raises`` on the bar enforcer, plus per-counter
assertions proving each anti-gaming counter bites. The clean fixtures PASS.

Fully offline / deterministic — no network, no live LLM.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
from dataclasses import dataclass

import pytest
from unittest.mock import MagicMock

from cograph_client.agent.capabilities import web_ingest_cap
from cograph_client.agent.capabilities.web_ingest_cap import WebIngestCapability
from cograph_client.agent.registry import AgentContext
from cograph_client.enrichment.job_store import InMemoryJobStore
from cograph_client.enrichment.models import JobStatus
from cograph_client.pipeline.find_metrics import FindMetrics, Thresholds, score_find
from cograph_client.resolver.models import IngestResult
from cograph_client.resolver.schema_resolver import (
    SchemaResolver,
    _is_fabricated_placeholder,
)
from cograph_client.web_sources import (
    DiscoverResult,
    register_web_source,
    reset_web_sources,
)
from cograph_client.web_sources.base import source_cost

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "p1_goals")
FIXTURE_PATHS = sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.json")))
# One representative pass + the fail control must always exist so the suite is
# meaningful even if fixtures are added/removed.
assert len(FIXTURE_PATHS) >= 3, "P1 find-eval needs at least 3 fixture goals"


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class FindBarNotMet(AssertionError):
    """Raised by :func:`enforce_bar` when a run misses the P1 Find bar."""


def enforce_bar(m: FindMetrics) -> None:
    """The gate. Raise iff the run misses coverage/precision/any counter."""
    if not m.passed:
        raise FindBarNotMet(
            f"P1 Find bar not met: failing gates={m.failures()} "
            f"(coverage={m.coverage:.3f} precision={m.precision:.3f} "
            f"fab_off={m.fab_offmembership_rate:.3f} "
            f"near_dup={m.near_dup_collapse_rate:.3f} "
            f"cost_per_tp={m.cost_per_net_new_tp:.4f})"
        )


class ScriptedProvider:
    """A deterministic, in-memory :class:`WebSourceProvider` for one fixture.

    Returns the fixture's canned rows verbatim (no live search, no LLM) and
    RECORDS every row it surfaces on a full (``sample=False``) discovery call, so
    the eval scores exactly what discovery surfaced — before any downstream
    dedupe. ``structured=True`` opts the rows into the deterministic
    ``ingest_structured_rows`` fast-path.
    """

    def __init__(self, fixture: dict) -> None:
        self.name = "scripted"
        prov = fixture.get("provider", {})
        self.is_paid = bool(prov.get("is_paid", False))
        self.cost_per_call = float(prov.get("cost_per_call", 0.0))
        self.structured = True  # take the deterministic ingest_structured_rows seam
        self._rows = fixture["rows"]
        self._sources = fixture.get("sources") or ["https://scripted.example/list"]
        self.calls: list[tuple] = []
        self.surfaced_rows: list[dict] = []

    async def discover(
        self, query, *, sample, max_rows, hint_columns, context, urls=None
    ) -> DiscoverResult:
        self.calls.append((query, sample, max_rows, tuple(hint_columns or ())))
        rows = self._rows[: (5 if sample else max_rows)]
        # Hand the rail independent copies (it mutates rows in place — attaches
        # source_url, dedupes) while we keep a faithful copy of what we surfaced.
        out = [dict(r) for r in rows]
        if not sample:
            self.surfaced_rows.extend(dict(r) for r in rows)
        return DiscoverResult(
            rows=out,
            sources=list(self._sources),
            estimated_total=len(self._rows),
            is_partial=sample,
        )


def _ctx(store: InMemoryJobStore) -> AgentContext:
    return AgentContext(
        tenant_id="demo-tenant",
        kg_name="p1-find-eval",
        neptune=MagicMock(),
        anthropic_key="sk-ant-test",
        openrouter_key="",
        extras={"prior_clarify_count": 0, "enrichment_job_store": store},
    )


def _spec_from(fixture: dict) -> dict:
    """The already-resolved discovery spec, injected via plan(parsed=...), so the
    LLM resolver never runs and the plan commits straight to a discover_ingest
    step (the confirmed attribute set makes it 'already scoped')."""
    return {
        "entity_type": fixture["entity_type"],
        "key_attribute": fixture["key_attribute"],
        "query": fixture["query"],
        "confirmed_attributes": list(fixture.get("confirmed_attributes") or []),
        "suggested_attributes": list(fixture.get("suggested_attributes") or []),
    }


async def _drive_discovery(fixture: dict, monkeypatch) -> tuple[list[dict], float, InMemoryJobStore, str]:
    """Register a scripted provider, drive plan+execute deterministically, and
    return ``(surfaced_rows, total_cost_usd, job_store, job_id)``."""
    reset_web_sources()
    provider = ScriptedProvider(fixture)
    register_web_source(provider)

    # Deterministic commit: force the structured fast-path ON and capture the
    # rows it commits. The open-ended LLM ingest() path must NEVER run.
    monkeypatch.setattr(web_ingest_cap, "_DISCOVERY_STRUCTURED_FASTPATH", True)

    async def fake_structured(self, rows, tenant_id, *, type_name, attributes=None,
                              source="", instance_graph=None, key_attribute=None,
                              key_join=None):
        return IngestResult(
            entities_extracted=len(rows), entities_resolved=len(rows)
        )

    async def forbidden_ingest(self, *a, **k):  # pragma: no cover - guard
        raise AssertionError(
            "the LLM ingest() detour must not run under the structured fast-path"
        )

    monkeypatch.setattr(SchemaResolver, "ingest_structured_rows", fake_structured)
    monkeypatch.setattr(SchemaResolver, "ingest", forbidden_ingest)

    async def noop_refresh(neptune, *, tenant_id=None, kg_name=None, affected_types=None):
        return None

    monkeypatch.setattr(web_ingest_cap, "refresh_after_write", noop_refresh)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap, "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    ctx = _ctx(store)
    cap = WebIngestCapability()

    steps = await cap.plan(ctx, fixture["goal"], parsed=_spec_from(fixture))
    assert len(steps) == 1, f"expected one plan step, got {len(steps)}"
    step = steps[0]
    assert step.action == "discover_ingest", (
        f"fixture {fixture['id']} did not produce a discover_ingest plan: {step.action}"
    )

    ack = await cap.execute(ctx, step)
    assert ack["kind"] == "ack"
    job_id = ack["job_id"]
    # Drive the background run to completion (deterministic, in-memory).
    await spawned["task"]

    # Cost = the provider's declared per-call cost (the ONE shared retrieval cost
    # seam, retrieval/cost.py::source_cost) × the number of paid FULL discover
    # calls the rail issued.
    _paid, cost_per_call = source_cost(provider)
    full_calls = sum(1 for c in provider.calls if c[1] is False)
    total_cost = cost_per_call * full_calls
    return provider.surfaced_rows, total_cost, store, job_id


def _score(fixture: dict, surfaced_rows: list[dict], total_cost: float) -> FindMetrics:
    return score_find(
        gold_roster=fixture["gold_roster"],
        result_rows=surfaced_rows,
        key_attribute=fixture["key_attribute"],
        membership_rule=fixture.get("membership_rule"),
        alias_table=fixture.get("alias_table"),
        expected_type=fixture.get("expected_type"),
        type_key=fixture.get("type_key", "_type"),
        total_cost_usd=total_cost,
        is_fabricated=_is_fabricated_placeholder,
        thresholds=Thresholds.from_dict(fixture.get("thresholds")),
    )


# --------------------------------------------------------------------------- #
# The eval, parametrized over every fixture goal.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fixture_path", FIXTURE_PATHS, ids=[os.path.basename(p) for p in FIXTURE_PATHS]
)
async def test_p1_find_fixture(fixture_path, monkeypatch):
    fixture = _load(fixture_path)
    surfaced_rows, total_cost, store, job_id = await _drive_discovery(fixture, monkeypatch)

    # The rail actually ran end-to-end: the provider surfaced its rows via a full
    # (non-sample) discover, and the tracked job settled to a terminal state.
    assert surfaced_rows, "discovery surfaced no rows"
    assert len(surfaced_rows) == len(fixture["rows"])
    job = await store.get(job_id)
    assert job is not None and job.status == JobStatus.applied

    metrics = _score(fixture, surfaced_rows, total_cost)

    if fixture["expect_pass"]:
        # No gate may be violated — enforce_bar must not raise.
        enforce_bar(metrics)
        assert metrics.passed, metrics.failures()
    else:
        # The CONTROL: the bar enforcer MUST raise (the counters bite).
        with pytest.raises(FindBarNotMet):
            enforce_bar(metrics)
        assert not metrics.passed


# --------------------------------------------------------------------------- #
# Load-bearing control: the padded fixture fails, and we assert WHICH gates.
# --------------------------------------------------------------------------- #
async def test_padded_fixture_trips_every_counter(monkeypatch):
    """The deliberately-padded low-precision fixture must miss precision AND all
    three anti-gaming counters — proving each counter is load-bearing, not
    decorative. (A clean fixture passing them all is covered above.)"""
    path = os.path.join(FIXTURE_DIR, "padded_gadgets.json")
    fixture = _load(path)
    surfaced_rows, total_cost, _store, _job_id = await _drive_discovery(fixture, monkeypatch)
    m = _score(fixture, surfaced_rows, total_cost)

    assert not m.passed
    # Precision floor breached (5 of 10 rows are true members).
    assert not m.precision_ok and m.precision == pytest.approx(0.5)
    # (a) fabrication + off-membership: 2 fabricated keys + 3 non-members / 10.
    assert not m.fab_offmembership_ok
    assert m.fabricated_rows == 2
    assert m.off_membership_rows == 3
    assert m.fab_offmembership_rate == pytest.approx(0.5)
    # (b) near-dup collapse: 10 rows collapse onto 6 distinct keys.
    assert not m.near_dup_ok
    assert m.near_dup_collapsed_rows == 4
    assert m.near_dup_collapse_rate == pytest.approx(0.4)
    # (c) $ per net-new true positive: $0.40 spend / 2 distinct members.
    assert not m.cost_ok
    assert m.distinct_true_members == 2
    assert m.cost_per_net_new_tp == pytest.approx(0.20)
    # Coverage also fails here (only 2 of 4 gold serials found).
    assert not m.coverage_ok and m.coverage == pytest.approx(0.5)
    assert set(m.failures()) == {
        "coverage", "precision", "fab_offmembership", "near_dup_collapse", "cost_per_tp"
    }


async def test_clean_fixture_passes_the_bar(monkeypatch):
    """A pristine closed-roster fixture clears every gate — the positive control
    opposite the padded one, and a check that the alias table lets a variant
    spelling ('Claude Opus 4.8') match its canonical gold key."""
    fixture = _load(os.path.join(FIXTURE_DIR, "models_openrouter.json"))
    surfaced_rows, total_cost, _store, _job_id = await _drive_discovery(fixture, monkeypatch)
    m = _score(fixture, surfaced_rows, total_cost)

    enforce_bar(m)  # must not raise
    assert m.passed
    assert m.coverage == pytest.approx(1.0)      # alias-matched row counts
    assert m.precision == pytest.approx(1.0)
    assert m.fabricated_rows == 0
    assert m.near_dup_collapsed_rows == 0


# --------------------------------------------------------------------------- #
# Pure-scorer unit checks (no rail) — pin the metric contract itself.
# --------------------------------------------------------------------------- #
def test_scorer_near_dup_does_not_hurt_precision():
    """A near-duplicate of a real member is still a true-member row: precision is
    untouched by duplication (counter (b) catches padding instead)."""
    gold = ["Acme Corp", "Globex"]
    rows = [
        {"name": "Acme Corp"},
        {"name": "acme corp"},   # near-dup of a real member
        {"name": "Globex"},
    ]
    m = score_find(gold_roster=gold, result_rows=rows, key_attribute="name")
    assert m.precision == pytest.approx(1.0)          # all rows are true members
    assert m.coverage == pytest.approx(1.0)           # both gold found
    assert m.near_dup_collapsed_rows == 1
    assert m.near_dup_collapse_rate == pytest.approx(1 / 3)
    assert m.distinct_true_members == 2               # coverage counts each once


def test_scorer_membership_rule_open_goal():
    """For an open goal the membership_rule decides precision; coverage still
    measures recall against the known gold roster."""
    rule = {"all": [{"field": "kind", "equals": "cafe"}]}
    gold = ["a", "b"]
    rows = [
        {"id": "a", "kind": "cafe"},   # member + gold
        {"id": "b", "kind": "cafe"},   # member + gold
        {"id": "z", "kind": "cafe"},   # member (rule) but NOT gold — fine for precision
        {"id": "q", "kind": "bar"},    # off-membership
    ]
    m = score_find(
        gold_roster=gold, result_rows=rows, key_attribute="id", membership_rule=rule
    )
    assert m.coverage == pytest.approx(1.0)     # a,b both found
    assert m.precision == pytest.approx(3 / 4)  # z is a member, q is not
    assert m.off_membership_rows == 1


def test_scorer_flags_fabricated_key_as_false_positive():
    """A fabricated placeholder KEY is never a true member even if it would
    otherwise match, and it counts toward the fabrication rate."""
    gold = ["real-1"]
    rows = [
        {"id": "real-1"},
        {"id": "1234567890"},   # fabricated placeholder
    ]
    m = score_find(
        gold_roster=gold, result_rows=rows, key_attribute="id",
        is_fabricated=_is_fabricated_placeholder,
    )
    assert m.fabricated_rows == 1
    assert m.precision == pytest.approx(0.5)
    assert m.coverage == pytest.approx(1.0)
