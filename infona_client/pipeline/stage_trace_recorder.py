"""Live :class:`StageTraceRecorder` + job-trace minting.

Look up sibling / facade names via :func:`_host` so tests that monkeypatch
``infona_client.pipeline.stage_trace.<name>`` keep working.
"""

from __future__ import annotations

from typing import Any, Optional

from infona_client.pipeline.stage_trace_models import (
    JobStageTrace,
    StageAction,
    StageProjectId,
    StageProjectTrace,
    StageStatus,
)


def _host():
    from infona_client.pipeline import stage_trace as _mod

    return _mod

# --------------------------------------------------------------------------- #
# Live recorder (mutates a JobStageTrace in place)
# --------------------------------------------------------------------------- #
class StageTraceRecorder:
    """Append/update per-project entries on a :class:`JobStageTrace`.

    Capabilities call this as they cross stage boundaries. Persistence is the
    caller's job (stamp ``job.stage_trace = recorder.trace`` then
    ``job_store.update(job)``).
    """

    def __init__(self, trace: JobStageTrace) -> None:
        self.trace = trace
        self.trace.source = "live"
        # Ensure a slot for every project so UI always shows P0–P9.
        self.trace.projects = _host().ensure_all_projects(self.trace.projects)

    def _get(self, pid: StageProjectId) -> StageProjectTrace:
        for p in self.trace.projects:
            if p.project_id == pid:
                return p
        entry = _host().empty_project(pid, status=StageStatus.pending)
        self.trace.projects.append(entry)
        self.trace.projects = _host().ensure_all_projects(self.trace.projects)
        for p in self.trace.projects:
            if p.project_id == pid:
                return p
        return entry  # pragma: no cover

    def begin(
        self,
        pid: StageProjectId,
        *,
        input: Optional[dict[str, Any]] = None,
    ) -> StageProjectTrace:
        p = self._get(pid)
        p.status = StageStatus.running
        p.started_at = p.started_at or _host()._now()
        p.reconstructed = False
        if input:
            p.input = {**p.input, **input}
        return p

    def action(
        self,
        pid: StageProjectId,
        name: str,
        *,
        detail: Optional[str] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> None:
        p = self._get(pid)
        if p.status == StageStatus.skipped:
            p.status = StageStatus.running
            p.started_at = p.started_at or _host()._now()
        p.actions.append(
            StageAction(name=name, detail=detail, at=_host()._now(), meta=meta or {})
        )

    def end(
        self,
        pid: StageProjectId,
        *,
        output: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
        status: Optional[StageStatus] = None,
    ) -> StageProjectTrace:
        p = self._get(pid)
        p.completed_at = _host()._now()
        if p.started_at:
            p.duration_ms = round(
                (p.completed_at - p.started_at).total_seconds() * 1000, 1
            )
        if output:
            p.output = {**p.output, **output}
        if error:
            p.error = error
            p.status = StageStatus.failed
        else:
            p.status = status or StageStatus.completed
        p.reconstructed = False
        return p

    def skip(self, pid: StageProjectId, *, reason: str = "not on this rail") -> None:
        p = self._get(pid)
        if p.status in (StageStatus.completed, StageStatus.failed, StageStatus.running):
            return
        p.status = StageStatus.skipped
        p.output = {**p.output, "skip_reason": reason}


def new_trace_for_job(job: Any) -> JobStageTrace:
    """Mint a live :class:`JobStageTrace` skeleton from an EnrichJob-like object."""
    return JobStageTrace(
        job_id=str(getattr(job, "id", "")),
        tenant_id=str(getattr(job, "tenant_id", "")),
        kg_name=str(getattr(job, "kg_name", "")),
        category=getattr(getattr(job, "category", None), "value", None)
        or (str(job.category) if getattr(job, "category", None) else None),
        status=getattr(getattr(job, "status", None), "value", None)
        or (str(job.status) if getattr(job, "status", None) else None),
        source="live",
        projects=_host().ensure_all_projects([]),
        summary={},
    )


def attach_recorder(job: Any) -> Optional[StageTraceRecorder]:
    """Return a live :class:`StageTraceRecorder` bound to ``job.stage_trace``.

    No-ops (returns ``None``) when ``job`` is ``None``. Creates a fresh trace
    skeleton on first call. Callers should ``await job_store.update(job)`` after
    mutating the recorder so the jsonb payload persists.
    """
    if job is None:
        return None
    if getattr(job, "stage_trace", None) is None:
        job.stage_trace = _host().new_trace_for_job(job)
    return StageTraceRecorder(job.stage_trace)
