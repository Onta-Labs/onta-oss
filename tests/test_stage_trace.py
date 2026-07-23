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
    stamp_enrichment_job_created,
    stamp_enrichment_run_failed,
    stamp_enrichment_run_finished,
    stamp_enrichment_run_started,
    stamp_enrichment_write_phase,
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
# Enrichment live P0 + P2/P4/P6 (ONTA-387)
# --------------------------------------------------------------------------- #


def _enrich_job(**kw) -> EnrichJob:
    base = dict(
        id="enrich-job-1",
        tenant_id="demo-tenant",
        kg_name="products",
        type_name="Product",
        attributes=["manufacturer", "website"],
        tier=EnrichmentTier.lite,
        status=JobStatus.queued,
        created_at=datetime.now(timezone.utc),
        conflict_policy=ConflictPolicy.stage,
        confidence_min=0.85,
        category=JobCategory.enrichment,
        progress=JobProgress(total=0, processed=0, filled=0),
        trigger=JobTrigger.manual,
    )
    base.update(kw)
    return EnrichJob(**base)


def test_stamp_enrichment_job_created_opens_live_p0():
    """New enrich jobs get live stage_trace (not only reconstructed) at create."""
    job = _enrich_job()
    assert job.stage_trace is None
    stamp_enrichment_job_created(job)
    assert job.stage_trace is not None
    assert job.stage_trace.source == "live"
    by = {p.project_id: p for p in job.stage_trace.projects}
    assert by[StageProjectId.p0].status == StageStatus.running
    assert by[StageProjectId.p0].input.get("category") == "enrichment"
    assert by[StageProjectId.p0].input.get("type_name") == "Product"
    assert any(a.name == "create_job" for a in by[StageProjectId.p0].actions)
    assert by[StageProjectId.p0].reconstructed is False
    # Other projects remain skipped/pending until run starts.
    assert by[StageProjectId.p2].status in (StageStatus.skipped, StageStatus.pending)


def test_enrichment_live_lifecycle_p0_p2_p4_p6_and_skips():
    """Full enrichment run stamps live P0/P2/P4/P6; skips rest with reasons."""
    job = _enrich_job()
    stamp_enrichment_job_created(job)
    stamp_enrichment_run_started(job)

    by = {p.project_id: p for p in job.stage_trace.projects}
    assert by[StageProjectId.p0].status == StageStatus.running
    assert by[StageProjectId.p2].status == StageStatus.running
    assert by[StageProjectId.p2].input.get("type_name") == "Product"
    assert any(a.name == "lookup" for a in by[StageProjectId.p2].actions)
    # P4 always opens on enrichment (conflict_policy + confidence_min apply).
    assert by[StageProjectId.p4].status == StageStatus.running
    assert by[StageProjectId.p4].input.get("conflict_policy") == "stage"
    assert by[StageProjectId.p4].input.get("confidence_min") == 0.85
    assert by[StageProjectId.p6].status == StageStatus.running

    job.progress = JobProgress(
        total=4, processed=4, filled=2, verified=1, conflicts=1, no_match=0
    )
    job.status = JobStatus.review
    stamp_enrichment_write_phase(
        job, write_policy="skip", has_conflicts=True, applied=True
    )
    stamp_enrichment_run_finished(job)

    by = {p.project_id: p for p in job.stage_trace.projects}
    assert by[StageProjectId.p0].status == StageStatus.completed
    assert by[StageProjectId.p0].output.get("status") == "review"
    assert by[StageProjectId.p2].status == StageStatus.completed
    assert by[StageProjectId.p2].output.get("progress", {}).get("filled") == 2
    assert by[StageProjectId.p4].status == StageStatus.completed
    assert by[StageProjectId.p4].output.get("conflicts") == 1
    assert by[StageProjectId.p6].status == StageStatus.completed
    assert by[StageProjectId.p6].output.get("status") == "review"
    # Skip reasons for rails not on the enrichment path.
    for pid in (
        StageProjectId.p1,
        StageProjectId.p3,
        StageProjectId.p5,
        StageProjectId.p7,
        StageProjectId.p8,
        StageProjectId.p9,
    ):
        assert by[pid].status == StageStatus.skipped
        assert by[pid].output.get("skip_reason")

    # resolve_trace prefers live over reconstructed.
    resolved = resolve_trace(job)
    assert resolved.source in ("live", "mixed")
    p2 = next(p for p in resolved.projects if p.project_id == StageProjectId.p2)
    assert p2.reconstructed is False
    assert p2.status == StageStatus.completed


def test_enrichment_run_failed_closes_open_projects():
    job = _enrich_job(status=JobStatus.running)
    stamp_enrichment_job_created(job)
    stamp_enrichment_run_started(job)
    stamp_enrichment_run_failed(job, "adapter boom")
    by = {p.project_id: p for p in job.stage_trace.projects}
    assert by[StageProjectId.p0].status == StageStatus.failed
    assert by[StageProjectId.p0].error and "boom" in by[StageProjectId.p0].error
    # Mid-run projects must not stay running on a failed job.
    for pid in (StageProjectId.p2, StageProjectId.p4, StageProjectId.p6):
        assert by[pid].status != StageStatus.running
    assert job.stage_trace.status == "failed"


@pytest.mark.asyncio
async def test_enrichment_executor_persists_live_stage_trace():
    """End-to-end: executor.run leaves a live P0/P2/P4/P6 stage_trace on the job."""
    from unittest.mock import AsyncMock

    from cograph_client.enrichment.cache import EnrichmentCache
    from cograph_client.enrichment.executor import EnrichmentExecutor
    from cograph_client.enrichment.models import Verdict

    class _FakeWikidata:
        name = "wikidata"

        def __init__(self, mapping):
            self._mapping = mapping

        async def lookup(self, entity_label, attribute, context):
            return list(self._mapping.get((entity_label, attribute), []))

    sparql = {
        "head": {"vars": ["e", "label", "nameAttr", "vals"]},
        "results": {
            "bindings": [
                {
                    "e": {
                        "type": "uri",
                        "value": "https://cograph.tech/entities/Product/p1",
                    },
                    "label": {"type": "literal", "value": "Widget"},
                    "vals": {"type": "literal", "value": ""},
                }
            ]
        },
    }
    neptune = AsyncMock()
    neptune.query = AsyncMock(return_value=sparql)
    neptune.update = AsyncMock(return_value=None)

    store = InMemoryJobStore()
    wikidata = _FakeWikidata(
        {
            ("Widget", "manufacturer"): [
                Verdict(
                    value="Acme",
                    confidence=0.95,
                    source="wikidata",
                    source_url="https://example.com",
                )
            ]
        }
    )
    executor = EnrichmentExecutor(neptune, store, EnrichmentCache(), wikidata)

    job = _enrich_job(attributes=["manufacturer"])
    job.conflict_policy = ConflictPolicy.skip
    stamp_enrichment_job_created(job)
    await store.create(job)
    await executor.run(job, "demo-tenant")

    final = await store.get(job.id)
    assert final is not None
    assert final.status == JobStatus.applied, (
        f"expected applied, got {final.status}: {final.error}"
    )
    assert final.stage_trace is not None
    assert final.stage_trace.source == "live"
    by = {p.project_id: p for p in final.stage_trace.projects}
    assert by[StageProjectId.p0].status == StageStatus.completed
    assert by[StageProjectId.p0].reconstructed is False
    assert by[StageProjectId.p2].status == StageStatus.completed
    assert by[StageProjectId.p2].reconstructed is False
    assert by[StageProjectId.p4].status == StageStatus.completed
    assert by[StageProjectId.p4].input.get("conflict_policy") == "skip"
    assert by[StageProjectId.p6].status == StageStatus.completed
    assert by[StageProjectId.p1].status == StageStatus.skipped
    assert by[StageProjectId.p1].output.get("skip_reason")
    # Operator resolve prefers live.
    resolved = resolve_trace(final)
    assert resolved.source in ("live", "mixed")
    live_p2 = next(p for p in resolved.projects if p.project_id == StageProjectId.p2)
    assert live_p2.reconstructed is False


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
