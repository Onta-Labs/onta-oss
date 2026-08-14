"""Cross-category open + finalize for job stage traces (ONTA-388).

Look up sibling / facade names via :func:`_host` so tests that monkeypatch
``infona_client.pipeline.stage_trace.<name>`` keep working.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog

from infona_client.pipeline.stage_trace_models import StageProjectId, StageStatus
from infona_client.pipeline.stage_trace_recorder import StageTraceRecorder

logger = structlog.stdlib.get_logger("infona.pipeline.stage_trace")


def _host():
    from infona_client.pipeline import stage_trace as _mod

    return _mod

# --------------------------------------------------------------------------- #
# Cross-category open + finalize (ONTA-388 / P0–A9 live contract)
# --------------------------------------------------------------------------- #
# Every job category (enrichment, dedupe, reconciliation, discovery, …) must
# open P0 on create and finalize all non-terminal projects when the job
# settles. Mid-run failures must never leave P2/P6 stuck as ``running`` on a
# terminal job. Helpers are try/except-isolated so operator observability can
# never fail a write path.
#
# Enrichment also has richer per-rail stamps above (ONTA-387); other categories
# use these general helpers. finalize_job_stage_trace is the shared "never leave
# running" primitive for any terminal job.

def open_job_stage_trace(
    job: Any,
    *,
    input: Optional[dict[str, Any]] = None,
    action_detail: Optional[str] = None,
) -> Optional[StageTraceRecorder]:
    """P0/A9 live open: stamp P0 ``running`` when a job is created/queued.

    Safe no-op on ``None`` job or any recorder error. Callers still own
    persistence (``job_store.create`` / ``update``).
    """
    try:
        rec = _host().attach_recorder(job)
        if rec is None:
            return None
        category = (
            getattr(getattr(job, "category", None), "value", None)
            or (str(job.category) if getattr(job, "category", None) else None)
        )
        p0_input: dict[str, Any] = {
            "job_id": getattr(job, "id", None),
            "category": category,
            "spend_ceiling_usd": getattr(job, "spend_ceiling_usd", None),
        }
        if input:
            p0_input.update(input)
        rec.begin(StageProjectId.p0, input=p0_input)
        detail = action_detail or (
            f"{category or 'job'} job queued" if category else "job queued"
        )
        rec.action(StageProjectId.p0, "create_job", detail=detail)
        if getattr(job, "stage_trace", None) is not None:
            job.stage_trace.status = (
                getattr(getattr(job, "status", None), "value", None)
                or (str(job.status) if getattr(job, "status", None) else "queued")
            )
        return rec
    except Exception:
        logger.warning(
            "stage_trace_open_failed",
            job_id=getattr(job, "id", None),
            exc_info=True,
        )
        return None


def finalize_job_stage_trace(
    job: Any,
    *,
    terminal_status: str,
    error: Optional[str] = None,
    summary: Optional[dict[str, Any]] = None,
    p0_output: Optional[dict[str, Any]] = None,
) -> None:
    """Stamp an honest terminal stage_trace for any job category.

    Ends every non-terminal project (running/pending) so mid-run failures never
    leave stages stuck as ``running`` on a settled job. Forces P0 to the
    matching terminal stage status. Isolated in try/except so operator
    observability cannot fail the write path.

    ``terminal_status`` should be a job status value (``applied`` / ``failed`` /
    ``review`` / ``cancelled``). Failures mark open projects + P0 as
    ``failed``; all other terminals mark them ``completed``.
    """
    try:
        rec = _host().attach_recorder(job)
        if rec is None:
            return
        status_l = (terminal_status or "").strip().lower()
        is_fail = status_l == "failed"
        end_status = StageStatus.failed if is_fail else StageStatus.completed
        err = (error or getattr(job, "error", None) or None) if is_fail else None

        for p in list(job.stage_trace.projects if job.stage_trace else []):
            if p.status in (StageStatus.running, StageStatus.pending):
                out = p0_output if p.project_id == StageProjectId.p0 else None
                rec.end(
                    p.project_id,
                    error=err,
                    status=end_status,
                    output=out,
                )

        # Always stamp orchestration terminal even if it was already completed
        # mid-run (e.g. partial instrumentation ended P0 early).
        p0 = None
        for p in job.stage_trace.projects if job.stage_trace else []:
            if p.project_id == StageProjectId.p0:
                p0 = p
                break
        if is_fail:
            rec.end(
                StageProjectId.p0,
                error=err,
                status=StageStatus.failed,
                output=p0_output,
            )
        elif p0 is None or p0.status in (
            StageStatus.running,
            StageStatus.pending,
            StageStatus.skipped,
        ):
            rec.end(
                StageProjectId.p0,
                status=StageStatus.completed,
                output=p0_output,
            )
        elif p0_output:
            p0.output = {**p0.output, **p0_output}

        job.stage_trace.status = status_l or terminal_status
        merged_summary: dict[str, Any] = dict(job.stage_trace.summary or {})
        if summary:
            merged_summary.update(summary)
        if err:
            merged_summary.setdefault("error", err)
        job.stage_trace.summary = merged_summary
    except Exception:
        logger.warning(
            "stage_trace_finalize_failed",
            job_id=getattr(job, "id", None),
            exc_info=True,
        )


def ensure_job_stage_trace_open(job: Any) -> Optional[StageTraceRecorder]:
    """Open P0 if the job has no live trace yet (belt for workers that start
    without a create-site open). No-op when a trace is already present."""
    if job is None:
        return None
    if getattr(job, "stage_trace", None) is not None:
        try:
            return _host().StageTraceRecorder(job.stage_trace)
        except Exception:
            return None
    return _host().open_job_stage_trace(job)
