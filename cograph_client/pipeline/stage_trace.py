"""Contract-level **job stage traces** for P0–P9 (operator Job Trace page).

Onta's decomposition is ten sub-projects (P0 Runtime … P9 Surfaces). Operators
debugging a job need to see, per project that participated:

* **input** the stage was given
* **what it did** (actions / steps)
* **output** it produced

aligned with the Stage Contract (Notion Sub-Project Stage Contracts / A0–A10).

This module is the durable schema + a small recorder + a **reconstructor** that
builds a best-effort view from fields already on :class:`EnrichJob` (manifest,
provider_logs, progress, …) so jobs that ran *before* live instrumentation still
render something useful.

Boundary: OSS. Imports only stdlib + pydantic (+ lazy EnrichJob for reconstructor).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

import structlog
from pydantic import BaseModel, Field

logger = structlog.stdlib.get_logger("cograph.pipeline.stage_trace")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StageProjectId(str, Enum):
    """The ten Stage Contract projects (P0–P9)."""

    p0 = "P0"
    p1 = "P1"
    p2 = "P2"
    p3 = "P3"
    p4 = "P4"
    p5 = "P5"
    p6 = "P6"
    p7 = "P7"
    p8 = "P8"
    p9 = "P9"


# Catalog: id → display name + contract blurb (producer→consumer artifact).
# Keep in sync with Notion "Sub-Project Stage Contracts" (v2).
STAGE_CATALOG: dict[StageProjectId, dict[str, str]] = {
    StageProjectId.p0: {
        "name": "Runtime & Orchestration",
        "consumes": "A9 Run Manifest (from every stage)",
        "emits": "run status; cost envelope; terminal halt reasons",
        "goal": "Own the run as an object — state machine, retries, partial-failure, cost.",
    },
    StageProjectId.p1: {
        "name": "Find Data",
        "consumes": "user goal · A8 Refresh Delta",
        "emits": "A1 Source Bundle",
        "goal": "Turn a goal into complete-enough, provenance-stamped source material.",
    },
    StageProjectId.p2: {
        "name": "Extraction",
        "consumes": "A1 Source Bundle (or uploaded file)",
        "emits": "A2 Candidate Facts",
        "goal": "Pull evidence-linked candidate facts from sources (soft-typed).",
    },
    StageProjectId.p3: {
        "name": "Clean",
        "consumes": "A2 Candidate Facts",
        "emits": "A3 Clean Facts",
        "goal": "Normalize values; log every transform/drop; preserve surface form.",
    },
    StageProjectId.p4: {
        "name": "Verify",
        "consumes": "A3 Clean Facts",
        "emits": "A4 Verified Facts",
        "goal": "Truth verdicts + evidence refs (identity-conditional where needed).",
    },
    StageProjectId.p5: {
        "name": "Ontology / Placement",
        "consumes": "A4 Verified Facts",
        "emits": "A5 Placement Plan",
        "goal": "Map facts to ontology terms; stamp ontology version.",
    },
    StageProjectId.p6: {
        "name": "Write",
        "consumes": "A5 Placement Plan",
        "emits": "A6 Graph Delta",
        "goal": "Mutate the graph (write / supersede / retract / merge) with receipts.",
    },
    StageProjectId.p7: {
        "name": "Answer",
        "consumes": "A6 Graph Delta · A9 Run Manifest",
        "emits": "A7 Answer",
        "goal": "Cited answer + coverage caveats from the run manifest.",
    },
    StageProjectId.p8: {
        "name": "Freshness",
        "consumes": "graph state · schedule",
        "emits": "A8 Refresh Delta → P1",
        "goal": "Diff-scoped re-acquisition; refresh as supersession, not silent add.",
    },
    StageProjectId.p9: {
        "name": "Surfaces",
        "consumes": "all artifacts (user-facing)",
        "emits": "A10 Correction & Feedback",
        "goal": "Everything the user touches; corrections re-enter P6 / gold sets.",
    },
}


class StageStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    skipped = "skipped"
    failed = "failed"
    reconstructed = "reconstructed"  # synthesized from job fields, not live-recorded


class StageAction(BaseModel):
    """One step the project took."""

    name: str
    detail: Optional[str] = None
    at: Optional[datetime] = None
    meta: dict[str, Any] = Field(default_factory=dict)


class StageProjectTrace(BaseModel):
    """One P0–P9 project's participation in a job run."""

    project_id: StageProjectId
    name: str
    status: StageStatus = StageStatus.pending
    # Contract summary (from STAGE_CATALOG) — rendered even when no live data.
    contract_goal: Optional[str] = None
    contract_consumes: Optional[str] = None
    contract_emits: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[float] = None
    # Free-form but intentionally structured for the UI (JSON-serializable).
    input: dict[str, Any] = Field(default_factory=dict)
    actions: list[StageAction] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    # True when this entry was synthesized after-the-fact from other job fields.
    reconstructed: bool = False


class JobStageTrace(BaseModel):
    """Full operator-facing stage trace for one job."""

    job_id: str
    tenant_id: str
    kg_name: str
    category: Optional[str] = None
    status: Optional[str] = None
    # How complete is the live instrumentation for this job?
    source: Literal["live", "reconstructed", "mixed"] = "reconstructed"
    projects: list[StageProjectTrace] = Field(default_factory=list)
    # Job-level summary (always useful at the top of the page).
    summary: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime = Field(default_factory=_now)


def _catalog_fields(pid: StageProjectId) -> dict[str, str]:
    cat = STAGE_CATALOG[pid]
    return {
        "name": cat["name"],
        "contract_goal": cat["goal"],
        "contract_consumes": cat["consumes"],
        "contract_emits": cat["emits"],
    }


def empty_project(pid: StageProjectId, *, status: StageStatus = StageStatus.skipped) -> StageProjectTrace:
    fields = _catalog_fields(pid)
    return StageProjectTrace(
        project_id=pid,
        status=status,
        **fields,
    )


def ensure_all_projects(projects: list[StageProjectTrace]) -> list[StageProjectTrace]:
    """Return P0…P9 in order, filling missing entries as ``skipped``."""
    by_id = {p.project_id: p for p in projects}
    out: list[StageProjectTrace] = []
    for pid in StageProjectId:
        if pid in by_id:
            out.append(by_id[pid])
        else:
            out.append(empty_project(pid))
    return out


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
        self.trace.projects = ensure_all_projects(self.trace.projects)

    def _get(self, pid: StageProjectId) -> StageProjectTrace:
        for p in self.trace.projects:
            if p.project_id == pid:
                return p
        entry = empty_project(pid, status=StageStatus.pending)
        self.trace.projects.append(entry)
        self.trace.projects = ensure_all_projects(self.trace.projects)
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
        p.started_at = p.started_at or _now()
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
            p.started_at = p.started_at or _now()
        p.actions.append(
            StageAction(name=name, detail=detail, at=_now(), meta=meta or {})
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
        p.completed_at = _now()
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
        projects=ensure_all_projects([]),
        summary={},
    )


# --------------------------------------------------------------------------- #
# Reconstructor — best-effort view for jobs without live stage_trace
# --------------------------------------------------------------------------- #
def reconstruct_from_job(job: Any) -> JobStageTrace:
    """Build a :class:`JobStageTrace` from existing EnrichJob fields.

    Used when ``job.stage_trace`` is None (older jobs) OR as a fill-in for
    projects the live recorder never touched. Marked ``reconstructed``.
    """
    category = getattr(getattr(job, "category", None), "value", None) or str(
        getattr(job, "category", "") or ""
    )
    status = getattr(getattr(job, "status", None), "value", None) or str(
        getattr(job, "status", "") or ""
    )
    projects: list[StageProjectTrace] = []

    # --- P0 Runtime ---------------------------------------------------------
    p0 = empty_project(StageProjectId.p0, status=StageStatus.reconstructed)
    p0.reconstructed = True
    p0.input = {
        "job_id": getattr(job, "id", None),
        "category": category,
        "trigger": getattr(getattr(job, "trigger", None), "value", None),
        "spend_ceiling_usd": getattr(job, "spend_ceiling_usd", None),
    }
    p0.actions = [
        StageAction(name="open_run", detail="Job record created / advanced"),
    ]
    manifest = getattr(job, "manifest", None)
    if manifest is not None:
        cov = None
        try:
            cov = manifest.coverage() if hasattr(manifest, "coverage") else None
        except Exception:  # pragma: no cover
            cov = None
        p0.output = {
            "manifest_state": getattr(getattr(manifest, "state", None), "value", None)
            or str(getattr(manifest, "state", None)),
            "halt_reason": getattr(manifest, "halt_reason", None)
            or getattr(getattr(manifest, "halt", None), "reason", None),
            "coverage": cov.model_dump() if cov is not None and hasattr(cov, "model_dump") else cov,
            "spend_usd": getattr(manifest, "spend_usd", None)
            or getattr(manifest, "total_spend_usd", None),
        }
        p0.actions.append(
            StageAction(name="a9_run_manifest", detail="A9 Run Manifest present on job")
        )
    p0.output = {
        **p0.output,
        "status": status,
        "cost": getattr(job, "cost", None),
        "error": getattr(job, "error", None),
        "started_at": _iso(getattr(job, "started_at", None)),
        "completed_at": _iso(getattr(job, "completed_at", None)),
    }
    if status in ("failed",):
        p0.status = StageStatus.failed
        p0.error = getattr(job, "error", None)
    elif status in ("applied", "review", "cancelled"):
        p0.status = StageStatus.completed
    elif status in ("running", "queued"):
        p0.status = StageStatus.running
    projects.append(p0)

    # --- P1 Find ------------------------------------------------------------
    p1 = empty_project(StageProjectId.p1, status=StageStatus.skipped)
    p1.reconstructed = True
    if category == "discovery" or getattr(job, "platforms", None):
        p1.status = StageStatus.reconstructed
        p1.input = {
            "type_name": getattr(job, "type_name", None),
            "attributes": getattr(job, "attributes", None),
            "kg_name": getattr(job, "kg_name", None),
            "instructions": getattr(job, "instructions", None),
        }
        plogs = getattr(job, "provider_logs", None) or []
        p1.actions = [
            StageAction(
                name="provider",
                detail=f"{getattr(pl, 'provider', '?')}: {getattr(pl, 'status', '?')}",
                meta=_provider_log_meta(pl),
            )
            for pl in plogs
        ] or [StageAction(name="find", detail="Discovery run (no provider_logs)")]
        p1.output = {
            "result_count": getattr(job, "result_count", None),
            "platforms": getattr(job, "platforms", None),
            "provider_count": len(plogs),
        }
    
    if category == "ingest":
        p1.status = StageStatus.skipped
        p1.reconstructed = True
        p1.output = {
            "skip_reason": "file is A1-like entry (source provided); Find Data not on this rail"
        }
projects.append(p1)

    # --- P2 Extract ---------------------------------------------------------
    p2 = empty_project(StageProjectId.p2, status=StageStatus.skipped)
    p2.reconstructed = True
    progress = getattr(job, "progress", None)
    # Answer runs (ONTA-389) are read-only Q&A — never reconstruct extract/write.
    if category != "answer" and (
        category in ("discovery", "enrichment", "ingest") or progress is not None
    ):
        p2.status = StageStatus.reconstructed
        p2.input = {
            "type_name": getattr(job, "type_name", None),
            "attributes": getattr(job, "attributes", None),
        }
        p2.actions = [StageAction(name="extract_or_lookup", detail=f"category={category}")]
        p2.output = {
            "progress": _progress_dict(progress),
            "result_count": getattr(job, "result_count", None),
            "row_results": len(getattr(job, "results", None) or []),
        }
    projects.append(p2)

    # --- P3 Clean -----------------------------------------------------------
    p3 = empty_project(StageProjectId.p3, status=StageStatus.skipped)
    p3.reconstructed = True
    # Clean is often fused; surface skip unless we have drop signals on manifest.
    if manifest is not None and getattr(manifest, "items", None):
        drops = [
            it
            for it in (manifest.items or [])
            if str(getattr(it, "status", "")).lower() in ("dropped", "drop", "failed")
        ]
        if drops:
            p3.status = StageStatus.reconstructed
            p3.output = {"dropped_items_sample": len(drops)}
            p3.actions = [StageAction(name="clean_drops", detail=f"{len(drops)} drop ledger entries")]
    projects.append(p3)

    # --- P4 Verify ----------------------------------------------------------
    p4 = empty_project(StageProjectId.p4, status=StageStatus.skipped)
    p4.reconstructed = True
    # Default-OFF on live path; enrichment conflict_policy is the closest signal.
    cp = getattr(getattr(job, "conflict_policy", None), "value", None)
    if category == "enrichment" and cp:
        p4.status = StageStatus.reconstructed
        p4.input = {
            "conflict_policy": cp,
            "confidence_min": getattr(job, "confidence_min", None),
        }
        p4.actions = [
            StageAction(
                name="conflict_policy",
                detail=f"policy={cp}, confidence_min={getattr(job, 'confidence_min', None)}",
            )
        ]
        p4.output = {
            "verified": getattr(progress, "verified", None) if progress else None,
            "conflicts": getattr(progress, "conflicts", None) if progress else None,
        }
    projects.append(p4)

    # --- P5 Ontology --------------------------------------------------------
    p5 = empty_project(StageProjectId.p5, status=StageStatus.skipped)
    p5.reconstructed = True
    if getattr(job, "type_name", None):
        p5.status = StageStatus.reconstructed
        p5.input = {"type_name": job.type_name, "attributes": getattr(job, "attributes", None)}
        p5.actions = [StageAction(name="type_resolve", detail=f"target type {job.type_name}")]
        p5.output = {"type_name": job.type_name}
    projects.append(p5)

    # --- P6 Write -----------------------------------------------------------
    p6 = empty_project(StageProjectId.p6, status=StageStatus.skipped)
    p6.reconstructed = True
    if category != "answer" and (
        category in ("discovery", "enrichment", "dedupe", "reconciliation", "ingest") or progress
    ):
        p6.status = StageStatus.reconstructed
        p6.input = {"kg_name": getattr(job, "kg_name", None), "category": category}
        filled = getattr(progress, "filled", None) if progress else None
        p6.actions = [StageAction(name="write_path", detail="insert_facts / conflict apply")]
        p6.output = {
            "filled": filled,
            "result_count": getattr(job, "result_count", None),
            "status": status,
        }
        if status == "review":
            p6.output["note"] = "staged for review (not yet applied)"
    projects.append(p6)

    # --- P7 Answer ----------------------------------------------------------
    p7 = empty_project(StageProjectId.p7, status=StageStatus.skipped)
    p7.reconstructed = True
    # Answer runs (ONTA-389, category=answer) are the P7 rail: A7 Answer from
    # /ask or agent question turns. Other non-write categories may also carry
    # answer-like work; leave a reconstructed breadcrumb for them.
    if category == "answer":
        p7.status = StageStatus.reconstructed
        p7.input = {
            "question": getattr(job, "instructions", None),
            "kg_name": getattr(job, "kg_name", None),
        }
        p7.actions = [
            StageAction(name="a7_answer", detail="answer job (P7 Answer emits A7)")
        ]
        p7.output = {
            "status": status,
            "result_count": getattr(job, "result_count", None),
            "error": getattr(job, "error", None),
        }
        if status in ("failed",):
            p7.status = StageStatus.failed
            p7.error = getattr(job, "error", None)
    elif category not in ("discovery", "enrichment", "dedupe", "reconciliation", "ingest"):
        p7.status = StageStatus.reconstructed
        p7.actions = [StageAction(name="answer", detail="non-write job category")]
    projects.append(p7)

    # --- P8 Freshness -------------------------------------------------------
    p8 = empty_project(StageProjectId.p8, status=StageStatus.skipped)
    p8.reconstructed = True
    trigger = getattr(getattr(job, "trigger", None), "value", None)
    if trigger == "scheduled":
        p8.status = StageStatus.reconstructed
        p8.input = {"trigger": "scheduled", "next_run": _iso(getattr(job, "next_run", None))}
        p8.actions = [StageAction(name="scheduled_refresh", detail="scheduled trigger")]
    projects.append(p8)

    # --- P9 Surfaces --------------------------------------------------------
    p9 = empty_project(StageProjectId.p9, status=StageStatus.skipped)
    p9.reconstructed = True
    if getattr(job, "thread_id", None):
        p9.status = StageStatus.reconstructed
        p9.input = {"thread_id": job.thread_id}
        p9.actions = [
            StageAction(name="chat_kickoff", detail="Job created from Ask-AI conversation")
        ]
        p9.output = {"thread_id": job.thread_id}
    projects.append(p9)

    return JobStageTrace(
        job_id=str(getattr(job, "id", "")),
        tenant_id=str(getattr(job, "tenant_id", "")),
        kg_name=str(getattr(job, "kg_name", "")),
        category=category or None,
        status=status or None,
        source="reconstructed",
        projects=ensure_all_projects(projects),
        summary={
            "type_name": getattr(job, "type_name", None),
            "attributes": getattr(job, "attributes", None),
            "result_count": getattr(job, "result_count", None),
            "cost": getattr(job, "cost", None),
            "error": getattr(job, "error", None),
            "thread_id": getattr(job, "thread_id", None),
            "platforms": getattr(job, "platforms", None),
            "progress": _progress_dict(progress),
        },
        recorded_at=_now(),
    )


def resolve_trace(job: Any) -> JobStageTrace:
    """Return the best available stage trace for a job.

    Prefer live ``job.stage_trace`` (fill any still-skipped slots from the
    reconstructor so the page always has P0–P9). Fall back to pure reconstruct.
    """
    live = getattr(job, "stage_trace", None)
    if live is None:
        return reconstruct_from_job(job)

    # live may be a dict (from older json) or a JobStageTrace
    if isinstance(live, dict):
        live = JobStageTrace.model_validate(live)
    elif not isinstance(live, JobStageTrace):
        try:
            live = JobStageTrace.model_validate(live)
        except Exception:
            return reconstruct_from_job(job)

    reconstructed = reconstruct_from_job(job)
    # Job terminal state — if live instrumentation left a project as running/
    # pending after the job already failed/applied, prefer recon (or force
    # failed) so the operator UI never shows a frozen spinner on a settled job.
    job_status = str(
        getattr(getattr(job, "status", None), "value", None)
        or getattr(job, "status", "")
        or ""
    ).lower()
    job_terminal = job_status in (
        "failed",
        "applied",
        "cancelled",
        "review",
    )
    job_error = getattr(job, "error", None)

    by_live = {p.project_id: p for p in live.projects}
    by_recon = {p.project_id: p for p in reconstructed.projects}
    merged: list[StageProjectTrace] = []
    any_live = False
    any_recon = False
    for pid in StageProjectId:
        lp = by_live.get(pid)
        rp = by_recon.get(pid)
        # Stale live running/pending on a terminal job is not trustworthy.
        if (
            lp is not None
            and job_terminal
            and lp.status in (StageStatus.running, StageStatus.pending)
        ):
            if rp is not None and rp.status not in (
                StageStatus.skipped,
                StageStatus.pending,
            ):
                merged.append(rp)
                any_recon = True
                continue
            fixed = lp.model_copy(deep=True)
            if job_status == "failed":
                fixed.status = StageStatus.failed
                fixed.error = fixed.error or job_error
            else:
                fixed.status = StageStatus.completed
            fixed.reconstructed = True
            merged.append(fixed)
            any_recon = True
            continue
        if lp is not None and lp.status not in (StageStatus.skipped, StageStatus.pending):
            merged.append(lp)
            any_live = True
        elif rp is not None and rp.status not in (StageStatus.skipped, StageStatus.pending):
            merged.append(rp)
            any_recon = True
        elif lp is not None:
            merged.append(lp)
        else:
            merged.append(empty_project(pid))

    if any_live and any_recon:
        source: Literal["live", "reconstructed", "mixed"] = "mixed"
    elif any_live:
        source = "live"
    else:
        source = "reconstructed"

    return JobStageTrace(
        job_id=live.job_id or reconstructed.job_id,
        tenant_id=live.tenant_id or reconstructed.tenant_id,
        kg_name=live.kg_name or reconstructed.kg_name,
        category=live.category or reconstructed.category,
        status=live.status or reconstructed.status,
        source=source,
        projects=ensure_all_projects(merged),
        summary={**reconstructed.summary, **(live.summary or {})},
        recorded_at=live.recorded_at or _now(),
    )


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _progress_dict(progress: Any) -> Optional[dict[str, Any]]:
    if progress is None:
        return None
    if hasattr(progress, "model_dump"):
        return progress.model_dump()
    if isinstance(progress, dict):
        return progress
    return {
        k: getattr(progress, k, None)
        for k in (
            "total",
            "processed",
            "filled",
            "verified",
            "conflicts",
            "skipped",
            "no_match",
            "cache_hits",
        )
    }


def _provider_log_meta(pl: Any) -> dict[str, Any]:
    if hasattr(pl, "model_dump"):
        d = pl.model_dump()
        # Drop huge request lists from the meta snippet
        reqs = d.get("requests") or []
        d["requests"] = reqs[:5]
        d["request_count"] = len(reqs)
        return d
    return {"provider": getattr(pl, "provider", None)}


def attach_recorder(job: Any) -> Optional[StageTraceRecorder]:
    """Return a live :class:`StageTraceRecorder` bound to ``job.stage_trace``.

    No-ops (returns ``None``) when ``job`` is ``None``. Creates a fresh trace
    skeleton on first call. Callers should ``await job_store.update(job)`` after
    mutating the recorder so the jsonb payload persists.
    """
    if job is None:
        return None
    if getattr(job, "stage_trace", None) is None:
        job.stage_trace = new_trace_for_job(job)
    return StageTraceRecorder(job.stage_trace)


# --------------------------------------------------------------------------- #
# Enrichment live instrumentation (ONTA-387)
# --------------------------------------------------------------------------- #
# Discovery (web_ingest) already records live P0/P1/P2/P6. Enrichment is a
# different rail: no P1 Find (entities already exist), P2 = lookup/extract,
# P4 = conflict/confidence/verify, P6 = apply/write. Everything is try/except
# so operator observability can never fail the write path.

# Rails not on the enrichment job path — stamped with reasons at terminal.
_ENRICHMENT_SKIP_REASONS: dict[StageProjectId, str] = {
    StageProjectId.p1: "find rail not on enrichment; entities already in the KG",
    StageProjectId.p3: "clean fused into extract/lookup on enrichment path",
    StageProjectId.p5: "type/attribute placement known a priori from job spec",
    StageProjectId.p7: "answer rail not on enrichment jobs",
    StageProjectId.p8: "not a refresh-delta run (P8 is schedule→P1, not enrich write)",
    StageProjectId.p9: "surface is the Jobs UI; no A10 correction on this path",
}


def _enum_value(v: Any) -> Optional[str]:
    if v is None:
        return None
    return getattr(v, "value", None) or str(v)


def stamp_enrichment_job_created(job: Any) -> None:
    """Open live stage_trace at enrichment job create (P0).

    Called from every create site (``POST /enrich/jobs``, agent enrich cap,
    ``POST /actions/enrich``) so a just-queued job already has ``source=live``
    rather than only a reconstructed view. Never raises.
    """
    try:
        rec = attach_recorder(job)
        if rec is None:
            return
        rec.begin(
            StageProjectId.p0,
            input={
                "job_id": getattr(job, "id", None),
                "category": "enrichment",
                "tier": _enum_value(getattr(job, "tier", None)),
                "type_name": getattr(job, "type_name", None),
                "attributes": list(getattr(job, "attributes", None) or []),
                "conflict_policy": _enum_value(getattr(job, "conflict_policy", None)),
                "confidence_min": getattr(job, "confidence_min", None),
                "spend_ceiling_usd": getattr(job, "spend_ceiling_usd", None),
                "kg_name": getattr(job, "kg_name", None),
            },
        )
        rec.action(
            StageProjectId.p0,
            "create_job",
            detail="enrichment job queued",
        )
        if getattr(job, "stage_trace", None) is not None:
            job.stage_trace.status = _enum_value(getattr(job, "status", None)) or "queued"
            job.stage_trace.summary = {
                "type_name": getattr(job, "type_name", None),
                "attributes": list(getattr(job, "attributes", None) or []),
                "tier": _enum_value(getattr(job, "tier", None)),
            }
    except Exception:  # pragma: no cover - never block job create on obs
        pass


def stamp_enrichment_run_started(job: Any) -> None:
    """P0 start_run + open P2 lookup/extract, P4 policy, P6 write.

    P4 is always opened on enrichment: every enrich job carries a conflict
    policy + confidence floor that gates verdicts (the contract-shaped
    "when policy applies" case). Never raises.
    """
    try:
        rec = attach_recorder(job)
        if rec is None:
            return
        rec.action(
            StageProjectId.p0,
            "start_run",
            detail="status=running",
        )
        # P2 Extraction — adapter-chain lookup / LLM extract over existing entities.
        rec.begin(
            StageProjectId.p2,
            input={
                "type_name": getattr(job, "type_name", None),
                "attributes": list(getattr(job, "attributes", None) or []),
                "tier": _enum_value(getattr(job, "tier", None)),
                "sources": list(getattr(job, "sources", None) or []) or None,
                "entity_uri_count": len(getattr(job, "entity_uris", None) or []),
                "scope": (
                    getattr(job, "scope", None).model_dump()
                    if getattr(job, "scope", None) is not None
                    and hasattr(getattr(job, "scope", None), "model_dump")
                    else None
                ),
            },
        )
        rec.action(
            StageProjectId.p2,
            "lookup",
            detail="adapter-chain lookup/extract",
        )
        # P4 Verify — conflict policy + confidence floor always apply on enrich.
        policy = _enum_value(getattr(job, "conflict_policy", None))
        rec.begin(
            StageProjectId.p4,
            input={
                "conflict_policy": policy,
                "confidence_min": getattr(job, "confidence_min", None),
            },
        )
        rec.action(
            StageProjectId.p4,
            "conflict_policy",
            detail=(
                f"policy={policy}, "
                f"confidence_min={getattr(job, 'confidence_min', None)}"
            ),
        )
        # P6 Write — apply path (insert_facts / supersede / stage).
        rec.begin(
            StageProjectId.p6,
            input={
                "kg_name": getattr(job, "kg_name", None),
                "conflict_policy": policy,
                "category": "enrichment",
            },
        )
        if getattr(job, "stage_trace", None) is not None:
            job.stage_trace.status = "running"
    except Exception:  # pragma: no cover - never block enrichment on obs
        pass


def stamp_enrichment_entities_selected(
    job: Any, *, entity_count: int, item_total: int
) -> None:
    """Record P2 entity-selection progress after the SELECT. Never raises."""
    try:
        rec = attach_recorder(job)
        if rec is None:
            return
        rec.action(
            StageProjectId.p2,
            "entities_selected",
            detail=f"entities={entity_count} items={item_total}",
            meta={"entity_count": entity_count, "item_total": item_total},
        )
        # Seed P6 with planned write scope once we know the item total.
        rec.action(
            StageProjectId.p6,
            "plan_write",
            detail=f"planned_items={item_total}",
            meta={"item_total": item_total},
        )
    except Exception:  # pragma: no cover
        pass


def stamp_enrichment_write_phase(
    job: Any,
    *,
    write_policy: Optional[str] = None,
    has_conflicts: bool = False,
    applied: bool = False,
) -> None:
    """Record a P4/P6 action around the apply/write phase. Never raises."""
    try:
        rec = attach_recorder(job)
        if rec is None:
            return
        progress = getattr(job, "progress", None)
        rec.action(
            StageProjectId.p4,
            "verdict_tally",
            detail=(
                f"filled={getattr(progress, 'filled', None)} "
                f"verified={getattr(progress, 'verified', None)} "
                f"conflicts={getattr(progress, 'conflicts', None)} "
                f"no_match={getattr(progress, 'no_match', None)}"
            ),
            meta={
                "filled": getattr(progress, "filled", None),
                "verified": getattr(progress, "verified", None),
                "conflicts": getattr(progress, "conflicts", None),
                "no_match": getattr(progress, "no_match", None),
                "has_conflicts": has_conflicts,
            },
        )
        rec.action(
            StageProjectId.p6,
            "write_path",
            detail=(
                f"write_policy={write_policy} applied={applied} "
                f"has_conflicts={has_conflicts}"
            ),
            meta={
                "write_policy": write_policy,
                "applied": applied,
                "has_conflicts": has_conflicts,
            },
        )
    except Exception:  # pragma: no cover
        pass


def stamp_enrichment_run_finished(job: Any) -> None:
    """Close P0/P2/P4/P6 live; skip other P* with reasons. Never raises."""
    try:
        rec = attach_recorder(job)
        if rec is None:
            return
        progress = getattr(job, "progress", None)
        status = _enum_value(getattr(job, "status", None))
        progress_out = {
            "total": getattr(progress, "total", None),
            "processed": getattr(progress, "processed", None),
            "filled": getattr(progress, "filled", None),
            "verified": getattr(progress, "verified", None),
            "conflicts": getattr(progress, "conflicts", None),
            "no_match": getattr(progress, "no_match", None),
            "skipped": getattr(progress, "skipped", None),
            "cache_hits": getattr(progress, "cache_hits", None),
        }
        rec.end(
            StageProjectId.p2,
            output={
                "row_results": len(getattr(job, "results", None) or []),
                "progress": progress_out,
            },
        )
        rec.end(
            StageProjectId.p4,
            output={
                "conflict_policy": _enum_value(getattr(job, "conflict_policy", None)),
                "confidence_min": getattr(job, "confidence_min", None),
                "verified": getattr(progress, "verified", None),
                "conflicts": getattr(progress, "conflicts", None),
                "filled": getattr(progress, "filled", None),
            },
        )
        rec.end(
            StageProjectId.p6,
            output={
                "status": status,
                "filled": getattr(progress, "filled", None),
                "result_count": getattr(job, "result_count", None)
                or len(getattr(job, "results", None) or []),
                "note": (
                    "staged for review (conflicts held)"
                    if status == "review"
                    else None
                ),
            },
        )
        rec.end(
            StageProjectId.p0,
            output={
                "status": status,
                "progress": progress_out,
                "cost": getattr(job, "cost", None),
                "error": getattr(job, "error", None),
            },
        )
        for pid, reason in _ENRICHMENT_SKIP_REASONS.items():
            rec.skip(pid, reason=reason)
        if getattr(job, "stage_trace", None) is not None:
            job.stage_trace.status = status
            job.stage_trace.summary = {
                "type_name": getattr(job, "type_name", None),
                "attributes": list(getattr(job, "attributes", None) or []),
                "tier": _enum_value(getattr(job, "tier", None)),
                "progress": progress_out,
                "cost": getattr(job, "cost", None),
                "status": status,
            }
    except Exception:  # pragma: no cover - never block enrichment on obs
        pass


def stamp_enrichment_run_failed(job: Any, error: str) -> None:
    """Honest terminal-failed stage_trace for enrichment. Never raises.

    Ends every non-terminal project so mid-run failures never leave P2/P4/P6
    stuck as ``running`` on a failed job.
    """
    try:
        rec = attach_recorder(job)
        if rec is None:
            return
        err = (error or "enrichment failed")[:500]
        for p in list(job.stage_trace.projects if job.stage_trace else []):
            if p.status in (StageStatus.running, StageStatus.pending):
                rec.end(p.project_id, error=err, status=StageStatus.failed)
        rec.end(StageProjectId.p0, error=err, status=StageStatus.failed)
        for pid, reason in _ENRICHMENT_SKIP_REASONS.items():
            rec.skip(pid, reason=reason)
        if getattr(job, "stage_trace", None) is not None:
            job.stage_trace.status = "failed"
            job.stage_trace.summary = {
                "error": err,
                "type_name": getattr(job, "type_name", None),
                "attributes": list(getattr(job, "attributes", None) or []),
            }
    except Exception:  # pragma: no cover - never block enrichment on obs
        pass


def stamp_enrichment_run_cancelled(job: Any) -> None:
    """Close open projects when an enrichment job is cancelled. Never raises."""
    try:
        rec = attach_recorder(job)
        if rec is None:
            return
        for p in list(job.stage_trace.projects if job.stage_trace else []):
            if p.status in (StageStatus.running, StageStatus.pending):
                rec.end(
                    p.project_id,
                    output={"status": "cancelled"},
                    status=StageStatus.completed,
                )
        rec.end(
            StageProjectId.p0,
            output={"status": "cancelled"},
            status=StageStatus.completed,
        )
        for pid, reason in _ENRICHMENT_SKIP_REASONS.items():
            rec.skip(pid, reason=reason)
        if getattr(job, "stage_trace", None) is not None:
            job.stage_trace.status = "cancelled"
            job.stage_trace.summary = {
                "status": "cancelled",
                "type_name": getattr(job, "type_name", None),
            }
    except Exception:  # pragma: no cover
        pass


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
        rec = attach_recorder(job)
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
        rec = attach_recorder(job)
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
            return StageTraceRecorder(job.stage_trace)
        except Exception:
            return None
    return open_job_stage_trace(job)
