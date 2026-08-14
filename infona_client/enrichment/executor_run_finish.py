"""Apply-phase of EnrichmentExecutor.run — write fills / refresh / stamp."""

from __future__ import annotations

from infona_client.analytics import distinct_id_for, emit
from infona_client.enrichment.executor_helpers import _host, _now
from infona_client.enrichment.models import ConflictPolicy, EnrichJob, JobStatus, RowResult
from infona_client.graph.store import resolve_optional_graph_store
from infona_client.pipeline.manifest import RunManifest
from infona_client.pipeline.stage_trace import (
    stamp_enrichment_run_finished,
    stamp_enrichment_write_phase,
)


class EnrichmentRunFinishMixin:
    """Write + complete the enrichment run after rows are collected."""

    async def _apply_run_writes(
        self,
        job: EnrichJob,
        tenant_id: str,
        all_rows: list[RowResult],
        graph_uri: str,
        manifest: RunManifest,
        sources_tried: list[str],
    ) -> None:
        # Apply phase
        policy = job.conflict_policy
        # `stage` semantics (ONTA-159): a conflict-free fill (the target field
        # was empty) has nothing to reconcile, so it is applied immediately —
        # exactly like `skip`. Only genuine value-vs-value CONFLICTS are held
        # for human review. Previously `stage` also held fills, but the review
        # surface (`GET /jobs/{id}/conflicts`) lists ONLY conflict rows, so
        # conflict-free staged fills were stranded: staged yet invisible and
        # un-approvable — a job sat "In review" with zero reviewable items.
        # So under `stage` we WRITE like `skip` (fills only) and land in
        # `review` only when there is at least one real conflict to resolve.
        has_conflicts = any(r.action == "conflict" for r in job.results)
        write_policy = (
            ConflictPolicy.skip if policy == ConflictPolicy.stage else policy
        )

        # FRESHNESS RE-STAMP (ONTA-245 F2): a `verified` row (the source
        # RE-CONFIRMS the existing value) writes NO primary value under
        # verify/skip/stage, so a decay-refresh that re-confirms a still-correct
        # value would never advance its freshness clock — defeating "verified in
        # the last N days" exactly where it matters most. Fix: for a `verified`
        # row under a refresh-appropriate policy (verify/skip/stage → write_policy
        # is verify or skip), re-emit ONLY the per-attribute provenance companions
        # (source + a fresh `_verified_at`), advancing the stamp WITHOUT rewriting
        # the unchanged primary value (no duplicate value triple). Idempotent:
        # re-asserting the same `_verified_at` predicate with a newer object simply
        # accretes a fresher stamp the NL "last N days" FILTER then matches.
        restamp_triples: list[tuple[str, str, str]] = []
        if write_policy in (ConflictPolicy.verify, ConflictPolicy.skip):
            for r in all_rows:
                if r.action == "verified" and r.verdict is not None:
                    restamp_triples.extend(
                        self._provenance_triples(
                            r.entity_uri, job.type_name, r.attribute, r.verdict
                        )
                    )

        # `applied_attr_values` is the source of truth for "was anything
        # applied?" — the attributes (primary + provenance companions) that
        # actually received a written value under `write_policy`, mapped to
        # their values. Empty ⇒ nothing to declare or write.
        applied_attr_values = self._applied_attribute_values(all_rows, write_policy)
        # E7: resolve GraphStore once for this write batch; None keeps the
        # default store resolution inside insert_facts.
        graph_store = resolve_optional_graph_store()
        if applied_attr_values:
            # Declare schema, THEN write data. Enrichment must EXTEND THE
            # ONTOLOGY (COG-112): before writing instance values, upsert the
            # ontology declaration for every attribute that actually got a
            # value (primary + its provenance companions) into the tenant
            # (ontology) graph, so an enriched attribute is first-class schema
            # — visible in the /schema view, the Explorer column schema, and
            # the Enrich dialog's predicate dropdown, not just as orphan data.
            # One idempotent upsert per attribute (not per row), each declared
            # with a range inferred from its actual applied values and never
            # downgrading an existing richer range. Runs for every write
            # policy AND for `stage`'s conflict-free fills (which now write
            # via `write_policy=skip`); only true conflicts held for review
            # declare nothing until accepted.
            #
            # `_declare_attributes` RETURNS the {attr -> resolved_datatype} map
            # it just declared, so we type each INSTANCE value with the SAME
            # datatype the attribute is DECLARED with (P1 fix): the stored
            # literal (`"92"^^xsd:integer`) now matches the declared range,
            # instead of a bare `xsd:string` literal the typed NL filters miss.
            resolved_datatypes = await self._declare_attributes(
                tenant_id,
                job.type_name,
                applied_attr_values,
                kg_name=job.kg_name,
            )
            # Canonical companion-provenance-GRAPH records (F1) for every applied
            # fill, dated from the verdict — flowed through the shared
            # insert_facts provenance seam (gated by INFONA_PROVENANCE_ENABLED).
            prov_graph_triples = self._canonical_provenance_triples(
                [r for r in all_rows if self._row_is_applied(r, write_policy)],
                job.type_name,
            )
            # REFRESH vs. INITIAL-FILL split (ONTA-279). A refresh (write policy
            # verify/overwrite) MUST supersede — a fresh value CLOSES the stale
            # value's validity interval and is arbitrated (authority > confidence
            # > recency) against the existing current value, so it can never
            # blind-append and can never clobber a user_assertion correction.
            # The initial-fill / skip path (write_policy=skip, from `skip`/`stage`)
            # keeps its plain conflict-free insert unchanged.
            is_refresh = write_policy in (
                ConflictPolicy.verify,
                ConflictPolicy.overwrite,
            )
            if is_refresh:
                # Route each applied PRIMARY value through the P6 supersession
                # op (consulting the suppression list); collect node-minting +
                # display-companion triples for one shared insert.
                companion_triples = await self._apply_refresh_writes(
                    graph_uri,
                    all_rows,
                    job.type_name,
                    write_policy,
                    resolved_datatypes,
                    job.id,
                )
                # F2 verified-row freshness re-stamps ride the same shared insert
                # (verify path; empty under overwrite where verifies rewrite the
                # value via the op).
                write_triples = companion_triples + restamp_triples
                if write_triples or prov_graph_triples:
                    await _host().insert_facts(
                        self._neptune,
                        graph_uri,
                        write_triples,
                        provenance_triples=prov_graph_triples or None,
                        store=graph_store,
                    )
                await _host().refresh_after_write(
                    self._neptune,
                    tenant_id=tenant_id,
                    kg_name=job.kg_name,
                    affected_types=self._affected_types(
                        job.type_name, resolved_datatypes
                    ),
                )
            else:
                # Initial-fill / skip path — unchanged conflict-free insert.
                # Build the instance triples USING that resolved-datatype map:
                # primitives route through validate_triple (typed literal, or a
                # skip on a non-conforming value); relationships write the entity
                # IRI directly; provenance companions stay plain string literals.
                triples = self._select_triples_for_policy(
                    all_rows, job.type_name, write_policy, resolved_datatypes
                )
                # Append the verified-row freshness re-stamps (F2) so a
                # decay-refresh advances the clock in the SAME write as the fills.
                triples.extend(restamp_triples)
                # Single shared write path — identical to CSV/JSON ingestion
                # (graph/kg_writer.py): batched insert, then post-write
                # housekeeping (invalidate the NL-planning cache, re-embed the
                # enriched type so semantic retrieval doesn't serve a stale schema
                # embedding, and recompute the Explorer's type-stats). Only fires
                # when something was actually applied.
                await _host().insert_facts(
                    self._neptune,
                    graph_uri,
                    triples,
                    provenance_triples=prov_graph_triples or None,
                    store=graph_store,
                )
                await _host().refresh_after_write(
                    self._neptune,
                    tenant_id=tenant_id,
                    kg_name=job.kg_name,
                    affected_types=self._affected_types(
                        job.type_name, resolved_datatypes
                    ),
                )
        elif restamp_triples:
            # No new fills, but a decay-refresh re-confirmed existing values:
            # write ONLY the freshness re-stamps so the clock still advances.
            # Same shared write path; no primary value is rewritten, and
            # NOTHING is declared — companions are attr_meta metadata, never
            # ontology attributes (ONTA-262; this branch used to declare
            # `_verified_at` as "first-class schema", which is exactly what
            # rendered it as a sibling column in every schema surface).
            await _host().insert_facts(
                self._neptune,
                graph_uri,
                restamp_triples,
                store=graph_store,
            )
            await _host().refresh_after_write(
                self._neptune,
                tenant_id=tenant_id,
                kg_name=job.kg_name,
                affected_types={job.type_name},
            )
        # `stage` with at least one real conflict stays in `review` — those
        # conflicts are now the ONLY thing the review queue holds (the fills
        # were just applied above). Everything else — a `stage` run with no
        # conflicts, or any write policy — is `applied`.
        if policy == ConflictPolicy.stage and has_conflicts:
            job.status = JobStatus.review
        else:
            job.status = JobStatus.applied
        job.completed_at = _now()
        # A9 manifest: the run finished its work (review = parked for human
        # decisions, applied = written) — a clean terminal COMPLETED.
        manifest.complete()
        # Operator Job Trace (ONTA-387): P4/P6 write-phase actions + close
        # P0/P2/P4/P6 live; skip P1/P3/P5/P7/P8/P9 with reasons.
        write_policy_s = (
            getattr(write_policy, "value", None) or str(write_policy)
        )
        stamp_enrichment_write_phase(
            job,
            write_policy=write_policy_s,
            has_conflicts=has_conflicts,
            applied=bool(applied_attr_values) or bool(restamp_triples),
        )
        stamp_enrichment_run_finished(job)
        await self._jobs.update(job)

        # Product-analytics event (ONTA-323). run() is a background task with
        # no request context, so there is no auth subject to attribute to →
        # a stable system:<tenant> distinct id (never a path-named tenant).
        # Fire-and-forget, no-op without a registered sink, never raises.
        emit(
            "enrichment_ran",
            distinct_id=distinct_id_for(None, tenant_id),
            tenant=tenant_id,
            kg=job.kg_name or "",
            type_name=job.type_name,
            tier=job.tier.value if hasattr(job.tier, "value") else str(job.tier),
            attrs_filled=job.progress.filled,
            verified=job.progress.verified,
            conflicts=job.progress.conflicts,
            sources=sources_tried,
            status=job.status.value if hasattr(job.status, "value") else str(job.status),
        )
