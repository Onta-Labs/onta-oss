"""Write path: ingest a SourceBundle via resolver → insert_facts.

Structured providers use ``ingest_structured_rows``; others use
``resolver.ingest``. One ``refresh_after_write`` per successful run
(in ``_settle_discovery_run``). Do not fork a second writer.
"""
from __future__ import annotations

import json
import math
from typing import Optional

from infona_client.enrichment.models import JobErrorItem, JobStatus
from infona_client.pipeline.stage_trace import (
    StageProjectId,
    attach_recorder,
    merge_a3_counts,
    summarize_a3_clean_report,
)
from infona_client.retrieval.errors import RetrievalError
from infona_client.agent.capabilities import web_ingest_cap as _wic
from infona_client.agent.capabilities.web_ingest_job import (
    _build_stage_contracts,
    _fail_billing_job,
    _fail_job,
    _finish_job,
)
from infona_client.agent.capabilities.web_ingest_project import (
    _chunk_rows,
    _group_rows_by_source_url,
)


async def _ingest_source_bundle(
    *,
    bundle,
    prov,
    resolver,
    ctx,
    query,
    instance_graph,
    proposed_type,
    attributes,
    attributes_exhaustive,
    key_attr,
    run_envelope,
    job,
    job_store,
    sub_i,
    subqueries,
    cap,
    acc: dict,
) -> Optional[object]:
    """Ingest one A1 bundle as citation-homogeneous micro-batches.

    Mutates ``acc`` counters. Returns a fatal ceiling error if the spend
    envelope trips, else ``None``.
    """
    processed = acc["processed"]
    entities_total = acc["entities_total"]
    affected_types = acc["affected_types"]
    a2_batches = acc["a2_batches"]
    a2_structured_batches = acc["a2_structured_batches"]
    a2_extracted = acc["a2_extracted"]
    a2_resolved = acc["a2_resolved"]
    a6_triples = acc["a6_triples"]
    a3_counts = acc["a3_counts"]
    a3_drop_reasons = acc["a3_drop_reasons"]
    a3_transforms_sample = acc["a3_transforms_sample"]
    a4_verified_count = acc["a4_verified_count"]
    a6_fact_count = acc["a6_fact_count"]
    a6_fan_in_count = acc["a6_fan_in_count"]
    a6_facts_sample = acc["a6_facts_sample"]
    a6_run_id = acc["a6_run_id"]
    a6_instance_graph = acc["a6_instance_graph"]
    platforms = acc["platforms"]
    plogs = acc["plogs"]
    fatal_ceiling_err = acc["fatal_ceiling_err"]

    micro_batches = [
        micro
        for group in _group_rows_by_source_url(bundle.rows)
        for micro in _chunk_rows(
            group, _wic._DISCOVERY_INGEST_SUBBATCH
        )
    ]
    # ONTA-272: a provider whose rows are ALREADY structured
    # (keyed by the confirmed attribute set — API-registry
    # pulls, structured captures) commits through the
    # deterministic mapping seam with NO LLM extractor, when
    # the fast-path is enabled and the provider opts in. All
    # other providers keep the byte-for-byte unchanged
    # ``resolver.ingest`` JSON detour below.
    structured_fastpath = (
        _wic._DISCOVERY_STRUCTURED_FASTPATH
        and getattr(prov, "structured", False)
    )
    for micro in micro_batches:
        # ONTA-371: unpack the A1 SourceRows. ``micro_rows``
        # is the row DATA (a snapshot copy of the batch
        # dicts) — byte-identical to what the extractor saw
        # before, so the write stays unchanged.
        # ``micro_fact_ids`` / ``micro_tier`` are the per-row
        # A1 lineage handed off to the resolver (A1→A2). All
        # rows in a bundle share one tier.
        micro_rows = [r.data for r in micro]
        micro_fact_ids = [r.fact_id for r in micro]
        micro_tier = micro[0].tier if micro else None
        if structured_fastpath:
            # Pre-structured rows already carry ``source_url``
            # (stamped above), which becomes the per-record
            # citation + the A2 evidence link. Deterministic:
            # preview == commit, no ``_extract``.
            result = await resolver.ingest_structured_rows(
                micro_rows,
                ctx.tenant_id,
                type_name=proposed_type,
                attributes=list(attributes),
                source=f"web:{prov.name}:{query}",
                instance_graph=instance_graph,
                key_attribute=key_attr,
                # ONTA-372: same run_id as the A1 bundle so
                # the A6 delta keys off ONE run lineage.
                run_id=run_envelope.run_id,
                # ONTA-371: per-row A1 lineage handoff.
                fact_ids=micro_fact_ids,
                tier=micro_tier,
                # ONTA-382: closed field list is a WRITE
                # ceiling on the structured fast-path too
                # (LLM extract already had this; API rows
                # used to invent extra ontology attrs).
                attributes_exhaustive=attributes_exhaustive,
            )
        else:
            content = json.dumps(
                micro_rows, default=str, ensure_ascii=False
            )
            result = await resolver.ingest(
                content,
                ctx.tenant_id,
                content_type="json",
                source=f"web:{prov.name}:{query}",
                instance_graph=instance_graph,
                # Discovery CONFIRMED the target type + attribute
                # set with the user, so it passes them to
                # extraction as a focus. SOFT (default): a PRIOR
                # that keeps extraction compact yet still
                # decomposes faithfully (subtypes, real-world
                # nodes, multi-valued splits) — the ONTA-199
                # follow-up that fixed the flat single-type
                # mis-modeling (NPs typed as Physician,
                # city/specialty as literals) without the
                # open-ended reifier's ~20-type blowup. HARD
                # (kill-switch): the original flat cage.
                constrain_types=[proposed_type],
                constrain_attributes={
                    proposed_type: list(attributes)
                },
                constrain_soft=_wic._DISCOVERY_SOFT_EXTRACT,
                # ONTA-382: exhaustive (closed) attribute
                # set → extraction allowlist/ceiling even
                # under soft mode. Illustrative/open keeps
                # the soft prior-only behavior.
                constrain_attributes_exhaustive=(
                    attributes_exhaustive
                ),
                # ONTA-372: same run_id as the A1 bundle so
                # the resolver keys the A6 Graph Delta off
                # ONE run lineage instead of a fresh uuid4.
                run_id=run_envelope.run_id,
                # ONTA-371: per-row A1 lineage handoff.
                fact_ids=micro_fact_ids,
                tier=micro_tier,
            )
        processed += len(micro)
        entities_total += int(
            getattr(result, "entities_resolved", 0) or 0
        )
        affected_types |= set(result.types_created)
        for attr_added in result.attributes_added:
            affected_types.add(attr_added.split(".")[0])
        # Live stage_trace: fold A2/A3/A4/A6 from this
        # IngestResult (ONTA-385). Isolated so a ledger
        # shape surprise cannot fail the write path.
        try:
            a2_batches += 1
            if structured_fastpath:
                a2_structured_batches += 1
            a2_extracted += int(
                getattr(result, "entities_extracted", 0) or 0
            )
            a2_resolved += int(
                getattr(result, "entities_resolved", 0) or 0
            )
            a6_triples += int(
                getattr(result, "triples_inserted", 0) or 0
            )
            a3_piece = summarize_a3_clean_report(
                getattr(result, "clean_report", None)
            )
            if a3_piece is not None:
                a3_counts = merge_a3_counts(a3_counts, a3_piece)
                for r in a3_piece.get("drop_reasons_sample") or []:
                    if r not in a3_drop_reasons:
                        a3_drop_reasons.append(r)
                for t in a3_piece.get("transforms_sample") or []:
                    if len(a3_transforms_sample) < 8:
                        a3_transforms_sample.append(t)
            verified = getattr(result, "verified_facts", None) or []
            a4_verified_count += len(verified)
            gd = getattr(result, "graph_delta", None)
            if gd is not None:
                gd_d = (
                    gd.to_dict()
                    if hasattr(gd, "to_dict")
                    and not isinstance(gd, dict)
                    else gd
                )
                if isinstance(gd_d, dict):
                    facts = list(gd_d.get("facts") or [])
                    fan = list(gd_d.get("fan_in") or [])
                    a6_fact_count += len(facts)
                    a6_fan_in_count += len(fan)
                    if gd_d.get("run_id"):
                        a6_run_id = gd_d.get("run_id")
                    if gd_d.get("instance_graph"):
                        a6_instance_graph = gd_d.get(
                            "instance_graph"
                        )
                    for f in facts:
                        if len(a6_facts_sample) < 3:
                            a6_facts_sample.append(f)
            if job is not None:
                rec = attach_recorder(job)
                if rec is not None:
                    rec.action(
                        StageProjectId.p2,
                        "extract_batch",
                        detail=(
                            f"entities_extracted="
                            f"{getattr(result, 'entities_extracted', 0)} "
                            f"resolved="
                            f"{getattr(result, 'entities_resolved', 0)}"
                        ),
                        meta={
                            "entities_extracted": getattr(
                                result, "entities_extracted", 0
                            ),
                            "entities_resolved": getattr(
                                result, "entities_resolved", 0
                            ),
                            "structured_fastpath": structured_fastpath,
                            "micro_rows": len(micro),
                        },
                    )
                    if a3_piece is not None:
                        rec.begin(
                            StageProjectId.p3,
                            input={
                                "artifact": "A2",
                                "name": "Candidate Facts",
                                "contract_consumes": (
                                    "A2 Candidate Facts"
                                ),
                                "contract_emits": "A3 Clean Facts",
                            },
                        )
                        rec.action(
                            StageProjectId.p3,
                            "clean_ledger",
                            detail=(
                                "A3 counts="
                                f"{a3_piece.get('counts')}"
                            ),
                            meta=a3_piece.get("counts") or {},
                        )
                    if verified:
                        rec.begin(
                            StageProjectId.p4,
                            input={
                                "artifact": "A3",
                                "name": "Clean Facts",
                                "verified_batch": len(verified),
                            },
                        )
                        rec.action(
                            StageProjectId.p4,
                            "verify",
                            detail=f"{len(verified)} A4 verdicts",
                        )
                    _gd_facts = 0
                    if isinstance(gd, dict):
                        _gd_facts = len(gd.get("facts") or [])
                    elif gd is not None and hasattr(gd, "facts"):
                        _gd_facts = len(gd.facts)
                    rec.action(
                        StageProjectId.p6,
                        "write_batch",
                        detail=(
                            f"triples="
                            f"{getattr(result, 'triples_inserted', 0)} "
                            f"entities="
                            f"{getattr(result, 'entities_resolved', 0)}"
                        ),
                        meta={
                            "triples_inserted": getattr(
                                result, "triples_inserted", 0
                            ),
                            "entities_resolved": getattr(
                                result, "entities_resolved", 0
                            ),
                            "graph_delta_facts": _gd_facts,
                        },
                    )
        except Exception:
            _wic.logger.warning(
                "stage_trace_ingest_fold_failed",
                job_id=getattr(job, "id", None)
                if job is not None
                else None,
                exc_info=True,
            )
        # A9 manifest: this micro-batch's rows LANDED —
        # record them as completed items so a later halt can
        # say exactly how many of the planned cap made it in
        # before the failure (honest partial coverage).
        if job is not None and job.manifest is not None:
            for _row in micro:
                # ONTA-371: ``_row`` is an A1 SourceRow now;
                # its snapshot ``data`` carries the key value.
                job.manifest.record_completed(
                    str(_row.data.get(key_attr, ""))
                )
            # A9 cost envelope (ONTA-282): the paid
            # provider spend for this run landed on the
            # manifest above; if cumulative spend has now
            # reached the HARD per-run ceiling, ABORT
            # CLEANLY — set the fatal flag and break out of
            # the micro-batch loop. The run then settles via
            # _fail_billing_job (terminal `failed`,
            # `cost_ceiling` kind, honest partial coverage),
            # never a silent overspend. None/0 ⇒ never trips.
            _ceiling_err = job.manifest.check_ceiling()
            if _ceiling_err is not None:
                fatal_ceiling_err = _ceiling_err
                break
        if job is not None and job_store is not None:
            # Rolling, honest total: what landed + the
            # average per-sub-query yield extrapolated over
            # the sub-queries still to run, never above the
            # cap. Settles to == processed at the end.
            # ``filled`` is the persona's success signal —
            # it MUST move mid-run, so we set it to the
            # entities resolved so far after each sub-batch
            # (it was previously written ONLY at
            # _finish_job, so it read 0 the whole session).
            subs_done = sub_i + 1
            subs_left = len(subqueries) - subs_done
            avg = math.ceil(processed / subs_done)
            job.progress.processed = processed
            job.progress.filled = entities_total
            job.progress.total = min(
                cap, processed + subs_left * avg
            )
            job.platforms = platforms
            job.provider_logs = list(plogs.values())
            await job_store.update(job)
    # The batch went through end-to-end (no 429/throttle) —
    # a successful call breaks any pending rate-limit streak.
    # A9 cost envelope (ONTA-282): the ceiling tripped inside
    # the micro-batch loop — abort the provider fan-out too;
    # every remaining call would only spend past the envelope.

    acc["processed"] = processed
    acc["entities_total"] = entities_total
    acc["affected_types"] = affected_types
    acc["a2_batches"] = a2_batches
    acc["a2_structured_batches"] = a2_structured_batches
    acc["a2_extracted"] = a2_extracted
    acc["a2_resolved"] = a2_resolved
    acc["a6_triples"] = a6_triples
    acc["a3_counts"] = a3_counts
    acc["a3_drop_reasons"] = a3_drop_reasons
    acc["a3_transforms_sample"] = a3_transforms_sample
    acc["a4_verified_count"] = a4_verified_count
    acc["a6_fact_count"] = a6_fact_count
    acc["a6_fan_in_count"] = a6_fan_in_count
    acc["a6_facts_sample"] = a6_facts_sample
    acc["a6_run_id"] = a6_run_id
    acc["a6_instance_graph"] = a6_instance_graph
    acc["platforms"] = platforms
    acc["fatal_ceiling_err"] = fatal_ceiling_err
    return fatal_ceiling_err


