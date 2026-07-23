"""ONTA-389: ask/agent answer runs mint P7/A7 + P0/A9 for operator Job Trace."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cograph_client.auth.api_keys import TenantContext
from cograph_client.enrichment.job_store import InMemoryJobStore
from cograph_client.enrichment.models import JobCategory, JobStatus
from cograph_client.models.query import NLResult
from cograph_client.pipeline.answer_run import (
    answer_run_lookup_path,
    record_answer_run,
)
from cograph_client.pipeline.stage_trace import (
    StageProjectId,
    StageStatus,
    resolve_trace,
)


@pytest.mark.asyncio
async def test_record_answer_run_mints_job_with_p0_and_p7():
    store = InMemoryJobStore()
    run_id = await record_answer_run(
        job_store=store,
        tenant_id="demo-tenant",
        kg_name="universities",
        question="How many universities are in BC?",
        answer="There are 25.",
        sparql="SELECT (COUNT(?s) AS ?c) WHERE { ?s a :University }",
        citations=[{"subject": "u1"}, {"subject": "u2"}],
        coverage_caveat="answered from 2 of 2 sources",
        ok=True,
        thread_id="sess-1",
        medium="explorer",
        timing={"rows": 1},
        source="agent",
    )
    assert run_id
    assert answer_run_lookup_path(run_id) == f"/operator/jobs/{run_id}/trace"

    job = await store.get(run_id)
    assert job is not None
    assert job.category == JobCategory.answer
    assert job.status == JobStatus.applied
    assert job.thread_id == "sess-1"
    assert job.manifest is not None
    assert job.manifest.run_id == run_id
    assert job.manifest.state.value == "completed"
    assert job.stage_trace is not None
    assert job.stage_trace.source == "live"

    by = {p.project_id: p for p in job.stage_trace.projects}
    assert by[StageProjectId.p0].status == StageStatus.completed
    assert by[StageProjectId.p0].output.get("status") == "applied"
    assert by[StageProjectId.p7].status == StageStatus.completed
    assert by[StageProjectId.p7].output.get("citation_count") == 2
    assert "25" in (by[StageProjectId.p7].output.get("answer") or "")
    assert by[StageProjectId.p7].output.get("coverage_caveat")
    # Write rails skipped — answer is read-only.
    for pid in (
        StageProjectId.p1,
        StageProjectId.p2,
        StageProjectId.p3,
        StageProjectId.p4,
        StageProjectId.p5,
        StageProjectId.p6,
        StageProjectId.p8,
        StageProjectId.p9,
    ):
        assert by[pid].status == StageStatus.skipped, pid

    # resolve_trace still surfaces P0/P7 for the operator page.
    trace = resolve_trace(job)
    tby = {p.project_id: p for p in trace.projects}
    assert tby[StageProjectId.p0].status == StageStatus.completed
    assert tby[StageProjectId.p7].status == StageStatus.completed


@pytest.mark.asyncio
async def test_record_answer_run_failed_marks_p0_p7_failed():
    store = InMemoryJobStore()
    run_id = await record_answer_run(
        job_store=store,
        tenant_id="t",
        kg_name="",
        question="boom?",
        answer="Could not answer",
        ok=False,
        error="provider exploded",
        source="ask",
    )
    job = await store.get(run_id)
    assert job.status == JobStatus.failed
    assert job.error and "provider" in job.error
    by = {p.project_id: p for p in job.stage_trace.projects}
    assert by[StageProjectId.p0].status == StageStatus.failed
    assert by[StageProjectId.p7].status == StageStatus.failed
    assert job.manifest.state.value == "failed"


@pytest.mark.asyncio
async def test_record_answer_run_noop_without_store_or_question():
    assert await record_answer_run(
        job_store=None,
        tenant_id="t",
        question="anything",
        answer="x",
    ) is None
    store = InMemoryJobStore()
    assert await record_answer_run(
        job_store=store,
        tenant_id="t",
        question="   ",
        answer="x",
    ) is None
    assert await store.list_for_tenant("t") == []


@pytest.mark.asyncio
async def test_query_capability_returns_run_id_and_persists_job():
    from cograph_client.agent.capabilities.query import QueryCapability
    from cograph_client.agent.registry import AgentContext

    store = InMemoryJobStore()
    ctx = AgentContext(
        tenant_id="demo-tenant",
        kg_name="kg1",
        neptune=MagicMock(),
        session_id="thread-abc",
        medium="cli",
        extras={"enrichment_job_store": store},
    )
    cap = QueryCapability()

    fake_result = NLResult(
        answer="42",
        sparql="SELECT ?x WHERE {}",
        explanation="e",
        coverage_caveat="ok",
        timing={"rows": 1},
    )

    async def _fake_ask(self, question, ontology_graph, instance_graph, **kw):
        return fake_result

    from cograph_client.nlp.pipeline import NLQueryPipeline

    original = NLQueryPipeline.ask
    NLQueryPipeline.ask = _fake_ask  # type: ignore[method-assign]
    try:
        out = await cap.answer(ctx, "how many?")
    finally:
        NLQueryPipeline.ask = original  # type: ignore[method-assign]

    assert out["answer"] == "42"
    assert out["run_id"]
    assert out["job_id"] == out["run_id"]
    job = await store.get(out["run_id"])
    assert job is not None
    assert job.category == JobCategory.answer
    assert job.thread_id == "thread-abc"
    p7 = next(p for p in job.stage_trace.projects if p.project_id == StageProjectId.p7)
    assert p7.status == StageStatus.completed


@pytest.mark.asyncio
async def test_query_capability_no_store_still_answers():
    from cograph_client.agent.capabilities.query import QueryCapability
    from cograph_client.agent.registry import AgentContext
    from cograph_client.nlp.pipeline import NLQueryPipeline

    ctx = AgentContext(
        tenant_id="demo-tenant",
        kg_name="kg1",
        neptune=MagicMock(),
        extras={},  # no job store
    )
    fake_result = NLResult(answer="n", sparql="", explanation="")

    async def _fake_ask(self, *a, **k):
        return fake_result

    original = NLQueryPipeline.ask
    NLQueryPipeline.ask = _fake_ask  # type: ignore[method-assign]
    try:
        out = await QueryCapability().answer(ctx, "q?")
    finally:
        NLQueryPipeline.ask = original  # type: ignore[method-assign]
    assert out["answer"] == "n"
    assert "run_id" not in out


@pytest.mark.asyncio
async def test_operator_trace_for_answer_run():
    """Documented path: run_id from answer → GET /operator/jobs/{id}/trace."""
    from cograph_client.api.routes import operator as operator_routes
    from cograph_client.api.deps import get_enrichment_job_store
    from cograph_client.auth import api_keys

    store = InMemoryJobStore()
    run_id = await record_answer_run(
        job_store=store,
        tenant_id="demo-tenant",
        kg_name="kg",
        question="how many?",
        answer="3",
        citations=[{"s": 1}],
        coverage_caveat="full",
        ok=True,
        source="ask",
    )
    assert run_id

    app = FastAPI()
    app.include_router(operator_routes.router)
    app.dependency_overrides[get_enrichment_job_store] = lambda: store
    app.dependency_overrides[api_keys.get_tenant] = (
        lambda tenant=None, api_key=None, request=None: TenantContext(
            tenant_id="demo-tenant", api_key="k", is_operator=True
        )
    )
    client = TestClient(app)
    r = client.get(answer_run_lookup_path(run_id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] == run_id
    assert body["category"] == "answer"
    assert body["status"] == "applied"
    assert len(body["projects"]) == 10
    by = {p["project_id"]: p for p in body["projects"]}
    assert by["P0"]["status"] == "completed"
    assert by["P7"]["status"] == "completed"
    assert by["P7"]["output"]["citation_count"] == 1
    assert by["P6"]["status"] == "skipped"


def test_ask_route_attaches_run_id(client, auth_headers):
    """POST /ask returns run_id; job is lookup-able for Job Trace."""
    from cograph_client.api.deps import get_enrichment_job_store
    from unittest.mock import patch

    store = InMemoryJobStore()
    # Override the app's job store so we can inspect it after the request.
    client.app.dependency_overrides[get_enrichment_job_store] = lambda: store
    try:
        ok = NLResult(answer="42", sparql="SELECT ...", explanation="e")
        with patch(
            "cograph_client.api.routes.ask.NLQueryPipeline.ask",
            new_callable=AsyncMock,
            return_value=ok,
        ):
            res = client.post(
                "/graphs/test-tenant/ask",
                json={"question": "what is the answer", "kg_name": "kg1"},
                headers=auth_headers,
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["answer"] == "42"
        assert body["run_id"], "NLResult.run_id must be set for Job Trace"
        # InMemoryJobStore stores jobs under the same dict the request wrote to.
        assert body["run_id"] in store._jobs
        job = store._jobs[body["run_id"]]
        assert job.category == JobCategory.answer
        assert job.kg_name == "kg1"
    finally:
        client.app.dependency_overrides.pop(get_enrichment_job_store, None)


def test_reconstruct_answer_category_surfaces_p7():
    from cograph_client.enrichment.models import (
        ConflictPolicy,
        EnrichJob,
        EnrichmentTier,
        JobCategory,
        JobStatus,
    )
    from cograph_client.pipeline.stage_trace import reconstruct_from_job

    job = EnrichJob(
        id="ans-1",
        tenant_id="t",
        kg_name="kg",
        type_name="",
        attributes=[],
        tier=EnrichmentTier.lite,
        status=JobStatus.applied,
        created_at=datetime.now(timezone.utc),
        conflict_policy=ConflictPolicy.stage,
        category=JobCategory.answer,
        instructions="How many?",
        result_count=1,
    )
    # No live stage_trace — reconstructor only.
    trace = reconstruct_from_job(job)
    by = {p.project_id: p for p in trace.projects}
    assert by[StageProjectId.p7].status in (
        StageStatus.reconstructed,
        StageStatus.completed,
    )
    assert by[StageProjectId.p7].input.get("question") == "How many?"
    assert by[StageProjectId.p6].status == StageStatus.skipped
