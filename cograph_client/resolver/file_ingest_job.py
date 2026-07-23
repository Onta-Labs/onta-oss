"""File CSV/JSON/text ingest as a tracked EnrichJob with live stage_trace (ONTA-386).

File is an **A1-like entry** — the uploaded/pasted payload is already the source
material (Stage Contract A1 Source Bundle analogue). The Find-Data rail (P1) is
therefore skipped with an explicit reason; live instrumentation covers:

* **P0** Runtime & Orchestration — open/close the run
* **P2** Extraction — schema map / extract candidate facts from the file
* **P5** Ontology / Placement — type + attribute placement against the ontology
* **P6** Write — insert_facts / refresh_after_write (never a bespoke write path)

All stage_trace mutations are isolated in try/except so operator observability
can never fail the ingest write path. Boundary: OSS (``cograph_client`` only).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import structlog

from cograph_client.enrichment.models import (
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobCategory,
    JobProgress,
    JobStatus,
    JobTrigger,
)
from cograph_client.pipeline.manifest import RunManifest
from cograph_client.pipeline.stage_trace import (
    StageProjectId,
    StageStatus,
    attach_recorder,
)

# StageStatus imported for fail path terminal stamps.

logger = structlog.stdlib.get_logger("cograph.resolver.file_ingest_job")

# Rails not on the file-ingest write path — skip with explicit Stage Contract reasons.
_FILE_INGEST_SKIPS: tuple[tuple[StageProjectId, str], ...] = (
    (
        StageProjectId.p1,
        "file is A1-like entry (source provided); Find Data not on this rail",
    ),
    (
        StageProjectId.p3,
        "clean fused into extract/map on file ingest path",
    ),
    (
        StageProjectId.p4,
        "verify default-OFF on authoritative file ingest",
    ),
    (StageProjectId.p7, "answer rail not on file ingest jobs"),
    (StageProjectId.p8, "not a refresh-delta run"),
    (StageProjectId.p9, "surface is the Jobs UI; no A10 on this path"),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_trace(job: Optional[EnrichJob], fn) -> None:
    """Run a stage_trace mutation; never raise into the write path."""
    if job is None:
        return
    try:
        fn()
    except Exception:  # pragma: no cover - observability must not fail ingest
        logger.warning(
            "file_ingest_stage_trace_failed",
            job_id=getattr(job, "id", None),
            exc_info=True,
        )


async def open_file_ingest_job(
    job_store: Any,
    *,
    tenant_id: str,
    kg_name: str,
    content_type: str,
    source: str = "",
    type_name: str = "",
    attributes: Optional[list[str]] = None,
    rows_hint: Optional[int] = None,
    thread_id: Optional[str] = None,
) -> Optional[EnrichJob]:
    """Create a queued file-ingest job with live stage_trace skeleton.

    Returns ``None`` when ``job_store`` is absent (tests / bare contexts) so
    callers degrade to today's untracked ingest.
    """
    if job_store is None:
        return None

    job_id = str(uuid4())
    attrs = list(attributes or [])
    resolved_type = type_name or _default_type_name(content_type)
    job = EnrichJob(
        id=job_id,
        tenant_id=tenant_id,
        kg_name=kg_name or "",
        type_name=resolved_type,
        attributes=attrs,
        tier=EnrichmentTier.lite,
        status=JobStatus.queued,
        created_at=_now(),
        conflict_policy=ConflictPolicy.skip,
        category=JobCategory.ingest,
        trigger=JobTrigger.manual,
        progress=JobProgress(
            total=int(rows_hint or 0),
            phase="queued",
        ),
        # A9: the EnrichJob IS the run.
        manifest=RunManifest(run_id=job_id, stage="file_ingest"),
        thread_id=thread_id,
        platforms=[f"file:{content_type}"],
    )

    def _open() -> None:
        rec = attach_recorder(job)
        if rec is None:
            return
        rec.begin(
            StageProjectId.p0,
            input={
                "job_id": job.id,
                "category": "ingest",
                "content_type": content_type,
                "source": (source or "")[:300],
                "kg_name": kg_name or "",
                "entry": "A1-like file",
            },
        )
        rec.action(
            StageProjectId.p0,
            "create_job",
            detail=f"file ingest queued content_type={content_type}",
        )
        # Mark non-participating rails early so the Jobs Trace never looks empty.
        for pid, reason in _FILE_INGEST_SKIPS:
            rec.skip(pid, reason=reason)

    _safe_trace(job, _open)

    await job_store.create(job)
    return job


async def mark_file_ingest_running(
    job: Optional[EnrichJob],
    job_store: Any,
    *,
    phase: str = "extracting",
    total: Optional[int] = None,
) -> None:
    """Flip the job to running and stamp P0 start + P2 begin."""
    if job is None or job_store is None:
        return
    job.status = JobStatus.running
    job.started_at = job.started_at or _now()
    job.progress.phase = phase
    if total is not None and total >= 0:
        job.progress.total = total
    if job.manifest is not None:
        try:
            job.manifest.start(total=job.progress.total or None)
        except Exception:  # pragma: no cover
            logger.warning("file_ingest_manifest_start_failed", job_id=job.id, exc_info=True)

    def _run() -> None:
        rec = attach_recorder(job)
        if rec is None:
            return
        rec.action(
            StageProjectId.p0,
            "start_run",
            detail=f"status=running phase={phase}",
        )
        rec.begin(
            StageProjectId.p2,
            input={
                "content_type": (job.platforms or ["file"])[0],
                "type_name": job.type_name,
                "attributes": job.attributes,
                "kg_name": job.kg_name,
                "total_hint": job.progress.total,
            },
        )
        rec.action(StageProjectId.p2, "map_or_extract", detail=f"phase={phase}")

    _safe_trace(job, _run)
    await job_store.update(job)


def note_file_ingest_p2(
    job: Optional[EnrichJob],
    *,
    action: str,
    detail: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> None:
    """Record a P2 action (schema map / extract). Never raises."""

    def _act() -> None:
        rec = attach_recorder(job)
        if rec is None:
            return
        rec.action(StageProjectId.p2, action, detail=detail, meta=meta)

    _safe_trace(job, _act)


def note_file_ingest_p5(
    job: Optional[EnrichJob],
    *,
    types_created: Optional[list[str]] = None,
    attributes_added: Optional[list[str]] = None,
    type_name: Optional[str] = None,
    action: str = "place",
    detail: Optional[str] = None,
) -> None:
    """Record ontology placement (P5). Never raises.

    Mid-run notes only append actions + merge output fields; ``finish_file_ingest_job``
    is the one that closes P5.
    """

    def _p5() -> None:
        rec = attach_recorder(job)
        if rec is None:
            return
        rec.begin(
            StageProjectId.p5,
            input={
                "type_name": type_name or (job.type_name if job else None),
                "attributes": list(job.attributes) if job else [],
            },
        )
        rec.action(StageProjectId.p5, action, detail=detail)
        # Merge partial output onto the running project (do not end yet).
        out: dict[str, Any] = {}
        if types_created is not None:
            out["types_created"] = list(types_created)[:50]
        if attributes_added is not None:
            out["attributes_added"] = list(attributes_added)[:50]
        if type_name:
            out["type_name"] = type_name
        if out:
            for p in rec.trace.projects:
                if p.project_id == StageProjectId.p5:
                    p.output = {**p.output, **out}
                    break

    _safe_trace(job, _p5)


def note_file_ingest_p6(
    job: Optional[EnrichJob],
    *,
    action: str = "insert_facts",
    detail: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> None:
    """Record a write-path action (P6). Never raises."""

    def _p6() -> None:
        rec = attach_recorder(job)
        if rec is None:
            return
        rec.begin(
            StageProjectId.p6,
            input={
                "kg_name": job.kg_name if job else None,
                "write_path": "insert_facts / refresh_after_write",
            },
        )
        rec.action(StageProjectId.p6, action, detail=detail, meta=meta or {})

    _safe_trace(job, _p6)


async def finish_file_ingest_job(
    job: Optional[EnrichJob],
    job_store: Any,
    *,
    result: Any = None,
    type_name: Optional[str] = None,
) -> None:
    """Mark the file-ingest job applied and close live stage_trace projects."""
    if job is None or job_store is None:
        return

    now = _now()
    entities = 0
    triples = 0
    rows_in = 0
    rows_dropped = 0
    types_created: list[str] = []
    attributes_added: list[str] = []
    if result is not None:
        entities = int(
            getattr(result, "entities_resolved", 0)
            or getattr(result, "entities_extracted", 0)
            or 0
        )
        triples = int(getattr(result, "triples_inserted", 0) or 0)
        rows_in = int(getattr(result, "rows_in", 0) or 0)
        rows_dropped = int(getattr(result, "rows_dropped", 0) or 0)
        types_created = list(getattr(result, "types_created", None) or [])
        attributes_added = list(getattr(result, "attributes_added", None) or [])
        if type_name is None and types_created:
            type_name = types_created[0]
        # Stamp job_id onto the result when the model supports it (IngestResult).
        if hasattr(result, "job_id") and not getattr(result, "job_id", None):
            try:
                result.job_id = job.id
            except Exception:  # pragma: no cover
                pass

    if type_name:
        job.type_name = type_name
    if types_created and not job.attributes and attributes_added:
        job.attributes = [a.split(".", 1)[-1] for a in attributes_added[:20]]

    job.status = JobStatus.applied
    job.progress.phase = "done"
    job.progress.processed = rows_in or entities
    job.progress.filled = entities
    if job.progress.total <= 0:
        job.progress.total = rows_in or entities
    job.result_count = entities
    job.completed_at = now
    job.last_run = now
    if job.manifest is not None:
        try:
            # Record completed items so coverage is honest (cap sample size).
            for i in range(min(entities, 50)):
                job.manifest.record_completed(ref=f"entity-{i}")
            # If more entities than the sample, bump completed count without
            # bloating the items ledger.
            if entities > 50:
                job.manifest.completed = entities
            job.manifest.complete()
        except Exception:  # pragma: no cover
            logger.warning("file_ingest_manifest_complete_failed", job_id=job.id, exc_info=True)

    def _finish() -> None:
        rec = attach_recorder(job)
        if rec is None:
            return
        # Close P2 extract/map.
        rec.end(
            StageProjectId.p2,
            output={
                "entities_extracted": getattr(result, "entities_extracted", entities)
                if result is not None
                else entities,
                "rows_in": rows_in,
                "rows_dropped": rows_dropped,
                "chunks_processed": getattr(result, "chunks_processed", 0)
                if result is not None
                else 0,
            },
        )
        # P5 ontology placement — always close (even if no new types: placement ran).
        rec.begin(
            StageProjectId.p5,
            input={"type_name": job.type_name, "attributes": job.attributes},
        )
        rec.action(
            StageProjectId.p5,
            "type_resolve",
            detail=f"target type {job.type_name}",
        )
        rec.end(
            StageProjectId.p5,
            output={
                "type_name": job.type_name,
                "types_created": types_created[:50],
                "attributes_added": attributes_added[:50],
            },
        )
        # P6 write receipt via the shared insert_facts path.
        rec.begin(
            StageProjectId.p6,
            input={
                "kg_name": job.kg_name,
                "write_path": "insert_facts / refresh_after_write",
            },
        )
        rec.action(
            StageProjectId.p6,
            "insert_facts",
            detail="shared write path (kg_writer)",
        )
        rec.end(
            StageProjectId.p6,
            output={
                "entities_resolved": entities,
                "triples_inserted": triples,
                "status": "applied",
                "write_path": "insert_facts / refresh_after_write",
            },
        )
        # Re-assert skips for rails not on this path (skip is a no-op if already
        # completed/failed/running — which is what we want for P0/P2/P5/P6).
        for pid, reason in _FILE_INGEST_SKIPS:
            rec.skip(pid, reason=reason)
        rec.end(
            StageProjectId.p0,
            output={
                "status": "applied",
                "result_count": entities,
                "triples_inserted": triples,
                "rows_in": rows_in,
            },
        )
        job.stage_trace.summary = {
            "result_count": entities,
            "triples_inserted": triples,
            "rows_in": rows_in,
            "rows_dropped": rows_dropped,
            "type_name": job.type_name,
            "types_created": types_created[:20],
            "content": (job.platforms or [None])[0],
            "entry": "A1-like file",
        }
        job.stage_trace.status = "applied"

    _safe_trace(job, _finish)
    await job_store.update(job)


async def fail_file_ingest_job(
    job: Optional[EnrichJob],
    job_store: Any,
    error: str,
) -> None:
    """Mark the file-ingest job failed and close open stage_trace projects."""
    if job is None or job_store is None:
        return
    now = _now()
    job.status = JobStatus.failed
    job.progress.phase = "failed"
    job.error = (error or "file ingest failed")[:500]
    job.completed_at = now
    job.last_run = now
    if job.manifest is not None:
        try:
            from cograph_client.pipeline.manifest import HaltReasonKind

            job.manifest.halt(HaltReasonKind.error, job.error)
        except Exception:  # pragma: no cover
            logger.warning("file_ingest_manifest_halt_failed", job_id=job.id, exc_info=True)

    def _fail() -> None:
        rec = attach_recorder(job)
        if rec is None:
            return
        for p in list(job.stage_trace.projects if job.stage_trace else []):
            if p.status in (StageStatus.running, StageStatus.pending):
                rec.end(p.project_id, error=job.error, status=StageStatus.failed)
        rec.end(StageProjectId.p0, error=job.error, status=StageStatus.failed)
        for pid, reason in _FILE_INGEST_SKIPS:
            rec.skip(pid, reason=reason)
        job.stage_trace.status = "failed"
        job.stage_trace.summary = {
            "error": job.error,
            "type_name": job.type_name,
            "entry": "A1-like file",
        }

    _safe_trace(job, _fail)
    await job_store.update(job)


def _default_type_name(content_type: str) -> str:
    ct = (content_type or "text").lower()
    if ct == "csv":
        return "CsvRecord"
    if ct in ("json", "jsonl"):
        return "JsonRecord"
    return "FileRecord"
