"""Discovery run loop: fetch → project → write.

Writes via resolver ingest → insert_facts / refresh_after_write. BYOR.
"""
from __future__ import annotations

import math
from typing import Optional
from urllib.parse import urlparse

from infona_client.enrichment.models import JobErrorItem, JobStatus, ProviderLog
from infona_client.graph.suppression import fetch_suppressed_entities
from infona_client.pipeline.source_scope import merge_provider_context
from infona_client.pipeline.stage_trace import StageProjectId, attach_recorder
from infona_client.retrieval.errors import LLMError, RateLimitEscalator, is_rate_limit_status
from infona_client.resolver.llm_router import OPENROUTER_BASE
from infona_client.web_sources.base import provider_accepts, provider_cost
from infona_client.agent.capabilities import web_ingest_cap as _wic
from infona_client.agent.capabilities.web_ingest_fetch import (
    _record_locate_trace,
    _record_provider_skip,
    _record_requests,
)
from infona_client.agent.capabilities.web_ingest_job import (
    _fail_job,
    _mark_discovery_running,
)
from infona_client.agent.capabilities.web_ingest_plan_preview import _build_resolver
from infona_client.agent.capabilities.web_ingest_project_batch import (
    _project_discovered_batch,
)
from infona_client.agent.capabilities.web_ingest_settle import _settle_discovery_run
from infona_client.agent.capabilities.web_ingest_write import _ingest_source_bundle

async def _run_discovery_inner(
    *,
    ctx,
    job,
    job_store,
    instance_graph,
    kg_name,
    ensemble,
    subqueries,
    cap,
    hint_columns,
    attributes,
    attributes_exhaustive,
    proposed_type,
    urls,
    query,
    pctx,
    ontology_lock,
    provider,
    run_id,
    run_envelope,
) -> None:
    await _mark_discovery_running(
        ctx=ctx,
        job=job,
        job_store=job_store,
        instance_graph=instance_graph,
        kg_name=kg_name,
        ensemble=ensemble,
        subqueries=subqueries,
        cap=cap,
        proposed_type=proposed_type,
    )
    # Per-provider activity log for "which providers we used" + their
    # outcomes, surfaced in the run-detail view alongside the platforms
    # list — one entry PER ENSEMBLE MEMBER, each accumulating its own
    # attempts/matches/errors across every sub-query.
    plogs: dict[str, ProviderLog] = {
        prov.name: ProviderLog(provider=prov.name) for prov in ensemble
    }
    any_discover_ok = False
    processed = 0  # unique rows ingested across all sub-queries/providers
    entities_total = 0
    affected_types: set[str] = set()
    platforms: list[str] = []
    # Live stage_trace contract accumulators (ONTA-385): A1/A2/A3/A6
    # summaries folded across every SourceBundle + ingest micro-batch so
    # the terminal P1/P2/P3/P6 outputs are Notion-contract-shaped.
    a1_acc: Optional[dict] = None
    # A1 validators (ONTA-393): run-level tallies of nav-chrome rows dropped
    # and type-invalid cells scrubbed at the A1 boundary, surfaced on the
    # terminal A1 contract so the Job Trace stays honest about them.
    a1_rows_dropped = 0
    a1_cells_scrubbed = 0
    a1_drop_reasons: list[str] = []
    # Structural quality counters (ONTA-465 / WS6): role-membership drops
    # + catalog-path / near-dup identity merges. Surfaced on stage_trace
    # actions and the terminal A1 contract / summary.
    role_drops = 0
    identity_merges = 0
    a2_extracted = 0
    a2_resolved = 0
    a2_source_rows = 0
    a2_batches = 0
    a2_structured_batches = 0
    a3_counts: Optional[dict] = None
    a3_drop_reasons: list[str] = []
    a3_transforms_sample: list[dict] = []
    a4_verified_count = 0
    a6_fact_count = 0
    a6_fan_in_count = 0
    a6_triples = 0
    a6_facts_sample: list = []
    a6_run_id: Optional[str] = run_envelope.run_id
    a6_instance_graph: Optional[str] = instance_graph
    # Cross-batch dedupe on the KEY attribute (the record identifier):
    # sub-query partitions overlap ("…in Tustin" and a directory row
    # listed under both cities) and so do ENSEMBLE members (the same
    # physician on Places AND a directory page) — re-ingesting the same
    # key would double-write. Specialized runs first, so its (more
    # structured) row wins; the general provider only contributes NEW keys.
    seen_keys: set[str] = set()
    # Cross-batch structural identity (catalog-path + surface forms) so a
    # later scrape display-name cannot re-mint a model already written
    # from an authoritative catalog API earlier in the same job.
    seen_identity_keys: set[str] = set()
    # Once any batch has catalog-path rows, later brand-only batches still
    # get sparse-self-role drops (live incident: Vapi scrape after OpenRouter).
    job_catalog_inventory = False
    key_attr = (attributes[0] if attributes else "name") or "name"
    acc = {
        "job_catalog_inventory": False,
        "seen_identity_keys": seen_identity_keys,
        "a1_rows_dropped": 0,
        "a1_cells_scrubbed": 0,
        "a1_drop_reasons": a1_drop_reasons,
        "role_drops": 0,
        "identity_merges": 0,
        "a1_acc": None,
        "a2_source_rows": 0,
        "processed": 0,
        "entities_total": 0,
        "affected_types": affected_types,
        "platforms": platforms,
        "plogs": plogs,
        "a2_extracted": 0,
        "a2_resolved": 0,
        "a2_batches": 0,
        "a2_structured_batches": 0,
        "a3_counts": None,
        "a3_drop_reasons": a3_drop_reasons,
        "a3_transforms_sample": a3_transforms_sample,
        "a4_verified_count": 0,
        "a6_fact_count": 0,
        "a6_fan_in_count": 0,
        "a6_triples": 0,
        "a6_facts_sample": a6_facts_sample,
        "a6_run_id": a6_run_id,
        "a6_instance_graph": a6_instance_graph,
        "errors_total": 0,
        "last_provider_err": None,
        "last_err_provider": None,
        "any_discover_ok": False,
        "fatal_llm_err": None,
        "fatal_ceiling_err": None,
    }
    # ONTA-345: entity-level RE-ACQUISITION guard. Consult the STICKY
    # suppression / tombstone list ONCE per run (batched — one query, then
    # an O(1) set-membership check per row below), so an ERASED entity is
    # never silently re-minted by discovery/refresh (the P1 'never
    # re-acquire erased data' rule; GDPR erasure blast radius). A row whose
    # would-be canonical subject is on this list is DROPPED post-dedupe,
    # BEFORE the SourceBundle is built and BEFORE resolver.ingest*, so a
    # suppressed entity never enters the bundle and never reaches the
    # writer. Best-effort + empty when there is no target KG — a suppression
    # read must never fail the run.
    suppressed_entities = await fetch_suppressed_entities(
        ctx.neptune, instance_graph
    )
    last_provider_err: Optional[str] = None
    last_err_provider: Optional[str] = None
    errors_total = 0
    # Set when a FATAL billing/auth error (402/401) aborts the run mid-way
    # (ONTA-201). Carries the clear, user-facing message out of the nested
    # sub-query/provider loops so we can fail the WHOLE job honestly —
    # rows-landed vs rows-lost — instead of swallowing it as one failed
    # batch and reporting "complete".
    fatal_llm_err: Optional[LLMError] = None
    # A9 cost envelope (ONTA-282): set when the run crosses its HARD per-run
    # spend ceiling mid-flight. A GOVERNANCE halt (not provider exhaustion),
    # but routed through the SAME abort-and-settle path as a 402
    # (_fail_billing_job → halt_from_exception) so the terminal state carries
    # an honest partial (rows-landed vs rows-dropped) instead of a silent
    # overspend. A parallel flag (not fatal_llm_err) keeps the proven
    # billing path untouched.
    fatal_ceiling_err = None
    # 429 policy (ONTA-273): a single rate-limit blip is a transient the
    # per-batch degrade retries; only SUSTAINED 429s (a run throttled to a
    # standstill) escalate to a run-level halt. This run-scoped escalator
    # draws that line — a non-429 outcome resets the streak, and only once
    # it crosses the threshold does it return a fatal LLMRateLimitError we
    # route through the SAME billing-halt machinery below.
    rate_escalator = RateLimitEscalator()
    # Each (sub-query, provider) call is bounded to the per-sub-query row
    # share the plan PRICED (cost = n_sub × pages(cap / n_sub)). Passing
    # the whole remaining cap instead let overlapping sub-queries spend up
    # to n_sub× the quoted estimate — the figure the ≤gate auto-confirm
    # trusted (adversarial-review F2).
    per_sub_budget = math.ceil(cap / max(1, len(subqueries)))
    try:
        for sub_i, sub_query in enumerate(subqueries):
            if cap - processed <= 0:
                break
            # ONTA-268: a fresh resolver PER sub-query (cheap — keeps no
            # cross-request state), all sharing the job's one ontology-write
            # lock. Per-sub-query resolvers eliminate the shared per-ingest
            # state that made a single reused resolver non-reentrant; the
            # shared lock keeps their ontology mutations serialized.
            resolver = _build_resolver(ctx, ontology_lock=ontology_lock)
            # ONTA-459: once per sub-query (not per provider) — structural
            # source_constraint from ensemble providers' own metadata.
            sub_pctx = merge_provider_context(pctx, sub_query, ensemble)
            for prov in ensemble:
                remaining = cap - processed
                if remaining <= 0:
                    break
                plog = plogs[prov.name]
                # ONTA-461 / R3 — provider_accepts only; no brand ifs.
                if not provider_accepts(prov, sub_query, sub_pctx):
                    _record_provider_skip(
                        job, prov.name, sub_query, reason="out_of_scope"
                    )
                    continue
                # The WHOLE batch (discover → dedupe → ingest) is guarded:
                # one provider returning garbage, or one batch failing to
                # ingest, must not sink batches already landed — partial
                # coverage beats nothing (adversarial-review F3).
                plog.attempts += 1
                phase = "discover"
                # User-facing progress phase (ONTA-238): each provider
                # iteration starts by SEARCHING the web (the phase flips to
                # "ingesting" once rows are found, above). Re-set per batch
                # so after an ingest the next batch's search reads honestly.
                if job is not None and job_store is not None:
                    job.progress.phase = "searching"
                    await job_store.update(job)
                try:
                    full = await prov.discover(
                        sub_query,
                        sample=False,
                        max_rows=min(per_sub_budget, remaining),
                        hint_columns=hint_columns,
                        context=sub_pctx,
                        urls=urls or None,
                    )
                    any_discover_ok = True
                    # A9 cost envelope (ONTA-282): a paid provider request
                    # was actually issued — feed its cost into the manifest's
                    # spend-to-date (once per discover call, matching how
                    # _estimate_cost prices a request). The ceiling is then
                    # checked as rows land in the micro-batch loop below. A
                    # free provider adds $0 (provider_cost → 0.0).
                    if job is not None and job.manifest is not None:
                        _paid, _cost_per_call = provider_cost(prov)
                        if _cost_per_call > 0.0:
                            job.manifest.add_spend(_cost_per_call)
                    phase = "ingest"
                    rows_found = list(getattr(full, "rows", None) or [])[
                        : min(per_sub_budget, remaining)
                    ]
                    # matches = rows the provider FOUND (pre-dedupe): a
                    # provider whose 50 finds were all already contributed
                    # by an earlier member still shows matches=50, not the
                    # "ran but found nothing" no_match the model reserves
                    # for genuinely empty results (adversarial-review F4).
                    plog.matches += len(rows_found)
                    # Request-level trace (API-source providers only):
                    # record every HTTP request this discover() issued so
                    # the run-detail view can show the requests + their
                    # payloads/statuses/record-counts. A request that
                    # returned zero rows is still worth showing, so this
                    # runs BEFORE the no-match continue below.
                    _record_requests(plog, getattr(full, "calls", None))
                    # ONTA-391: surface the provider's locate→select→fetch
                    # step counts as P1 stage-trace actions — BEFORE the
                    # no-match continue, so even a page-minimising run that
                    # located nothing (or pages with no rows) shows its
                    # locate/fetch work + skip reason. A provider that doesn't
                    # locate+scrape leaves locate_trace None → no-op.
                    _record_locate_trace(
                        job,
                        getattr(full, "locate_trace", None),
                        prov.name,
                        sub_query,
                    )
                    if not rows_found:
                        # Distinguish hard locate/API failure from a clean
                        # empty (dogfood 802b2672): when the provider stamps
                        # locate_errors (HTTP 4xx/5xx / transport on Parallel
                        # or Gemini), count as error + last_error so Job Trace
                        # / provider_logs show status=error, not silent
                        # no_match. Soft empty (searched OK, no list page)
                        # stays no_match.
                        lt = getattr(full, "locate_trace", None) or {}
                        locate_errs = [
                            str(e)
                            for e in (lt.get("locate_errors") or [])
                            if e
                        ]
                        hard_err = None
                        if locate_errs:
                            hard_err = "; ".join(locate_errs)[:300]
                        elif getattr(full, "error", None) and str(
                            full.error
                        ).startswith("locate APIs failed"):
                            hard_err = str(full.error)[:300]
                        if hard_err:
                            plog.errors += 1
                            plog.last_error = hard_err
                            last_provider_err = hard_err
                            last_err_provider = prov.name
                            errors_total += 1
                        else:
                            plog.no_match += 1
                        continue
                    bundle = await _project_discovered_batch(
                        rows_found=rows_found,
                        key_attr=key_attr,
                        seen_keys=seen_keys,
                        provenance=getattr(full, "provenance", None) or {},
                        proposed_type=proposed_type,
                        suppressed_entities=suppressed_entities,
                        full=full,
                        prov=prov,
                        job=job,
                        job_store=job_store,
                        attributes=attributes,
                        ctx=ctx,
                        run_id=run_id,
                        sub_query=sub_query,
                        kg_name=kg_name,
                        instance_graph=instance_graph,
                        run_envelope=run_envelope,
                        acc=acc,
                    )
                    if bundle is None:
                        continue
                    job_catalog_inventory = acc["job_catalog_inventory"]
                    seen_identity_keys = acc["seen_identity_keys"]
                    a1_rows_dropped = acc["a1_rows_dropped"]
                    a1_cells_scrubbed = acc["a1_cells_scrubbed"]
                    a1_drop_reasons = acc["a1_drop_reasons"]
                    role_drops = acc["role_drops"]
                    identity_merges = acc["identity_merges"]
                    a1_acc = acc["a1_acc"]
                    a2_source_rows = acc["a2_source_rows"]
                    platforms = acc["platforms"]

                    fatal_ceiling_err = await _ingest_source_bundle(
                        bundle=bundle,
                        prov=prov,
                        resolver=resolver,
                        ctx=ctx,
                        query=query,
                        instance_graph=instance_graph,
                        proposed_type=proposed_type,
                        attributes=attributes,
                        attributes_exhaustive=attributes_exhaustive,
                        key_attr=key_attr,
                        run_envelope=run_envelope,
                        job=job,
                        job_store=job_store,
                        sub_i=sub_i,
                        subqueries=subqueries,
                        cap=cap,
                        acc=acc,
                    )
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
                    rate_escalator.record_success()
                    if fatal_ceiling_err is not None:
                        break

                except LLMError as exc:
                    # FATAL, SYSTEMIC LLM-backend failure (402 billing /
                    # 401 auth) surfaced by the extraction call inside
                    # resolver.ingest (ONTA-201). It WILL recur on every
                    # remaining chunk/sub-query, so aborting the whole run
                    # now is the honest, cheap answer — NOT swallowing it
                    # as one failed batch (`web_ingest_subquery_failed`)
                    # and letting the run report "complete". Record it and
                    # break out of BOTH loops; the terminal state below
                    # reflects rows-landed vs rows-lost.
                    fatal_llm_err = exc
                    _wic.logger.error(
                        "web_ingest_llm_backend_fatal",
                        query=sub_query,
                        provider=prov.name,
                        phase=phase,
                        processed=processed,
                        error=str(exc),
                    )
                    break
                except Exception as exc:  # noqa: BLE001 — one batch
                    # 429 policy (ONTA-273): a rate-limit response is NOT a
                    # per-batch failure to attribute — it is a transient the
                    # escalator counts. A single/occasional 429 falls through
                    # to the per-batch degrade below (retry the next batch);
                    # only a SUSTAINED streak returns a fatal error we route
                    # through the billing-halt machinery (fail-fast, honest
                    # partials) instead of spinning on doomed calls.
                    _status = getattr(
                        getattr(exc, "response", None), "status_code", None
                    )
                    if _status is not None and is_rate_limit_status(_status):
                        _rate_fatal = rate_escalator.record_rate_limited(
                            provider="openrouter",
                            host=urlparse(OPENROUTER_BASE).hostname,
                            detail=str(exc)[:120],
                        )
                        if _rate_fatal is not None:
                            fatal_llm_err = _rate_fatal
                            _wic.logger.error(
                                "web_ingest_llm_backend_fatal",
                                query=sub_query,
                                provider=prov.name,
                                phase=phase,
                                processed=processed,
                                error=str(_rate_fatal),
                            )
                            break
                    else:
                        # Any non-429 outcome breaks the 429 streak.
                        rate_escalator.record_success()
                    # failing must not sink the run. Attribution follows
                    # the phase: a discover crash is the PROVIDER's; an
                    # ingest/bookkeeping crash after a clean discover is
                    # a JOB-side error — the provider log is never
                    # mis-blamed for it.
                    last_provider_err = str(exc)
                    errors_total += 1
                    if phase == "discover":
                        last_err_provider = prov.name
                        plog.errors += 1
                        plog.last_error = last_provider_err[:300]
                    else:
                        last_err_provider = None
                    _wic.logger.warning(
                        "web_ingest_subquery_failed",
                        query=sub_query,
                        provider=prov.name,
                        phase=phase,
                        exc_info=True,
                    )
                    continue
            # A fatal billing/auth error (402/401) OR a cost-ceiling breach
            # (ONTA-282) broke the inner provider loop — abort the whole
            # sub-query fan-out too; every remaining call would fail
            # identically (402) or only overspend (ceiling). The terminal
            # FAILED state (with honest partials) is set below.
            if fatal_llm_err is not None or fatal_ceiling_err is not None:
                break

        # Push loop locals into acc for the shared settle path.
        acc.update({
            "processed": processed,
            "entities_total": entities_total,
            "affected_types": affected_types,
            "platforms": platforms,
            "plogs": plogs,
            "errors_total": errors_total,
            "last_provider_err": last_provider_err,
            "last_err_provider": last_err_provider,
            "any_discover_ok": any_discover_ok,
            "fatal_llm_err": fatal_llm_err,
            "fatal_ceiling_err": fatal_ceiling_err,
            "a1_acc": a1_acc,
            "a1_rows_dropped": a1_rows_dropped,
            "a1_cells_scrubbed": a1_cells_scrubbed,
            "a1_drop_reasons": a1_drop_reasons,
            "role_drops": role_drops,
            "identity_merges": identity_merges,
            "a2_extracted": a2_extracted,
            "a2_resolved": a2_resolved,
            "a2_source_rows": a2_source_rows,
            "a2_batches": a2_batches,
            "a2_structured_batches": a2_structured_batches,
            "a3_counts": a3_counts,
            "a3_drop_reasons": a3_drop_reasons,
            "a3_transforms_sample": a3_transforms_sample,
            "a4_verified_count": a4_verified_count,
            "a6_fact_count": a6_fact_count,
            "a6_fan_in_count": a6_fan_in_count,
            "a6_triples": a6_triples,
            "a6_facts_sample": a6_facts_sample,
            "a6_run_id": a6_run_id,
            "a6_instance_graph": a6_instance_graph,
        })
        await _settle_discovery_run(
            ctx=ctx,
            job=job,
            job_store=job_store,
            instance_graph=instance_graph,
            kg_name=kg_name,
            ensemble=ensemble,
            subqueries=subqueries,
            query=query,
            proposed_type=proposed_type,
            attributes=attributes,
            run_envelope=run_envelope,
            provider=provider,
            acc=acc,
        )
    except Exception as exc:  # noqa: BLE001 — background job self-contains errors
        _wic.logger.error(
            "web_ingest_failed", query=query,
            kg_name=kg_name or None, instance_graph=instance_graph,
            exc_info=True,
        )
        msg = str(exc)
        # Per-(sub-query, provider) errors are handled in the loop, so a
        # crash HERE is past discovery (ingest/refresh/bookkeeping) — a
        # job-level failure — unless no discover ever returned (setup
        # crash), which stays provider-attributed. Matches the enrichment
        # executor's fatal-path classification.
        if not any_discover_ok:
            primary_plog = plogs[provider.name]
            primary_plog.errors += 1
            primary_plog.status = "error"
            primary_plog.last_error = msg[:300]
        if job is not None:
            job.provider_logs = list(plogs.values())
            job.error_summary = [
                JobErrorItem(
                    provider=provider.name if not any_discover_ok else None,
                    kind="error" if not any_discover_ok else "job",
                    message=msg[:300],
                )
            ]
        await _fail_job(job, job_store, msg)
