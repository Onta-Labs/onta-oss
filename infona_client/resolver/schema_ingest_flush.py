from __future__ import annotations

"""Mapped-row flush: apply CSV mapping and write via insert_facts.

Job: the shared tail of CSV / mapped / structured ingest. Instance
relationship edges must land on ``onto/<leaf>``; entity IRIs via
``entity_uri``; flush only through ``insert_facts``.
"""

from datetime import datetime, timezone
from uuid import uuid4

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import build_graph_delta
from infona_client.graph.ontology_queries import PRIMITIVE_TYPES, batch_entity_exists_query
from infona_client.graph.store import resolve_optional_graph_store
from infona_client.pipeline.envelope import derive_fact_id
from infona_client.resolver.attribute_resolver import AttributeSchema
from infona_client.resolver.csv_resolver import CSVResolver
from infona_client.resolver.models import (
    CSVSchemaMapping,
    ExtractionResult,
    IngestResult,
    KeyJoin,
)
from infona_client.resolver.predicate_normalizer import normalize_predicate
# Call-time host lookups so tests that patch schema_resolver.logger /
# insert_facts / _entity_uri / env flags keep working after this extract.
from infona_client.resolver import schema_resolver as _sr


class SchemaIngestFlushMixin:
    """CSV/mapped flush half of SchemaResolver — one insert_facts write path."""

    async def _ingest_mapped(
        self,
        mapping: CSVSchemaMapping,
        rows: list[dict[str, str]],
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        source: str,
        key_join: KeyJoin | None = None,
        *,
        instance_graph: str | None = None,
        parent_of: dict[str, str] | None = None,
        run_id: str | None = None,
    ) -> IngestResult:
        """Apply a pre-inferred mapping to rows and run the resolve→insert tail.

        Extracted verbatim from the former ``_ingest_csv`` body (Step 2 onward)
        so CSV ingest and web-discovery ingest commit through one code path.

        ``run_id`` (ONTA-372): STABLE run identity threaded from the discovery
        structured fast-path (``web_ingest_cap`` → :meth:`ingest_structured_rows` →
        :meth:`ingest_mapped_records`). When set, the ``batch_id`` is DERIVED from
        it (replay-stable, mirroring :meth:`ingest`) and an A6 :class:`GraphDelta`
        keyed to it is emitted on ``result.graph_delta`` — the SAME run the A1
        Source Bundle carries, so discovery lineage no longer diverges. Instance
        + relationship triples always flush through the shared ``insert_facts``
        write (dual-routes Neptune SPARQL / GraphStore Neo4j). ``None`` (the CSV
        route) keeps the fresh-uuid4 batch_id and no delta — unchanged.

        ``key_join`` (ONTA-250): when set, each row is matched to an EXISTING
        entity by an exact key attribute and its attributes are merged ONTO that
        node instead of minting a duplicate — a first-class, deterministic
        complement to signal-ER. The merge rides the SAME resolve→insert tail (the
        row's minted URI is simply rebound to the existing node's URI before the
        write), so it flows through the shared write path untouched.

        ``instance_graph`` / ``parent_of`` (ONTA-268): CALL-LOCAL overrides; fall
        back to the ``self.`` attributes for legacy direct callers.
        """
        parent_of = self._parent_of if parent_of is None else parent_of
        # Resolve the call-local target graph ONCE up front so it is bound for the
        # rollback except-block too (ONTA-268).
        instance_graph = (
            instance_graph if instance_graph is not None
            else getattr(self, "_instance_graph", graph_uri)
        )
        from infona_client.resolver.csv_resolver import CSVResolver

        # Step 2: Apply mapping deterministically to ALL rows (no LLM)
        applied = CSVResolver.apply_mapping(mapping, rows)
        entities, relationships = applied.entities, applied.relationships

        # Step 3: Resolve entities + insert in batches. ONTA-372: when a run_id is
        # threaded (discovery structured fast-path), DERIVE the batch_id from it so
        # a preserved-run_id replay reuses the same batch token (idempotent
        # BATCH_PREDICATE triple), mirroring the LLM-extract `ingest` path; the CSV
        # route (run_id=None) keeps a fresh uuid4 per call — unchanged.
        # Instance triples always accumulate for ONE shared insert_facts flush
        # (Neo4j-safe; dual-routes). When run_id is threaded the same collector
        # also feeds the A6 Graph Delta projection below.
        batch_id = (
            derive_fact_id(run_id=run_id, stage="A6-batch") if run_id else str(uuid4())
        )
        collected_entity_triples: list[tuple[str, str, str]] = []
        collected_provenance_triples: list[tuple[str, str, str]] = []
        result = IngestResult(
            entities_extracted=len(entities),
            chunks_processed=1,
            batch_id=batch_id,
            # Row-conservation accounting (ADR 0003 §2).
            rows_in=applied.rows_in,
            rows_dropped=applied.rows_dropped,
            drops_by_entity=applied.drops_by_entity,
        )
        entity_uri_map: dict[str, str] = {}
        entity_type_map: dict[str, str] = {}

        try:
            # Pass 1: Resolve types and compute URIs
            pending_uris: list[str] = []
            resolved_types: dict[str, str] = {}
            # Mapping-declared type name -> resolved ontology type name, so the
            # schema-time text_kind verdicts (keyed by the mapping's types) can
            # target the attr URIs actually written (ONTA-177). setdefault: the
            # first resolution wins, matching how attributes are declared.
            resolved_by_decl_type: dict[str, str] = {}
            for i, entity in enumerate(entities):
                if i > 0 and i % self.ONTOLOGY_REFRESH_INTERVAL == 0:
                    await self._refresh_ontology(graph_uri, existing_types, existing_attrs)

                resolved_type = await self._resolve_type(
                    entity, graph_uri, existing_types, existing_attrs, result,
                    parent_of=parent_of,
                )
                if resolved_type:
                    resolved_types[entity.id] = resolved_type
                    resolved_by_decl_type.setdefault(entity.type_name, resolved_type)
                    entity_uri = _sr._entity_uri(resolved_type, entity.id)
                    entity_uri_map[entity.id] = entity_uri
                    entity_type_map[entity.id] = resolved_type

            # instance_graph resolved once at method top (ONTA-268 call-local).

            # ONTA-250 join-by-exact-key: rebind matched rows onto the EXISTING
            # node's URI BEFORE the existence check, so a merged row's URI is seen
            # as a duplicate (Pass 2 merges attributes, skips a second rdf:type)
            # and never mints a parallel node. Runs on the mapping's stub
            # relationship-target entities too, but those carry no key value so
            # they fall through as unmatched-minted (unchanged). Returns the ids
            # to SKIP entirely (unmatched + mint_unmatched=false).
            skip_ids: set[str] = set()
            if key_join is not None:
                skip_ids = await self._resolve_key_join(
                    entities, resolved_types, entity_uri_map,
                    instance_graph, key_join, result,
                )

            # Only URIs we will actually write get the existence check.
            pending_uris = [
                entity_uri_map[e.id]
                for e in entities
                if e.id in resolved_types and e.id not in skip_ids
            ]

            # Batch existence check (SPARQL). Skip when GraphStore is live.
            existing_uris: set[str] = set()
            _use_sparql_exists = True
            try:
                from infona_client.graph.store import get_graph_store

                get_graph_store()
                _use_sparql_exists = False
            except Exception:  # noqa: BLE001
                _use_sparql_exists = True
            if _use_sparql_exists:
                BATCH_CHECK_SIZE = 500
                for i in range(0, len(pending_uris), BATCH_CHECK_SIZE):
                    batch = pending_uris[i : i + BATCH_CHECK_SIZE]
                    sparql = batch_entity_exists_query(instance_graph, batch)
                    found = await self._neptune.batch_exists(sparql)
                    existing_uris.update(found)
            if existing_uris:
                _sr.logger.info("csv_batch_dedup_found", existing=len(existing_uris), total=len(pending_uris))

            # Pass 2: Resolve attributes and insert
            for entity in entities:
                if entity.id not in resolved_types:
                    continue
                if entity.id in skip_ids:
                    continue  # key-join unmatched with mint_unmatched=false
                resolved_type = resolved_types[entity.id]
                entity_uri = entity_uri_map[entity.id]
                is_duplicate = entity_uri in existing_uris
                if is_duplicate:
                    result.entities_deduplicated += 1
                await self._resolve_and_insert_entity(
                    entity, resolved_type, entity_uri, is_duplicate,
                    graph_uri, existing_types, existing_attrs, source, result, batch_id,
                    # Always collect — flush via shared insert_facts below (Neo4j-
                    # safe dual-route). Also feeds A6 Graph Delta when run_id set.
                    _collect_triples=collected_entity_triples,
                    _collect_provenance=collected_provenance_triples,
                    instance_graph=instance_graph,  # ONTA-268: call-local target
                )

            # Flush collected instance (+ companion provenance) triples through
            # the SAME shared write path as the extract / enrichment rails.
            # ONTA-528: count triples_inserted only after insert_facts succeeds
            # (never on collect inside `_resolve_and_insert_entity`).
            if collected_entity_triples or collected_provenance_triples:
                await _sr.insert_facts(
                    self._neptune,
                    instance_graph,
                    collected_entity_triples,
                    provenance_triples=collected_provenance_triples or None,
                    store=resolve_optional_graph_store(),
                )
                result.triples_inserted += len(collected_entity_triples)

            # ONTA-177: persist the schema pass's free-text verdicts (the
            # mapping's per-column text_kind, decided ONCE at schema-inference
            # time by the REASON pass + name-blind auto tier) as textKind
            # ontology markers on the resolved attribute URIs. No re-decision
            # here — a legacy/hand-written mapping without text_kind writes no
            # markers (candidacy undecided; ONTA-181f's reconciler-side
            # heuristic covers those attributes later).
            await self._apply_mapping_text_markers(
                mapping, resolved_by_decl_type, graph_uri, result,
            )

            # Step 4: Batch-insert relationships
            rel_triples: list[tuple[str, str, str]] = []
            for rel in relationships:
                # An edge whose source or target was skipped (key-join unmatched
                # with mint_unmatched=false) has no node to hang off — drop it.
                if rel.source_id in skip_ids or rel.target_id in skip_ids:
                    continue
                source_uri = entity_uri_map.get(rel.source_id)
                target_uri = entity_uri_map.get(rel.target_id)
                if source_uri and target_uri:
                    # Normalize predicate against existing predicates on this type
                    source_type = entity_type_map.get(rel.source_id)
                    existing_preds = set()
                    if source_type:
                        for attr_name, schema in existing_attrs.get(source_type, {}).items():
                            if schema.datatype not in PRIMITIVE_TYPES:
                                existing_preds.add(attr_name)
                    canonical_pred = normalize_predicate(rel.predicate, existing_preds)

                    predicate = f"{IRI_BASE}/onto/{canonical_pred}"
                    rel_triples.append((source_uri, predicate, target_uri))

                    # Register relationship as object property in ontology
                    target_type = entity_type_map.get(rel.target_id)
                    if source_type and target_type:
                        type_attrs = existing_attrs.get(source_type, {})
                        existing = type_attrs.get(canonical_pred)
                        if existing is None:
                            await self._commit_ontology(graph_uri, [
                                self._mut_attr(source_type, canonical_pred, target_type),
                            ])
                            result.attributes_added.append(f"{source_type}.{canonical_pred}")
                            existing_attrs.setdefault(source_type, {})[canonical_pred] = AttributeSchema(
                                name=canonical_pred, datatype=target_type,
                            )
                        elif existing.datatype in PRIMITIVE_TYPES:
                            # Upgrade a primitive attribute to a relationship range
                            # so the Explorer overview draws the edge (see entity
                            # ingest path above for the full rationale).
                            await self._commit_ontology(graph_uri, [
                                self._mut_range(source_type, canonical_pred, target_type),
                            ])
                            existing_attrs[source_type][canonical_pred] = AttributeSchema(
                                name=canonical_pred, datatype=target_type,
                            )

            # Same shared write path as entity facts (insert_facts). Mirrors the
            # extract-path fix (#330): SPARQL batched_insert 500s on Neo4j.
            if rel_triples:
                await _sr.insert_facts(
                    self._neptune,
                    instance_graph,
                    rel_triples,
                    store=resolve_optional_graph_store(),
                )
            result.triples_inserted += len(rel_triples)

            result.entities_resolved = len(entity_uri_map)
            _sr.logger.info(
                "csv_ingest_complete",
                rows=len(rows),
                entities=result.entities_resolved,
                triples=result.triples_inserted,
                types=result.types_created,
            )
            # ONTA-372: emit the run's deterministic A6 Graph Delta when a run_id
            # was threaded (discovery structured fast-path), keyed to the SAME
            # run_id as the A1 Source Bundle so discovery lineage no longer
            # diverges. Built over the COMPLETE instance facts (entity triples +
            # relationship triples) via the shared `build_graph_delta` — the same
            # projection the LLM-extract path emits. `fan_in` records key-join
            # merges: >1 source entity id resolving to ONE final URI is a merge, so
            # the non-canonical sources' natural URIs map to the shared node.
            if run_id is not None:
                ids_by_uri: dict[str, list[str]] = {}
                for eid, uri in entity_uri_map.items():
                    ids_by_uri.setdefault(uri, []).append(eid)
                fan_in: dict[str, str] = {}
                for uri, eids in ids_by_uri.items():
                    if len(eids) > 1:
                        for eid in eids:
                            natural = _sr._entity_uri(entity_type_map.get(eid, ""), eid)
                            if natural != uri:
                                fan_in[natural] = uri
                result.graph_delta = build_graph_delta(
                    instance_graph,
                    (collected_entity_triples or []) + rel_triples,
                    run_id=run_id,
                    fan_in=fan_in,
                ).to_dict()
            return result

        except Exception:
            _sr.logger.error(
                "csv_ingest_failed_rolling_back",
                batch_id=batch_id,
                entities_so_far=result.entities_resolved,
                exc_info=True,
            )
            # instance_graph resolved once at method top (ONTA-268 call-local).
            # ONTA-528: no NeptuneClient.update / SPARQL delete_batch — see
            # the extract-path rollback note above.
            _sr.logger.info(
                "csv_batch_rollback_skipped",
                batch_id=batch_id,
                instance_graph=instance_graph,
                reason="delete_batch not ported to GraphStore (ONTA-528)",
            )
            raise
