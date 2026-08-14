"""Human review accept path — apply_decisions for EnrichmentExecutor."""

from __future__ import annotations

from infona_client.enrichment.executor_helpers import _host, _now
from infona_client.enrichment.models import ConflictReview, JobStatus
from infona_client.graph.queries import kg_graph_uri
from infona_client.graph.store import resolve_optional_graph_store
from infona_client.resolver.models import CleanReport


class EnrichmentApplyMixin:
    """Accept staged conflict reviews and write through the shared path."""

    async def apply_decisions(
        self, job_id: str, decisions: list[ConflictReview]
    ) -> int:
        job = await self._jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        graph_uri = kg_graph_uri(job.tenant_id, job.kg_name)
        applied = 0  # number of accepted facts (provenance triples don't count)
        # Insertion-ordered map of applied attribute name -> the string values
        # written for it, so declarations infer the right range (and never
        # downgrade an existing one), mirroring run()'s _applied_attribute_values.
        applied_attr_values: dict[str, list[str]] = {}
        # The accepted decisions whose primary value we'll later type + write.
        accepted: list[ConflictReview] = []
        for d in decisions:
            if d.decision != "accept":
                continue
            accepted.append(d)
            # Track the PRIMARY attribute names + values actually written so we
            # declare them in the ontology, mirroring run(). Provenance companions
            # are deliberately NOT declared (ONTA-262): they are attr_meta
            # metadata, not attributes — their instance triples still ride the
            # same write below.
            applied_attr_values.setdefault(d.attribute, []).append(d.proposed.value)
            applied += 1
        if applied_attr_values:
            # Declare schema, THEN write data — accepted review decisions extend
            # the ontology too (COG-112), so the enriched attribute is first-class
            # schema, mirroring the auto-apply path in run(). The returned
            # {attr -> resolved_datatype} map types each INSTANCE value with the
            # SAME datatype the attribute is DECLARED with (P1 fix): the stored
            # literal matches the declared range instead of a bare xsd:string.
            resolved_datatypes = await self._declare_attributes(
                job.tenant_id,
                job.type_name,
                applied_attr_values,
                kg_name=job.kg_name,
            )
            # Build the instance triples USING that map: primitives route through
            # validate_triple (typed literal, or a skip on a non-conforming value);
            # relationships write the entity IRI directly; provenance companions
            # stay plain string literals.
            triples: list[tuple[str, str, str]] = []
            clean_report = CleanReport()  # A3 ledger: partition every applied primitive value
            for d in accepted:
                datatype = resolved_datatypes.get(d.attribute, "string")
                triples.extend(
                    self._instance_triples_for_value(
                        d.entity_uri, job.type_name, d.attribute, d.proposed.value, datatype,
                        clean_report=clean_report,
                    )
                )
                triples.extend(
                    self._provenance_triples(
                        d.entity_uri, job.type_name, d.attribute, d.proposed
                    )
                )
            self._log_clean_report(clean_report, type_name=job.type_name, phase="apply_decisions")
            # Canonical companion-provenance-GRAPH records (F1) for the accepted
            # decisions, dated from the verdict — same seam as the auto-apply path
            # (gated by INFONA_PROVENANCE_ENABLED).
            prov_graph_triples = self._canonical_provenance_triples(
                accepted, job.type_name
            )
            # Same shared write path as run() / ingestion (graph/kg_writer.py):
            # batched insert + post-write housekeeping (cache-invalidate,
            # re-embed the type, recompute stats). E7: GraphStore when neo4j.
            await _host().insert_facts(
                self._neptune,
                graph_uri,
                triples,
                provenance_triples=prov_graph_triples or None,
                store=resolve_optional_graph_store(),
            )
            await _host().refresh_after_write(
                self._neptune,
                tenant_id=job.tenant_id,
                kg_name=job.kg_name,
                affected_types=self._affected_types(job.type_name, resolved_datatypes),
            )
        job.status = JobStatus.applied
        job.completed_at = _now()
        # Operator Job Trace (ONTA-387): human accept of staged conflicts is a
        # P6 write — record an action if a live trace is present.
        try:
            from infona_client.pipeline.stage_trace import (
                StageProjectId,
                attach_recorder,
            )

            rec = attach_recorder(job)
            if rec is not None:
                rec.action(
                    StageProjectId.p6,
                    "apply_decisions",
                    detail=f"accepted={applied}",
                    meta={"accepted": applied},
                )
                rec.end(
                    StageProjectId.p6,
                    output={
                        "status": "applied",
                        "accepted": applied,
                        "source": "conflict_review",
                    },
                )
                if job.stage_trace is not None:
                    job.stage_trace.status = "applied"
        except Exception:  # pragma: no cover - never block apply on obs
            pass
        await self._jobs.update(job)
        return applied
