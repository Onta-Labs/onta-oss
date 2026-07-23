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
    ensure_job_stage_trace_open,
    finalize_job_stage_trace,
    new_trace_for_job,
    open_job_stage_trace,
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


def test_resolve_trace_heals_stale_running_on_failed_job():
    """If live left P2 running but the job is failed, don't show a frozen spinner."""
    job = _job(status=JobStatus.failed, error="boom")
    rec = attach_recorder(job)
    rec.begin(StageProjectId.p2, input={"x": 1})
    # Deliberately do NOT end p2 — simulates incomplete failure instrumentation.
    assert next(
        p for p in job.stage_trace.projects if p.project_id == StageProjectId.p2
    ).status == StageStatus.running
    trace = resolve_trace(job)
    p2 = next(p for p in trace.projects if p.project_id == StageProjectId.p2)
    # Must not remain live-running once the job is terminal.
    assert p2.status != StageStatus.running
    assert p2.status in (
        StageStatus.failed,
        StageStatus.reconstructed,
        StageStatus.completed,
        StageStatus.skipped,
    )


def test_attach_recorder_none():
    assert attach_recorder(None) is None


# --------------------------------------------------------------------------- #
# ONTA-388 — P0 open + finalize on every job category
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "category",
    [
        JobCategory.enrichment,
        JobCategory.dedupe,
        JobCategory.reconciliation,
        JobCategory.discovery,
    ],
)
def test_open_job_stage_trace_starts_p0_running(category):
    """P0 begins on create for every job category (ONTA-388)."""
    job = _job(category=category, status=JobStatus.queued, stage_trace=None)
    rec = open_job_stage_trace(job)
    assert rec is not None
    assert job.stage_trace is not None
    p0 = next(p for p in job.stage_trace.projects if p.project_id == StageProjectId.p0)
    assert p0.status == StageStatus.running
    assert p0.input.get("category") in (category.value, str(category))
    assert any(a.name == "create_job" for a in p0.actions)
    # Other projects stay skipped until a rail touches them.
    for p in job.stage_trace.projects:
        if p.project_id != StageProjectId.p0:
            assert p.status == StageStatus.skipped


@pytest.mark.parametrize(
    "category,terminal,expect_fail",
    [
        (JobCategory.enrichment, "applied", False),
        (JobCategory.enrichment, "failed", True),
        (JobCategory.enrichment, "review", False),
        (JobCategory.dedupe, "applied", False),
        (JobCategory.dedupe, "failed", True),
        (JobCategory.reconciliation, "review", False),
        (JobCategory.reconciliation, "failed", True),
        (JobCategory.discovery, "applied", False),
        (JobCategory.discovery, "failed", True),
        (JobCategory.enrichment, "cancelled", False),
    ],
)
def test_finalize_never_leaves_running_projects(category, terminal, expect_fail):
    """Terminal jobs must never have running stage projects (ONTA-388).

    Simulates mid-run instrumentation that left P2/P6 open, then finalize.
    """
    job = _job(
        category=category,
        status=JobStatus.running,
        stage_trace=None,
        error="boom" if expect_fail else None,
    )
    rec = open_job_stage_trace(job)
    assert rec is not None
    # Simulate mid-run stages that would freeze as spinners without finalize.
    rec.begin(StageProjectId.p2, input={"phase": "extract"})
    rec.begin(StageProjectId.p6, input={"phase": "write"})
    assert next(
        p for p in job.stage_trace.projects if p.project_id == StageProjectId.p2
    ).status == StageStatus.running
    assert next(
        p for p in job.stage_trace.projects if p.project_id == StageProjectId.p6
    ).status == StageStatus.running

    job.status = JobStatus(terminal)
    if expect_fail:
        job.error = job.error or "simulated failure"

    finalize_job_stage_trace(
        job,
        terminal_status=terminal,
        error=job.error if expect_fail else None,
        summary={"category": category.value},
    )

    assert job.stage_trace is not None
    assert job.stage_trace.status == terminal
    for p in job.stage_trace.projects:
        assert p.status != StageStatus.running, (
            f"{p.project_id} still running on terminal {terminal} ({category.value})"
        )
        assert p.status != StageStatus.pending, (
            f"{p.project_id} still pending on terminal {terminal} ({category.value})"
        )

    p0 = next(p for p in job.stage_trace.projects if p.project_id == StageProjectId.p0)
    if expect_fail:
        assert p0.status == StageStatus.failed
        assert p0.error
    else:
        assert p0.status == StageStatus.completed

    # resolve_trace must also never surface running on a terminal job.
    job.status = JobStatus(terminal)
    resolved = resolve_trace(job)
    for p in resolved.projects:
        assert p.status != StageStatus.running


def test_finalize_is_idempotent_and_exception_safe():
    job = _job(category=JobCategory.dedupe, status=JobStatus.failed, error="x")
    open_job_stage_trace(job)
    finalize_job_stage_trace(job, terminal_status="failed", error="x")
    finalize_job_stage_trace(job, terminal_status="failed", error="x")  # no raise
    p0 = next(p for p in job.stage_trace.projects if p.project_id == StageProjectId.p0)
    assert p0.status == StageStatus.failed


def test_ensure_job_stage_trace_open_is_noop_when_present():
    job = _job(category=JobCategory.enrichment, status=JobStatus.queued)
    open_job_stage_trace(job)
    p0_before = next(
        p for p in job.stage_trace.projects if p.project_id == StageProjectId.p0
    )
    n_actions = len(p0_before.actions)
    rec = ensure_job_stage_trace_open(job)
    assert rec is not None
    p0_after = next(
        p for p in job.stage_trace.projects if p.project_id == StageProjectId.p0
    )
    # Does not re-fire create_job.
    assert len(p0_after.actions) == n_actions


def test_ensure_job_stage_trace_open_creates_when_missing():
    job = _job(category=JobCategory.dedupe, status=JobStatus.running, stage_trace=None)
    assert job.stage_trace is None
    rec = ensure_job_stage_trace_open(job)
    assert rec is not None
    assert job.stage_trace is not None
    p0 = next(p for p in job.stage_trace.projects if p.project_id == StageProjectId.p0)
    assert p0.status == StageStatus.running


def test_open_and_finalize_none_job_safe():
    assert open_job_stage_trace(None) is None
    finalize_job_stage_trace(None, terminal_status="failed")  # no raise
    assert ensure_job_stage_trace_open(None) is None


def test_actions_new_job_opens_p0():
    """actions._new_job (dedupe/enrich/recon create) opens live P0."""
    from cograph_client.api.routes.actions import _new_job

    job = _new_job(
        tenant_id="demo-tenant",
        kg_name="kg",
        category=JobCategory.dedupe,
    )
    assert job.stage_trace is not None
    p0 = next(p for p in job.stage_trace.projects if p.project_id == StageProjectId.p0)
    assert p0.status == StageStatus.running
    assert p0.input.get("category") == "dedupe"


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
