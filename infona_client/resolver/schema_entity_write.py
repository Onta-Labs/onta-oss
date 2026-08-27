from __future__ import annotations

"""Entity-batch resolve + insert for text/JSON/web-discovery ingest.

Job: two-pass resolve (types then attributes) and flush instance triples
through ``insert_facts``. Relationship INSTANCE edges go on
``onto/<leaf>`` (never ``attrs/<leaf>``). Entity IRIs via ``entity_uri``.
"""

import os

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import build_graph_delta
from infona_client.graph.ontology_queries import PRIMITIVE_TYPES, batch_entity_exists_query
from infona_client.graph.store import resolve_optional_graph_store
from infona_client.resolver.attribute_resolver import AttributeSchema
from infona_client.resolver.models import ExtractionResult, IngestResult, KeyJoin
from infona_client.resolver.entity_map import (
    SEP,
    lookup_type,
    lookup_uri,
    map_key,
    qualified_count,
    register_entity,
    rel_source_key,
    rel_target_key,
)
from infona_client.resolver.predicate_normalizer import normalize_predicate
from infona_client.resolver.schema_focus import _primary_entity_ids
# Call-time host lookups so tests that patch schema_resolver.logger /
# insert_facts / _entity_uri / env flags keep working after this extract.
from infona_client.resolver import schema_resolver as _sr


class SchemaEntityWriteMixin:
    """Batch resolve-and-insert half of SchemaResolver."""

    async def _resolve_and_insert(
        self,
        extraction: ExtractionResult,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        source: str,
        result: IngestResult,
        entity_uri_map: dict[str, str],
        entity_type_map: dict[str, str],
        batch_id: str,
        decide_text_candidacy: bool = False,
        key_join: KeyJoin | None = None,
        *,
        instance_graph: str | None = None,
        parent_of: dict[str, str] | None = None,
        ontology_version_stamp: str | None = None,
        run_id: str | None = None,
        observed_at: datetime | None = None,
        workspace_id: str | None = None,
        focus_types: list[str] | None = None,
    ) -> IngestResult:
        """Inner pipeline: resolve entities, insert triples. Separated for rollback.

        Two-pass architecture for I/O efficiency:
          Pass 1: Resolve types for all entities, compute URIs
          Batch check: Which URIs already exist in Neptune (one query per 500)
          Pass 2: Resolve attributes, validate, insert triples

        ``decide_text_candidacy`` (ONTA-177): when True, string-attribute
        values are sampled during pass 2 and free-text candidacy is decided +
        persisted as ``textKind`` ontology markers after the write — set by
        :meth:`ingest` (text/JSON/web-discovery), where this call IS the
        schema pass. Deliberately OFF by default: ``/ingest/csv/rows`` calls
        this method with a client-supplied mapping and never runs a schema
        pass — its contract is "no LLM call", and its candidacy is covered
        later by a reconciler-side default heuristic (ONTA-181).

        ``instance_graph`` / ``parent_of`` (ONTA-268, reentrancy): CALL-LOCAL
        overrides threaded from :meth:`ingest`. When ``None`` (legacy direct
        callers — the ``/ingest/csv/rows`` route sets ``self._instance_graph``
        and unit tests seed ``self._parent_of``) they fall back to the instance
        attributes. On the reentrant ``ingest`` path they carry per-call state so
        two interleaved ingests never read each other's target graph / parent map.

        ``ontology_version_stamp`` (ONTA-270): the ontology fingerprint
        :meth:`ingest` computed for the A5 placement plan. When set, the apply is
        an optimistic-concurrency P6: before pass 1 computes any placement we
        reconcile the stamp against the CURRENT ontology and reject-and-recompute
        a stale plan (see :meth:`_reconcile_ontology_version`). ``None`` (legacy
        direct callers) skips the guard, preserving today's behavior exactly.

        ``run_id`` / ``observed_at`` (ONTA-271): stable run identity + the run's
        ingested_at stamp, threaded call-local (alongside ``instance_graph`` /
        ``parent_of``). ``observed_at`` is passed to each per-entity write so the
        ``onto/ingested_at`` triple is replay-stable; ``run_id`` keys the A6
        :class:`GraphDelta` receipt built on ``result.graph_delta`` at the end,
        so a preserved-run_id replay reproduces a byte-identical delta. Both
        ``None`` (legacy direct callers) → no delta, wall-clock ingested_at.

        ``focus_types`` (ONTA-383): soft-mode discovery focus (``proposed_type``).
        When set, each focus type is pre-seeded so the type matcher can prefer
        SUBTYPE over free-standing peers, primary records with no parent are
        anchored under the focus, and junk property-class types are rejected.
        ``None`` (default, every non-soft / open path) leaves type resolution
        unchanged.
        """
        instance_graph = (
            instance_graph if instance_graph is not None
            else getattr(self, "_instance_graph", graph_uri)
        )
        parent_of = self._parent_of if parent_of is None else parent_of
        # ONTA-270: P6 optimistic-concurrency guard. If a concurrent run advanced
        # the ontology while we were extracting, the snapshot this plan was
        # computed against is STALE and applying it verbatim mints duplicate
        # terms; reconcile brings the in-place snapshot current so pass 1 resolves
        # against the new version. No-op (single cheap read) when nothing raced.
        if ontology_version_stamp is not None:
            await self._reconcile_ontology_version(
                graph_uri, ontology_version_stamp,
                existing_types, existing_attrs, parent_of,
            )
        # ONTA-177: (resolved_type, attr_name) -> sampled string values,
        # filled by _resolve_and_insert_entity during pass 2.
        text_values: dict[tuple[str, str], list[str]] | None = (
            {} if decide_text_candidacy else None
        )

        # ONTA-383: primary vs dimension entity ids. A dimension node is only a
        # relationship TARGET (City, Specialty, …); a primary record is a source
        # (or an orphan with no edges). Consolidation under focus_types applies
        # only to primaries so dimension nodes keep free minting.
        focus_types = list(focus_types) if focus_types else None
        primary_ids: set[str] | None = None
        if focus_types:
            primary_ids = _primary_entity_ids(extraction)
            await self._ensure_focus_types(
                focus_types, graph_uri, existing_types, existing_attrs, result,
            )

        # Pass 1: Resolve types and compute entity URIs
        resolved_types: dict[str, str] = {}  # entity.id → resolved_type
        pending_uris: list[str] = []
        # ER index triples (block keys + denormalized signals) for newly minted
        # entities. Empty for merged/dedup'd entities.
        er_index_triples: list[tuple[str, str, str]] = []
        # Genuine independent co-classifications per entity id (ADR rule 1).
        # Empty for the common single-type case.
        entity_also_types: dict[str, list[str]] = {}
        # Track which entity IDs were merged into existing URIs (for telemetry)
        er_merged_count = 0
        unqualified_owner: dict[str, str] = {}
        collided_ids: set[str] = set()
        for i, entity in enumerate(extraction.entities):
            if i > 0 and i % self.ONTOLOGY_REFRESH_INTERVAL == 0:
                await self._refresh_ontology(graph_uri, existing_types, existing_attrs)

            resolved_type = await self._resolve_type(
                entity, graph_uri, existing_types, existing_attrs, result,
                parent_of=parent_of,
                focus_types=focus_types,
                is_primary=None if primary_ids is None else entity.id in primary_ids,
            )
            if resolved_type:
                # Resolve genuine co-types so they exist in the ontology; record
                # them for the multi-type write in pass 2. The declared primary
                # type (resolved_type) still owns URI minting + ER.
                also = await self._resolve_also_types(
                    entity, resolved_type, graph_uri, existing_types, existing_attrs, result,
                    parent_of=parent_of,
                )
                if also:
                    entity_also_types[map_key(entity)] = also
                entity_uri = _sr._entity_uri(resolved_type, entity.id)

                # Cross-file ER: see if this entity matches an existing one.
                # Failures here MUST never block ingest — log and fall through.
                if self._er_enabled:
                    try:
                        from infona_client.resolver.er import MergeAction, config_for_with_hierarchy
                        # Climb the subclass chain so a granular leaf (HotelGuest)
                        # inherits a configured ancestor's (Guest) ER config and
                        # ER fires on the subtype.
                        er_config = config_for_with_hierarchy(resolved_type, parent_of)
                        er_applies = er_config is not None
                        type_uri = f"{IRI_BASE}/types/{resolved_type}"
                        decision = await self._er.find_match(
                            entity, resolved_type, type_uri, instance_graph,
                            config=er_config, parent_of=parent_of,
                        )
                        if decision.action == MergeAction.AUTO_MERGE and decision.canonical_uri:
                            entity_uri = decision.canonical_uri
                            er_merged_count += 1
                            # Merge expansion: write the incoming entity's
                            # ER signals onto the CANONICAL URI so future
                            # ingests can find this same person via the new
                            # signals (e.g. a CRM merge adds the secondary
                            # email as an alias of the canonical Guest,
                            # letting a Loyalty ingest match later via that
                            # email). Triples are idempotent on Neptune.
                            normalized, keys = self._er.signals_and_keys(entity)
                            if normalized and keys:
                                er_index_triples.extend(
                                    self._er._blocker.index_triples(entity_uri, normalized, keys)
                                )
                        else:
                            # No match — mint a new URI via entity_uri(type, id).
                            #
                            # We deliberately do NOT append an ER signal-hash
                            # suffix. That suffix was intended to keep two
                            # "John Smith"s apart, but it silently forked every
                            # multi-file join: customers.csv (email present)
                            # minted Customer/C1001-<hash> while orders.csv FK
                            # stubs minted Customer/C1001 — multi-hop then
                            # returned empty on a graph that looked fine
                            # (OSS dogfood S2/S5). Cross-rail URI convergence
                            # (entity_uri) is the join contract; disambiguation
                            # of same-name different people is ER's merge path
                            # + future explicit type_id / decisive signals, not
                            # a mint-time URI mutation.
                            #
                            # Opt back into the old suffix with
                            # INFONA_ER_FINGERPRINT=1 (not recommended for
                            # multi-table CSVs).
                            normalized, keys = self._er.signals_and_keys(entity)
                            if (
                                er_applies
                                and os.environ.get("INFONA_ER_FINGERPRINT", "0") == "1"
                                and normalized is not None
                            ):
                                import hashlib
                                fingerprint_parts = [
                                    normalized.email or "",
                                    normalized.phone_e164 or "",
                                    normalized.dob_iso or "",
                                    "|".join(normalized.email_aliases),
                                ]
                                if any(fingerprint_parts):
                                    fp = hashlib.sha1(
                                        "|".join(fingerprint_parts).encode("utf-8")
                                    ).hexdigest()[:8]
                                    entity_uri = f"{entity_uri}-{fp}"
                            if normalized and keys:
                                er_index_triples.extend(
                                    self._er._blocker.index_triples(entity_uri, normalized, keys)
                                )
                    except Exception as e:
                        _sr.logger.warning("er_pipeline_failed", error=str(e), entity_id=entity.id)

                register_entity(
                    declared_type=entity.type_name,
                    entity_id=entity.id,
                    resolved_type=resolved_type,
                    uri=entity_uri,
                    uri_map=entity_uri_map,
                    type_map=entity_type_map,
                    resolved_types=resolved_types,
                    unqualified_owner=unqualified_owner,
                    collided=collided_ids,
                )
                pending_uris.append(entity_uri)
        if er_merged_count:
            _sr.logger.info("er_merged_entities", count=er_merged_count, total=len(extraction.entities))

        # ONTA-250 join-by-exact-key: rebind each row-entity whose key value
        # matches an EXISTING entity onto that nodef's URI, so the existence check
        # below sees a duplicate (Pass 2 merges attributes, skips a second
        # rdf:type/label) instead of minting a parallel node. Runs AFTER ER so a
        # caller-declared exact key wins over signal-based minting. Returns the ids
        # to SKIP (unmatched with mint_unmatched=false).
        skip_ids: set[str] = set()
        if key_join is not None:
            skip_ids = await self._resolve_key_join(
                extraction.entities, resolved_types, entity_uri_map,
                instance_graph, key_join, result,
            )
            # Only URIs we will actually write get the existence check.
            pending_uris = []
            for e in extraction.entities:
                q = map_key(e)
                if q not in resolved_types and e.id not in resolved_types:
                    continue
                if q in skip_ids or e.id in skip_ids:
                    continue
                uri = lookup_uri(entity_uri_map, e.id, e.type_name)
                if uri:
                    pending_uris.append(uri)

        # Batch existence check: one SPARQL query per 500 URIs instead of N individual ASKs.
        # On Neo4j/GraphStore, SPARQL is unavailable — skip (insert_facts is idempotent
        # MERGE); a GraphStore existence probe can land later.
        existing_uris: set[str] = set()
        _use_sparql_exists = True
        try:
            from infona_client.graph.store import GraphConfigError, get_graph_store

            get_graph_store()
            _use_sparql_exists = False
        except Exception:  # noqa: BLE001 — GraphConfigError or import
            _use_sparql_exists = True
        if _use_sparql_exists:
            BATCH_CHECK_SIZE = 500
            for i in range(0, len(pending_uris), BATCH_CHECK_SIZE):
                batch = pending_uris[i : i + BATCH_CHECK_SIZE]
                sparql = batch_entity_exists_query(instance_graph, batch)
                found = await self._neptune.batch_exists(sparql)
                existing_uris.update(found)
        if existing_uris:
            _sr.logger.info("batch_dedup_found", existing=len(existing_uris), total=len(pending_uris))

        # Pass 2: Resolve attributes, validate, collect triples
        # All entity triples are collected into one list, then batch-inserted
        # in a single call. This is ~10-50x faster than per-entity INSERT.
        all_entity_triples: list[tuple[str, str, str]] = []
        # Provenance collector (COG-46): statement-metadata triples for the
        # COMPANION provenance graph accumulate here during entity processing
        # and flush in one batched INSERT below, instead of one awaited
        # Neptune update per entity. Stays empty unless the flag is on.
        all_provenance_triples: list[tuple[str, str, str]] = []
        for entity in extraction.entities:
            q = map_key(entity)
            if q not in resolved_types and entity.id not in resolved_types:
                continue
            if q in skip_ids or entity.id in skip_ids:
                continue  # key-join unmatched with mint_unmatched=false
            resolved_type = resolved_types.get(q) or resolved_types[entity.id]
            entity_uri = lookup_uri(entity_uri_map, entity.id, entity.type_name)
            if not entity_uri:
                continue
            is_duplicate = entity_uri in existing_uris

            if is_duplicate:
                result.entities_deduplicated += 1

            await self._resolve_and_insert_entity(
                entity, resolved_type, entity_uri, is_duplicate,
                graph_uri, existing_types, existing_attrs, source, result, batch_id,
                _collect_triples=all_entity_triples,
                _collect_provenance=all_provenance_triples,
                also_types=entity_also_types.get(map_key(entity)) or entity_also_types.get(entity.id),
                _collect_text_values=text_values,
                # ONTA-259: this is the model-proposed extraction path (text /
                # JSON / web-discovery), the only rail where an LLM can invent an
                # identifier value — enable the anti-fabrication backstop here.
                # The CSV path (`_ingest_mapped`) leaves it off: cells are
                # authoritative and written verbatim.
                drop_placeholder_values=True,
                instance_graph=instance_graph,  # ONTA-268: call-local target
                observed_at=observed_at,  # ONTA-271: replay-stable ingested_at
            )

        # Append ER index triples (block keys + denormalized signals) to the
        # same batch so future ingests can find these entities in O(1).
        if er_index_triples:
            all_entity_triples.extend(er_index_triples)

        # ONTA-370: A4 Verify seam — the OPT-IN wedge between the A3 clean ledger
        # (`result.clean_report`, complete now that the per-entity loop above has
        # run) and the write below. DEFAULT-OFF: with no VerifyPolicy configured
        # this short-circuits BEFORE constructing a verifier or iterating facts, so
        # the `insert_facts` write and the returned result are byte-identical. When
        # a policy turns it on, it stamps VerifiedFacts on the result. It sits
        # before the write and is read-only — it never forks the converged writer.
        self._verify_clean_facts(result, workspace_id=workspace_id, run_id=run_id)

        # ONTA-375: PERSIST each A4 verdict as an attr_meta/ companion. DEFAULT-OFF
        # no-op (empty verified_facts ⇒ [] ⇒ byte-identical write). When the seam ran,
        # the verdict companions are appended to the SAME instance-triple collector,
        # so they flow through the shared insert_facts write below (never a bespoke
        # insert) onto an internal predicate, invisible to every user surface but
        # queryable by the P7 answer layer.
        verdict_companions = self._verdict_companion_triples(
            result, entity_uri_map, entity_type_map,
        )
        if verdict_companions:
            all_entity_triples.extend(verdict_companions)

        # Single shared write path (graph/kg_writer.py) — the SAME function the
        # enrichment writer uses: batched instance-triple insert + the companion
        # provenance graph, in one place, so ingestion and enrichment can never
        # drift on HOW facts are written. (Per-fact provenance is flushed in one
        # batched INSERT per ingest, COG-46 — the exact triples a per-entity
        # write would produce; only the write pattern is batched.)
        # E7: resolve GraphStore once per write batch when neo4j backend is
        # active; None keeps the Neptune SPARQL default.
        # ONTA-528: triples_inserted counts only facts that actually landed —
        # increment AFTER a successful insert_facts, never on collect/stage.
        if all_entity_triples or all_provenance_triples:
            # instance_graph resolved once at method top (ONTA-268 call-local).
            await _sr.insert_facts(
                self._neptune,
                instance_graph,
                all_entity_triples,
                provenance_triples=all_provenance_triples or None,
                store=resolve_optional_graph_store(),
            )
            result.triples_inserted += len(all_entity_triples)

        # ONTA-177: decide + persist free-text candidacy for the attributes this
        # schema pass touched — written alongside the other attribute upserts of
        # pass 2, best-effort (never blocks or fails ingest).
        if text_values:
            await self._mark_free_text_attributes(graph_uri, text_values, result)

        # Incrementally embed newly created types for future embedding pre-filter matches
        if result.types_created and self._embedding_service is not None:
            try:
                await self._embedding_service.embed_types(
                    graph_uri, result.types_created, self._neptune,
                )
                _sr.logger.info("embedded_new_types", count=len(result.types_created))
            except Exception:
                _sr.logger.warning("embed_new_types_failed", exc_info=True)

        # ONTA-537: keep the NL mention index in sync as the ontology expands
        # (ask-time type/rel resolve). Best-effort — never blocks ingest.
        # Requires embed API key; hermetic / no-key deploys skip until configured.
        if result.types_created:
            try:
                from infona_client.nlp.ontology_mention_index import reindex_types

                api_key = (getattr(self, "_openrouter_key", "") or "").strip()
                if api_key:
                    parent_of = getattr(self, "_parent_of", None) or {}
                    specs = [
                        {
                            "name": tn,
                            "description": "",
                            "parents": (
                                [parent_of[tn]]
                                if isinstance(parent_of, dict) and tn in parent_of
                                else []
                            ),
                        }
                        for tn in result.types_created
                    ]
                    await reindex_types(
                        specs,
                        api_key=api_key,
                        child_to_parent=(
                            parent_of if isinstance(parent_of, dict) else None
                        ),
                        replace=False,
                    )
            except Exception:
                _sr.logger.warning("ontology_mention_reindex_failed", exc_info=True)

        # Step 4: Insert relationships (instance triples to instance graph, ontology to base graph)
        # instance_graph resolved once at method top (ONTA-268 call-local).
        rel_triples: list[tuple[str, str, str]] = []
        for rel in extraction.relationships:
            # An edge whose source or target was skipped (key-join unmatched with
            # mint_unmatched=false) has no node to hang off — drop it.
            src_k = rel_source_key(rel)
            tgt_k = rel_target_key(rel)
            if (
                rel.source_id in skip_ids or rel.target_id in skip_ids
                or src_k in skip_ids or tgt_k in skip_ids
            ):
                continue
            source_uri = lookup_uri(
                entity_uri_map, rel.source_id, getattr(rel, "source_type", None),
            )
            target_uri = lookup_uri(
                entity_uri_map, rel.target_id, getattr(rel, "target_type", None),
            )
            if source_uri and target_uri:
                # Normalize predicate against existing predicates on this type
                source_type = lookup_type(
                    entity_type_map, rel.source_id, getattr(rel, "source_type", None),
                )
                existing_preds = set()
                if source_type:
                    for attr_name, schema in existing_attrs.get(source_type, {}).items():
                        if schema.datatype not in PRIMITIVE_TYPES:
                            existing_preds.add(attr_name)
                canonical_pred = normalize_predicate(rel.predicate, existing_preds)

                predicate = f"{IRI_BASE}/onto/{canonical_pred}"
                rel_triples.append((source_uri, predicate, target_uri))

                # Register relationship as object property in ontology
                target_type = lookup_type(
                    entity_type_map, rel.target_id, getattr(rel, "target_type", None),
                )
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
                        # First seen as a primitive attribute, now carrying an
                        # entity object: upgrade its ontology range to the target
                        # type so the schema-only Explorer overview draws the edge
                        # (the detail view already shows it from instance data).
                        await self._commit_ontology(graph_uri, [
                            self._mut_range(source_type, canonical_pred, target_type),
                        ])
                        existing_attrs[source_type][canonical_pred] = AttributeSchema(
                            name=canonical_pred, datatype=target_type,
                        )

        # Batch insert relationship triples — same shared write path as entity
        # facts (insert_facts). On Neo4j, SPARQL batched_insert is unavailable;
        # previously this forked to Neptune-only and 500'd the bookstore multi-
        # entity inferred path when GraphStore is the live backend.
        if rel_triples:
            await _sr.insert_facts(
                self._neptune,
                instance_graph,
                rel_triples,
                store=resolve_optional_graph_store(),
            )
            result.triples_inserted += len(rel_triples)

        result.entities_resolved = qualified_count(entity_uri_map) or len(entity_uri_map)

        # ONTA-271: emit the run's deterministic A6 Graph Delta receipt. Built
        # over the COMPLETE set of instance facts this run wrote (entity triples
        # + relationship triples), via the shared `build_graph_delta` — the same
        # projection `insert_facts` returns for its own portion. We assemble it
        # HERE rather than take `insert_facts`'s return because relationship
        # triples are written after it (second insert_facts call on Neo4j), so
        # only the run owner sees every fact.
        # Nonces (ingested_at/batch_id) are projected out and each fact is keyed
        # by its stable fact_id, so a preserved-run_id replay reproduces byte-
        # identical bytes and P6 dedupes it. `fan_in` records source facts that
        # merged onto one node (ER auto-merge, key-join, in-run same-key dedup):
        # >1 source entity id resolving to the SAME final URI is a merge, so the
        # non-canonical sources' natural URIs map to the shared node.
        if run_id is not None:
            ids_by_uri: dict[str, list[str]] = {}
            for eid, uri in entity_uri_map.items():
                if SEP not in eid:
                    continue
                ids_by_uri.setdefault(uri, []).append(eid)
            fan_in: dict[str, str] = {}
            for uri, eids in ids_by_uri.items():
                if len(eids) > 1:
                    for eid in eids:
                        _decl, raw = eid.split(SEP, 1)
                        natural = _sr._entity_uri(entity_type_map.get(eid, ""), raw)
                        if natural != uri:
                            fan_in[natural] = uri
            result.graph_delta = build_graph_delta(
                instance_graph,
                all_entity_triples + rel_triples,
                run_id=run_id,
                fan_in=fan_in,
            ).to_dict()

        _sr.logger.info(
            "ingest_complete",
            entities_resolved=result.entities_resolved,
            triples_inserted=result.triples_inserted,
            types_created=result.types_created,
            rejections=len(result.rejections),
        )
        return result
