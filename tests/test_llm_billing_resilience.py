"""ONTA-201 — LLM billing/auth (402/401) resilience.

On 2026-07-04 the shared OpenRouter prepaid account hit $0 mid-discovery-run, so
``/chat/completions`` returned ``402 Payment Required``. The 402 bubbled through
the extraction split-and-retry recovery (wasting calls), was logged as a vague
``web_ingest_subquery_failed``, and the run reported "complete" with silently
dropped batches.

These tests prove the fix:

* (a) a 402 (and 401) from the LLM call raises a distinct, typed FATAL error
  (``LLMBillingError`` / ``LLMAuthError``) and does NOT trigger split-retry;
* (b) the discovery run short-circuits to a FAILED job with the clear,
  user-facing message on a billing error — recording honest partials (rows
  landed vs lost), never a silent "complete";
* (c) a NON-402 error still degrades per-batch as before (regression guard).

Following the repo convention (see tests/test_stage_timing.py): assertions on
log/behavior use a MagicMock module logger, NOT ``structlog.testing.capture_logs``
(unreliable under the full suite because the module logger is cached by earlier
tests).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cograph_client.resolver import llm_router
from cograph_client.resolver.llm_router import openrouter_chat
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
    IngestResult,
)
from cograph_client.resolver.schema_resolver import SchemaResolver
from cograph_client.resolver.verdict_cache import JsonVerdictCache
from cograph_client.retrieval.errors import (
    LLMAuthError,
    LLMBillingError,
    LLMError,
    RetrievalError,
    classify_llm_status_error,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _status_error(status: int, *, body: dict | None = None) -> httpx.HTTPStatusError:
    """Build the exact exception ``res.raise_for_status()`` raises for ``status``
    — the real traceback shape the 402 came through."""
    request = httpx.Request("POST", f"{llm_router.OPENROUTER_BASE}/chat/completions")
    response = httpx.Response(
        status,
        request=request,
        json=body if body is not None else {"error": {"message": "Insufficient credits"}},
    )
    return httpx.HTTPStatusError(
        f"{status} error", request=request, response=response
    )


class _FakeAsyncClient:
    """Stand-in for ``httpx.AsyncClient`` whose ``post`` returns a response whose
    ``raise_for_status`` raises the configured HTTP error (or none)."""

    def __init__(self, status: int, body: dict | None = None):
        self._status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *a, **k):
        resp = MagicMock()
        if self._status >= 400:
            err = _status_error(self._status, body=self._body)

            def _raise():
                raise err

            resp.raise_for_status = _raise
            resp.status_code = self._status
            resp.json = lambda: (
                self._body
                if self._body is not None
                else {"error": {"message": "Insufficient credits"}}
            )
            resp.text = json.dumps(self._body or {})
        else:
            resp.raise_for_status = lambda: None
            resp.json = lambda: {
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]
            }
        return resp


def _patch_client(monkeypatch, status: int, body: dict | None = None):
    monkeypatch.setattr(
        llm_router.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeAsyncClient(status, body),
    )


# --------------------------------------------------------------------------- #
# (a) openrouter_chat classifies 402/401 as typed fatal errors; others stay raw
# --------------------------------------------------------------------------- #
async def test_openrouter_402_raises_billing_error(monkeypatch):
    _patch_client(monkeypatch, 402)
    with pytest.raises(LLMBillingError) as ei:
        await openrouter_chat("key", "sys", "user")
    # Clear, actionable, user-facing message referencing 402.
    assert "402" in str(ei.value)
    assert "Payment Required" in str(ei.value)
    # And it is part of the ONE retrieval error hierarchy (single-except contract).
    assert isinstance(ei.value, LLMError)
    assert isinstance(ei.value, RetrievalError)


async def test_openrouter_401_raises_auth_error(monkeypatch):
    _patch_client(monkeypatch, 401)
    with pytest.raises(LLMAuthError) as ei:
        await openrouter_chat("key", "sys", "user")
    assert "401" in str(ei.value)
    assert isinstance(ei.value, LLMError)


async def test_openrouter_detail_folded_into_message(monkeypatch):
    _patch_client(
        monkeypatch, 402, body={"error": {"message": "Prepaid balance is $0.00"}}
    )
    with pytest.raises(LLMBillingError) as ei:
        await openrouter_chat("key", "sys", "user")
    assert "Prepaid balance is $0.00" in str(ei.value)


async def test_openrouter_429_still_raises_raw_httpstatuserror(monkeypatch):
    """Regression guard: a NON-402/401 status (rate limit) is a normal transient —
    it must keep propagating as the raw ``HTTPStatusError``, NOT be escalated to a
    fatal LLM error."""
    _patch_client(monkeypatch, 429)
    with pytest.raises(httpx.HTTPStatusError):
        await openrouter_chat("key", "sys", "user")


async def test_openrouter_500_still_raises_raw_httpstatuserror(monkeypatch):
    _patch_client(monkeypatch, 500)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await openrouter_chat("key", "sys", "user")
    assert not isinstance(ei.value, LLMError)


# --------------------------------------------------------------------------- #
# (a2) the billing/auth message names the ACTUAL provider (2026-07-08 bug)
#
# openrouter_chat routes to Cerebras when OMNIX_LLM_PROVIDER=cerebras, but the
# 402/401 message used to hardcode "OpenRouter". On 2026-07-08 a Cerebras 402
# (Cerebras out of credits) told the operator to check the *OpenRouter* balance
# — an hour lost debugging the wrong account. These assert the MECHANISM: the
# message names whatever provider it is HANDED and never a different one. They
# use invented provider strings on purpose, so a fix that only special-cases the
# two real literals (a hardcoded {cerebras, openrouter} table) would FAIL them.
# --------------------------------------------------------------------------- #
def _billing_msg(provider=None, host=None) -> str:
    err = classify_llm_status_error(402, provider=provider, host=host)
    assert isinstance(err, LLMBillingError)  # sanity: 402 → billing
    return str(err)


@pytest.mark.parametrize(
    "provider, host",
    [
        ("cerebras", "api.cerebras.ai"),  # the real regressor
        ("openrouter", "openrouter.ai"),  # the other real backend
        ("acme-llm", "api.acme-llm.example"),  # invented → proves derivation
        ("Zephyr", "llm.zephyr.test"),  # arbitrary casing → still derived
    ],
)
def test_billing_message_names_the_handed_provider_only(provider, host):
    """For ANY provider it is given, the message names THAT provider (via slug or
    host) and never leaks a DIFFERENT known backend's name — the exact 2026-07-08
    failure mode was a Cerebras 402 that named OpenRouter."""
    msg = _billing_msg(provider=provider, host=host).lower()
    # It always names the provider it was handed (the slug and the host both do).
    assert provider.lower() in msg
    assert host.lower() in msg
    # It NEVER names a different backend. Any known backend token that is not part
    # of the handed provider/host must be absent (no cross-provider misdiagnosis).
    handed = f"{provider.lower()} {host.lower()}"
    for foreign in ("cerebras", "openrouter", "api.cerebras.ai", "openrouter.ai"):
        if foreign not in handed:
            assert foreign not in msg, f"{provider!r} 402 wrongly mentioned {foreign!r}"


def test_cerebras_402_says_cerebras_not_openrouter():
    """The literal production regression, spelled out: a Cerebras 402 must name
    Cerebras and must NOT say OpenRouter."""
    msg = _billing_msg(provider="cerebras", host="api.cerebras.ai")
    assert "Cerebras" in msg
    assert "OpenRouter" not in msg
    assert "openrouter" not in msg.lower()


def test_openrouter_402_says_openrouter_not_cerebras():
    """The mirror: an OpenRouter 402 must name OpenRouter and must NOT say
    Cerebras."""
    msg = _billing_msg(provider="openrouter", host="openrouter.ai").lower()
    assert "openrouter" in msg
    assert "cerebras" not in msg


def test_billing_message_without_provider_is_generic_and_backward_compatible():
    """Every pre-existing caller passes no provider/host — the message must stay a
    valid, actionable LLMBillingError that invents NO specific backend name."""
    msg = _billing_msg()  # no provider, no host
    assert "402 Payment Required" in msg
    lower = msg.lower()
    assert "cerebras" not in lower
    assert "openrouter" not in lower
    # Still points the operator somewhere sensible (a generic provider account).
    assert "provider" in lower


def test_auth_401_message_is_also_provider_aware():
    """401 (auth) threads the same provider so a bad Cerebras key doesn't tell the
    operator to rotate the OpenRouter key."""
    err = classify_llm_status_error(401, provider="acme-llm", host="api.acme-llm.example")
    assert isinstance(err, LLMAuthError)
    msg = str(err).lower()
    assert "401" in msg
    assert "acme-llm" in msg
    assert "cerebras" not in msg
    assert "openrouter" not in msg


def test_classify_detail_still_folded_with_provider():
    """The provider threading composes with the existing ``detail`` folding — the
    provider body reason is still appended."""
    err = classify_llm_status_error(
        402, detail="out of credits", provider="acme-llm", host="api.acme-llm.example"
    )
    msg = str(err)
    assert "out of credits" in msg
    assert "Acme-Llm" in msg or "acme-llm" in msg.lower()


async def test_cerebras_provider_message_flows_through_openrouter_chat(monkeypatch):
    """End-to-end at the RAISE site: a 402 for a call that ACTUALLY hit Cerebras
    names Cerebras (its host), not OpenRouter — proving the call site threads the
    provider actually served, not just the pure classifier. A BARE model slug is
    what routes to Cerebras (the #163 slug-shape flip); a vendor/model slug would
    route to OpenRouter regardless of OMNIX_LLM_PROVIDER (see the next test)."""
    monkeypatch.setenv("OMNIX_LLM_PROVIDER", "cerebras")
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")
    _patch_client(monkeypatch, 402)
    with pytest.raises(LLMBillingError) as ei:
        # Bare slug -> Cerebras branch. api_key is ignored in cerebras mode.
        await openrouter_chat("ignored-openrouter-key", "sys", "user", model="gpt-oss-120b")
    msg = str(ei.value).lower()
    assert "cerebras" in msg
    assert "openrouter" not in msg


async def test_cerebras_provider_with_slashed_model_names_openrouter(monkeypatch):
    """Reconciliation guard (#162 x #163): OMNIX_LLM_PROVIDER=cerebras but a
    vendor/model (slash) slug routes to OpenRouter by slug shape, so a 402 there
    must name OpenRouter — the account that ACTUALLY served the call — never the
    globally-configured 'cerebras'. This is the interaction that a naive
    provider=_llm_provider() would misreport (and did, until the raise site began
    deriving the provider from the base it actually hit)."""
    monkeypatch.setenv("OMNIX_LLM_PROVIDER", "cerebras")
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")
    _patch_client(monkeypatch, 402)
    with pytest.raises(LLMBillingError) as ei:
        await openrouter_chat("openrouter-key", "sys", "user", model="anthropic/claude-opus-4.8")
    msg = str(ei.value).lower()
    assert "openrouter" in msg
    assert "cerebras" not in msg


async def test_openrouter_provider_message_flows_through_openrouter_chat(monkeypatch):
    """Mirror end-to-end: the default (OpenRouter) backend's 402 names OpenRouter,
    not Cerebras."""
    monkeypatch.delenv("OMNIX_LLM_PROVIDER", raising=False)  # default → openrouter
    _patch_client(monkeypatch, 402)
    with pytest.raises(LLMBillingError) as ei:
        await openrouter_chat("openrouter-key", "sys", "user")
    msg = str(ei.value).lower()
    assert "openrouter" in msg
    assert "cerebras" not in msg


# --------------------------------------------------------------------------- #
# (b) the recovery path does NOT split-retry on a billing error
# --------------------------------------------------------------------------- #
@pytest.fixture
def mock_neptune():
    c = AsyncMock()
    c.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    c.update.return_value = None
    c.batch_exists.return_value = set()
    return c


@pytest.fixture
def mock_cache(tmp_path):
    return JsonVerdictCache(tmp_path / "cache.json")


def _records(n: int) -> list[dict]:
    return [{"id": i, "name": f"model_{i}"} for i in range(n)]


async def test_billing_error_does_not_trigger_split_retry(mock_neptune, mock_cache):
    """A 402 from the extraction call must propagate OUT of the JSON recovery path
    as the typed fatal error — NOT be swallowed into an empty extraction and split
    into halves (which would burn more equally-doomed calls). We count _extract
    invocations to prove no splitting occurred."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    content = json.dumps(_records(40))  # dense enough to be split IF it degraded

    extract_calls: list[int] = []

    async def boom_extract(chunk, content_type, existing_types=None):
        try:
            n = len(json.loads(chunk))
        except json.JSONDecodeError:
            n = 0
        extract_calls.append(n)
        raise LLMBillingError(
            "LLM extraction backend returned 402 Payment Required — check balance."
        )

    with patch.object(resolver, "_extract", side_effect=boom_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            with pytest.raises(LLMBillingError):
                await resolver.ingest(content, "test-tenant", content_type="json")

    # Exactly ONE extraction attempt: the fatal error propagated immediately, no
    # split-and-retry recursion (which would have produced 2+ smaller-chunk calls).
    assert len(extract_calls) == 1


async def test_non_billing_empty_extraction_still_splits(
    mock_neptune, mock_cache
):
    """Regression guard: an ordinary empty extraction (truncation) STILL recovers
    via split-and-retry — the billing short-circuit must not disturb it."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    content = json.dumps(_records(40))

    extract_calls: list[int] = []

    async def maybe_extract(chunk, content_type, existing_types=None):
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            data = []
        n = len(data)
        extract_calls.append(n)
        # A big chunk "truncates" (empty); a small enough one succeeds.
        if 0 < n <= 12:
            return ExtractionResult(
                entities=[
                    ExtractedEntity(
                        type_name="Model",
                        id=str(r["id"]),
                        attributes=[
                            ExtractedAttribute(
                                name="name", value=r["name"], datatype="string"
                            )
                        ],
                    )
                    for r in data
                ],
                relationships=[],
            )
        return ExtractionResult(entities=[], relationships=[])

    with patch.object(resolver, "_extract", side_effect=maybe_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            result = await resolver.ingest(content, "test-tenant", content_type="json")

    # Splitting happened (more than the initial chunk count of extraction calls)
    # and NO records were lost — the ordinary recovery path is untouched.
    assert len(extract_calls) > 1
    assert result.rows_dropped == 0


# --------------------------------------------------------------------------- #
# (c) the discovery run short-circuits to a failed job with honest partials
# --------------------------------------------------------------------------- #
from cograph_client.agent.capabilities import web_ingest_cap  # noqa: E402
from cograph_client.agent.capabilities.web_ingest_cap import (  # noqa: E402
    WebIngestCapability,
)
from cograph_client.agent.registry import AgentContext  # noqa: E402
from cograph_client.web_sources import (  # noqa: E402
    DiscoverResult,
    register_web_source,
    reset_web_sources,
)


ROWS = [
    # 2+ char names so the ONTA-393 A1 validators keep these synthetic discovery
    # rows (a length<2 key cell is dropped as chrome); this test is about honest
    # partial coverage on a billing error, not the entity names.
    {"name": "aa", "context_length": "1"},
    {"name": "bb", "context_length": "2"},
    {"name": "cc", "context_length": "3"},
    {"name": "dd", "context_length": "4"},
]

SPEC = {
    "entity_type": "OpenRouterModel",
    "key_attribute": "name",
    "query": "OpenRouter models",
    "confirmed_attributes": ["context_length"],
    "suggested_attributes": ["provider", "context_length"],
    # Two sub-queries so we can prove the SECOND is never attempted after the
    # billing error aborts the first.
    "subqueries": ["OpenRouter models A", "OpenRouter models B"],
}


class _FakeProvider:
    def __init__(self, rows=None):
        self.name = "fake"
        self.is_paid = False
        self.cost_per_call = 0.0
        self._rows = ROWS if rows is None else rows
        self.discover_calls = 0

    async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
        self.discover_calls += 1
        rows = self._rows[: (5 if sample else max_rows)]
        if hint_columns:
            rows = [{c: r.get(c, "unknown") for c in hint_columns} for r in rows]
        return DiscoverResult(
            rows=rows,
            provenance={},
            sources=["https://openrouter.ai/models"],
            estimated_total=len(self._rows),
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


async def test_billing_error_fails_whole_run_with_clear_message(monkeypatch):
    """A 402 raised by the ingest of the FIRST batch short-circuits the WHOLE
    discovery run: the job is FAILED with the clear, user-facing message, the
    second sub-query is never attempted, and the failure is logged distinctly (NOT
    as web_ingest_subquery_failed)."""
    import asyncio

    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    provider = _FakeProvider()
    register_web_source(provider)
    _patch_preview(monkeypatch)

    # Distinct module logger to assert on (mock-logger pattern, not capture_logs).
    rec = MagicMock()
    monkeypatch.setattr(web_ingest_cap, "logger", rec)

    async def billing_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        raise LLMBillingError(
            "LLM extraction backend returned 402 Payment Required — check the "
            "OpenRouter balance."
        )

    monkeypatch.setattr(SchemaResolver, "ingest", billing_ingest)

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
    # Terminal FAILED — not stuck running, not a silent applied/complete.
    assert job.status == JobStatus.failed
    assert job.status != JobStatus.applied
    # The clear, user-facing message survives onto the job.
    assert "402 Payment Required" in (job.error or "")
    assert "OpenRouter balance" in (job.error or "")

    # The provider's discover ran for the FIRST batch only — the run aborted, so
    # the second sub-query never fetched (fail-fast, no wasted calls).
    assert provider.discover_calls == 1

    # Logged distinctly as a fatal backend error — NOT swallowed as a per-batch
    # web_ingest_subquery_failed, and NOT reported complete.
    events = [c.args[0] for c in rec.error.call_args_list if c.args]
    assert "web_ingest_llm_backend_fatal" in events
    warn_events = [c.args[0] for c in rec.warning.call_args_list if c.args]
    assert "web_ingest_subquery_failed" not in warn_events
    info_events = [c.args[0] for c in rec.info.call_args_list if c.args]
    assert "web_ingest_complete" not in info_events


async def test_billing_error_records_honest_partials(monkeypatch):
    """When the FIRST batch already landed and the SECOND ingest hits a 402, the
    terminal job records rows-LANDED vs rows-LOST — never a silent complete. The
    error_summary carries a job-level item naming the partial."""
    import asyncio

    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    # DISTINCT rows per sub-query so the second batch survives cross-batch dedupe
    # and actually reaches a second ingest (where the 402 fires). A per-call
    # counter drives the two disjoint pages.
    class _TwoPageProvider(_FakeProvider):
        def __init__(self):
            super().__init__()
            self._page = 0

        async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
            self.discover_calls += 1
            if sample:
                page = ROWS[:2]
            else:
                page = (
                    ROWS[:2] if self._page == 0 else
                    [{"name": "ee", "context_length": "5"}, {"name": "ff", "context_length": "6"}]
                )
                self._page += 1
            if hint_columns:
                page = [{c: r.get(c, "unknown") for c in hint_columns} for r in page]
            return DiscoverResult(
                rows=page, provenance={},
                sources=["https://openrouter.ai/models"],
                estimated_total=4, is_partial=sample,
            )

    provider = _TwoPageProvider()
    register_web_source(provider)
    _patch_preview(monkeypatch)

    calls = {"n": 0}
    LANDED = 2  # the first sub-query ingests 2 rows before the 402 hits

    async def landing_then_billing(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            rows = json.loads(content)
            return IngestResult(
                entities_extracted=len(rows), entities_resolved=len(rows)
            )
        raise LLMBillingError(
            "LLM extraction backend returned 402 Payment Required — check balance."
        )

    monkeypatch.setattr(SchemaResolver, "ingest", landing_then_billing)

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
    assert job.status == JobStatus.failed
    # Honest partial: the first batch's rows are recorded as landed, and the error
    # says so — NOT dropped silently, NOT presented as a full success.
    landed = LANDED
    assert job.progress.processed == landed
    assert job.result_count == landed
    assert f"{landed} record(s) were ingested" in (job.error or "")
    # error_summary carries a job-level (backend) item, not provider-blamed.
    assert job.error_summary
    item = job.error_summary[0]
    assert item.kind == "job"
    assert item.provider is None


# --------------------------------------------------------------------------- #
# (c-regression) a NON-402 error still degrades per-batch as before
# --------------------------------------------------------------------------- #
async def test_non_billing_error_still_degrades_per_batch(monkeypatch):
    """A plain RuntimeError from ingest keeps TODAY's per-batch behavior: it is
    caught in the loop as web_ingest_subquery_failed and, since nothing landed,
    the job fails with the attributed error — NOT the billing short-circuit."""
    import asyncio

    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.enrichment.models import JobStatus

    provider = _FakeProvider()
    register_web_source(provider)
    _patch_preview(monkeypatch)

    rec = MagicMock()
    monkeypatch.setattr(web_ingest_cap, "logger", rec)

    async def boom(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr(SchemaResolver, "ingest", boom)

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
    assert job.status == JobStatus.failed
    assert "ingest exploded" in (job.error or "")

    # The per-batch recovery path ran (each (sub_query, provider) failure logged
    # as web_ingest_subquery_failed) — NOT the billing fatal path.
    warn_events = [c.args[0] for c in rec.warning.call_args_list if c.args]
    assert "web_ingest_subquery_failed" in warn_events
    error_events = [c.args[0] for c in rec.error.call_args_list if c.args]
    assert "web_ingest_llm_backend_fatal" not in error_events
    # Both sub-queries were attempted (per-batch degrade, no early abort): the
    # ingest was called once per (sub_query, provider) that discovered rows.
    assert provider.discover_calls == 2
