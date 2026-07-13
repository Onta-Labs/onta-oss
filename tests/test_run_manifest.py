"""ONTA-273 — A9 Run Manifest + run state machine + halt-and-report on 402/429.

The triggering incident: OpenRouter returned ``402 Payment Required`` (credits
exhausted) mid-run; runs sat "Running" with partial/zero results and NO artifact
carried "failed for N% of items", so downstream could not caveat coverage it
could not see. This suite proves the fix:

* the A9 :class:`RunManifest` is a first-class run object — per-item status,
  drops, retries, spend-to-date, and a ``coverage()`` view;
* its state machine ALWAYS reaches a terminal state, and a provider-exhaustion
  halt (402 / sustained-429) is terminal ``failed`` with a user-visible reason;
* the 429 policy: a single/occasional 429 is a transient (retry); only a
  SUSTAINED streak escalates to a run-level halt;
* the ``/api/v1/key`` balance helper diagnoses remaining credits (mocked httpx);
* **the acceptance bar** — an injected 402 in the LLM extraction call layer during
  a DISCOVERY (and an ENRICHMENT) run yields a terminal ``failed`` run whose reason
  names provider exhaustion AND a manifest showing partial coverage (completed vs
  dropped), NOT a stuck spinner and NOT a silent success.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cograph_client.pipeline.manifest import (
    HaltReasonKind,
    RunCoverage,
    RunManifest,
    RunState,
    classify_halt,
)
from cograph_client.retrieval.errors import (
    RATE_LIMIT_HALT_THRESHOLD,
    LLMAuthError,
    LLMBillingError,
    LLMError,
    LLMRateLimitError,
    RateLimitEscalator,
    is_rate_limit_status,
)


# --------------------------------------------------------------------------- #
# 1. RunManifest unit tests — state machine, per-item accounting, coverage
# --------------------------------------------------------------------------- #
def test_manifest_starts_pending_and_reaches_running():
    m = RunManifest(run_id="r1", stage="discovery")
    assert m.state is RunState.pending
    assert not m.state.is_terminal()
    m.start(total=10)
    assert m.state is RunState.running
    assert m.total == 10
    assert m.started_at is not None


def test_manifest_complete_is_terminal_and_settles_total_down():
    """A clean run collapses the planned cap denominator to what actually ran —
    a 200-cap discovery that found 3 rows reads "3 of 3 — complete", not
    "3 of 200 — 197 dropped"."""
    m = RunManifest(run_id="r1").start(total=200)
    for i in range(3):
        m.record_completed(f"row{i}")
    m.complete()
    assert m.state is RunState.completed
    assert m.state.is_terminal()
    cov = m.coverage()
    assert (cov.completed, cov.total, cov.dropped) == (3, 3, 0)
    assert cov.complete is True
    assert "3 of 3 items completed" in cov.summary


def test_manifest_halt_rolls_remainder_into_dropped_partial_coverage():
    """The core partial-coverage property: on a halt the unfilled planned
    remainder becomes ``dropped`` so coverage shows "N of M; (M-N) dropped"."""
    m = RunManifest(run_id="r1").start(total=10)
    m.record_completed("a")
    m.record_completed("b")
    m.halt(HaltReasonKind.billing, "provider exhaustion — 402 Payment Required")
    assert m.state is RunState.failed
    assert m.state.is_terminal()
    cov = m.coverage()
    assert cov.completed == 2
    assert cov.total == 10
    assert cov.dropped == 8
    assert cov.complete is False
    assert "2 of 10 items completed" in cov.summary
    assert "8 dropped" in cov.summary
    assert "provider exhaustion" in cov.summary


def test_manifest_records_drops_and_retries_and_spend():
    m = RunManifest(run_id="r1").start(total=5)
    m.record_completed("a", spend_usd=0.10, retries=1)
    m.record_dropped("b", reason="unreachable", retries=2)
    m.record_retry("c")
    m.add_spend(0.05)
    assert m.completed == 1
    assert m.dropped == 1
    assert m.retries == 4  # 1 + 2 + 1
    assert m.spend_usd == pytest.approx(0.15)
    # A dropped item's reason is retained in the per-item sample.
    dropped = [i for i in m.items if i.status == "dropped"]
    assert dropped and dropped[0].reason == "unreachable"


def test_manifest_items_list_is_bounded_but_counters_stay_exact():
    from cograph_client.pipeline.manifest import _MAX_ITEMS

    m = RunManifest(run_id="r1").start(total=_MAX_ITEMS + 50)
    for i in range(_MAX_ITEMS + 50):
        m.record_completed(f"row{i}")
    # Counters exact; persisted per-item sample bounded.
    assert m.completed == _MAX_ITEMS + 50
    assert len(m.items) <= _MAX_ITEMS


def test_manifest_cancel_is_terminal():
    m = RunManifest(run_id="r1").start(total=3)
    m.cancel()
    assert m.state is RunState.cancelled
    assert m.state.is_terminal()
    assert m.halt_reason_kind is HaltReasonKind.cancelled


@pytest.mark.parametrize(
    "exc, kind",
    [
        (LLMBillingError("402"), HaltReasonKind.billing),
        (LLMRateLimitError("429 sustained"), HaltReasonKind.rate_limit),
        (LLMAuthError("401"), HaltReasonKind.auth),
        (TimeoutError("slow"), HaltReasonKind.timeout),
        (RuntimeError("boom"), HaltReasonKind.error),
    ],
)
def test_classify_halt_maps_exceptions(exc, kind):
    assert classify_halt(exc) is kind


def test_halt_from_billing_exception_names_provider_exhaustion():
    m = RunManifest(run_id="r1").start(total=4)
    m.record_completed("a")
    err = LLMBillingError(
        "LLM extraction backend returned 402 Payment Required — check the "
        "OpenRouter account balance (the prepaid account is likely at $0)."
    )
    m.halt_from_exception(err, landed_note="1 of 4 items completed before the failure.")
    assert m.state is RunState.failed
    assert m.halt_reason_kind is HaltReasonKind.billing
    assert m.halt_reason_kind.is_provider_exhaustion
    reason = (m.halt_reason or "").lower()
    assert "provider exhaustion" in reason
    assert "402 payment required" in reason
    assert "1 of 4 items completed" in (m.halt_reason or "")


def test_haltreasonkind_exhaustion_predicate():
    assert HaltReasonKind.billing.is_provider_exhaustion
    assert HaltReasonKind.rate_limit.is_provider_exhaustion
    assert not HaltReasonKind.auth.is_provider_exhaustion
    assert not HaltReasonKind.error.is_provider_exhaustion
    assert not HaltReasonKind.none.is_provider_exhaustion


# --------------------------------------------------------------------------- #
# 2. 429 policy — a blip retries; a sustained streak escalates to a halt
# --------------------------------------------------------------------------- #
def test_is_rate_limit_status():
    assert is_rate_limit_status(429)
    assert not is_rate_limit_status(402)
    assert not is_rate_limit_status(500)


def test_single_429_is_transient_not_fatal():
    """A single (or few) 429s must NOT escalate — the run keeps retrying."""
    esc = RateLimitEscalator(threshold=5)
    for _ in range(4):
        assert esc.record_rate_limited() is None
    assert esc.consecutive == 4


def test_sustained_429_escalates_to_fatal_rate_limit_error():
    esc = RateLimitEscalator(threshold=3)
    assert esc.record_rate_limited() is None
    assert esc.record_rate_limited() is None
    fatal = esc.record_rate_limited(provider="openrouter", host="openrouter.ai")
    assert isinstance(fatal, LLMRateLimitError)
    assert isinstance(fatal, LLMError)
    msg = str(fatal).lower()
    assert "429" in msg
    assert "provider exhaustion" in msg
    assert "openrouter" in msg


def test_success_resets_the_429_streak():
    """A non-429 outcome breaks the streak — 429s must be CONSECUTIVE to escalate,
    so an intermittent rate limit never accumulates to a false halt."""
    esc = RateLimitEscalator(threshold=3)
    esc.record_rate_limited()
    esc.record_rate_limited()
    esc.record_success()  # progress made
    assert esc.consecutive == 0
    # Two more 429s do NOT trip the threshold now (streak restarted).
    assert esc.record_rate_limited() is None
    assert esc.record_rate_limited() is None


def test_default_threshold_is_reasonable():
    assert RATE_LIMIT_HALT_THRESHOLD >= 3


# --------------------------------------------------------------------------- #
# 3. /api/v1/key balance helper — mocked httpx, no live call
# --------------------------------------------------------------------------- #
from cograph_client.resolver import llm_router  # noqa: E402
from cograph_client.resolver.llm_router import (  # noqa: E402
    OpenRouterKeyStatus,
    openrouter_key_status,
)


class _FakeKeyClient:
    def __init__(self, status: int, payload: dict):
        self._status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *a, **k):
        resp = MagicMock()
        if self._status >= 400:
            request = httpx.Request("GET", f"{llm_router.OPENROUTER_BASE}/key")
            response = httpx.Response(self._status, request=request, json=self._payload)
            err = httpx.HTTPStatusError("err", request=request, response=response)

            def _raise():
                raise err

            resp.raise_for_status = _raise
            resp.status_code = self._status
            resp.json = lambda: self._payload
            resp.text = json.dumps(self._payload)
        else:
            resp.raise_for_status = lambda: None
            resp.json = lambda: self._payload
        return resp


def _patch_key_client(monkeypatch, status: int, payload: dict):
    monkeypatch.setattr(
        llm_router.httpx, "AsyncClient", lambda *a, **k: _FakeKeyClient(status, payload)
    )


async def test_key_status_reports_exhausted_account(monkeypatch):
    _patch_key_client(
        monkeypatch,
        200,
        {"data": {"usage": 20.0, "limit": 20.0, "limit_remaining": 0.0, "is_free_tier": False}},
    )
    st = await openrouter_key_status("sk-or-test")
    assert isinstance(st, OpenRouterKeyStatus)
    assert st.exhausted is True
    summary = st.summary()
    assert "used $20.00 of $20.00" in summary
    assert "$0.00 remaining" in summary


async def test_key_status_reports_remaining_credits(monkeypatch):
    _patch_key_client(
        monkeypatch,
        200,
        {"data": {"usage": 3.0, "limit": 20.0, "limit_remaining": 17.0}},
    )
    st = await openrouter_key_status("sk-or-test")
    assert st.exhausted is False
    assert "$17.00 remaining" in st.summary()


async def test_key_status_unlimited_is_never_exhausted(monkeypatch):
    _patch_key_client(monkeypatch, 200, {"data": {"usage": 5.0, "limit": None}})
    st = await openrouter_key_status("sk-or-test")
    assert st.exhausted is False
    assert "unlimited" in st.summary()


async def test_key_status_402_on_diagnosis_raises_billing_error(monkeypatch):
    """A 402 on the diagnostic call itself surfaces the SAME typed fatal error, so
    a caller can treat it identically to a 402 on a chat completion."""
    _patch_key_client(monkeypatch, 402, {"error": {"message": "Insufficient credits"}})
    with pytest.raises(LLMBillingError):
        await openrouter_key_status("sk-or-test")


# --------------------------------------------------------------------------- #
# 4. ACCEPTANCE — a 402 in the extraction (LLM) call layer halts a DISCOVERY run
#    to terminal FAILED with a provider-exhaustion reason + a partial-coverage
#    manifest. Reuses the proven ONTA-201 discovery harness.
# --------------------------------------------------------------------------- #
from cograph_client.agent.capabilities import web_ingest_cap  # noqa: E402
from cograph_client.agent.capabilities.web_ingest_cap import (  # noqa: E402
    WebIngestCapability,
)
from cograph_client.agent.registry import AgentContext  # noqa: E402
from cograph_client.enrichment.job_store import InMemoryJobStore  # noqa: E402
from cograph_client.enrichment.models import JobStatus  # noqa: E402
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
    # Two sub-queries: the first lands rows, the second hits the 402.
    "subqueries": ["OpenRouter models A", "OpenRouter models B"],
}


class _TwoPageProvider:
    """First sub-query returns 2 rows; second returns 2 DISTINCT rows (so they
    survive cross-batch dedupe and reach a second ingest, where the 402 fires)."""

    def __init__(self):
        self.name = "fake"
        self.is_paid = False
        self.cost_per_call = 0.0
        self.discover_calls = 0
        self._page = 0

    async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
        self.discover_calls += 1
        if sample:
            page = ROWS[:2]
        else:
            page = (
                ROWS[:2]
                if self._page == 0
                else [{"name": "e", "context_length": "5"}, {"name": "f", "context_length": "6"}]
            )
            self._page += 1
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


async def test_injected_402_halts_discovery_run_with_partial_coverage_manifest(monkeypatch):
    """THE ACCEPTANCE BAR. Inject an ``LLMBillingError`` (the exact typed error
    ``openrouter_chat`` raises on a real 402) into the extraction/ingest LLM call
    layer AFTER the first batch lands. Assert the run is terminal ``failed``, the
    reason names provider exhaustion, and the A9 manifest records partial coverage
    (completed vs dropped) — not a stuck spinner, not a silent success."""
    provider = _TwoPageProvider()
    register_web_source(provider)
    _patch_preview(monkeypatch)

    LANDED = 2  # the first sub-query ingests 2 rows before the 402 hits

    async def landing_then_402(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        # The LLM extraction call inside ingest: the first batch extracts cleanly;
        # the second is refused with 402 Payment Required (credits exhausted).
        if not hasattr(landing_then_402, "_n"):
            landing_then_402._n = 0
        landing_then_402._n += 1
        if landing_then_402._n == 1:
            rows = json.loads(content)
            return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))
        raise LLMBillingError(
            "LLM extraction backend returned 402 Payment Required — check the "
            "OpenRouter account balance (the prepaid account is likely at $0)."
        )

    monkeypatch.setattr(SchemaResolver, "ingest", landing_then_402)

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

    # (1) TERMINAL failed — not stuck running, not a silent applied/complete.
    assert job.status == JobStatus.failed
    assert JobStatus.failed.is_terminal()

    # (2) A9 manifest exists, is terminal, and names PROVIDER EXHAUSTION.
    m = job.manifest
    assert m is not None
    assert m.state is RunState.failed
    assert m.state.is_terminal()
    assert m.halt_reason_kind is HaltReasonKind.billing
    assert m.halt_reason_kind.is_provider_exhaustion
    reason = (m.halt_reason or "").lower()
    assert "provider exhaustion" in reason
    assert "402 payment required" in reason
    # The user-visible reason is also mirrored onto the job for the UI.
    assert "402 Payment Required" in (job.error or "")

    # (3) PARTIAL COVERAGE — completed vs dropped. N of M completed before halt.
    cov = m.coverage()
    assert isinstance(cov, RunCoverage)
    assert m.completed == LANDED
    assert cov.completed == LANDED
    assert cov.dropped > 0  # the unfilled planned remainder was dropped
    assert cov.complete is False
    assert cov.total >= cov.completed + cov.dropped
    assert f"{LANDED} of" in cov.summary

    # Fail-fast: the second sub-query's discover still ran (2 pages), but the run
    # aborted at the second ingest — no third batch was attempted.
    assert provider.discover_calls == 2


# --------------------------------------------------------------------------- #
# 5. ACCEPTANCE (enrichment) — a 402 in the lookup (LLM) layer halts an
#    ENRICHMENT run to terminal FAILED + a provider-exhaustion manifest.
# --------------------------------------------------------------------------- #
from datetime import datetime, timezone  # noqa: E402

from cograph_client.enrichment.cache import EnrichmentCache  # noqa: E402
from cograph_client.enrichment.executor import EnrichmentExecutor  # noqa: E402
from cograph_client.enrichment.models import (  # noqa: E402
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
)


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


async def test_injected_402_halts_enrichment_run_with_manifest():
    """The same bar on the enrichment rail: a 402 raised from the lookup (LLM) layer
    halts the run to terminal ``failed`` with a provider-exhaustion manifest whose
    coverage records the items that never completed (dropped)."""
    rows = [
        {"uri": "https://cograph.tech/entities/Product/p1", "label": "Bosch", "vals": ""},
        {"uri": "https://cograph.tech/entities/Product/p2", "label": "Makita", "vals": ""},
        {"uri": "https://cograph.tech/entities/Product/p3", "label": "DeWalt", "vals": ""},
    ]
    neptune = AsyncMock()
    neptune.query.return_value = _entities_query_response(rows)
    neptune.update.return_value = None

    store = InMemoryJobStore()
    executor = EnrichmentExecutor(neptune, store, EnrichmentCache(), MagicMock())

    job = EnrichJob(
        id="enrich-402",
        tenant_id="test-tenant",
        kg_name="kg",
        type_name="Product",
        attributes=["manufacturer"],
        tier=EnrichmentTier.lite,
        status=JobStatus.queued,
        created_at=datetime.now(timezone.utc),
        conflict_policy=ConflictPolicy.stage,
    )
    await store.create(job)

    async def boom_lookup(*a, **k):
        raise LLMBillingError(
            "LLM extraction backend returned 402 Payment Required — check the "
            "OpenRouter account balance (the prepaid account is likely at $0)."
        )

    with patch.object(executor, "_lookup_chain", side_effect=boom_lookup):
        await executor.run(job, "test-tenant")

    final = await store.get(job.id)
    assert final is not None
    # Terminal failed — never a stuck running.
    assert final.status == JobStatus.failed
    assert final.status.is_terminal()
    # The user-visible reason survives onto the job.
    assert "402 Payment Required" in (final.error or "")

    # A9 manifest: terminal, provider exhaustion, honest partial coverage.
    m = final.manifest
    assert m is not None
    assert m.state is RunState.failed
    assert m.halt_reason_kind is HaltReasonKind.billing
    assert "provider exhaustion" in (m.halt_reason or "").lower()
    cov = m.coverage()
    assert cov.total == 3  # 3 entities x 1 attribute planned
    assert cov.dropped > 0  # items were dropped by the halt
    assert cov.complete is False
