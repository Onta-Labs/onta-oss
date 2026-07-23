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

from pydantic import BaseModel, Field


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
    projects.append(p1)

    # --- P2 Extract ---------------------------------------------------------
    p2 = empty_project(StageProjectId.p2, status=StageStatus.skipped)
    p2.reconstructed = True
    progress = getattr(job, "progress", None)
    if category in ("discovery", "enrichment") or progress is not None:
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
    if category in ("discovery", "enrichment", "dedupe", "reconciliation") or progress:
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
    # Query/ask jobs are not always EnrichJobs; leave skipped unless thread_id
    # suggests chat-kicked work that might have answered.
    if category not in ("discovery", "enrichment", "dedupe", "reconciliation"):
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
# Contract-shaped artifact summaries (A1 / A2 / A3 / A6) for operator UI
# --------------------------------------------------------------------------- #
# Notion Stage Contracts name the inter-stage artifacts A0–A10. Live discovery
# instrumentation stamps compact, JSON-safe summaries of those shapes onto
# StageProjectTrace.input / .output so the Job Trace page can render "what P1
# emitted (A1)", "what P2 emitted (A2)", etc. without dumping full payloads.
# Keep samples capped — these ride on the job jsonb row.


_SAMPLE_CAP = 8


def _cap_list(values: Any, *, n: int = _SAMPLE_CAP) -> list[Any]:
    if not values:
        return []
    out: list[Any] = []
    for v in values:
        if v is None or v == "":
            continue
        out.append(v)
        if len(out) >= n:
            break
    return out


def summarize_a1_source_bundle(bundle: Any) -> dict[str, Any]:
    """Compact A1 Source Bundle summary for P1 output / P2 input.

    Accepts a :class:`~cograph_client.pipeline.source_bundle.SourceBundle` or any
    duck-typed object with ``envelope`` / ``rows`` / ``secret_refs``. Never raises
    on partial shapes — returns whatever fields are available.
    """
    envelope = getattr(bundle, "envelope", None)
    rows = list(getattr(bundle, "rows", None) or ())
    secret_refs = list(getattr(bundle, "secret_refs", None) or ())
    tiers: list[str] = []
    providers: list[str] = []
    source_urls: list[str] = []
    fact_ids: list[str] = []
    for r in rows:
        t = getattr(r, "tier", None) or (r.get("tier") if isinstance(r, dict) else None)
        if t and t not in tiers:
            tiers.append(str(t))
        p = getattr(r, "provider", None) or (
            r.get("provider") if isinstance(r, dict) else None
        )
        if p and p not in providers:
            providers.append(str(p))
        su = getattr(r, "source_url", None) or (
            r.get("source_url") if isinstance(r, dict) else None
        )
        if su:
            source_urls.append(str(su))
        fid = getattr(r, "fact_id", None) or (
            r.get("fact_id") if isinstance(r, dict) else None
        )
        if fid:
            fact_ids.append(str(fid))
    return {
        "artifact": "A1",
        "name": "Source Bundle",
        "run_id": getattr(envelope, "run_id", None)
        or getattr(bundle, "run_id", None),
        "workspace_id": getattr(envelope, "workspace_id", None)
        or getattr(bundle, "workspace_id", None),
        "root_fact_id": getattr(envelope, "fact_id", None),
        "row_count": len(rows),
        "providers": providers,
        "tiers": tiers,
        "secret_refs": secret_refs,  # logical refs only (SourceBundle invariant)
        "source_urls_sample": _cap_list(source_urls),
        "fact_ids_sample": _cap_list(fact_ids),
    }


def merge_a1_summaries(
    acc: Optional[dict[str, Any]], piece: dict[str, Any]
) -> dict[str, Any]:
    """Fold one per-batch A1 summary into a run-level A1 aggregate for P1 end."""
    if not acc:
        base = dict(piece)
        base["bundles_emitted"] = 1
        base["row_count"] = int(piece.get("row_count") or 0)
        return base
    out = dict(acc)
    out["bundles_emitted"] = int(out.get("bundles_emitted") or 0) + 1
    out["row_count"] = int(out.get("row_count") or 0) + int(piece.get("row_count") or 0)
    # Prefer the first non-empty run identity (one run_id per discovery run).
    for k in ("run_id", "workspace_id", "root_fact_id"):
        if not out.get(k) and piece.get(k):
            out[k] = piece[k]
    for list_key in ("providers", "tiers", "secret_refs"):
        seen = list(out.get(list_key) or [])
        for v in piece.get(list_key) or []:
            if v not in seen:
                seen.append(v)
        out[list_key] = seen
    for sample_key in ("source_urls_sample", "fact_ids_sample"):
        out[sample_key] = _cap_list(
            list(out.get(sample_key) or []) + list(piece.get(sample_key) or [])
        )
    out["artifact"] = "A1"
    out["name"] = "Source Bundle"
    return out


def summarize_a2_candidates(
    *,
    entities_extracted: int = 0,
    entities_resolved: int = 0,
    source_row_count: int = 0,
    focus_type: Optional[str] = None,
    focus_attributes: Optional[list[str]] = None,
    run_id: Optional[str] = None,
    soft_typed: bool = True,
    evidence_linked: bool = True,
    structured_fastpath: bool = False,
    batches: int = 0,
) -> dict[str, Any]:
    """Compact A2 Candidate Facts summary for P2 output."""
    return {
        "artifact": "A2",
        "name": "Candidate Facts",
        "entities_extracted": int(entities_extracted or 0),
        "entities_resolved": int(entities_resolved or 0),
        "source_row_count": int(source_row_count or 0),
        "focus_type": focus_type,
        "focus_attributes": list(focus_attributes or [])[:40],
        "soft_typed": bool(soft_typed),
        "evidence_linked": bool(evidence_linked),
        "structured_fastpath": bool(structured_fastpath),
        "run_id": run_id,
        "batches": int(batches or 0),
    }


def summarize_a3_clean_report(report: Any) -> Optional[dict[str, Any]]:
    """Compact A3 Clean Facts summary from a CleanReport-like ledger.

    Returns ``None`` when there is no ledger / total is 0 — caller should
    ``skip`` P3 rather than complete an empty clean stage.
    """
    if report is None:
        return None
    counts: Optional[dict[str, Any]] = None
    if hasattr(report, "counts") and callable(report.counts):
        try:
            counts = dict(report.counts())
        except Exception:  # pragma: no cover
            counts = None
    if counts is None and isinstance(report, dict):
        counts = {
            "passed": len(report.get("passed") or []),
            "transformed": len(report.get("transformed") or []),
            "dropped": len(report.get("dropped") or []),
            "total": int(report.get("total") or 0)
            or (
                len(report.get("passed") or [])
                + len(report.get("transformed") or [])
                + len(report.get("dropped") or [])
            ),
        }
    if counts is None:
        # Duck-typed attribute access
        passed = list(getattr(report, "passed", None) or [])
        transformed = list(getattr(report, "transformed", None) or [])
        dropped = list(getattr(report, "dropped", None) or [])
        counts = {
            "passed": len(passed),
            "transformed": len(transformed),
            "dropped": len(dropped),
            "total": len(passed) + len(transformed) + len(dropped),
        }
    if int(counts.get("total") or 0) <= 0:
        return None
    # Sample drop reasons for the operator UI (not full CleanFact dumps).
    drop_reasons: list[str] = []
    dropped_items = getattr(report, "dropped", None)
    if dropped_items is None and isinstance(report, dict):
        dropped_items = report.get("dropped")
    for fact in list(dropped_items or [])[:20]:
        reason = getattr(fact, "reason", None) or (
            fact.get("reason") if isinstance(fact, dict) else None
        )
        if reason and reason not in drop_reasons:
            drop_reasons.append(str(reason))
        if len(drop_reasons) >= _SAMPLE_CAP:
            break
    transform_sample: list[dict[str, Any]] = []
    transformed_items = getattr(report, "transformed", None)
    if transformed_items is None and isinstance(report, dict):
        transformed_items = report.get("transformed")
    for fact in list(transformed_items or [])[:_SAMPLE_CAP]:
        raw = getattr(fact, "raw_value", None) or (
            fact.get("raw_value") if isinstance(fact, dict) else None
        )
        clean = getattr(fact, "clean_value", None) or (
            fact.get("clean_value") if isinstance(fact, dict) else None
        )
        attr = getattr(fact, "attribute", None) or (
            fact.get("attribute") if isinstance(fact, dict) else None
        )
        transform_sample.append(
            {"attribute": attr, "raw": raw, "clean": clean}
        )
    return {
        "artifact": "A3",
        "name": "Clean Facts",
        "counts": counts,
        "drop_reasons_sample": drop_reasons,
        "transforms_sample": transform_sample,
    }


def merge_a3_counts(
    acc: Optional[dict[str, int]], piece: Optional[dict[str, Any]]
) -> Optional[dict[str, int]]:
    """Sum partition counts across micro-batch clean ledgers."""
    if piece is None:
        return acc
    counts = piece.get("counts") if "counts" in piece else piece
    if not counts:
        return acc
    if acc is None:
        return {
            "passed": int(counts.get("passed") or 0),
            "transformed": int(counts.get("transformed") or 0),
            "dropped": int(counts.get("dropped") or 0),
            "total": int(counts.get("total") or 0),
        }
    return {
        "passed": int(acc.get("passed") or 0) + int(counts.get("passed") or 0),
        "transformed": int(acc.get("transformed") or 0)
        + int(counts.get("transformed") or 0),
        "dropped": int(acc.get("dropped") or 0) + int(counts.get("dropped") or 0),
        "total": int(acc.get("total") or 0) + int(counts.get("total") or 0),
    }


def summarize_a6_graph_delta(
    *,
    graph_delta: Any = None,
    run_id: Optional[str] = None,
    instance_graph: Optional[str] = None,
    entities_written: int = 0,
    triples_inserted: int = 0,
    status: Optional[str] = None,
) -> dict[str, Any]:
    """Compact A6 Graph Delta summary for P6 output.

    ``graph_delta`` may be a :class:`~cograph_client.graph.kg_writer.GraphDelta`,
    its ``to_dict()`` form, or ``None`` (then only counters are emitted).
    """
    gd = graph_delta
    if gd is not None and hasattr(gd, "to_dict") and not isinstance(gd, dict):
        try:
            gd = gd.to_dict()
        except Exception:  # pragma: no cover
            gd = None
    facts: list[Any] = []
    fan_in: list[Any] = []
    gd_run_id = run_id
    gd_graph = instance_graph
    if isinstance(gd, dict):
        facts = list(gd.get("facts") or [])
        fan_in = list(gd.get("fan_in") or [])
        gd_run_id = gd.get("run_id") or run_id
        gd_graph = gd.get("instance_graph") or instance_graph
    return {
        "artifact": "A6",
        "name": "Graph Delta",
        "run_id": gd_run_id,
        "instance_graph": gd_graph,
        "fact_count": len(facts),
        "fan_in_count": len(fan_in),
        "entities_written": int(entities_written or 0),
        "triples_inserted": int(triples_inserted or 0),
        "status": status,
        # Tiny sample so operators can see shape without dumping the full delta.
        "facts_sample": _cap_list(facts, n=3),
    }
