"""Live enrichment stage-trace stamps (ONTA-387).

Look up sibling / facade names via :func:`_host` so tests that monkeypatch
``infona_client.pipeline.stage_trace.<name>`` keep working.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog

from infona_client.pipeline.stage_trace_models import StageProjectId, StageStatus

logger = structlog.stdlib.get_logger("infona.pipeline.stage_trace")


def _host():
    from infona_client.pipeline import stage_trace as _mod

    return _mod

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
        rec = _host().attach_recorder(job)
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
        rec = _host().attach_recorder(job)
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
        rec = _host().attach_recorder(job)
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
        rec = _host().attach_recorder(job)
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
        rec = _host().attach_recorder(job)
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
        rec = _host().attach_recorder(job)
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
        rec = _host().attach_recorder(job)
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
