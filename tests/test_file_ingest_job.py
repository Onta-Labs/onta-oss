"""ONTA-386 — file CSV/JSON ingest as a tracked job with live stage_trace.

File is an A1-like entry: live P0/P2/P5/P6, other projects skipped with reasons.
Writes stay on insert_facts / refresh_after_write (not re-tested here — the
routes still call those; this suite asserts the job + stage_trace contract).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from cograph_client.enrichment.job_store import InMemoryJobStore
from cograph_client.enrichment.models import (
    ConflictPolicy,
    EnrichmentTier,
    JobCategory,
    JobStatus,
)
from cograph_client.pipeline.stage_trace import (
    StageProjectId,
    StageStatus,
    reconstruct_from_job,
    resolve_trace,
)
from cograph_client.resolver.file_ingest_job import (
    fail_file_ingest_job,
    finish_file_ingest_job,
    mark_file_ingest_running,
    note_file_ingest_p2,
    note_file_ingest_p5,
    note_file_ingest_p6,
    open_file_ingest_job,
)
from cograph_client.resolver.models import IngestResult


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _by_id(trace):
    return {p.project_id: p for p in trace.projects}


@pytest.mark.asyncio
async def test_open_file_ingest_job_none_store():
    assert await open_file_ingest_job(None, tenant_id="t", kg_name="k", content_type="csv") is None


@pytest.mark.asyncio
async def test_open_and_finish_live_stage_trace_p0_p2_p5_p6():
    store = InMemoryJobStore()
    job = await open_file_ingest_job(
        store,
        tenant_id="demo-tenant",
        kg_name="books",
        content_type="csv",
        source="bookstore.csv",
        type_name="Book",
        attributes=["title", "author"],
        rows_hint=3,
    )
    assert job is not None
    assert job.category == JobCategory.ingest
    assert job.status == JobStatus.queued
    assert job.stage_trace is not None
    assert job.stage_trace.source == "live"

    await mark_file_ingest_running(job, store, phase="mapping", total=3)
    job = await store.get(job.id)
    assert job.status == JobStatus.running

    note_file_ingest_p2(job, action="apply_mapping", detail="3 rows")
    note_file_ingest_p5(job, action="pre_register_ontology", type_name="Book")
    note_file_ingest_p6(job, action="insert_facts", detail="shared write path")

    result = IngestResult(
        entities_extracted=3,
        entities_resolved=3,
        triples_inserted=12,
        rows_in=3,
        types_created=["Book"],
        attributes_added=["Book.title", "Book.author"],
    )
    await finish_file_ingest_job(job, store, result=result, type_name="Book")

    done = await store.get(job.id)
    assert done.status == JobStatus.applied
    assert done.result_count == 3
    assert done.category == JobCategory.ingest
    assert result.job_id == done.id

    trace = resolve_trace(done)
    assert trace.source in ("live", "mixed")
    by = _by_id(trace)

    assert by[StageProjectId.p0].status == StageStatus.completed
    assert by[StageProjectId.p2].status == StageStatus.completed
    assert by[StageProjectId.p2].output.get("rows_in") == 3
    assert by[StageProjectId.p5].status == StageStatus.completed
    assert by[StageProjectId.p5].output.get("type_name") == "Book"
    assert by[StageProjectId.p6].status == StageStatus.completed
    assert by[StageProjectId.p6].output.get("write_path") == (
        "insert_facts / refresh_after_write"
    )
    assert by[StageProjectId.p6].output.get("triples_inserted") == 12

    # Skipped rails carry reasons (A1-like: no Find Data).
    assert by[StageProjectId.p1].status == StageStatus.skipped
    assert "A1" in (by[StageProjectId.p1].output.get("skip_reason") or "")
    for pid in (
        StageProjectId.p3,
        StageProjectId.p4,
        StageProjectId.p7,
        StageProjectId.p8,
        StageProjectId.p9,
    ):
        assert by[pid].status == StageStatus.skipped, pid
        assert by[pid].output.get("skip_reason"), pid


@pytest.mark.asyncio
async def test_fail_file_ingest_job_marks_failed_trace():
    store = InMemoryJobStore()
    job = await open_file_ingest_job(
        store,
        tenant_id="t",
        kg_name="k",
        content_type="json",
    )
    await mark_file_ingest_running(job, store)
    await fail_file_ingest_job(job, store, "boom")
    done = await store.get(job.id)
    assert done.status == JobStatus.failed
    assert "boom" in (done.error or "")
    by = _by_id(done.stage_trace)
    assert by[StageProjectId.p0].status == StageStatus.failed


@pytest.mark.asyncio
async def test_stage_trace_never_raises_into_write_path():
    """A broken recorder path must not break finish/fail."""
    store = InMemoryJobStore()
    job = await open_file_ingest_job(
        store, tenant_id="t", kg_name="k", content_type="csv"
    )
    # Corrupt stage_trace into something attach_recorder still accepts but
    # begin will choke on — replace projects with a non-list after attach.
    with patch(
        "cograph_client.resolver.file_ingest_job.attach_recorder",
        side_effect=RuntimeError("trace broken"),
    ):
        # These must not raise.
        note_file_ingest_p2(job, action="x")
        note_file_ingest_p5(job, action="y")
        note_file_ingest_p6(job, action="z")
        await finish_file_ingest_job(
            job, store, result=IngestResult(entities_resolved=1)
        )
    done = await store.get(job.id)
    # Job still terminal-applied even if trace writes failed.
    assert done.status == JobStatus.applied


def test_reconstruct_ingest_category_skips_p1_surfaces_p2_p6():
    from cograph_client.enrichment.models import EnrichJob, JobProgress, JobTrigger

    job = EnrichJob(
        id="j-ingest",
        tenant_id="t",
        kg_name="kg",
        type_name="Book",
        attributes=["title"],
        tier=EnrichmentTier.lite,
        status=JobStatus.applied,
        created_at=datetime.now(timezone.utc),
        conflict_policy=ConflictPolicy.skip,
        category=JobCategory.ingest,
        trigger=JobTrigger.manual,
        progress=JobProgress(total=5, processed=5, filled=5),
        result_count=5,
        platforms=["file:csv"],
    )
    trace = reconstruct_from_job(job)
    by = _by_id(trace)
    assert by[StageProjectId.p1].status == StageStatus.skipped
    assert "A1" in (by[StageProjectId.p1].output.get("skip_reason") or "")
    assert by[StageProjectId.p2].status == StageStatus.reconstructed
    assert by[StageProjectId.p6].status == StageStatus.reconstructed
    assert by[StageProjectId.p7].status == StageStatus.skipped


def test_job_category_ingest_in_enum():
    assert JobCategory.ingest.value == "ingest"
    assert "ingest" in {c.value for c in JobCategory}


@pytest.mark.asyncio
async def test_ingest_route_creates_job_visible_on_jobs_list(
    app, mock_neptune, auth_headers
):
    """POST /ingest opens category=ingest job; GET /jobs lists it with live trace."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.resolver.models import IngestResult

    store = InMemoryJobStore()
    app.state.enrichment_job_store = store

    fake_result = IngestResult(
        entities_extracted=2,
        entities_resolved=2,
        triples_inserted=6,
        rows_in=2,
        types_created=["Person"],
        attributes_added=["Person.name"],
    )

    with (
        patch(
            "cograph_client.api.routes.ingest.refresh_after_write",
            new=AsyncMock(),
        ),
        patch("cograph_client.api.routes.ingest.SchemaResolver") as MockResolver,
    ):
        inst = MockResolver.return_value
        inst.ingest = AsyncMock(return_value=fake_result)

        with TestClient(app) as client:
            app.state.neptune_client = mock_neptune
            app.state.enrichment_job_store = store
            r = client.post(
                "/graphs/test-tenant/ingest",
                json={
                    "content": '[{"name":"Ada"},{"name":"Grace"}]',
                    "content_type": "json",
                    "kg_name": "people",
                    "source": "people.json",
                },
                headers=auth_headers,
            )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("job_id"), body
    assert body["entities_resolved"] == 2

    listed = await store.list_for_tenant("test-tenant")
    assert any(s.category == JobCategory.ingest for s in listed)
    job = await store.get(body["job_id"])
    assert job is not None
    assert job.status == JobStatus.applied
    assert job.kg_name == "people"
    by = _by_id(job.stage_trace)
    assert by[StageProjectId.p0].status == StageStatus.completed
    assert by[StageProjectId.p2].status == StageStatus.completed
    assert by[StageProjectId.p5].status == StageStatus.completed
    assert by[StageProjectId.p6].status == StageStatus.completed
    assert by[StageProjectId.p1].status == StageStatus.skipped


@pytest.mark.asyncio
async def test_csv_rows_route_creates_ingest_job(app, mock_neptune, auth_headers):
    """Use the real app fixture so rate-limiter + job store wiring match prod."""
    from cograph_client.enrichment.job_store import InMemoryJobStore
    from cograph_client.resolver.models import IngestResult

    store = InMemoryJobStore()
    app.state.enrichment_job_store = store

    mapping = {
        "entity_type": "Book",
        "columns": [
            {
                "column_name": "title",
                "role": "type_id",
                "attribute_name": "title",
                "datatype": "string",
            },
            {
                "column_name": "year",
                "role": "attribute",
                "attribute_name": "year",
                "datatype": "integer",
            },
        ],
    }
    rows = [
        {"title": "Dune", "year": "1965"},
        {"title": "Neuromancer", "year": "1984"},
    ]
    fake = IngestResult(
        entities_extracted=2,
        entities_resolved=2,
        triples_inserted=8,
        rows_in=2,
        types_created=["Book"],
    )

    with (
        patch(
            "cograph_client.api.routes.ingest.refresh_after_write",
            new=AsyncMock(),
        ),
        patch("cograph_client.api.routes.ingest.SchemaResolver") as MockResolver,
    ):
        inst = MockResolver.return_value
        inst._fetch_ontology = AsyncMock(return_value=({}, {}))
        inst._instance_graph = None
        inst._resolve_and_insert = AsyncMock(return_value=fake)

        with TestClient(app) as client:
            app.state.neptune_client = mock_neptune
            app.state.enrichment_job_store = store
            r = client.post(
                "/graphs/test-tenant/ingest/csv/rows",
                json={
                    "mapping": mapping,
                    "rows": rows,
                    "kg_name": "bookstore",
                    "source": "books.csv",
                },
                headers=auth_headers,
            )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("job_id"), body
    job = await store.get(body["job_id"])
    assert job is not None
    assert job.category == JobCategory.ingest
    assert job.status == JobStatus.applied
    assert job.type_name == "Book"
    by = _by_id(job.stage_trace)
    assert by[StageProjectId.p2].status == StageStatus.completed
    assert by[StageProjectId.p5].status == StageStatus.completed
    assert by[StageProjectId.p6].status == StageStatus.completed
    assert by[StageProjectId.p1].status == StageStatus.skipped
