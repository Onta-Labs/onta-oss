"""Operator Job Stage Trace (P0–P9 contract-level I/O)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cograph_client.auth.api_keys import AuthVerdict, TenantContext, register_external_verifier
from cograph_client.enrichment.job_store import InMemoryJobStore
from cograph_client.enrichment.models import (
    EnrichJob,
    EnrichmentTier,
    JobCategory,
    JobProgress,
    JobStatus,
    JobTrigger,
    ConflictPolicy,
    ProviderLog,
)
from cograph_client.pipeline.stage_trace import (
    StageProjectId,
    StageStatus,
    StageTraceRecorder,
    attach_recorder,
    ensure_all_projects,
    new_trace_for_job,
    reconstruct_from_job,
    resolve_trace,
)


def _job(**kw) -> EnrichJob:
    base = dict(
        id="job-1",
        tenant_id="demo-tenant",
        kg_name="universities",
        type_name="University",
        attributes=["name", "website"],
        tier=EnrichmentTier.lite,
        status=JobStatus.applied,
        created_at=datetime.now(timezone.utc),
        conflict_policy=ConflictPolicy.stage,
        category=JobCategory.discovery,
        progress=JobProgress(total=10, processed=10, filled=8),
        result_count=8,
        platforms=["source_first", "wikipedia.org"],
        provider_logs=[
            ProviderLog(provider="source_first", status="ok", attempts=1, matches=8),
        ],
        trigger=JobTrigger.manual,
    )
    base.update(kw)
    return EnrichJob(**base)


def test_ensure_all_projects_fills_p0_to_p9():
    projects = ensure_all_projects([])
    assert [p.project_id for p in projects] == list(StageProjectId)
    assert all(p.status == StageStatus.skipped for p in projects)


def test_recorder_begin_action_end():
    job = _job()
    rec = attach_recorder(job)
    assert rec is not None
    rec.begin(StageProjectId.p1, input={"goal": "BC universities"})
    rec.action(StageProjectId.p1, "search", detail="fan-out")
    rec.end(StageProjectId.p1, output={"sources": 25})
    p1 = next(p for p in job.stage_trace.projects if p.project_id == StageProjectId.p1)
    assert p1.status == StageStatus.completed
    assert p1.input["goal"] == "BC universities"
    assert p1.output["sources"] == 25
    assert len(p1.actions) == 1
    assert p1.duration_ms is not None


def test_reconstruct_discovery_job_surfaces_p0_p1_p6():
    job = _job()
    trace = reconstruct_from_job(job)
    assert trace.source == "reconstructed"
    assert len(trace.projects) == 10
    by = {p.project_id: p for p in trace.projects}
    assert by[StageProjectId.p0].status in (
        StageStatus.completed,
        StageStatus.reconstructed,
    )
    assert by[StageProjectId.p1].status == StageStatus.reconstructed
    assert by[StageProjectId.p1].output.get("result_count") == 8
    assert by[StageProjectId.p6].status == StageStatus.reconstructed
    assert by[StageProjectId.p7].status == StageStatus.skipped


def test_resolve_trace_prefers_live_then_fills():
    job = _job()
    rec = attach_recorder(job)
    rec.begin(StageProjectId.p1, input={"goal": "live"})
    rec.end(StageProjectId.p1, output={"n": 1})
    trace = resolve_trace(job)
    assert trace.source in ("live", "mixed")
    p1 = next(p for p in trace.projects if p.project_id == StageProjectId.p1)
    assert p1.input.get("goal") == "live"
    assert p1.reconstructed is False


def test_operator_route_403_for_non_operator():
    from cograph_client.api.routes import operator as operator_routes
    from cograph_client.api.deps import get_enrichment_job_store

    store = InMemoryJobStore()
    app = FastAPI()
    app.include_router(operator_routes.router)
    app.dependency_overrides[get_enrichment_job_store] = lambda: store

    # Force non-operator via a fake get_tenant
    from cograph_client.auth import api_keys

    def _non_op(tenant=None, api_key=None, request=None):
        return TenantContext(tenant_id="t", api_key="k", is_operator=False)

    app.dependency_overrides[api_keys.get_tenant] = _non_op
    # Also override the Depends used in require_operator — it uses get_tenant
    # from the same module; FastAPI resolves by callable identity.
    client = TestClient(app)
    r = client.get("/operator/jobs/job-1/trace")
    assert r.status_code == 403
    assert r.json()["detail"] == "operator only"


@pytest.mark.asyncio
async def test_operator_route_returns_trace_for_operator():
    from cograph_client.api.routes import operator as operator_routes
    from cograph_client.api.deps import get_enrichment_job_store
    from cograph_client.auth import api_keys

    store = InMemoryJobStore()
    job = _job(id="job-xyz")
    await store.create(job)

    app = FastAPI()
    app.include_router(operator_routes.router)
    app.dependency_overrides[get_enrichment_job_store] = lambda: store
    app.dependency_overrides[api_keys.get_tenant] = lambda tenant=None, api_key=None, request=None: TenantContext(
        tenant_id="demo-tenant", api_key="k", is_operator=True
    )
    client = TestClient(app)
    r = client.get("/operator/jobs/job-xyz/trace")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] == "job-xyz"
    assert body["tenant_id"] == "demo-tenant"
    assert len(body["projects"]) == 10
    assert body["projects"][0]["project_id"] == "P0"
    assert body["source"] in ("reconstructed", "live", "mixed")


def test_operator_route_404_unknown_job():
    from cograph_client.api.routes import operator as operator_routes
    from cograph_client.api.deps import get_enrichment_job_store
    from cograph_client.auth import api_keys

    store = InMemoryJobStore()
    app = FastAPI()
    app.include_router(operator_routes.router)
    app.dependency_overrides[get_enrichment_job_store] = lambda: store
    app.dependency_overrides[api_keys.get_tenant] = lambda tenant=None, api_key=None, request=None: TenantContext(
        tenant_id="demo-tenant", api_key="k", is_operator=True
    )
    client = TestClient(app)
    r = client.get("/operator/jobs/missing/trace")
    assert r.status_code == 404
