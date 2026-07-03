"""Tests for URL-targeted enrichment (WP2 of the Firecrawl/URL-extract feature).

Covers the three OSS seams the feature threads URLs through:

  1. ``EnrichRequest.target_urls`` / ``EnrichJob.source_urls`` model fields.
  2. The executor folding ``job.source_urls`` into the adapter lookup
     ``context`` as ``target_urls`` (so a URL-aware premium adapter reads the
     supplied pages) — at the chain call site. A fake adapter records the
     context it received.
  3. The enrich capability threading supplied URLs (from the message via
     ``extract_urls`` OR from structured ``ctx.urls``) into the plan step params
     and then into the EnrichJob at execute time.

Everything is stubbed (no network, no real Neptune) and each async test is
wrapped in ``asyncio.wait_for`` so a hang fails loudly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from cograph_client.agent.capabilities.enrich_cap import EnrichCapability
from cograph_client.agent.registry import AgentContext, PlanStep
from cograph_client.enrichment.cache import EnrichmentCache
from cograph_client.enrichment.executor import EnrichmentExecutor
from cograph_client.enrichment.job_store import InMemoryJobStore
from cograph_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    EnrichRequest,
    JobStatus,
    Verdict,
)

TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Helpers / fakes (kept local so this file is self-contained)
# ---------------------------------------------------------------------------


def _make_job(
    *, source_urls: list[str] | None = None, type_name: str = "Product"
) -> EnrichJob:
    return EnrichJob(
        id="job-urls-1",
        tenant_id="test-tenant",
        kg_name="kg",
        type_name=type_name,
        attributes=["manufacturer"],
        tier=EnrichmentTier.lite,
        status=JobStatus.queued,
        created_at=datetime.now(timezone.utc),
        conflict_policy=ConflictPolicy.stage,
        confidence_min=0.85,
        source_urls=source_urls or [],
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
    return {
        "head": {"vars": ["e", "label", "nameAttr", "vals"]},
        "results": {"bindings": bindings},
    }


def _single_product_neptune():
    from unittest.mock import AsyncMock

    rows = [
        {"uri": "https://cograph.tech/entities/Product/p1", "label": "Bosch", "vals": ""},
    ]
    neptune = AsyncMock()
    neptune.query.return_value = _entities_query_response(rows)
    neptune.update.return_value = None
    return neptune


class _FakeWikidata:
    name = "wikidata"

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    async def lookup(self, entity_label, attribute, context):
        self.calls.append((entity_label, attribute, dict(context)))
        return []


class _RecordingAdapter:
    """A SourceAdapter that records the context dict it was called with so a test
    can assert ``target_urls`` was threaded in. Yields a fixed verdict."""

    def __init__(self, name: str, value: str = "FromAdapter") -> None:
        self.name = name
        self._value = value
        self.calls: list[tuple[str, str, dict]] = []

    async def lookup(self, entity_label, attribute, context):
        self.calls.append((entity_label, attribute, dict(context)))
        return [Verdict(value=self._value, confidence=0.95, source=self.name)]


# ---------------------------------------------------------------------------
# 1. Model fields
# ---------------------------------------------------------------------------


def test_enrich_request_accepts_target_urls():
    """EnrichRequest carries an optional ``target_urls`` list; default None."""
    req = EnrichRequest(
        type_name="Broker",
        attributes=["website"],
        kg_name="kg",
        target_urls=["https://example.com/a", "https://example.com/b"],
    )
    assert req.target_urls == ["https://example.com/a", "https://example.com/b"]

    # Omitted → None (backward compatible, unchanged default).
    req2 = EnrichRequest(type_name="Broker", attributes=["website"], kg_name="kg")
    assert req2.target_urls is None


def test_enrich_job_source_urls_default_empty():
    """EnrichJob.source_urls defaults to an empty list when not supplied."""
    job = _make_job()
    assert job.source_urls == []
    job2 = _make_job(source_urls=["https://example.com/x"])
    assert job2.source_urls == ["https://example.com/x"]


# ---------------------------------------------------------------------------
# 2. Executor threads source_urls -> adapter context["target_urls"]
# ---------------------------------------------------------------------------


def test_executor_source_urls_flow_into_chain_lookup_context():
    """job.source_urls is threaded into the chain adapter lookup context as
    ``target_urls``; absent → the context carries no ``target_urls`` key."""

    async def run():
        from cograph_client.enrichment.sources.base import register_adapter

        # WITH source_urls.
        neptune = _single_product_neptune()
        store = InMemoryJobStore()
        executor = EnrichmentExecutor(
            neptune, store, EnrichmentCache(), _FakeWikidata()
        )
        adapter = _RecordingAdapter("urlsrc", value="Robert Bosch GmbH")
        register_adapter(adapter)

        job = _make_job(source_urls=["https://example.com/p1", "https://example.com/p2"])
        job.sources = ["urlsrc"]
        await store.create(job)
        await asyncio.wait_for(executor.run(job, "test-tenant"), timeout=TIMEOUT)

        assert adapter.calls
        ctx = adapter.calls[0][2]
        assert ctx.get("target_urls") == [
            "https://example.com/p1",
            "https://example.com/p2",
        ]

        # WITHOUT source_urls → no target_urls key (unchanged call shape).
        neptune2 = _single_product_neptune()
        store2 = InMemoryJobStore()
        executor2 = EnrichmentExecutor(
            neptune2, store2, EnrichmentCache(), _FakeWikidata()
        )
        adapter2 = _RecordingAdapter("urlsrc2", value="Robert Bosch GmbH")
        register_adapter(adapter2)
        job2 = _make_job()  # no source_urls
        job2.sources = ["urlsrc2"]
        await store2.create(job2)
        await asyncio.wait_for(executor2.run(job2, "test-tenant"), timeout=TIMEOUT)

        assert adapter2.calls
        assert "target_urls" not in adapter2.calls[0][2]

    asyncio.run(run())


def test_executor_source_urls_flow_into_wikidata_lookup_context():
    """The single-adapter ``_lookup`` path also threads source_urls into the
    context as ``target_urls`` (covers the wikidata/default code path)."""

    async def run():
        neptune = _single_product_neptune()
        store = InMemoryJobStore()
        wikidata = _FakeWikidata()
        executor = EnrichmentExecutor(neptune, store, EnrichmentCache(), wikidata)

        job = _make_job(source_urls=["https://example.com/page"])
        await store.create(job)
        # Drive the single-adapter primitive directly so we exercise _lookup's
        # context construction (the chain path is covered by the test above).
        verdicts = await asyncio.wait_for(
            executor._lookup("Bosch", "manufacturer", job, cache_hit_inc=False),
            timeout=TIMEOUT,
        )
        assert verdicts == []
        assert wikidata.calls
        assert wikidata.calls[0][2].get("target_urls") == [
            "https://example.com/page"
        ]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2b. Executor threads job.type_name -> adapter context["entity_type"] (ONTA-191)
# ---------------------------------------------------------------------------


def test_executor_type_name_flows_into_chain_lookup_context():
    """job.type_name is threaded into the chain adapter lookup context as
    ``entity_type`` — a bare canonical type label, not a URI. The key is present
    because every job carries a type_name (unchanged call shape otherwise)."""

    async def run():
        from cograph_client.enrichment.sources.base import register_adapter

        neptune = _single_product_neptune()
        store = InMemoryJobStore()
        executor = EnrichmentExecutor(
            neptune, store, EnrichmentCache(), _FakeWikidata()
        )
        adapter = _RecordingAdapter("typesrc", value="Robert Bosch GmbH")
        register_adapter(adapter)

        job = _make_job(type_name="Restaurant")
        job.sources = ["typesrc"]
        await store.create(job)
        await asyncio.wait_for(executor.run(job, "test-tenant"), timeout=TIMEOUT)

        assert adapter.calls
        ctx = adapter.calls[0][2]
        # Bare canonical type label (ontology casing), NOT a URI, NOT lowercased.
        assert ctx.get("entity_type") == "Restaurant"

    asyncio.run(run())


def test_executor_type_name_flows_into_wikidata_lookup_context():
    """The single-adapter ``_lookup`` path also threads job.type_name into the
    context as ``entity_type`` (covers the wikidata/default code path)."""

    async def run():
        neptune = _single_product_neptune()
        store = InMemoryJobStore()
        wikidata = _FakeWikidata()
        executor = EnrichmentExecutor(neptune, store, EnrichmentCache(), wikidata)

        job = _make_job(type_name="Person")
        await store.create(job)
        verdicts = await asyncio.wait_for(
            executor._lookup("Ada Lovelace", "manufacturer", job, cache_hit_inc=False),
            timeout=TIMEOUT,
        )
        assert verdicts == []
        assert wikidata.calls
        assert wikidata.calls[0][2].get("entity_type") == "Person"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. Capability threads URLs from instruction/ctx -> params -> EnrichJob
# ---------------------------------------------------------------------------


class _FakeJobStore:
    def __init__(self):
        self.created: list[EnrichJob] = []

    async def create(self, job):
        self.created.append(job)

    async def get(self, job_id):
        for j in self.created:
            if j.id == job_id:
                return j
        return None

    async def update(self, job):
        pass


class _FakeExecutor:
    def __init__(self):
        self.ran = []

    async def run(self, job, tenant_id):
        self.ran.append((job, tenant_id))


def _ctx(neptune=None, urls=None, **extras_kw):
    from unittest.mock import AsyncMock

    ctx = AgentContext(
        tenant_id="t1",
        kg_name="kg1",
        neptune=neptune or AsyncMock(),
        type_name="Broker",
        openrouter_key="fake-key",
        anthropic_key="fake-anthropic",
        extras={
            "enrichment_executor": extras_kw.get("executor", _FakeExecutor()),
            "enrichment_job_store": extras_kw.get("job_store", _FakeJobStore()),
        },
    )
    # AgentContext may not declare ``urls`` yet (lands in a sibling WP); set it
    # dynamically so we can exercise the structured-context path. Capabilities
    # read it defensively via getattr.
    if urls is not None:
        object.__setattr__(ctx, "urls", urls)
    return ctx


def _stub_plan_deps(monkeypatch, *, schema=None, extract=None):
    """Stub the type list + schema + extraction LLM the enrich plan() needs so it
    runs without Neptune/LLM. Returns nothing; mutates via monkeypatch."""
    import json

    schema = schema or {"attributes": ["website"], "relationships": []}
    extract = extract or {"attributes": ["website"], "scope": None, "tier": "core"}

    async def fake_list_types(ctx):
        return ["Broker"]

    async def fake_schema(neptune, tenant_id, type_name):
        return schema

    async def fake_chat(*args, **kwargs):
        return json.dumps(extract)

    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap._list_types", fake_list_types
    )
    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.list_type_schema", fake_schema
    )
    monkeypatch.setattr(
        "cograph_client.agent.capabilities.enrich_cap.openrouter_chat", fake_chat
    )


@pytest.mark.asyncio
async def test_plan_extracts_urls_from_instruction_into_params(monkeypatch):
    """URLs pasted in the message are extracted into the enrich step's
    ``source_urls`` params and reflected in the rationale/preview."""
    _stub_plan_deps(monkeypatch)
    cap = EnrichCapability()
    ctx = _ctx()
    steps = await asyncio.wait_for(
        cap.plan(
            ctx,
            "enrich the website for brokers from https://acme.example/a "
            "and https://acme.example/b",
        ),
        TIMEOUT,
    )
    assert len(steps) == 1
    step = steps[0]
    assert step.action == "run_enrichment"
    assert step.params["source_urls"] == [
        "https://acme.example/a",
        "https://acme.example/b",
    ]
    # Reflected to the user.
    assert "2 supplied page" in step.rationale
    assert step.preview["source_urls"] == [
        "https://acme.example/a",
        "https://acme.example/b",
    ]


@pytest.mark.asyncio
async def test_plan_prefers_structured_ctx_urls(monkeypatch):
    """Structured Explorer context (ctx.urls) wins over message extraction."""
    _stub_plan_deps(monkeypatch)
    cap = EnrichCapability()
    ctx = _ctx(urls=["https://structured.example/page"])
    steps = await asyncio.wait_for(
        cap.plan(ctx, "enrich the website for brokers from https://typed.example/x"),
        TIMEOUT,
    )
    assert steps[0].params["source_urls"] == ["https://structured.example/page"]
    assert "1 supplied page" in steps[0].rationale


@pytest.mark.asyncio
async def test_plan_without_urls_omits_source_urls(monkeypatch):
    """No URLs in the message or context → no ``source_urls`` key in params
    (existing non-URL plans are byte-for-byte unchanged) and no page mention."""
    _stub_plan_deps(monkeypatch)
    cap = EnrichCapability()
    ctx = _ctx()
    steps = await asyncio.wait_for(
        cap.plan(ctx, "enrich the website for brokers"), TIMEOUT
    )
    assert "source_urls" not in steps[0].params
    assert "supplied page" not in steps[0].rationale


@pytest.mark.asyncio
async def test_execute_threads_source_urls_into_job(monkeypatch):
    """An enrich step carrying ``source_urls`` builds an EnrichJob with those URLs
    on ``job.source_urls`` (so the executor hands them to the adapter)."""
    cap = EnrichCapability()
    job_store = _FakeJobStore()
    ctx = _ctx(job_store=job_store)
    urls = ["https://example.com/u1", "https://example.com/u2"]
    step = PlanStep(
        capability="enrich",
        action="run_enrichment",
        params={
            "type_name": "Broker",
            "attributes": ["website"],
            "tier": "core",
            "confidence_min": 0.4,
            "scope": None,
            "limit": None,
            "entity_uris": None,
            "source_urls": urls,
        },
    )
    out = await asyncio.wait_for(cap.execute(ctx, step), TIMEOUT)
    assert out["kind"] == "ack"
    assert len(job_store.created) == 1
    job = job_store.created[0]
    assert job.source_urls == urls


@pytest.mark.asyncio
async def test_execute_without_source_urls_defaults_empty(monkeypatch):
    """A step with no ``source_urls`` → the job's source_urls defaults to []."""
    cap = EnrichCapability()
    job_store = _FakeJobStore()
    ctx = _ctx(job_store=job_store)
    step = PlanStep(
        capability="enrich",
        action="run_enrichment",
        params={
            "type_name": "Broker",
            "attributes": ["website"],
            "tier": "core",
            "confidence_min": 0.4,
            "scope": None,
            "limit": None,
            "entity_uris": None,
        },
    )
    await asyncio.wait_for(cap.execute(ctx, step), TIMEOUT)
    assert job_store.created[0].source_urls == []
