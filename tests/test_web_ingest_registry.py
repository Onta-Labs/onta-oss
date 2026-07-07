"""API-registry routing on the discovery rail (ONTA-194, phase 2).

Verifies the ``web_ingest`` capability consults the registry before web search on
every query-mode discovery, prepends a matched source-of-truth ahead of web, and
persists the picks so execute() reruns them without a second LLM call — and,
critically, that a ``web_only`` decision (no match / no LLM key) leaves the
discovery path completely unchanged.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from cograph_client.agent.capabilities import web_ingest_cap
from cograph_client.agent.capabilities.web_ingest_cap import (
    WebIngestCapability,
    _merge_registry_ensemble,
    _rebuild_registry_sources,
)
from cograph_client.agent.registry import AgentContext, PlanStep
from cograph_client.api_registry import (
    ApiCallResult,
    MODE_API_ONLY,
    MODE_API_PLUS_WEB,
    RegistryApiSource,
    RoutingDecision,
    RoutingPick,
)
from cograph_client.api_registry.catalog import reset_api_source_layers
from cograph_client.resolver.schema_resolver import IngestResult, SchemaResolver
from cograph_client.web_sources.base import (
    DiscoverResult,
    register_web_source,
    reset_web_sources,
)

CONFIRMED_SPEC = {
    "entity_type": "Physician",
    "key_attribute": "name",
    "query": "cardiologists in San Francisco",
    "confirmed_attributes": ["specialty"],
    "suggested_attributes": ["specialty", "npi"],
}


class FakeWeb:
    def __init__(self, name="fake"):
        self.name = name
        self.is_paid = False
        self.cost_per_call = 0.0

    async def discover(self, query, *, sample, max_rows, hint_columns, context, urls=None):
        return DiscoverResult(rows=[{"name": "web-row"}], provenance={}, sources=["https://web.example"])


def _ctx() -> AgentContext:
    from unittest.mock import MagicMock
    return AgentContext(
        tenant_id="demo-tenant", kg_name="docs", neptune=MagicMock(),
        anthropic_key="sk-ant-test", openrouter_key="", extras={"prior_clarify_count": 1},
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_web_sources()
    reset_api_source_layers()
    yield
    reset_web_sources()
    reset_api_source_layers()


def _canned_decision():
    return RoutingDecision(
        mode=MODE_API_ONLY,
        picks=[RoutingPick(slug="nppes", endpoint="search",
                           bindings={"taxonomy_description": "cardiology", "city": "San Francisco", "state": "CA"})],
        rationale="NPPES is the official US clinician registry",
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def test_merge_api_only_drops_web():
    reg = ["REG"]
    assert _merge_registry_ensemble(["WEB"], reg, MODE_API_ONLY) == ["REG"]


def test_merge_api_plus_web_is_registry_first():
    assert _merge_registry_ensemble(["WEB"], ["REG"], MODE_API_PLUS_WEB) == ["REG", "WEB"]


def test_merge_api_only_falls_back_to_web_when_no_registry():
    assert _merge_registry_ensemble(["WEB"], [], MODE_API_ONLY) == ["WEB"]


@pytest.mark.asyncio
async def test_rebuild_registry_sources_from_params():
    srcs, mode = await _rebuild_registry_sources({
        "registry_picks": [{"slug": "nppes", "endpoint": "search", "bindings": {"state": "CA"}}],
        "registry_mode": "api_plus_web",
    }, "test-tenant")
    assert mode == "api_plus_web"
    assert [s.name for s in srcs] == ["api:nppes"]


@pytest.mark.asyncio
async def test_rebuild_registry_sources_empty_when_absent():
    srcs, _ = await _rebuild_registry_sources({}, "test-tenant")
    assert srcs == []


def test_record_requests_accumulates_caps_and_skips_malformed():
    from cograph_client.agent.capabilities.web_ingest_cap import (
        _MAX_REQUEST_TRACES_PER_PROVIDER,
        _record_requests,
    )
    from cograph_client.enrichment.models import ProviderLog

    plog = ProviderLog(provider="api:nppes")
    _record_requests(plog, [
        {"url": "https://x/api/?skip=0", "params": {"skip": "0"}, "status": 200, "records": 2, "error": None},
        {"url": "https://x/api/?skip=2", "params": {"skip": "2"}, "status": 500, "records": 0, "error": "HTTP 500"},
    ])
    assert len(plog.requests) == 2
    assert plog.requests[0].status == 200 and plog.requests[0].records == 2
    assert plog.requests[1].error == "HTTP 500"

    # None/empty is a no-op — web-search providers pass no trace.
    _record_requests(plog, None)
    _record_requests(plog, [])
    assert len(plog.requests) == 2

    # Malformed entries are skipped; valid ones still land.
    _record_requests(plog, ["not-a-dict", {"url": "https://x/api/?skip=4", "records": 1}])
    assert len(plog.requests) == 3
    assert plog.requests[2].url.endswith("skip=4")

    # Cap: accumulation never exceeds the per-provider limit.
    _record_requests(plog, [{"url": f"https://x/?p={i}"} for i in range(_MAX_REQUEST_TRACES_PER_PROVIDER + 50)])
    assert len(plog.requests) == _MAX_REQUEST_TRACES_PER_PROVIDER


def test_old_job_without_requests_field_deserializes():
    # Jobs whose provider_logs predate the `requests` field (and jobs with no
    # provider_logs at all) must load unchanged, with requests defaulting to [].
    from cograph_client.enrichment.models import EnrichJob

    old = {
        "id": "j0", "tenant_id": "t", "kg_name": "docs", "type_name": "Physician",
        "attributes": ["name"], "tier": "auto", "status": "applied",
        "created_at": "2026-01-01T00:00:00Z", "conflict_policy": "skip",
        "provider_logs": [{"provider": "api:nppes", "matches": 5}],  # no "requests" key
    }
    job = EnrichJob.model_validate(old)
    assert job.provider_logs[0].requests == []
    job2 = EnrichJob.model_validate(dict(old, provider_logs=[]))
    assert job2.provider_logs == []


def test_provider_log_request_trace_survives_json_round_trip():
    # The persisted job serializes ProviderLog.requests to JSON and back
    # unchanged (the PostgresJobStore path), so the trace reaches the UI.
    from cograph_client.enrichment.models import ApiRequestTrace, EnrichJob, ProviderLog

    job = EnrichJob.model_validate({
        "id": "j1", "tenant_id": "t", "kg_name": "docs", "type_name": "Physician",
        "attributes": ["name"], "tier": "auto", "status": "applied",
        "created_at": "2026-07-06T00:00:00Z", "conflict_policy": "skip",
        "provider_logs": [ProviderLog(provider="api:nppes", matches=2, requests=[
            ApiRequestTrace(url="https://npiregistry.cms.hhs.gov/api/?skip=0",
                            params={"skip": "0", "limit": "200"}, status=200, records=2),
        ])],
    })
    back = EnrichJob.model_validate_json(job.model_dump_json())
    req = back.provider_logs[0].requests[0]
    assert req.status == 200 and req.records == 2
    assert req.params["limit"] == "200"


# --------------------------------------------------------------------------- #
# plan()
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_plan_consults_registry_and_prepends_match(monkeypatch):
    # Registry routing runs on every query-mode discovery (no feature flag).
    register_web_source(FakeWeb())

    async def fake_route(query, catalog, **kw):
        assert "cardiologist" in query.lower() or "cardiolog" in query.lower()
        return _canned_decision()

    monkeypatch.setattr(web_ingest_cap, "route_query", fake_route)

    step = (await WebIngestCapability().plan(_ctx(), "add all cardiologists in San Francisco", parsed=CONFIRMED_SPEC))[0]
    assert step.action == "discover_ingest"
    # Picks persisted for execute() to rerun without a second LLM call.
    assert step.params["registry_picks"] == [{
        "slug": "nppes", "endpoint": "search",
        "bindings": {"taxonomy_description": "cardiology", "city": "San Francisco", "state": "CA"},
    }]
    assert step.params["registry_mode"] == MODE_API_ONLY
    # Registry provider is in the persisted ensemble, ahead of web.
    assert step.params["providers"][0] == "api:nppes"
    # Plan card names the API + its source-of-truth status.
    assert "NPPES" in step.rationale and "source of truth" in step.rationale
    assert "NPPES" in step.preview["summary"]


@pytest.mark.asyncio
async def test_plan_web_only_decision_leaves_step_unchanged(monkeypatch):
    # The router IS consulted on every query, but a web_only decision (no entry
    # covers the ask, or no LLM key) leaves the plan step identical to today:
    # no registry_picks, the web ensemble untouched, no registry card.
    register_web_source(FakeWeb())

    async def web_only_route(query, catalog, **kw):
        return RoutingDecision()  # web_only

    monkeypatch.setattr(web_ingest_cap, "route_query", web_only_route)

    step = (await WebIngestCapability().plan(_ctx(), "add all cardiologists in San Francisco", parsed=CONFIRMED_SPEC))[0]
    assert "registry_picks" not in step.params
    assert step.params["providers"] == ["fake"]
    assert "NPPES" not in step.rationale


# --------------------------------------------------------------------------- #
# execute() — the E2E: NPPES runs, rows land with api:nppes provenance
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_execute_runs_registry_and_ingests_with_api_provenance(monkeypatch):
    register_web_source(FakeWeb())

    canned = ApiCallResult(
        slug="nppes", source="api:nppes",
        rows=[{"npi": "1234567893", "last_name": "GARCIA", "primary_taxonomy": "Cardiovascular Disease"}],
        provenance={"0": "https://npiregistry.cms.hhs.gov/api/?version=2.1&skip=0"},
        sources=["https://npiregistry.cms.hhs.gov/api/?version=2.1&skip=0"],
    )

    async def fake_execute(self, spec, bindings=None, *, endpoint_name=None, max_rows=50, sample=False, budget=None, secret_resolver=None):
        assert spec.slug == "nppes"
        assert bindings.get("taxonomy_description") == "cardiology"
        return canned

    monkeypatch.setattr(RegistryApiSource, "execute", fake_execute)

    captured: dict = {}

    async def fake_ingest(self, content, tenant_id, content_type="text", source="", instance_graph=None, **_kw):
        captured.update(content=content, source=source, content_type=content_type)
        rows = json.loads(content)
        return IngestResult(entities_extracted=len(rows), entities_resolved=len(rows))

    monkeypatch.setattr(SchemaResolver, "ingest", fake_ingest)

    spawned: dict = {}
    monkeypatch.setattr(web_ingest_cap, "_spawn",
                        lambda coro: spawned.__setitem__("task", asyncio.ensure_future(coro)))

    step = PlanStep(
        capability="web_ingest", action="discover_ingest",
        params={
            "query": "cardiologists in San Francisco",
            "subqueries": [],
            "proposed_type": "Physician",
            "attributes": ["name"],
            "hint_columns": ["name", "npi", "primary_taxonomy"],
            "max_rows": 50,
            "kg_name": "docs",
            "provider": "fake",
            "providers": ["fake"],
            "urls": [],
            "registry_picks": [{"slug": "nppes", "endpoint": "search",
                                "bindings": {"taxonomy_description": "cardiology", "city": "San Francisco", "state": "CA"}}],
            "registry_mode": MODE_API_ONLY,  # registry alone; web must NOT run
        },
    )

    ack = await WebIngestCapability().execute(_ctx(), step)
    assert ack["kind"] == "ack"
    await spawned["task"]

    # The NPPES rows were ingested, tagged with the api:nppes run-level source.
    assert captured["content_type"] == "json"
    assert captured["source"] == "web:api:nppes:cardiologists in San Francisco"
    rows_back = json.loads(captured["content"])
    assert rows_back[0]["last_name"] == "GARCIA"
