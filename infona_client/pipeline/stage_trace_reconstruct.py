"""Reconstruct / resolve a job stage trace from EnrichJob fields.

Look up sibling / facade names via :func:`_host` so tests that monkeypatch
``infona_client.pipeline.stage_trace.<name>`` keep working.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

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
    p0 = _host().empty_project(StageProjectId.p0, status=StageStatus.reconstructed)
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
        "started_at": _host()._iso(getattr(job, "started_at", None)),
        "completed_at": _host()._iso(getattr(job, "completed_at", None)),
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
    p1 = _host().empty_project(StageProjectId.p1, status=StageStatus.skipped)
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
                meta=_host()._provider_log_meta(pl),
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
            "skip_reason": (
                "file is A1-like entry (source provided); Find Data not on this rail"
            ),
        }

    projects.append(p1)

    # --- P2 Extract ---------------------------------------------------------
    p2 = _host().empty_project(StageProjectId.p2, status=StageStatus.skipped)
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
            "progress": _host()._progress_dict(progress),
            "result_count": getattr(job, "result_count", None),
            "row_results": len(getattr(job, "results", None) or []),
        }
    projects.append(p2)

    # --- P3 Clean -----------------------------------------------------------
    p3 = _host().empty_project(StageProjectId.p3, status=StageStatus.skipped)
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
    p4 = _host().empty_project(StageProjectId.p4, status=StageStatus.skipped)
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
    p5 = _host().empty_project(StageProjectId.p5, status=StageStatus.skipped)
    p5.reconstructed = True
    if getattr(job, "type_name", None):
        p5.status = StageStatus.reconstructed
        p5.input = {"type_name": job.type_name, "attributes": getattr(job, "attributes", None)}
        p5.actions = [StageAction(name="type_resolve", detail=f"target type {job.type_name}")]
        p5.output = {"type_name": job.type_name}
    projects.append(p5)

    # --- P6 Write -----------------------------------------------------------
    p6 = _host().empty_project(StageProjectId.p6, status=StageStatus.skipped)
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
    p7 = _host().empty_project(StageProjectId.p7, status=StageStatus.skipped)
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
    p8 = _host().empty_project(StageProjectId.p8, status=StageStatus.skipped)
    p8.reconstructed = True
    trigger = getattr(getattr(job, "trigger", None), "value", None)
    if trigger == "scheduled":
        p8.status = StageStatus.reconstructed
        p8.input = {"trigger": "scheduled", "next_run": _host()._iso(getattr(job, "next_run", None))}
        p8.actions = [StageAction(name="scheduled_refresh", detail="scheduled trigger")]
    projects.append(p8)

    # --- P9 Surfaces --------------------------------------------------------
    p9 = _host().empty_project(StageProjectId.p9, status=StageStatus.skipped)
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
        projects=_host().ensure_all_projects(projects),
        summary={
            "type_name": getattr(job, "type_name", None),
            "attributes": getattr(job, "attributes", None),
            "result_count": getattr(job, "result_count", None),
            "cost": getattr(job, "cost", None),
            "error": getattr(job, "error", None),
            "thread_id": getattr(job, "thread_id", None),
            "platforms": getattr(job, "platforms", None),
            "progress": _host()._progress_dict(progress),
        },
        recorded_at=_host()._now(),
    )


def resolve_trace(job: Any) -> JobStageTrace:
    """Return the best available stage trace for a job.

    Prefer live ``job.stage_trace`` (fill any still-skipped slots from the
    reconstructor so the page always has P0–P9). Fall back to pure reconstruct.
    """
    live = getattr(job, "stage_trace", None)
    if live is None:
        return _host().reconstruct_from_job(job)

    # live may be a dict (from older json) or a JobStageTrace
    if isinstance(live, dict):
        live = JobStageTrace.model_validate(live)
    elif not isinstance(live, JobStageTrace):
        try:
            live = JobStageTrace.model_validate(live)
        except Exception:
            return _host().reconstruct_from_job(job)

    reconstructed = _host().reconstruct_from_job(job)
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
            merged.append(_host().empty_project(pid))

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
        projects=_host().ensure_all_projects(merged),
        summary={**reconstructed.summary, **(live.summary or {})},
        recorded_at=live.recorded_at or _host()._now(),
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
