"""EnrichmentExecutor.run — select entities, walk adapters, collect rows."""

from __future__ import annotations

import asyncio

from infona_client.api_registry.enrichment import apply_registry_selection
from infona_client.enrichment.executor_const import (
    PROGRESS_FLUSH_EVERY,
    WORKER_POOL_SIZE,
    _MAX_ERROR_MSG,
)
from infona_client.enrichment.executor_helpers import (
    _attr_uri,
    _host,
    _now,
    _strategy_version_with_instructions,
    _values_match_with_strategy,
)
from infona_client.enrichment.executor_select import (
    _extract_bind_attrs,
    _select_entities_via_store,
)
from infona_client.enrichment.executor_tally import _ProviderTally
from infona_client.enrichment.models import EnrichJob, JobErrorItem, JobStatus, RowResult
from infona_client.enrichment.strategy import load_strategy, resolve_type_name, unknown_type_message
from infona_client.enrichment.tiers import get_chain
from infona_client.graph.provenance import (
    attr_provenance_companion_uri,
    legacy_attr_companion_uri,
)
from infona_client.graph.queries import kg_graph_uri
from infona_client.pipeline.manifest import HaltReasonKind, RunManifest, resolve_spend_ceiling
from infona_client.pipeline.stage_trace import (
    stamp_enrichment_entities_selected,
    stamp_enrichment_run_cancelled,
    stamp_enrichment_run_failed,
    stamp_enrichment_run_started,
)


class EnrichmentRunMixin:
    """Job lifecycle: type resolve, select, worker pool, row collection."""

    async def _attach_bind_attrs(
        self,
        job: EnrichJob,
        tenant_id: str,
        strategy,
        entities: list[dict],
        graph_uri: str,
    ) -> None:
        """Pre-load binding-source attributes for ``attribute:<attr>`` recipes."""
        bind_leaves: set[str] = set()
        try:
            chain_names: set[str] = set()
            for _attribute in job.attributes:
                _attr_strategy = strategy.attributes.get(_attribute)
                if _attr_strategy and _attr_strategy.sources:
                    chain_names.update(_attr_strategy.sources)
                elif job.sources:
                    _available = [
                        s for s in job.sources if _host().get_adapter(s) is not None
                    ]
                    chain_names.update(_available if _available else get_chain(job.tier))
                else:
                    chain_names.update(get_chain(job.tier))
            for _name in chain_names:
                _ad = _host().get_adapter(_name)
                bind_leaves |= getattr(_ad, "binding_source_attributes", frozenset())
        except Exception:  # noqa: BLE001 - never break the job over binding setup
            bind_leaves = set()
        if not (bind_leaves and entities):
            return
        _bmap: dict[str, dict[str, str]] = {}
        need_fetch: list[str] = []
        for e in entities:
            from_props = _extract_bind_attrs(
                e.get("props") or {},
                bind_leaves,
                uri=e.get("uri") or "",
                label=e.get("label") or "",
            )
            if from_props:
                _bmap[e["uri"]] = from_props
            elif not (e.get("props") or {}):
                # No stashed props — re-read the store for this URI.
                need_fetch.append(e["uri"])
        if need_fetch:
            try:
                fetched = await self._load_binding_attrs(
                    graph_uri,
                    need_fetch,
                    job.type_name,
                    bind_leaves,
                    tenant_id=tenant_id,
                    kg_name=job.kg_name,
                )
            except Exception:  # noqa: BLE001
                fetched = {}
            for uri, attrs in (fetched or {}).items():
                _bmap.setdefault(uri, {}).update(attrs)
        for e in entities:
            e["bind_attrs"] = _bmap.get(e["uri"], {})

    async def run(self, job: EnrichJob, tenant_id: str) -> None:
        # Per-provider activity + error accumulator for this run; stamped onto the
        # job at every terminal path so the run-detail view shows which providers
        # we used and a summary of the errors hit. Defined before the try so the
        # failure path can still surface whatever was recorded before the crash.
        tally = _ProviderTally()
        # A9 Run Manifest (ONTA-273): make this enrichment run a first-class object
        # so a run halted by provider exhaustion (a 402/sustained-429 from the LLM
        # extraction backend) reaches a TERMINAL failed state with a user-visible
        # reason AND honest partial coverage ("N of M items completed before halt"),
        # instead of a silent partial. Created here if the route did not mint one;
        # settled at every terminal path below. run_id = the job id (the job IS the
        # run). Defined before the try so the failure path can halt it too.
        if job.manifest is None:
            job.manifest = RunManifest(run_id=job.id, stage="enrichment")
        manifest = job.manifest
        # A9 cost envelope (ONTA-282): stamp the HARD per-run spend ceiling before
        # work starts. A per-job override (job.spend_ceiling_usd) wins; else the
        # deployment default (config). None/0 ⇒ unlimited (unchanged behavior). The
        # per-item spend feed below (via _lookup_chain) + the check in
        # process_entity then halt the run cleanly if it crosses this envelope.
        manifest.spend_ceiling_usd = resolve_spend_ceiling(
            getattr(job, "spend_ceiling_usd", None),
            _host().settings.enrich_spend_ceiling_usd,
        )
        manifest.start()
        try:
            job.status = JobStatus.running
            job.started_at = _now()
            # Operator Job Trace (ONTA-387): open live P0/P2/P4/P6 for enrichment.
            stamp_enrichment_run_started(job)
            await self._jobs.update(job)

            # Resolve the target type to the tenant's canonical declared name
            # BEFORE selecting entities. The SELECT keys on ?e a <types/Name>
            # case-sensitively, so a miscased/unknown type would otherwise match
            # zero entities and this run would finish "Completed" having enriched
            # nothing (the reported no-op). Auto-correct a case-insensitive match;
            # fail fast with a clear error for a type that genuinely doesn't
            # exist. This guards EVERY caller of run() (direct enrich, schedules,
            # actions), not just the enrich route. Fail-open: when the ontology
            # read fails or declares no types (known == []) we proceed unchanged.
            canonical, known_types = await resolve_type_name(
                self._neptune, tenant_id, job.type_name
            )
            if known_types and canonical is None:
                job.status = JobStatus.failed
                job.error = unknown_type_message(job.type_name, known_types)
                job.completed_at = _now()
                job.error_summary = [JobErrorItem(kind="job", message=job.error)]
                manifest.halt(HaltReasonKind.error, job.error)
                stamp_enrichment_run_failed(job, job.error)
                await self._jobs.update(job)
                return
            if canonical and canonical != job.type_name:
                job.type_name = canonical
                await self._jobs.update(job)

            # Load ontology-driven strategy. Always returns a TypeStrategy.
            strategy = await load_strategy(self._neptune, tenant_id, job.type_name)
            # Cache-key version for this strategy. A change here auto-invalidates
            # the cache (different key -> clean miss). TODO(ADR-0005 §2): the ADR
            # wants a real strategy_version field on TypeStrategy/AttributeStrategy;
            # derive a stable string until that lands.
            strategy_version = str(getattr(strategy, "version", "v1"))
            # Fold optional custom instructions into the cache version so two
            # different instruction sets never collide on a cached verdict (an
            # agentic adapter can read job.instructions and return a different
            # value). No instructions → unchanged version (clean reuse of the
            # existing cache keys). See _strategy_version_with_instructions.
            strategy_version = _strategy_version_with_instructions(
                strategy_version, job.instructions
            )
            # Track which adapter names were missing so we warn once per job.
            missing_adapter_names: set[str] = set()

            graph_uri = kg_graph_uri(tenant_id, job.kg_name)
            # GraphStore only (ONTA-527). SPARQL HTTP is retired (ONTA-534);
            # a dual-arm that fail-opened into the retired client is how
            # prod jobs finished 50/50 no_match in <1s. Store miss → empty
            # select, logged loudly. Tests seed MemoryGraphStore.
            store_entities = await _select_entities_via_store(
                tenant_id,
                job.kg_name,
                job.type_name,
                job.attributes,
                limit=job.limit,
                scope=job.scope,
                entity_uris=job.entity_uris,
            )
            if store_entities is None:
                _host().logger.error(
                    "enrich_entity_select_no_store",
                    tenant_id=tenant_id,
                    kg_name=job.kg_name,
                    type_name=job.type_name,
                )
                entities = []
            else:
                entities = store_entities

            # Pre-load binding-source attributes for the `attribute:<attr>`
            # enrich_from recipe (ONTA-194 phase 3).
            await self._attach_bind_attrs(job, tenant_id, strategy, entities, graph_uri)

            job.progress.total = len(entities) * len(job.attributes)
            # A9 manifest: the planned item denominator (M) is one item per
            # (entity, attribute) — the same unit progress counts.
            manifest.set_total(job.progress.total)
            stamp_enrichment_entities_selected(
                job,
                entity_count=len(entities),
                item_total=job.progress.total,
            )
            await self._jobs.update(job)

            sem = asyncio.Semaphore(WORKER_POOL_SIZE)
            counter = {"n": 0}
            counter_lock = asyncio.Lock()

            async def process_entity(ent: dict) -> list[RowResult]:
                results: list[RowResult] = []
                async with sem:
                    for attribute in job.attributes:
                        # Cooperative cancellation
                        latest = await self._jobs.get(job.id)
                        if latest and latest.status == JobStatus.cancelled:
                            return results

                        existing = ent["vals"].get(_attr_uri(job.type_name, attribute))
                        # The incumbent value's provenance companions, read from the
                        # same selection (fetched via the extended in_list). Carried
                        # onto a conflict row so both sources are visible for review
                        # (ONTA-246). None when the existing value has no prior
                        # provenance (e.g. an ingested value). Dual-read (ONTA-262):
                        # the attr_meta namespace is current; the legacy attribute-
                        # namespace shape covers KGs written before the migration.
                        existing_source_url = ent["vals"].get(
                            attr_provenance_companion_uri(
                                job.type_name, attribute, "source_url"
                            )
                        ) or ent["vals"].get(
                            legacy_attr_companion_uri(
                                job.type_name, attribute, "source_url"
                            )
                        )
                        existing_verified_at = ent["vals"].get(
                            attr_provenance_companion_uri(
                                job.type_name, attribute, "verified_at"
                            )
                        ) or ent["vals"].get(
                            legacy_attr_companion_uri(
                                job.type_name, attribute, "verified_at"
                            )
                        )
                        attr_strategy = strategy.attributes.get(attribute)

                        # Strategy merge: request value wins; ontology fills gaps.
                        # confidence_min: if ontology specifies one and the
                        # request is at the default (0.85), take the ontology
                        # value. Pragmatic heuristic since EnrichRequest has no
                        # "unset" sentinel.
                        effective_confidence = job.confidence_min
                        if attr_strategy and attr_strategy.confidence_min is not None:
                            if abs(job.confidence_min - 0.85) < 1e-9:
                                effective_confidence = attr_strategy.confidence_min

                        # Adapter chain precedence (most specific wins):
                        #   1. per-attribute ontology strategy sources, then
                        #   2. the request-level job.sources override, then
                        #   3. the tier default chain.
                        # ``chain_from_tier`` marks the branches derived from
                        # get_chain(tier) — the ONLY ones whose registry lead
                        # prefix the scalable selector (ONTA-341) may reshape. An
                        # explicit strategy/job override is the user's exact chain
                        # and is never reshaped.
                        chain_from_tier = False
                        if attr_strategy and attr_strategy.sources:
                            chain = list(attr_strategy.sources)
                        elif job.sources:
                            # Request-level provider override. Keep only names
                            # that resolve to a registered adapter; if the
                            # override names ONLY unavailable providers (e.g. a
                            # premium adapter not registered on this deployment),
                            # fall back to the tier default chain rather than
                            # enriching nothing — matching the UI's "falls back
                            # to Auto if unavailable" promise. A partially-valid
                            # override uses just its available names.
                            available = [
                                s
                                for s in job.sources
                                if _host().get_adapter(s) is not None
                            ]
                            if available:
                                chain = available
                            else:
                                chain = get_chain(job.tier)
                                chain_from_tier = True
                        else:
                            chain = get_chain(job.tier)
                            chain_from_tier = True

                        # ONTA-341: replace the O(N) linear self-gating registry
                        # scan with retrieve-top-K → gate → arbitrate for this
                        # (entity_type, attribute). Identity when the feature flag
                        # is OFF (default) → byte-identical chain. Only applied to
                        # tier-derived chains (never a user override), and it never
                        # raises (returns the chain unchanged on any failure).
                        if chain_from_tier and job.type_name:
                            chain = await apply_registry_selection(
                                chain,
                                job.type_name,
                                attribute,
                                cache_scope=job.tenant_id or "",
                                openrouter_key=_host().settings.openrouter_api_key,
                            )

                        verdicts = await self._lookup_chain(
                            ent["label"],
                            attribute,
                            chain,
                            job,
                            missing_adapter_names,
                            effective_confidence,
                            strategy_version,
                            tally=tally,
                            manifest=manifest,
                            entity_attrs=ent.get("bind_attrs"),
                        )
                        best = self._pick_best(verdicts, effective_confidence)

                        action: str
                        if best is None:
                            action = "no_match"
                        elif existing is None or existing == "":
                            action = "filled"
                        elif _values_match_with_strategy(
                            existing, best.value, attr_strategy
                        ):
                            action = "verified"
                        else:
                            action = "conflict"

                        results.append(
                            RowResult(
                                entity_uri=ent["uri"],
                                attribute=attribute,
                                existing_value=existing,
                                verdict=best,
                                action=action,  # type: ignore[arg-type]
                                existing_source_url=existing_source_url,
                                existing_verified_at=existing_verified_at,
                            )
                        )

                        async with counter_lock:
                            counter["n"] += 1
                            if action == "filled":
                                job.progress.filled += 1
                            elif action == "verified":
                                job.progress.verified += 1
                            elif action == "conflict":
                                job.progress.conflicts += 1
                            elif action == "skipped":
                                job.progress.skipped += 1
                            elif action == "no_match":
                                job.progress.no_match += 1
                            job.progress.processed = counter["n"]
                            # A9 manifest: this (entity, attribute) item was
                            # handled — record it completed so a later halt can
                            # caveat exactly how many items finished before it.
                            manifest.record_completed(
                                f"{ent['uri']}#{attribute}"
                            )
                            # A9 cost envelope (ONTA-282): the item's paid adapter
                            # calls fed their spend into the manifest as they ran
                            # (see _lookup_chain). If cumulative run spend has now
                            # reached the HARD per-run ceiling, HALT CLEANLY.
                            ceiling_error = manifest.check_ceiling()
                            if ceiling_error is not None:
                                raise ceiling_error
                            if counter["n"] % PROGRESS_FLUSH_EVERY == 0:
                                await self._jobs.update(job)
                return results

            tasks = [asyncio.create_task(process_entity(e)) for e in entities]
            all_rows: list[RowResult] = []
            for t in tasks:
                rows = await t
                all_rows.extend(rows)

            # Stamp the per-provider activity log + aggregated error summary onto
            # the job now, so every terminal path below (cancelled, review,
            # applied) persists "which providers we used + the errors we hit".
            job.provider_logs = tally.to_logs()
            job.error_summary = tally.to_error_summary()

            # Re-check cancellation after work loop.
            latest = await self._jobs.get(job.id)
            if latest and latest.status == JobStatus.cancelled:
                job.status = JobStatus.cancelled
                job.completed_at = _now()
                manifest.cancel()
                stamp_enrichment_run_cancelled(job)
                await self._jobs.update(job)
                return

            # Keep conflicts AND fills/verifications in results so the cited
            # verdict (value + source_url + provenance) is retrievable via the
            # job API, not just conflicts. Skips/no-matches carry no verdict.
            job.results = [
                r for r in all_rows if r.action in ("conflict", "filled", "verified")
            ]

            # One structured summary on the common terminal path (covers BOTH the
            # review and applied states below). Makes the miss count visible from
            # logs so a run that simply found nothing is distinguishable from a
            # broken pipeline. NOT emitted on the cancelled/failed early-returns.
            sources_tried = sorted(
                {
                    pl.provider
                    for pl in (job.provider_logs or [])
                    if pl.provider and pl.status != "skipped"
                }
                or {
                    r.verdict.source
                    for r in all_rows
                    if r.verdict and getattr(r.verdict, "source", None)
                }
            )
            _host().logger.info(
                "enrichment_job_summary",
                job_id=job.id,
                type_name=job.type_name,
                tier=job.tier.value if hasattr(job.tier, "value") else str(job.tier),
                total=job.progress.total,
                filled=job.progress.filled,
                verified=job.progress.verified,
                conflicts=job.progress.conflicts,
                no_match=job.progress.no_match,
                sources_tried=sources_tried,
            )

            await self._apply_run_writes(
                job, tenant_id, all_rows, graph_uri, manifest, sources_tried
            )

        except Exception as exc:  # noqa: BLE001
            _host().logger.exception("enrichment_job_failed", job_id=job.id, error=str(exc))
            job.status = JobStatus.failed
            job.error = str(exc)
            job.completed_at = _now()
            # Surface whatever providers ran (and any per-provider errors) before
            # the fatal crash, plus the crash itself as a job-level error entry.
            job.provider_logs = tally.to_logs()
            job.error_summary = tally.to_error_summary() + [
                JobErrorItem(kind="job", message=str(exc)[:_MAX_ERROR_MSG])
            ]
            # A9 manifest: terminal FAILED with the derived reason. A provider
            # exhaustion (402 billing / sustained-429) is named as such and the
            # unfinished planned items are rolled into `dropped` — so a run halted
            # mid-flight caveats partial coverage instead of a silent partial.
            landed = (
                f"{job.progress.processed} of {job.progress.total} items completed "
                "before the failure."
                if job.progress.total
                else ""
            )
            manifest.halt_from_exception(exc, landed_note=landed)
            stamp_enrichment_run_failed(job, str(exc))
            try:
                await self._jobs.update(job)
            except Exception:  # noqa: BLE001
                pass
