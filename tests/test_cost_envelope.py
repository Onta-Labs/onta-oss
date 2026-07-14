"""ONTA-282 — cost envelope enforcement (P0 runtime).

A HARD per-run spend ceiling on the A9 Run Manifest's ``spend_usd``, wired into
the existing halt machinery, so a run that exceeds its envelope HALTS CLEANLY
(terminal manifest state) instead of continuing as a silent partial.

This mirrors the ONTA-273 402-halt acceptance shape (``test_run_manifest.py``)
but injects a LOW spend ceiling instead of a 402:

* the manifest reaches terminal ``failed`` with ``halt_reason_kind ==
  HaltReasonKind.cost_ceiling`` (a GOVERNANCE halt — NOT provider exhaustion);
* ``coverage()`` shows an ACCURATE partial (completed vs dropped remainder),
  never a silent partial;
* a LOAD-BEARING control — the SAME run under a HIGH ceiling completes cleanly —
  proves the halt is caused by the ceiling, not an unrelated error.

Both driver loops are wired for the spend feed + ceiling check:
* enrichment: ``EnrichmentExecutor.run()`` per-item loop (paid adapter calls feed
  ``manifest.add_spend`` via ``_lookup_chain``; the ceiling is checked after each
  ``record_completed`` and a breach raises ``CostCeilingExceeded`` into the outer
  ``except`` → ``halt_from_exception``);
* discovery: ``web_ingest_cap._run_inner`` micro-batch loop (paid provider calls
  feed ``manifest.add_spend``; a breach sets a fatal flag routed through
  ``_fail_billing_job`` → ``halt_from_exception``).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from cograph_client.config import settings
from cograph_client.pipeline.manifest import (
    HaltReasonKind,
    RunManifest,
    RunState,
    classify_halt,
    resolve_spend_ceiling,
)
from cograph_client.retrieval.errors import CostCeilingExceeded, RetrievalError


# --------------------------------------------------------------------------- #
# 1. Pure-unit — check_ceiling / over_ceiling / classify_halt / resolve
# --------------------------------------------------------------------------- #
def test_over_ceiling_and_check_ceiling():
    m = RunManifest(run_id="r1", spend_ceiling_usd=2.5).start(total=10)
    assert not m.over_ceiling()
    assert m.check_ceiling() is None
    m.add_spend(2.0)
    assert not m.over_ceiling()
    assert m.check_ceiling() is None
    m.add_spend(0.6)  # 2.6 >= 2.5
    assert m.over_ceiling()
    err = m.check_ceiling()
    assert isinstance(err, CostCeilingExceeded)
    assert isinstance(err, RetrievalError)
    # message names both the spend and the ceiling so the halt is self-explanatory
    assert "2.6" in str(err) or "2.60" in str(err)
    assert "2.50" in str(err)
    assert "ceiling" in str(err).lower()


def test_unlimited_ceiling_never_trips():
    for ceiling in (None, 0.0):
        m = RunManifest(run_id="r", spend_ceiling_usd=ceiling).start(total=5)
        m.add_spend(1000.0)
        assert not m.over_ceiling()
        assert m.check_ceiling() is None


def test_check_ceiling_boundary_is_inclusive():
    """Reaching the ceiling exactly (>=) trips — an envelope is a HARD cap."""
    m = RunManifest(run_id="r", spend_ceiling_usd=1.0).start(total=3)
    m.add_spend(1.0)
    assert m.over_ceiling()
    assert isinstance(m.check_ceiling(), CostCeilingExceeded)


def test_classify_halt_maps_cost_ceiling():
    assert classify_halt(CostCeilingExceeded("x")) is HaltReasonKind.cost_ceiling


def test_cost_ceiling_is_not_provider_exhaustion():
    assert not HaltReasonKind.cost_ceiling.is_provider_exhaustion


def test_halt_from_cost_ceiling_names_envelope():
    m = RunManifest(run_id="r", spend_ceiling_usd=2.5).start(total=4)
    m.record_completed("a")
    m.add_spend(3.0)
    err = m.check_ceiling()
    assert err is not None
    m.halt_from_exception(err, landed_note="1 of 4 items completed before the halt.")
    assert m.state is RunState.failed
    assert m.state.is_terminal()
    assert m.halt_reason_kind is HaltReasonKind.cost_ceiling
    assert not m.halt_reason_kind.is_provider_exhaustion
    reason = (m.halt_reason or "").lower()
    assert "cost envelope exceeded" in reason
    assert "ceiling" in reason
    # provider-exhaustion phrasing must NOT be used for a governance halt
    assert "provider exhaustion" not in reason
    # honest partial: the unfilled planned remainder rolled into dropped
    cov = m.coverage()
    assert cov.completed == 1
    assert cov.dropped == 3
    assert cov.complete is False


def test_resolve_spend_ceiling():
    # explicit per-run value wins
    assert resolve_spend_ceiling(2.5, 0.0) == 2.5
    # falls back to the deployment default when unset
    assert resolve_spend_ceiling(None, 1.0) == 1.0
    # 0 / None / negative ⇒ unlimited (None)
    assert resolve_spend_ceiling(None, 0.0) is None
    assert resolve_spend_ceiling(0.0, 0.0) is None
    assert resolve_spend_ceiling(-3.0, 0.0) is None
    # malformed default degrades to unlimited, never raises
    assert resolve_spend_ceiling(None, "bad") is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 2. ACCEPTANCE (enrichment) — a LOW ceiling halts the run at the envelope with
#    a cost_ceiling manifest + accurate partial coverage; a HIGH ceiling (the
#    load-bearing control) completes clean.
# --------------------------------------------------------------------------- #
from cograph_client.enrichment.cache import EnrichmentCache  # noqa: E402
from cograph_client.enrichment.executor import EnrichmentExecutor  # noqa: E402
from cograph_client.enrichment.job_store import InMemoryJobStore  # noqa: E402
from cograph_client.enrichment.models import (  # noqa: E402
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobStatus,
    Verdict,
)
from cograph_client.enrichment.sources.base import register_adapter  # noqa: E402


class _FixedCostAdapter:
    """A registered adapter that charges a fixed ``cost_per_call`` on every lookup
    and returns a LOW-confidence verdict (below the default 0.85 threshold) — so
    each (entity, attribute) item incurs a known spend but resolves to ``no_match``
    (no write path is exercised; the run's fate is decided purely by the ceiling)."""

    def __init__(self, cost: float):
        self.name = "fixedcost"
        self.is_paid = True
        self.cost_per_call = cost

    async def lookup(self, entity_label, attribute, context):
        return [Verdict(value=f"{attribute}-val", confidence=0.5, source=self.name)]


def _entities_query_response(rows: list[dict]) -> dict:
    bindings = []
    for r in rows:
        b: dict = {"e": {"type": "uri", "value": r["uri"]}}
        if r.get("label") is not None:
            b["label"] = {"type": "literal", "value": r["label"]}
        if r.get("vals") is not None:
            b["vals"] = {"type": "literal", "value": r["vals"]}
        bindings.append(b)
    return {"head": {"vars": ["e", "label", "nameAttr", "vals"]}, "results": {"bindings": bindings}}


def _make_enrich_executor():
    neptune = AsyncMock()
    # One entity, five attributes → five (entity, attribute) items in one worker
    # (single entity ⇒ fully sequential ⇒ deterministic ceiling trip point).
    rows = [
        {"uri": "https://cograph.tech/entities/Product/p1", "label": "Bosch", "vals": ""},
    ]
    neptune.query.return_value = _entities_query_response(rows)
    neptune.update.return_value = None
    store = InMemoryJobStore()
    executor = EnrichmentExecutor(neptune, store, EnrichmentCache(), MagicMock())
    register_adapter(_FixedCostAdapter(cost=1.0))
    return executor, store


def _make_enrich_job(job_id: str, ceiling: float) -> EnrichJob:
    return EnrichJob(
        id=job_id,
        tenant_id="test-tenant",
        kg_name="kg",
        type_name="Product",
        attributes=["a1", "a2", "a3", "a4", "a5"],
        tier=EnrichmentTier.lite,
        status=JobStatus.queued,
        created_at=datetime.now(timezone.utc),
        conflict_policy=ConflictPolicy.stage,
        sources=["fixedcost"],  # force the chain to the fixed-cost paid adapter
        spend_ceiling_usd=ceiling,
    )


async def test_low_ceiling_halts_enrichment_run_with_partial_coverage():
    """THE ACCEPTANCE BAR (enrichment). Five paid $1 items against a $2.5 ceiling:
    the run halts at the third item (cumulative $3 ≥ $2.5) to terminal ``failed``
    with a ``cost_ceiling`` manifest and an ACCURATE partial (3 completed, 2
    dropped) — never a silent partial, never a bogus success."""
    executor, store = _make_enrich_executor()
    job = _make_enrich_job("enrich-ceiling-low", ceiling=2.5)
    await store.create(job)

    await executor.run(job, "test-tenant")

    final = await store.get(job.id)
    assert final is not None
    # (1) TERMINAL failed — not a stuck running, not a silent applied.
    assert final.status == JobStatus.failed
    assert final.status.is_terminal()
    # the user-visible reason (mentions the ceiling) survives onto the job
    assert "ceiling" in (final.error or "").lower()

    # (2) A9 manifest: terminal, cost_ceiling, NOT provider exhaustion.
    m = final.manifest
    assert m is not None
    assert m.state is RunState.failed
    assert m.halt_reason_kind is HaltReasonKind.cost_ceiling
    assert not m.halt_reason_kind.is_provider_exhaustion
    reason = (m.halt_reason or "").lower()
    assert "cost envelope exceeded" in reason
    assert "ceiling" in reason

    # (3) ACCURATE partial coverage — 3 completed at the ceiling, 2 dropped.
    cov = m.coverage()
    assert m.completed == 3
    assert cov.completed == 3
    assert cov.dropped == 2
    assert cov.total == 5  # 1 entity × 5 attributes planned
    assert cov.completed + cov.dropped == cov.total
    assert cov.complete is False
    # spend crossed the envelope (3 × $1 ≥ $2.5)
    assert m.spend_usd >= 2.5


async def test_high_ceiling_control_completes_clean():
    """LOAD-BEARING CONTROL. The SAME run under a HIGH ceiling ($1000) completes
    normally — all five items done, nothing dropped, terminal ``completed`` — so
    the halt above is PROVEN to be caused by the ceiling, not an unrelated error."""
    executor, store = _make_enrich_executor()
    job = _make_enrich_job("enrich-ceiling-high", ceiling=1000.0)
    await store.create(job)

    await executor.run(job, "test-tenant")

    final = await store.get(job.id)
    assert final is not None
    # clean terminal — the run finished its work with no cost halt.
    assert final.status in (JobStatus.applied, JobStatus.review)
    assert final.error is None

    m = final.manifest
    assert m is not None
    assert m.state is RunState.completed
    assert m.halt_reason_kind is HaltReasonKind.none
    cov = m.coverage()
    assert cov.completed == 5
    assert cov.dropped == 0
    assert cov.complete is True
    # spend still accrued (5 paid calls) but stayed under the high envelope.
    assert m.spend_usd == pytest.approx(5.0)


# --------------------------------------------------------------------------- #
# 3. ACCEPTANCE (discovery) — a LOW ceiling halts the web-ingest run at the
#    envelope with a cost_ceiling manifest + accurate partial coverage.
# --------------------------------------------------------------------------- #
from cograph_client.agent.capabilities import web_ingest_cap  # noqa: E402
from cograph_client.agent.capabilities.web_ingest_cap import (  # noqa: E402
    WebIngestCapability,
)
from cograph_client.agent.registry import AgentContext  # noqa: E402
from cograph_client.resolver.models import (  # noqa: E402
    ExtractedEntity,
    ExtractionResult,
    IngestResult,
)
from cograph_client.resolver.schema_resolver import SchemaResolver  # noqa: E402
from cograph_client.web_sources import (  # noqa: E402
    DiscoverResult,
    register_web_source,
    reset_web_sources,
)


ROWS = [
    {"name": "a", "context_length": "1"},
    {"name": "b", "context_length": "2"},
]

SPEC = {
    "entity_type": "OpenRouterModel",
    "key_attribute": "name",
    "query": "OpenRouter models",
    "confirmed_attributes": ["context_length"],
    "suggested_attributes": ["context_length"],
    "subqueries": ["OpenRouter models A", "OpenRouter models B"],
}


class _PaidProvider:
    """A PAID web-discovery provider whose per-call cost is small enough to keep
    plan() on the lean auto-confirm path (estimate ≤ the preview gate) yet large
    enough that the FIRST discover call crosses a low run ceiling at execute time."""

    def __init__(self, cost: float):
        self.name = "paidfake"
        self.is_paid = True
        self.cost_per_call = cost
        self.discover_calls = 0

    async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
        self.discover_calls += 1
        page = ROWS[:2]
        if hint_columns:
            page = [{c: r.get(c, "unknown") for c in hint_columns} for r in page]
        return DiscoverResult(
            rows=page,
            provenance={},
            sources=["https://openrouter.ai/models"],
            estimated_total=4,
            is_partial=sample,
        )


@pytest.fixture(autouse=True)
def _reset_sources():
    reset_web_sources()
    yield
    reset_web_sources()


def _patch_preview(monkeypatch):
    async def fake_fetch_ontology(self, graph_uri):
        return {}, {}

    async def fake_extract(self, content, content_type, existing=None):
        return ExtractionResult(
            entities=[
                ExtractedEntity(type_name="OpenRouterModel", id=r["name"], attributes=[])
                for r in ROWS[:2]
            ],
            relationships=[],
        )

    monkeypatch.setattr(SchemaResolver, "_fetch_ontology", fake_fetch_ontology)
    monkeypatch.setattr(SchemaResolver, "_extract", fake_extract)


def _ctx_with_store(store) -> AgentContext:
    return AgentContext(
        tenant_id="demo-tenant",
        kg_name="models",
        neptune=MagicMock(),
        anthropic_key="sk-ant-test",
        openrouter_key="",
        extras={"prior_clarify_count": 0, "enrichment_job_store": store},
    )


async def test_low_ceiling_halts_discovery_run_with_partial_coverage(monkeypatch):
    """THE ACCEPTANCE BAR (discovery). A paid provider ($0.20/call) against a
    deployment ceiling of $0.10: the very first discover crosses the envelope, so
    the run halts to terminal ``failed`` with a ``cost_ceiling`` manifest recording
    an honest partial (rows-landed vs the planned remainder dropped)."""
    # Deployment-default ceiling (the discovery job carries no per-job override).
    monkeypatch.setattr(settings, "enrich_spend_ceiling_usd", 0.10)

    provider = _PaidProvider(cost=0.20)
    register_web_source(provider)
    _patch_preview(monkeypatch)

    async def ok_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", ok_ingest)

    spawned: dict = {}
    monkeypatch.setattr(
        web_ingest_cap,
        "_spawn",
        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)),
    )

    store = InMemoryJobStore()
    cap = WebIngestCapability()
    step = (await cap.plan(_ctx_with_store(store), "list of OpenRouter models", parsed=SPEC))[0]
    ack = await cap.execute(_ctx_with_store(store), step)
    await spawned["task"]

    job = await store.get(ack["job_id"])
    assert job is not None

    # (1) TERMINAL failed with a ceiling reason on the job.
    assert job.status == JobStatus.failed
    assert JobStatus.failed.is_terminal()
    assert "ceiling" in (job.error or "").lower()

    # (2) A9 manifest: terminal cost_ceiling, NOT provider exhaustion.
    m = job.manifest
    assert m is not None
    assert m.state is RunState.failed
    assert m.halt_reason_kind is HaltReasonKind.cost_ceiling
    assert not m.halt_reason_kind.is_provider_exhaustion
    reason = (m.halt_reason or "").lower()
    assert "cost envelope exceeded" in reason
    assert "ceiling" in reason

    # (3) ACCURATE partial coverage — the landed rows completed, the planned
    #     remainder dropped; never a silent partial.
    cov = m.coverage()
    assert m.completed == 2  # the first (and only) discover landed 2 rows
    assert cov.completed == 2
    assert cov.dropped > 0
    assert cov.complete is False
    assert cov.total >= cov.completed + cov.dropped
    assert m.spend_usd >= 0.10

    # Fail-fast: the ceiling tripped on the FIRST discover; the second sub-query
    # was never consulted.
    assert provider.discover_calls == 1
