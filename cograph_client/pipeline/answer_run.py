"""Mint a trackable **answer run** for ask / agent Q&A (ONTA-389).

P7 Answer emits **A7**; P0/A9 carry run health. This is the operator Job Trace
path for read-only turns:

1. A meaningful ask/agent completion mints an :class:`EnrichJob` with
   ``category=answer`` (``run_id == job.id``).
2. Live :class:`JobStageTrace` records **P0** (Runtime / A9) + **P7** (Answer /
   A7); other P* slots are skipped with explicit reasons.
3. The answer payload echoes ``run_id`` so an operator can open
   ``GET /operator/jobs/{run_id}/trace`` and see P7 (+ P0).

**Not every chat message.** Only completed NL answers (``/ask`` and agent
``kind:answer`` from the query capability) mint a run — clarifies, plan cards,
and short ack turns do not.

Observability never breaks the answer path: every public entrypoint is wrapped
in try/except and no-ops when ``job_store`` is missing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from cograph_client.pipeline.manifest import HaltReasonKind, RunManifest, RunState
from cograph_client.pipeline.stage_trace import (
    StageProjectId,
    StageStatus,
    attach_recorder,
)

logger = structlog.stdlib.get_logger("cograph.pipeline.answer_run")

# Truncate free-text fields stamped into stage_trace so a huge SPARQL/answer
# never bloats the job jsonb payload.
_MAX_QUESTION = 500
_MAX_ANSWER = 800
_MAX_SPARQL = 400
_MAX_CAVEAT = 400


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clip(text: Any, n: int) -> str:
    s = (str(text) if text is not None else "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


async def record_answer_run(
    *,
    job_store: Any,
    tenant_id: str,
    kg_name: str = "",
    question: str,
    answer: str = "",
    sparql: str = "",
    citations: Optional[list[Any]] = None,
    coverage_caveat: str = "",
    ok: bool = True,
    error: Optional[str] = None,
    thread_id: Optional[str] = None,
    medium: str = "",
    timing: Optional[dict[str, Any]] = None,
    source: str = "ask",
) -> Optional[str]:
    """Mint a job + live stage_trace for one answer turn; return ``run_id``.

    Operator lookup path (documented for acceptance ONTA-389):

    * Response field ``run_id`` (alias of job id) from ``/ask`` or ``/agent``
      when ``kind=answer``.
    * ``GET /operator/jobs/{run_id}/trace`` (operator-only) → P0 completed/failed
      + P7 completed with A7 summary (answer clip, citation count, coverage
      caveat) and A9 manifest terminal state.

    Returns ``None`` when the store is unavailable, the question is empty, or
    persistence fails — never raises into the answer path.
    """
    if job_store is None:
        return None
    q = (question or "").strip()
    if not q:
        return None

    try:
        return await _record_answer_run_inner(
            job_store=job_store,
            tenant_id=tenant_id,
            kg_name=kg_name or "",
            question=q,
            answer=answer or "",
            sparql=sparql or "",
            citations=citations or [],
            coverage_caveat=coverage_caveat or "",
            ok=ok,
            error=error,
            thread_id=thread_id,
            medium=medium or "",
            timing=timing or {},
            source=source,
        )
    except Exception:  # noqa: BLE001 — never break the answer path
        logger.warning(
            "answer_run_record_failed",
            tenant_id=tenant_id,
            kg_name=kg_name,
            source=source,
            exc_info=True,
        )
        return None


async def _record_answer_run_inner(
    *,
    job_store: Any,
    tenant_id: str,
    kg_name: str,
    question: str,
    answer: str,
    sparql: str,
    citations: list[Any],
    coverage_caveat: str,
    ok: bool,
    error: Optional[str],
    thread_id: Optional[str],
    medium: str,
    timing: dict[str, Any],
    source: str,
) -> str:
    # Lazy import: enrichment.models imports JobStageTrace from pipeline; keep
    # the import edge one-way (pipeline → enrichment only inside this function).
    from cograph_client.enrichment.models import (
        ConflictPolicy,
        EnrichJob,
        EnrichmentTier,
        JobCategory,
        JobStatus,
        JobTrigger,
    )

    run_id = str(uuid.uuid4())
    now = _now()
    citation_count = len(citations) if citations else 0
    rows = timing.get("rows") if isinstance(timing, dict) else None
    try:
        result_count = int(rows) if rows is not None else None
    except (TypeError, ValueError):
        result_count = None

    # A9 Run Manifest: the run is the job; settle immediately (answer turns are
    # synchronous — there is no background worker phase).
    manifest = RunManifest(run_id=run_id, stage="answer")
    try:
        manifest.start(total=1)
        if ok:
            manifest.record_completed(ref="answer")
            manifest.complete()
        else:
            reason = _clip(error or "answer failed", 200)
            manifest.record_dropped(ref="answer", reason=reason)
            manifest.halt(HaltReasonKind.error, reason)
    except Exception:  # noqa: BLE001 — manifest is best-effort
        logger.warning("answer_run_manifest_settle_failed", run_id=run_id, exc_info=True)

    status = JobStatus.applied if ok else JobStatus.failed
    job = EnrichJob(
        id=run_id,
        tenant_id=tenant_id,
        kg_name=kg_name,
        # Answer runs are KG-scoped Q&A, not type-scoped enrichment. Empty
        # type/attrs keep the EnrichJob model happy without inventing fake types.
        type_name="",
        attributes=[],
        tier=EnrichmentTier.lite,
        status=status,
        created_at=now,
        started_at=now,
        completed_at=now,
        last_run=now,
        conflict_policy=ConflictPolicy.stage,
        category=JobCategory.answer,
        trigger=JobTrigger.manual,
        error=None if ok else _clip(error or "answer failed", 500),
        result_count=result_count,
        instructions=_clip(question, _MAX_QUESTION),
        manifest=manifest,
        thread_id=thread_id,
    )

    # Live stage_trace: P0 Runtime + P7 Answer; skip write/find rails.
    try:
        rec = attach_recorder(job)
        if rec is not None:
            rec.begin(
                StageProjectId.p0,
                input={
                    "job_id": run_id,
                    "category": JobCategory.answer.value,
                    "source": source,
                    "medium": medium or None,
                    "thread_id": thread_id,
                    "kg_name": kg_name,
                },
            )
            rec.action(
                StageProjectId.p0,
                "open_run",
                detail="answer turn minted (A9 run = job)",
            )
            rec.action(
                StageProjectId.p0,
                "a9_run_manifest",
                detail=f"manifest.state={getattr(getattr(manifest, 'state', None), 'value', manifest.state)}",
            )

            rec.begin(
                StageProjectId.p7,
                input={
                    "question": _clip(question, _MAX_QUESTION),
                    "kg_name": kg_name,
                    "source": source,
                },
            )
            rec.action(
                StageProjectId.p7,
                "a7_answer",
                detail="emit A7 Answer (cited answer + coverage caveat)",
                meta={
                    "citation_count": citation_count,
                    "has_sparql": bool(sparql),
                    "has_coverage_caveat": bool(coverage_caveat),
                },
            )
            a7_output: dict[str, Any] = {
                "answer": _clip(answer, _MAX_ANSWER),
                "citation_count": citation_count,
                "coverage_caveat": _clip(coverage_caveat, _MAX_CAVEAT),
                "has_sparql": bool(sparql),
                "sparql_preview": _clip(sparql, _MAX_SPARQL) if sparql else "",
                "result_count": result_count,
                "ok": ok,
            }
            if ok:
                rec.end(StageProjectId.p7, output=a7_output)
            else:
                rec.end(
                    StageProjectId.p7,
                    output=a7_output,
                    error=_clip(error or "answer failed", 500),
                    status=StageStatus.failed,
                )

            # Rails not on the read-only answer path.
            for pid, reason in (
                (StageProjectId.p1, "find-data rail not on answer turns"),
                (StageProjectId.p2, "extraction rail not on answer turns"),
                (StageProjectId.p3, "clean rail not on answer turns"),
                (StageProjectId.p4, "verify rail not on answer turns"),
                (StageProjectId.p5, "ontology placement not on answer turns"),
                (StageProjectId.p6, "write rail not on answer turns (read-only)"),
                (StageProjectId.p8, "not a refresh-delta run"),
                (
                    StageProjectId.p9,
                    "surface is Ask-AI / /ask; A10 corrections are separate",
                ),
            ):
                rec.skip(pid, reason=reason)

            p0_output: dict[str, Any] = {
                "status": status.value,
                "manifest_state": getattr(
                    getattr(manifest, "state", None), "value", str(manifest.state)
                ),
                "ok": ok,
                "source": source,
            }
            try:
                cov = manifest.coverage() if hasattr(manifest, "coverage") else None
                if cov is not None and hasattr(cov, "model_dump"):
                    p0_output["coverage"] = cov.model_dump()
                elif cov is not None:
                    p0_output["coverage"] = cov
            except Exception:  # noqa: BLE001
                pass

            if ok:
                rec.end(StageProjectId.p0, output=p0_output)
            else:
                rec.end(
                    StageProjectId.p0,
                    output=p0_output,
                    error=_clip(error or "answer failed", 500),
                    status=StageStatus.failed,
                )

            job.stage_trace.summary = {
                "question": _clip(question, _MAX_QUESTION),
                "answer": _clip(answer, _MAX_ANSWER),
                "citation_count": citation_count,
                "coverage_caveat": _clip(coverage_caveat, _MAX_CAVEAT),
                "ok": ok,
                "source": source,
                "medium": medium or None,
                "thread_id": thread_id,
            }
            job.stage_trace.status = status.value
            job.stage_trace.category = JobCategory.answer.value
    except Exception:  # noqa: BLE001 — stage_trace is best-effort
        logger.warning(
            "answer_run_stage_trace_failed",
            run_id=run_id,
            exc_info=True,
        )

    await job_store.create(job)
    logger.info(
        "answer_run_recorded",
        run_id=run_id,
        tenant_id=tenant_id,
        kg_name=kg_name,
        source=source,
        ok=ok,
        citation_count=citation_count,
    )
    return run_id


def answer_run_lookup_path(run_id: str) -> str:
    """Canonical operator URL path for a minted answer run (docs / tests)."""
    return f"/operator/jobs/{run_id}/trace"


# Re-export RunState for callers that want to assert terminal A9 state in tests.
__all__ = [
    "answer_run_lookup_path",
    "record_answer_run",
    "RunState",
]
