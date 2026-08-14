"""Discovery job settle: stage-trace contracts, finish, fail.

Observability only — never writes instance facts. A refresh hiccup after
a landed ingest is handled in the write mixin, not here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from infona_client.enrichment.models import (
    EnrichJob,
    JobErrorItem,
    JobStatus,
    ProviderLog,
)
from infona_client.pipeline.manifest import HaltReasonKind
from infona_client.pipeline.stage_trace import (
    StageProjectId,
    StageStatus,
    attach_recorder,
    finalize_job_stage_trace,
    summarize_a2_candidates,
    summarize_a6_graph_delta,
)
from infona_client.retrieval.errors import RetrievalError
from infona_client.agent.capabilities import web_ingest_cap as _wic

def _build_stage_contracts(
    *,
    a1_acc: Optional[dict],
    a1_rows_dropped: int = 0,
    a1_cells_scrubbed: int = 0,
    a1_drop_reasons: Optional[list] = None,
    role_drops: int = 0,
    identity_merges: int = 0,
    a2_extracted: int,
    a2_resolved: int,
    a2_source_rows: int,
    a2_batches: int,
    a2_structured_batches: int,
    a3_counts: Optional[dict],
    a3_drop_reasons: list,
    a3_transforms_sample: list,
    a4_verified_count: int,
    a6_fact_count: int,
    a6_fan_in_count: int,
    a6_triples: int,
    a6_facts_sample: list,
    a6_run_id: Optional[str],
    a6_instance_graph: Optional[str],
    entities_written: int,
    focus_type: Optional[str],
    focus_attributes: list,
    run_id: Optional[str],
) -> dict:
    """Assemble terminal Notion-contract-shaped I/O for P1/P2/P3/P6 (ONTA-385)."""
    a1 = dict(a1_acc or {})
    if a1:
        a1.setdefault("artifact", "A1")
        a1.setdefault("name", "Source Bundle")
        a1.setdefault("run_id", run_id)
    else:
        a1 = {
            "artifact": "A1",
            "name": "Source Bundle",
            "run_id": run_id,
            "row_count": 0,
            "bundles_emitted": 0,
        }
    # A1 validators (ONTA-393): surface the nav-chrome / type-invalid rejections on
    # the terminal A1 contract only when there were any, so a clean run's contract is
    # byte-identical to pre-393.
    if a1_rows_dropped or a1_cells_scrubbed:
        a1["rows_dropped"] = int(a1_rows_dropped)
        a1["cells_scrubbed"] = int(a1_cells_scrubbed)
        a1["drop_reasons_sample"] = list(a1_drop_reasons or [])[:8]
    # Structural quality gates (ONTA-465 / WS6): domain-agnostic counters.
    if role_drops:
        a1["role_drops"] = int(role_drops)
    if identity_merges:
        a1["identity_merges"] = int(identity_merges)
    a2 = summarize_a2_candidates(
        entities_extracted=a2_extracted,
        entities_resolved=a2_resolved,
        source_row_count=a2_source_rows,
        focus_type=focus_type,
        focus_attributes=focus_attributes,
        run_id=run_id,
        soft_typed=True,
        evidence_linked=True,
        structured_fastpath=a2_structured_batches > 0
        and a2_structured_batches == a2_batches
        and a2_batches > 0,
        batches=a2_batches,
    )
    a3: Optional[dict] = None
    if a3_counts and int(a3_counts.get("total") or 0) > 0:
        a3 = {
            "artifact": "A3",
            "name": "Clean Facts",
            "counts": a3_counts,
            "drop_reasons_sample": list(a3_drop_reasons)[:8],
            "transforms_sample": list(a3_transforms_sample)[:8],
        }
    a4: Optional[dict] = None
    if a4_verified_count > 0:
        a4 = {
            "artifact": "A4",
            "name": "Verified Facts",
            "verified_count": a4_verified_count,
        }
    a6 = {
        "artifact": "A6",
        "name": "Graph Delta",
        "run_id": a6_run_id or run_id,
        "instance_graph": a6_instance_graph,
        "fact_count": a6_fact_count,
        "fan_in_count": a6_fan_in_count,
        "entities_written": entities_written,
        "triples_inserted": a6_triples,
        "status": "applied",
        "facts_sample": list(a6_facts_sample)[:3],
    }
    return {"a1": a1, "a2": a2, "a3": a3, "a4": a4, "a6": a6}


async def _finish_job(
    job: Optional[EnrichJob],
    job_store,
    *,
    processed: int,
    entities: int,
    platforms: list[str],
    stage_contracts: Optional[dict] = None,
) -> None:
    """Mark a discovery job applied with its result count + final progress."""
    if job is None or job_store is None:
        return
    now = datetime.now(timezone.utc)
    job.progress.processed = processed
    job.progress.filled = entities
    # Settle the rolling ``total`` estimate to the exact processed count on EVERY
    # terminal-applied path (ONTA-238). The non-empty happy path settles it just
    # before calling this; the EMPTY (0-row) path does not, so without this a
    # completed-empty job would keep the early ``total = cap`` seed and read as a
    # misleading ``0/200`` (looks unfinished) instead of ``0/0``. Settling here
    # makes the invariant caller-independent.
    job.progress.total = processed
    # Terminal phase (ONTA-238): a completed job reads "done", so a client that
    # keyed a spinner off the phase can retire it. Paired with the terminal
    # ``applied`` status + ``result_count``, a completed-EMPTY run (0 records) is
    # now fully distinguishable from a still-running one — same terminal status,
    # phase "done", result_count 0, progress 0/0 — instead of looking identical
    # to "running".
    job.progress.phase = "done"
    job.result_count = entities
    if platforms:
        job.platforms = platforms
    job.status = JobStatus.applied
    # A9 manifest: settle to a terminal COMPLETED state. complete() collapses the
    # seeded cap denominator down to what actually ran, so a clean run reads
    # "N of N — complete", never "N of cap — dropped".
    if job.manifest is not None:
        job.manifest.complete()
    job.completed_at = now
    job.last_run = now
    try:
        rec = attach_recorder(job)
        if rec is not None:
            contracts = stage_contracts or {}
            a1 = contracts.get("a1") or {
                "artifact": "A1",
                "name": "Source Bundle",
                "row_count": processed,
            }
            a2 = contracts.get("a2") or summarize_a2_candidates(
                entities_resolved=entities,
                source_row_count=processed,
                focus_type=job.type_name,
                focus_attributes=list(job.attributes or []),
            )
            a3 = contracts.get("a3")
            a4 = contracts.get("a4")
            a6 = contracts.get("a6") or summarize_a6_graph_delta(
                entities_written=entities,
                status="applied",
            )

            # P1 Find → A1 Source Bundle
            rec.end(
                StageProjectId.p1,
                output={
                    **a1,
                    "result_count": entities,
                    "platforms": platforms,
                    "processed": processed,
                },
            )
            # P2 Extract → A2 Candidate Facts
            rec.end(
                StageProjectId.p2,
                output={
                    **a2,
                    "processed": processed,
                    "entities_written": entities,
                },
            )
            # P3 Clean → A3 Clean Facts (complete only when a clean ledger ran).
            # Use end(..., skipped) so a mid-run begin(P3) cannot leave P3 running.
            if a3 and int((a3.get("counts") or {}).get("total") or 0) > 0:
                rec.end(StageProjectId.p3, output=a3)
            else:
                rec.end(
                    StageProjectId.p3,
                    status=StageStatus.skipped,
                    output={
                        "skip_reason": (
                            "no A3 clean ledger on this run "
                            "(empty ingest or clean fused with zero values)"
                        )
                    },
                )
            # P4 Verify → A4 (default-OFF; complete only when verdicts present)
            if a4 and int(a4.get("verified_count") or 0) > 0:
                rec.end(StageProjectId.p4, output=a4)
            else:
                rec.end(
                    StageProjectId.p4,
                    status=StageStatus.skipped,
                    output={
                        "skip_reason": (
                            "verify default-OFF on discovery path (no A4 verdicts)"
                        )
                    },
                )
            # P5 Ontology / Placement stays fused into resolver ingest
            rec.end(
                StageProjectId.p5,
                status=StageStatus.skipped,
                output={
                    "skip_reason": (
                        "type placement happens inside resolver ingest (no separate A5)"
                    )
                },
            )
            # P6 Write → A6 Graph Delta
            rec.end(
                StageProjectId.p6,
                output={
                    **a6,
                    "entities_written": entities,
                    "status": "applied",
                },
            )
            rec.end(
                StageProjectId.p0,
                output={
                    "status": "applied",
                    "result_count": entities,
                    "processed": processed,
                    "platforms": platforms,
                    "cost": job.cost,
                    "run_id": a1.get("run_id") or a6.get("run_id"),
                },
            )
            # Rails not on the discovery write path stay skipped.
            for pid, reason in (
                (StageProjectId.p7, "answer rail not on discovery jobs"),
                (StageProjectId.p8, "not a refresh-delta run"),
                (StageProjectId.p9, "surface is the Jobs UI; no A10 on this path"),
            ):
                rec.skip(pid, reason=reason)
            # ONTA-394: entity fan-out ratio (a2 extracted / a1 rows). Surfaced on
            # the trace summary + warned when high, so soft-extract amplification is
            # never silent. Observability only — nothing is dropped here.
            _a1_rows = a1.get("row_count") or 0
            _a2_extracted = a2.get("entities_extracted") or 0
            _fanout_ratio = (
                round(_a2_extracted / _a1_rows, 2) if _a1_rows else None
            )
            _fanout_high = bool(
                _fanout_ratio is not None
                and _fanout_ratio > _wic._DISCOVERY_FANOUT_WARN_RATIO
            )
            if _fanout_high:
                _wic.logger.warning(
                    "discovery_high_entity_fanout",
                    job_id=getattr(job, "id", None),
                    a1_row_count=_a1_rows,
                    a2_entities_extracted=_a2_extracted,
                    fanout_ratio=_fanout_ratio,
                    threshold=_wic._DISCOVERY_FANOUT_WARN_RATIO,
                )
            _summary = {
                "result_count": entities,
                "processed": processed,
                "platforms": platforms,
                "type_name": job.type_name,
                "attributes": job.attributes,
                "cost": job.cost,
                "a1_row_count": a1.get("row_count"),
                "a2_entities_extracted": a2.get("entities_extracted"),
                "entity_fanout_ratio": _fanout_ratio,
                "entity_fanout_high": _fanout_high,
                "a3_counts": (a3 or {}).get("counts") if a3 else None,
                "a6_fact_count": a6.get("fact_count"),
                "run_id": a1.get("run_id") or a6.get("run_id"),
            }
            # Structural quality counters (ONTA-465) — only when non-zero so a
            # clean run's summary stays compact / back-compat.
            if a1.get("role_drops"):
                _summary["role_drops"] = a1["role_drops"]
            if a1.get("identity_merges"):
                _summary["identity_merges"] = a1["identity_merges"]
            job.stage_trace.summary = _summary
            job.stage_trace.status = "applied"
            # Safety sweep (ONTA-388): end any leftover running/pending stages.
            finalize_job_stage_trace(
                job,
                terminal_status="applied",
                summary={
                    "result_count": entities,
                    "processed": processed,
                    "platforms": platforms,
                    "type_name": job.type_name,
                    "attributes": job.attributes,
                    "cost": job.cost,
                },
            )
    except Exception:
        _wic.logger.warning(
            "stage_trace_finish_failed",
            job_id=getattr(job, "id", None),
            exc_info=True,
        )
    await job_store.update(job)


def _finalize_stage_trace_failed(
    job: EnrichJob,
    error: str,
    *,
    summary: Optional[dict] = None,
) -> None:
    """Stamp an honest terminal-failed stage_trace (delegates to pipeline helper).

    Ends every non-terminal project so mid-run failures never leave P2/P6 stuck
    as ``running`` on a failed job. Isolated in try/except inside the shared
    helper so operator observability cannot fail the discovery write path.
    """
    finalize_job_stage_trace(
        job,
        terminal_status="failed",
        error=error,
        summary={"type_name": getattr(job, "type_name", None), **(summary or {})},
    )


async def _fail_job(job: Optional[EnrichJob], job_store, error: str) -> None:
    """Mark a discovery job failed, carrying a (truncated) error for the UI."""
    if job is None or job_store is None:
        return
    now = datetime.now(timezone.utc)
    job.status = JobStatus.failed
    job.progress.phase = "failed"
    job.error = (error or "discovery failed")[:500]
    # A9 manifest: terminal FAILED with the reason. Any planned items not completed
    # are rolled into `dropped`, so coverage shows the partial honestly.
    if job.manifest is not None:
        job.manifest.halt(HaltReasonKind.error, job.error)
    job.completed_at = now
    job.last_run = now
    _finalize_stage_trace_failed(job, job.error)
    await job_store.update(job)


async def _fail_billing_job(
    job: Optional[EnrichJob],
    job_store,
    provider_logs: list[ProviderLog],
    error: RetrievalError,
    *,
    processed: int,
    platforms: list[str],
) -> None:
    """Fail a discovery job on a FATAL run-level abort — an LLM billing/auth error
    (402/401, ONTA-201) OR a cost-ceiling breach (ONTA-282) — recording HONEST
    PARTIALS.

    Unlike :func:`_fail_job`, this fires when the run ABORTED mid-way: the shared
    LLM backend went unbillable/unauthorized, or the run reached its HARD per-run
    spend envelope. Either way some batches may already have landed, so the
    terminal state must reflect rows-LANDED vs rows-LOST — never a silent
    "complete". ``halt_from_exception`` derives the manifest's reason KIND from the
    error type (``billing`` / ``cost_ceiling``), so a ceiling abort reads "cost
    envelope exceeded", not "provider exhaustion". We stamp:

    * the clear, user-facing ``error`` message (top up / rotate the key);
    * the per-provider logs so the run detail still shows what each source did;
    * an ``error_summary`` ``JobErrorItem`` (``kind="job"`` — a run-level backend
      failure, not any one provider's fault) whose message names the rows that DID
      land, so the partial is explicit;
    * ``progress`` settled to what actually landed (processed == filled).
    """
    if job is None or job_store is None:
        return
    now = datetime.now(timezone.utc)
    landed = (
        f" {processed} record(s) were ingested before the failure; "
        "the remaining batches were not processed."
        if processed
        else " No records were ingested."
    )
    message = f"{error}{landed}"
    job.status = JobStatus.failed
    job.progress.phase = "failed"
    job.error = message[:500]
    job.provider_logs = list(provider_logs)
    job.error_summary = [
        JobErrorItem(provider=None, kind="job", message=message[:300])
    ]
    # Settle the rolling estimate to the exact partial count — the run is NOT
    # complete, but the count of what survived must be honest.
    job.progress.processed = processed
    job.progress.filled = processed
    job.result_count = processed
    if platforms:
        job.platforms = platforms
    # A9 manifest: terminal FAILED with a PROVIDER-EXHAUSTION reason (402 billing /
    # sustained-429 rate-limit) and honest partial coverage. `completed` already
    # tracks the landed rows (recorded per micro-batch); `halt_from_exception`
    # rolls the unfilled planned remainder into `dropped` and stamps the reason.
    if job.manifest is not None:
        job.manifest.halt_from_exception(error, landed_note=landed)
    job.completed_at = now
    job.last_run = now
    _finalize_stage_trace_failed(
        job,
        job.error or str(error),
        summary={
            "processed": processed,
            "platforms": platforms,
            "result_count": processed,
            "halt": "billing_or_cost_ceiling",
        },
    )
    await job_store.update(job)


async def _mark_discovery_running(
    *,
    ctx,
    job,
    job_store,
    instance_graph,
    kg_name,
    ensemble,
    subqueries,
    cap,
    proposed_type,
) -> None:
    """Flip the job to running, seed progress, log the write target."""
    if job is not None and job_store is not None:
        job.status = JobStatus.running
        job.started_at = datetime.now(timezone.utc)
        job.progress.total = cap
        job.progress.phase = "searching"
        if job.manifest is not None:
            job.manifest.start(total=cap)
        try:
            rec = attach_recorder(job)
            if rec is not None:
                rec.action(
                    StageProjectId.p0,
                    "start_run",
                    detail=f"status=running total_cap={cap}",
                )
                rec.action(
                    StageProjectId.p1,
                    "searching",
                    detail="phase=searching",
                )
        except Exception:
            _wic.logger.warning(
                "stage_trace_start_failed",
                job_id=getattr(job, "id", None),
                exc_info=True,
            )
        await job_store.update(job)
    _wic.logger.info(
        "web_ingest_run_start",
        tenant=ctx.tenant_id,
        kg_name=kg_name or None,
        instance_graph=instance_graph,
        providers=[pr.name for pr in ensemble],
        subqueries=len(subqueries),
        cap=cap,
        proposed_type=proposed_type,
        job_id=job.id if job is not None else None,
    )
    if instance_graph is None:
        _wic.logger.warning(
            "web_ingest_no_target_kg",
            tenant=ctx.tenant_id,
            detail=(
                "kg_name is empty; instance data will land in the tenant "
                "base graph and will NOT be visible in any per-KG Explorer "
                "view. The run likely lost its KG context upstream."
            ),
            job_id=job.id if job is not None else None,
        )
