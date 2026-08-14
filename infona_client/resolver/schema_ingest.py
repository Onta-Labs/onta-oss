from __future__ import annotations

"""Ingest orchestrator: extract → constrain → resolve → insert.

Job: ``ingest`` / ``_ingest_csv`` entry points. Instance writes still go
through ``insert_facts`` on the shared write path; this module only
decides WHAT to extract and WHEN to resolve.
"""

import time
from datetime import datetime, timezone
from uuid import uuid4

from infona_client.graph.ontology_queries import ontology_version
from infona_client.graph.queries import tenant_graph_uri
from infona_client.pipeline.envelope import derive_fact_id
from infona_client.resolver.attribute_resolver import AttributeSchema
from infona_client.resolver.models import (
    CleanFact,
    ExtractionConstraint,
    ExtractionResult,
    IngestResult,
    validate_soft_a2,
)
from infona_client.resolver.schema_extract_constraints import (
    _apply_attribute_ceiling,
    _drop_offplan_compound_attributes,
)
from infona_client.resolver.schema_focus import _apply_soft_focus_floor
# Call-time host lookups so tests that patch schema_resolver.logger /
# insert_facts / _entity_uri / env flags keep working after this extract.
from infona_client.resolver import schema_resolver as _sr


class SchemaIngestMixin:
    """Top-level ingest orchestration for SchemaResolver."""

    async def ingest(
        self,
        content: str,
        tenant_id: str,
        content_type: str = "text",
        source: str = "",
        instance_graph: str | None = None,
        constrain_types: list[str] | None = None,
        constrain_attributes: dict[str, list[str]] | None = None,
        constrain_soft: bool = False,
        constrain_attributes_exhaustive: bool = False,
        run_id: str | None = None,
        observed_at: datetime | None = None,
        fact_ids: list[str] | None = None,
        tier: str | None = None,
    ) -> IngestResult:
        """Full ingestion pipeline: extract → resolve → validate → insert.

        Args:
            instance_graph: If set, instance data goes into this graph while
                ontology updates go into the tenant's base graph. This enables
                multiple KGs sharing one ontology.
            run_id: STABLE identity of this logical ingest run (ONTA-271). Every
                fact_id + the batch_id + the A6 Graph Delta derive from it, so a
                retry/replay that PRESERVES the run_id reproduces a byte-identical
                graph (P6 dedupes the replay instead of duplicating). Defaults to
                a fresh ``uuid4`` per call — today's behavior, and the "no stable
                id" control (each call is a distinct run, so nothing dedupes).
            observed_at: The run's ``onto/ingested_at`` timestamp (ONTA-271). A
                nonce if left to wall-clock, so it is threaded from the envelope
                and a replay REUSES it to stay byte-identical. Defaults to now.
            constrain_types: OPT-IN, DISCOVERY-ONLY (ONTA-199). When set, extraction
                is constrained to emit ONLY entities of these confirmed type(s).
                ``None`` (the default, and every document/CSV/text caller) keeps
                the fully open-ended multi-type extractor unchanged.
            constrain_attributes: OPT-IN, DISCOVERY-ONLY. Per-type allowed
                attribute names (snake_case) paired with ``constrain_types``. A
                type absent from this map is unrestricted on attributes. ``None``
                = no attribute restriction. Only meaningful alongside
                ``constrain_types``.
            constrain_attributes_exhaustive: OPT-IN, DISCOVERY-ONLY (ONTA-382).
                When True, ``constrain_attributes`` is a CEILING (allowlist) even
                under soft extraction — unlisted attributes on focus-type records
                are dropped and ledgered. When False (default), soft mode treats
                the list as an illustrative prior / floor (LLM may extend). Hard
                mode always ceilings regardless. Open default preserves pre-382
                behavior for every non-discovery / open-mode caller.
            fact_ids: OPT-IN A1→A2 lineage handoff (ONTA-371). The per-row A1
                ``fact_id`` of each row in this micro-batch, in row order, forwarded
                from the discovery capability's A1 Source Bundle. Recorded for
                lineage observability; the emitted graph is byte-identical (the A6
                delta still keys off ``run_id``) — a PASS-THROUGH of provenance, not
                a change to WHAT is written. ``None`` for every non-discovery
                caller — unchanged.
            tier: OPT-IN A1→A2 lineage (ONTA-371). The source authority tier the
                bundle rows came from (``authoritative`` / ``web``). Pass-through
                provenance only. ``None`` for non-discovery callers.
        """
        # Build the opt-in extraction constraint (ONTA-199). None / empty types →
        # inactive → every _extract prompt is byte-for-byte the open-ended default,
        # so document/CSV/text ingestion is provably unchanged.
        constraint: ExtractionConstraint | None = None
        if constrain_types:
            constraint = ExtractionConstraint(
                types=list(constrain_types),
                attributes={k: list(v) for k, v in (constrain_attributes or {}).items()},
                soft=constrain_soft,
                attributes_exhaustive=bool(constrain_attributes_exhaustive),
            )
        # ONTA-371: record the A1→A2 lineage handoff (the discovery capability now
        # drives extraction from the A1 Source Bundle and forwards each row's A1
        # fact_id + source tier). Observability only — the emitted graph is
        # byte-identical (the A6 delta keys off run_id). Fires only when a
        # discovery run threads lineage; every other caller passes None → silent.
        if fact_ids or tier is not None:
            _sr.logger.debug(
                "a1_a2_lineage_handoff",
                path="ingest",
                run_id=run_id,
                source_fact_ids=len(fact_ids or ()),
                source_tier=tier,
            )
        graph_uri = tenant_graph_uri(tenant_id)
        # Ontology always goes to the base tenant graph
        # Instance data goes to instance_graph if specified, otherwise base graph.
        # ONTA-268 (reentrancy): the target instance graph is CALL-LOCAL and
        # threaded down the write path so two ingest() calls interleaving on ONE
        # shared resolver can't clobber each other's target (the leak
        # `qc/isolation.py::check_isolation` catches). The `self.` attribute is
        # written too, but only as the fallback legacy direct-call sites read; the
        # reentrant path below never reads it.
        target_instance_graph = instance_graph or graph_uri
        self._instance_graph = target_instance_graph
        # Set graph URI on type matcher so embedding pre-filter can find the right
        # store — threaded per-call to `match(graph_uri=...)` below; the attribute
        # stays a fallback only.
        self._type_matcher._graph_uri = graph_uri

        # Layer stack for the subclass-closure parent map (ONTA-397). Reads are
        # layered so a tenant leaf under a Public parent resolves; writes still
        # land only on the tenant graph. Entitlement is decided by the OSS seam
        # (env allowlist / Clerk-stamped bit) — never a client flag.
        layer_stack = self._layer_stack_for(tenant_id, graph_uri)

        # Step 1: Fetch existing ontology (needed for extraction context)
        existing_types, existing_attrs = await self._fetch_ontology(graph_uri)
        # Build the child->parent subclass map once per ingest. Used to climb the
        # hierarchy for ER config selection and ancestor synthesis. Mutated
        # in-place as new subtypes are created during this ingest. CALL-LOCAL and
        # threaded (ONTA-268): a fresh dict per ingest, so concurrent ingests each
        # mutate their own map; `self._parent_of` remains the legacy fallback.
        parent_of = await self._fetch_parent_map(graph_uri, layer_stack=layer_stack)
        self._parent_of = parent_of
        # Stash for _reconcile_ontology_version (same run, same stack).
        self._active_layer_stack = layer_stack

        # ONTA-270: fingerprint the ontology snapshot THIS run (P5) planned
        # against. Stamped onto the A5 placement plan and threaded into the apply
        # (`_resolve_and_insert`), where P6 rejects/recomputes it if a concurrent
        # run advances the ontology during the (long, async) extraction below.
        # Computed here, right after the snapshot read, so it captures exactly the
        # state every downstream placement decision is made against.
        plan_ontology_version = ontology_version(existing_types, existing_attrs, parent_of)

        # Stage timing (ONTA-198 follow-up): time the two heavy halves of an
        # ingest — LLM EXTRACTION vs type-RESOLUTION+insert — so a slow run reveals
        # which half dominates without hand-reconstructing it from request gaps.
        _t_extract = time.monotonic()

        # CSV: use schema-inference pipeline (1 LLM call for schema, deterministic for rows)
        if content_type == "csv":
            return await self._ingest_csv(
                content, graph_uri, existing_types, existing_attrs, source,
                instance_graph=target_instance_graph, parent_of=parent_of,  # ONTA-268
            )

        # Text/JSON: chunk and process
        from infona_client.resolver.chunker import (
            chunk_text,
            chunk_json_array,
            json_array_len,
        )
        is_json = content_type in ("json", "jsonl")
        if is_json:
            # Token-budget batching (ONTA-196): size each batch so its predicted
            # reified output stays under a fraction of THIS resolver's extraction
            # cap, so the common dense-record case extracts first-try instead of
            # overflowing max_tokens and dropping into the slow split-and-retry
            # recovery (which remains the safety net below).
            chunks = chunk_json_array(content, max_tokens=self.EXTRACT_MAX_TOKENS)
        else:
            chunks = chunk_text(content)

        # Row-conservation accounting for the JSON path (ADR 0003 §2): a chunk
        # whose extraction yields nothing (e.g. truncated output) must not vanish
        # silently. We count records IN and records DROPPED so the run can never
        # be presented as complete while a whole batch was lost.
        rows_in = 0
        rows_dropped = 0

        # ONTA-199: forward the constraint kwarg to ``_extract`` ONLY when it's
        # active. The default document path then calls ``_extract`` with the EXACT
        # argument shape it had before this change, so existing tests that patch
        # ``_extract`` with a mock lacking a ``constraint`` parameter still pass
        # (the no-op path never sends the kwarg). Real methods below
        # (``_extract_json_chunk_with_recovery`` / ``_extract_json_chunks_calibrated``)
        # always accept ``constraint`` so they take it directly.
        _extract_c = {"constraint": constraint} if constraint is not None else {}

        if len(chunks) <= 1:
            # Small content — single extraction. JSON STILL routes through the
            # truncation-recovery helper (FIX 1): even one chunk's reified output
            # (each row → Model + reified Score + Organization + relationships) can
            # exceed max_tokens and get truncated, and bare _extract would then
            # silently return ZERO entities for the whole pull. Recovery splits +
            # retries down to the floor so a single chunk can't vanish.
            if is_json:
                rows_in = json_array_len(content)
                extraction, dropped = await self._extract_json_chunk_with_recovery(
                    content, existing_types, constraint=constraint,
                )
                rows_dropped += dropped
            else:
                extraction = await self._extract(
                    content, content_type, existing_types,
                    existing_attrs=existing_attrs, **_extract_c,
                )
        elif is_json:
            # Multiple JSON chunks: first-batch CALIBRATION (ONTA-197 item 2) +
            # bounded CONCURRENCY (item 3), composed. The two features compose
            # naturally because calibration NEEDS chunk 1's result before it can
            # re-size the rest:
            #   1. Extract chunk 1 sequentially (with recovery).
            #   2. Measure its REAL output-tokens-per-record and RE-CHUNK the
            #      not-yet-processed remainder ONCE with the observed ratio — the
            #      conservative ONTA-196 default only ever sized the FIRST batch,
            #      so sparse records get ~4-7x bigger (still cap-safe) batches now.
            #   3. Extract the re-chunked remainder CONCURRENTLY under a semaphore,
            #      preserving order and per-chunk recovery + drop accounting.
            extraction, chunk_rows_in, chunk_dropped = (
                await self._extract_json_chunks_calibrated(
                    chunks, content, existing_types, constraint=constraint,
                )
            )
            rows_in += chunk_rows_in
            rows_dropped += chunk_dropped
        else:
            # Multiple TEXT chunks — independent, no token-budget calibration
            # (calibration is a JSON-record concept). Extract concurrently under
            # the same semaphore, then merge in deterministic chunk order.
            results = await self._extract_chunks_concurrently(
                [
                    lambda c=chunk: self._extract(
                        c, content_type, existing_types,
                        existing_attrs=existing_attrs, **_extract_c,
                    )
                    for chunk in chunks
                ]
            )
            merged_entities = []
            merged_relationships = []
            seen_ids: set[str] = set()
            for extraction in results:
                for e in extraction.entities:
                    if e.id not in seen_ids:
                        merged_entities.append(e)
                        seen_ids.add(e.id)
                merged_relationships.extend(extraction.relationships)
            extraction = ExtractionResult(
                entities=merged_entities,
                relationships=merged_relationships,
                source_text=content[:500],
            )

        # ONTA-255: SOFT-mode focus-type floor over the FULLY-MERGED extraction.
        # This is the AUTHORITATIVE pass (allow_strip=True): the per-chunk backstop
        # in `_apply_extraction_constraint` only RE-HOMES within a chunk and never
        # strips, so a metric whose subject sits in a different chunk survives to
        # here. With the whole batch in view this pass re-homes such a metric onto
        # the right subject (or, if no subject exists anywhere, strips it off the
        # concept node and logs the loss / starvation — nothing silent). It also
        # covers callers/tests that stub `_extract` wholesale. Idempotent: once the
        # metrics are off the concept nodes, this is a no-op.
        if constraint is not None and constraint.is_active and constraint.soft:
            extraction = _apply_soft_focus_floor(extraction, constraint)
            # ONTA-394: drop compound-of-plan attribute fabrications (website_city
            # from website + city) BEFORE the ceiling. Runs regardless of
            # attributes_exhaustive — a merged pair of requested fields is wrong
            # even when the plan attrs are only illustrative.
            extraction = _drop_offplan_compound_attributes(extraction, constraint)
            # ONTA-382: exhaustive attribute set → ceiling on focus-type attrs
            # after the full-batch soft focus floor (authoritative merged view).
            if getattr(constraint, "attributes_exhaustive", False):
                extraction = _apply_attribute_ceiling(extraction, constraint)
            # A2 zero-ontology-commitment contract (ONTA-272): the soft-typed
            # candidate facts must carry NO committed ontology reference in any type
            # slot (soft lineage is fine — it is P5's suggestion, not a commitment).
            # OBSERVE-ONLY here: imperfect LLM output must never HARD-fail a run, so
            # a violation is logged, not raised (the deterministic pre-structured
            # fast path asserts the same contract FATALLY, where it can only be a bug).
            _a2_violations = validate_soft_a2(extraction)
            if _a2_violations:
                _sr.logger.warning(
                    "soft_a2_contract_violation",
                    count=len(_a2_violations),
                    sample=_a2_violations[:3],
                )

        _sr.logger.info(
            "extraction_complete",
            entities=len(extraction.entities),
            relationships=len(extraction.relationships),
            rows_in=rows_in,
            rows_dropped=rows_dropped,
        )
        _sr.logger.info(
            "stage_timing",
            stage="extract",
            duration_ms=round((time.monotonic() - _t_extract) * 1000, 1),
            entities=len(extraction.entities),
            rows_in=rows_in,
        )

        if not extraction.entities:
            return IngestResult(
                entities_extracted=0, rows_in=rows_in, rows_dropped=rows_dropped,
            )

        # Step 3: Resolve types and attributes, validate, insert
        # ONTA-271: STABLE run identity. run_id defaults to a fresh uuid4 (a
        # distinct run per call — today's behavior), but a caller that preserves
        # it across a retry makes the whole write replay-deterministic. batch_id
        # is DERIVED from run_id (was a bare uuid4) so a replay reuses the same
        # batch token → the BATCH_PREDICATE triple is idempotent instead of a
        # per-call nonce; rollback-by-batch is unchanged (still a unique token
        # per distinct run). observed_at feeds onto/ingested_at (see
        # _resolve_and_insert_entity), threaded so a replay reuses it.
        run_id = run_id or str(uuid4())
        observed_at = observed_at or datetime.now(timezone.utc)
        batch_id = derive_fact_id(run_id=run_id, stage="A6-batch")
        result = IngestResult(
            entities_extracted=len(extraction.entities),
            batch_id=batch_id,
            rows_in=rows_in,
            rows_dropped=rows_dropped,
        )
        # ONTA-382: fold attribute-ceiling drops into the A3 CleanReport ledger
        # so unlisted attributes the extractor emitted are never silent-discarded.
        # Each drop is a DROPPED CleanFact with reason="attribute_ceiling".
        for drop in getattr(extraction, "ceiling_drops", None) or []:
            if isinstance(drop, CleanFact):
                result.clean_report.record(drop)
            elif isinstance(drop, dict):
                try:
                    result.clean_report.record(CleanFact(**drop))
                except Exception:  # noqa: BLE001 — ledger is observability-only
                    _sr.logger.warning(
                        "attribute_ceiling_drop_unrecordable", drop=drop, exc_info=True
                    )
        entity_uri_map: dict[str, str] = {}  # entity id → URI
        entity_type_map: dict[str, str] = {}  # entity id → resolved type name

        _t_resolve = time.monotonic()
        # ONTA-383: soft-mode focus types (discovery's proposed_type) are the
        # consolidation anchor. Threaded into resolve so subtype sprawl
        # (University/College/PublicInstitution peers) collapses under the
        # confirmed focus (Institution) and junk property-class types are
        # rejected. HARD constraint / open ingest pass None (no change).
        focus_types: list[str] | None = None
        if (
            constraint is not None
            and constraint.is_active
            and constraint.soft
        ):
            focus_types = list(constraint.types)
        try:
            final = await self._resolve_and_insert(
                extraction, graph_uri, existing_types, existing_attrs,
                source, result, entity_uri_map, entity_type_map, batch_id,
                # ONTA-177: text/JSON/web-discovery ingest IS the schema pass
                # for these modalities (extract + apply happen in one call),
                # so free-text candidacy is decided here.
                decide_text_candidacy=True,
                # ONTA-268: thread the call-local target graph + parent map so
                # the write path never reads shared `self.` state.
                instance_graph=target_instance_graph,
                parent_of=parent_of,
                # ONTA-270: the version P5 stamped the plan at, so P6 (the apply
                # inside `_resolve_and_insert`) can reject/recompute a stale plan.
                ontology_version_stamp=plan_ontology_version,
                # ONTA-271: stable run identity + the run's ingested_at stamp,
                # threaded call-local (like instance_graph/parent_of) so the A6
                # Graph Delta and every fact_id are replay-deterministic.
                run_id=run_id,
                observed_at=observed_at,
                # ONTA-370/372: the workspace scope for the A4 Verify seam's run
                # envelope. `tenant_id` IS the product-facing `workspace_id`
                # (ADR 0011 §3 — pipeline code says workspace_id). Only consumed
                # when a VerifyPolicy turns the seam on; ignored on the default path.
                workspace_id=tenant_id,
                focus_types=focus_types,
            )
            _sr.logger.info(
                "stage_timing",
                stage="resolve_insert",
                duration_ms=round((time.monotonic() - _t_resolve) * 1000, 1),
                entities=final.entities_resolved,
                types_created=len(final.types_created),
            )
            # Never present a run as complete while a whole chunk was lost to
            # truncation (FIX 1): a non-zero drop count after recovery is an
            # ERROR-level signal carried back on the result for the caller.
            if final.rows_dropped:
                _sr.logger.error(
                    "ingest_rows_dropped",
                    batch_id=batch_id,
                    rows_in=final.rows_in,
                    rows_dropped=final.rows_dropped,
                )
            return final
        except Exception:
            _sr.logger.error(
                "ingest_failed_rolling_back",
                batch_id=batch_id,
                entities_so_far=result.entities_resolved,
                exc_info=True,
            )
            instance_graph = target_instance_graph  # ONTA-268: call-local, not self
            # ONTA-528: batch delete-by-id is not yet ported to GraphStore. The
            # old path called ``await self._neptune.update(delete_batch_query(...))``
            # — a dead SPARQL client on Neo4j-only that either ConnectError'd
            # (masking the original failure) or silently no-op'd. Do not call
            # SPARQL HTTP update here; re-raise the original ingest failure.
            # Partial writes for this batch_id may remain until a GraphStore
            # batch-rollback lands; never claim rollback succeeded via SPARQL.
            _sr.logger.info(
                "batch_rollback_skipped",
                batch_id=batch_id,
                instance_graph=instance_graph,
                reason="delete_batch not ported to GraphStore (ONTA-528)",
            )
            raise
    async def _ingest_csv(
        self,
        content: str,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        source: str,
        *,
        instance_graph: str | None = None,
        parent_of: dict[str, str] | None = None,
    ) -> IngestResult:
        """CSV ingestion: 1 LLM call for schema inference, deterministic mapping for all rows.

        ``instance_graph`` / ``parent_of`` (ONTA-268): CALL-LOCAL overrides
        threaded from :meth:`ingest` down through :meth:`_ingest_mapped`."""
        import csv
        import io
        from infona_client.resolver.csv_resolver import CSVResolver

        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
        if not rows:
            return IngestResult(entities_extracted=0)

        headers = list(rows[0].keys())
        _sr.logger.info("csv_ingest_start", rows=len(rows), columns=len(headers))

        # Step 1: Infer schema from sample (1 LLM call). Pass existing_attrs so
        # inference reuses declared properties (Drug.manufacturer) instead of
        # inventing parallel names (manufactured_by) — see reconcile_mapping_to_existing.
        csv_resolver = CSVResolver(self._anthropic, self._openrouter_key)
        mapping = await csv_resolver.infer_schema(
            headers,
            rows[:10],
            existing_types,
            total_rows=len(rows),
            existing_attrs=existing_attrs,
        )

        # Step 2+: apply the mapping and run the shared resolve→dedup→insert
        # tail (also reused by web-discovery ingest via ingest_mapped_records).
        return await self._ingest_mapped(
            mapping, rows, graph_uri, existing_types, existing_attrs, source,
            instance_graph=instance_graph, parent_of=parent_of,
        )
