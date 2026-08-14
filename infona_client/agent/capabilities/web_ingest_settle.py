"""Terminal settle for a discovery run: finish, fail, one refresh.

Writes already landed via resolver → insert_facts. This module only
settles the job and calls ``refresh_after_write`` once.
"""
from __future__ import annotations

from typing import Optional

from infona_client.enrichment.models import JobErrorItem, JobStatus
from infona_client.retrieval.errors import RetrievalError
from infona_client.agent.capabilities import web_ingest_cap as _wic
from infona_client.agent.capabilities.web_ingest_job import (
    _build_stage_contracts,
    _fail_billing_job,
    _fail_job,
    _finish_job,
)

async def _settle_discovery_run(
    *,
    ctx,
    job,
    job_store,
    instance_graph,
    kg_name,
    ensemble,
    subqueries,
    query,
    proposed_type,
    attributes,
    run_envelope,
    provider,
    acc: dict,
) -> None:
    """Terminal finish / fail / refresh. One ``refresh_after_write`` on success."""
    processed = acc["processed"]
    entities_total = acc["entities_total"]
    affected_types = acc["affected_types"]
    platforms = acc["platforms"]
    plogs = acc["plogs"]
    errors_total = acc["errors_total"]
    last_provider_err = acc["last_provider_err"]
    last_err_provider = acc["last_err_provider"]
    any_discover_ok = acc["any_discover_ok"]
    fatal_llm_err = acc["fatal_llm_err"]
    fatal_ceiling_err = acc["fatal_ceiling_err"]
    a1_acc = acc["a1_acc"]
    a1_rows_dropped = acc["a1_rows_dropped"]
    a1_cells_scrubbed = acc["a1_cells_scrubbed"]
    a1_drop_reasons = acc["a1_drop_reasons"]
    role_drops = acc["role_drops"]
    identity_merges = acc["identity_merges"]
    a2_extracted = acc["a2_extracted"]
    a2_resolved = acc["a2_resolved"]
    a2_source_rows = acc["a2_source_rows"]
    a2_batches = acc["a2_batches"]
    a2_structured_batches = acc["a2_structured_batches"]
    a3_counts = acc["a3_counts"]
    a3_drop_reasons = acc["a3_drop_reasons"]
    a3_transforms_sample = acc["a3_transforms_sample"]
    a4_verified_count = acc["a4_verified_count"]
    a6_fact_count = acc["a6_fact_count"]
    a6_fan_in_count = acc["a6_fan_in_count"]
    a6_triples = acc["a6_triples"]
    a6_facts_sample = acc["a6_facts_sample"]
    a6_run_id = acc["a6_run_id"]
    a6_instance_graph = acc["a6_instance_graph"]

    # FATAL run-level abort: a billing/auth failure (402/401, ONTA-201)
    # or a cost-ceiling breach (ONTA-282). Fail the WHOLE job with the
    # clear, user-facing message, recording rows-landed vs rows-lost so
    # the run is NEVER presented as complete when batches were dropped to
    # a systemic backend error or the spend envelope. This precedes the
    # normal roll-up because it is a run-level abort, not a per-provider
    # outcome. Both flow through _fail_billing_job → halt_from_exception,
    # which classifies the reason kind (billing / cost_ceiling) from the
    # error type — so a ceiling halt reads "cost envelope exceeded", not
    # "provider exhaustion".
    _fatal_run_err: Optional[RetrievalError] = (
        fatal_llm_err or fatal_ceiling_err
    )
    if _fatal_run_err is not None:
        for plog in plogs.values():
            plog.status = (
                "error" if plog.attempts and not plog.matches
                else ("ok" if plog.matches else "skipped")
            )
        await _fail_billing_job(
            job, job_store, list(plogs.values()), _fatal_run_err,
            processed=processed, platforms=platforms,
        )
        return

    for plog in plogs.values():
        # Roll-up per the ProviderLog contract: "skipped" = named but
        # never consulted (cap filled before its turn), NOT no_match.
        if plog.attempts == 0:
            plog.status = "skipped"
        elif plog.matches:
            plog.status = "ok"
        elif plog.errors:
            plog.status = "error"
        else:
            plog.status = "no_match"
    if processed == 0:
        if errors_total and last_provider_err is not None:
            # Nothing landed AND something errored (every discover
            # died, or the found rows could not be ingested) → a
            # failed job carrying the attributed error, not a silent
            # empty success.
            if job is not None:
                job.provider_logs = list(plogs.values())
                job.error_summary = [
                    JobErrorItem(
                        # provider set only when a DISCOVER died;
                        # a job-side (ingest) failure carries
                        # kind="job" with no provider blamed.
                        provider=last_err_provider,
                        kind="error" if last_err_provider else "job",
                        message=last_provider_err[:300],
                    )
                ]
            await _fail_job(job, job_store, last_provider_err)
            return
        _wic.logger.info(
            "web_ingest_no_rows", query=query,
            kg_name=kg_name or None, instance_graph=instance_graph,
        )
        if job is not None and job_store is not None:
            job.provider_logs = list(plogs.values())
        await _finish_job(
            job,
            job_store,
            processed=0,
            entities=0,
            platforms=platforms,
            stage_contracts=_build_stage_contracts(
                a1_acc=a1_acc,
                a1_rows_dropped=a1_rows_dropped,
                a1_cells_scrubbed=a1_cells_scrubbed,
                a1_drop_reasons=a1_drop_reasons,
                role_drops=role_drops,
                identity_merges=identity_merges,
                a2_extracted=a2_extracted,
                a2_resolved=a2_resolved,
                a2_source_rows=a2_source_rows,
                a2_batches=a2_batches,
                a2_structured_batches=a2_structured_batches,
                a3_counts=a3_counts,
                a3_drop_reasons=a3_drop_reasons,
                a3_transforms_sample=a3_transforms_sample,
                a4_verified_count=a4_verified_count,
                a6_fact_count=a6_fact_count,
                a6_fan_in_count=a6_fan_in_count,
                a6_triples=a6_triples,
                a6_facts_sample=a6_facts_sample,
                a6_run_id=a6_run_id,
                a6_instance_graph=a6_instance_graph,
                entities_written=0,
                focus_type=proposed_type,
                focus_attributes=list(attributes),
                run_id=run_envelope.run_id,
            ),
        )
        return
    _wic.logger.info(
        "web_ingest_complete",
        query=query,
        subqueries=len(subqueries),
        providers=[pr.name for pr in ensemble],
        rows=processed,
        entities=entities_total,
        types=sorted(affected_types) or None,
        # The graph the rows actually landed in — pair this with the row
        # count so "N filled" is always attributable to a concrete graph.
        kg_name=kg_name or None,
        instance_graph=instance_graph,
    )
    # Single shared post-write housekeeping path (graph/kg_writer.py) —
    # the SAME refresh ingestion + enrichment run: invalidate the
    # NL-planning ontology cache, re-embed affected types (new types +
    # types that gained an attribute), and recompute Explorer type-stats.
    # ONE refresh for the whole fan-out (not per batch): the union of
    # affected types is what downstream caches care about. Best-effort:
    # a refresh hiccup must NOT present as a failed ingest — the data +
    # ontology already landed.
    try:
        await _wic.refresh_after_write(
            ctx.neptune,
            tenant_id=ctx.tenant_id,
            kg_name=kg_name,
            affected_types=affected_types,
        )
    except Exception:  # noqa: BLE001 — refresh failure must not fail a landed ingest
        _wic.logger.warning("web_ingest_refresh_failed", exc_info=True)
    if job is not None:
        # Settle the rolling estimate to the exact final count.
        job.progress.total = processed
    await _finish_job(
        job,
        job_store,
        processed=processed,
        entities=entities_total,
        platforms=platforms,
        stage_contracts=_build_stage_contracts(
            a1_acc=a1_acc,
            a1_rows_dropped=a1_rows_dropped,
            a1_cells_scrubbed=a1_cells_scrubbed,
            a1_drop_reasons=a1_drop_reasons,
            role_drops=role_drops,
            identity_merges=identity_merges,
            a2_extracted=a2_extracted,
            a2_resolved=a2_resolved,
            a2_source_rows=a2_source_rows,
            a2_batches=a2_batches,
            a2_structured_batches=a2_structured_batches,
            a3_counts=a3_counts,
            a3_drop_reasons=a3_drop_reasons,
            a3_transforms_sample=a3_transforms_sample,
            a4_verified_count=a4_verified_count,
            a6_fact_count=a6_fact_count,
            a6_fan_in_count=a6_fan_in_count,
            a6_triples=a6_triples,
            a6_facts_sample=a6_facts_sample,
            a6_run_id=a6_run_id,
            a6_instance_graph=a6_instance_graph,
            entities_written=entities_total,
            focus_type=proposed_type,
            focus_attributes=list(attributes),
            run_id=run_envelope.run_id,
        ),
    )
