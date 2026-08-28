from __future__ import annotations

"""Mapped / structured-row ingest + key-join.

Job: ingest pre-mapped records and PRE-STRUCTURED rows (no LLM extract).
Key-join rebinds a row onto an existing entity URI. Writes go through
``_ingest_mapped`` / ``insert_facts`` — do not add a second flush.
"""

from datetime import datetime, timezone
from uuid import uuid4

from infona_client.graph.ontology_queries import entities_by_key_value_query
from infona_client.graph.queries import tenant_graph_uri
from infona_client.pipeline.envelope import derive_fact_id
from infona_client.resolver.attribute_resolver import _normalize_attr_name
from infona_client.resolver.models import (
    CSVSchemaMapping,
    ExtractedEntity,
    ExtractionResult,
    IngestResult,
    KeyJoin,
    assert_soft_a2,
    soft_a2_from_structured_rows,
)
from infona_client.resolver.schema_ingest_struct import (
    _STRUCTURED_PROVENANCE_COLS,
    _project_structured_rows_to_attributes,
    _structured_rows_mapping,
)
# Call-time host lookups so tests that patch schema_resolver.logger /
# insert_facts / _entity_uri / env flags keep working after this extract.
from infona_client.resolver import schema_resolver as _sr


class SchemaIngestMappedMixin:
    """Mapped-record + structured-row + key-join half of SchemaResolver."""

    async def ingest_mapped_records(
        self,
        rows: list[dict[str, str]],
        mapping: CSVSchemaMapping,
        tenant_id: str,
        source: str = "",
        instance_graph: str | None = None,
        key_join: KeyJoin | None = None,
        run_id: str | None = None,
    ) -> IngestResult:
        """Ingest pre-mapped records (no schema inference) — the fixed-mapping seam.

        A caller infers a :class:`CSVSchemaMapping` once (e.g. from a sample at
        plan time) and applies that SAME mapping to the full record set here. The
        mapping is applied DETERMINISTICALLY (no LLM, no re-inference), so the
        schema previewed to the user is exactly the schema committed
        (preview == commit). This is the CSV path's guarantee; the web-DISCOVERY
        path instead routes through :meth:`ingest` (the non-deterministic
        ``_extract``), where the previewed shape is only a sample-based estimate,
        not an exact match. Records flow through the identical type-resolution,
        batch existence-dedup, ER and batch-insert path CSV ingest uses.

        Mirrors :meth:`ingest`'s per-call setup (instance graph, type-matcher
        graph URI, ontology + parent-map fetch) so it can be called standalone,
        not only inside the CSV pipeline.

        ``run_id`` (ONTA-372): the run-scoped lineage id, forwarded to
        :meth:`_ingest_mapped`. When set (the discovery structured fast-path), the
        batch_id is derived from it and an A6 Graph Delta keyed to it is emitted on
        the result — the SAME run identity the A1 Source Bundle carries. ``None``
        (the CSV route) keeps the fresh-uuid4-per-call behavior, unchanged.
        """
        graph_uri = tenant_graph_uri(tenant_id)
        # Ontology always goes to the base tenant graph; instance data goes to
        # instance_graph when a specific KG is targeted, else the base graph.
        # ONTA-268: CALL-LOCAL target + parent map threaded down the write path;
        # the `self.` attributes stay as the legacy fallback only.
        target_instance_graph = instance_graph or graph_uri
        self._instance_graph = target_instance_graph
        self._type_matcher._graph_uri = graph_uri
        layer_stack = self._layer_stack_for(tenant_id, graph_uri)
        existing_types, existing_attrs = await self._fetch_ontology(graph_uri)
        parent_of = await self._fetch_parent_map(graph_uri, layer_stack=layer_stack)
        self._parent_of = parent_of
        self._active_layer_stack = layer_stack
        return await self._ingest_mapped(
            mapping, rows, graph_uri, existing_types, existing_attrs, source,
            key_join=key_join,
            instance_graph=target_instance_graph, parent_of=parent_of,
            run_id=run_id,
        )

    async def ingest_structured_rows(
        self,
        rows: list[dict],
        tenant_id: str,
        type_name: str,
        attributes: list[str] | None = None,
        source: str = "",
        instance_graph: str | None = None,
        key_attribute: str | None = None,
        key_join: KeyJoin | None = None,
        run_id: str | None = None,
        fact_ids: list[str] | None = None,
        tier: str | None = None,
        attributes_exhaustive: bool = False,
    ) -> IngestResult:
        """FAST-PATH for PRE-STRUCTURED rows (ONTA-272) — no unstructured LLM ``_extract``.

        Pre-structured payloads (an API-registry pull with a known field mapping, a
        structured / extension capture) already arrive as clean rows keyed by the
        confirmed attribute set, so running the open-ended LLM extractor over them
        is a nonsensical, non-deterministic detour. This commits them through the
        SAME deterministic mapping seam CSV ingest uses (:meth:`ingest_mapped_records`
        → ``apply_mapping``, NO LLM): a fixed :class:`CSVSchemaMapping` with one
        column per field and the key attribute as the type-id.

        Before committing it materializes the SOFT-TYPED A2 witness for the rows
        (:func:`soft_a2_from_structured_rows`) and ASSERTS the zero-ontology-
        commitment contract (:func:`assert_soft_a2`) — pre-structured rows are
        inherently soft (candidate type, literal attributes, evidence = the
        per-record ``source_url``), so this can only fire on a genuine bug (fail
        fast). ``require_evidence`` is asserted only when the rows actually carry a
        ``source_url``, so a provenance-less structured source is not force-failed.
        Returns the SAME :class:`IngestResult` the deterministic path produces.

        ``attributes_exhaustive`` (ONTA-382, structured ceiling): when True with a
        non-empty ``attributes`` list, ONLY those attributes (+ key +
        ``source_url``) are written — rich provider payloads fetched via
        ``hint_columns`` no longer invent ontology columns the user did not
        confirm. Mirrors the LLM-extract path's allowlist under soft+exhaustive.

        ``run_id`` (ONTA-372): the run-scoped lineage id threaded from the discovery
        P1 entry (``web_ingest_cap``). Forwarded through
        :meth:`ingest_mapped_records` so the structured fast-path keys its batch_id
        and A6 Graph Delta off the SAME run as the A1 Source Bundle instead of a
        fresh uuid4. ``None`` (the default) preserves today's per-call behavior.

        ``fact_ids`` / ``tier`` (ONTA-371): the OPT-IN A1→A2 lineage handoff — the
        per-row A1 ``fact_id`` (row order) + source authority tier forwarded from
        the discovery capability's A1 Source Bundle. Recorded for lineage
        observability; the committed graph is byte-identical (the deterministic
        mapping seam is untouched). ``None`` for the CSV / non-discovery route.
        """
        if not rows:
            return IngestResult(rows_in=0)
        # ONTA-371: record the A1→A2 lineage handoff for the structured fast-path.
        # Observability only — the deterministic mapping write below is unchanged.
        if fact_ids or tier is not None:
            _sr.logger.debug(
                "a1_a2_lineage_handoff",
                path="ingest_structured_rows",
                run_id=run_id,
                source_fact_ids=len(fact_ids or ()),
                source_tier=tier,
            )
        # The key field is the join/identity column: an explicit key_attribute, else
        # the first confirmed attribute, else the row's natural "name".
        key_field = key_attribute or (attributes[0] if attributes else None) or "name"
        # ONTA-382 structured ceiling: clip rich provider rows before A2 + mapping.
        rows = _project_structured_rows_to_attributes(
            rows,
            key_field=key_field,
            attributes=attributes,
            attributes_exhaustive=bool(attributes_exhaustive),
        )
        if not rows:
            return IngestResult(rows_in=0)
        allowlist: frozenset[str] | None = None
        if attributes_exhaustive and attributes:
            allowlist = frozenset(
                {str(a) for a in attributes if a}
                | {key_field}
                | set(_STRUCTURED_PROVENANCE_COLS)
            )
        # A2 CONTRACT (zero ontology commitment): render the pre-structured rows as
        # candidate facts and assert soft-typed-only (+ evidence-linked where
        # provenance exists) at the point A2 is emitted.
        witness = soft_a2_from_structured_rows(rows, type_name, key_field=key_field)
        # Require evidence only when EVERY row carries a source_url — a
        # provenance-less (or mixed) structured source must never be force-failed by
        # the fatal assert; it still asserts soft-typed-only. Discovery micro-batches
        # are partitioned by source_url upstream, so the common case is all-or-none.
        require_evidence = bool(rows) and all(
            isinstance(r, dict) and str(r.get("source_url") or "").strip()
            for r in rows
        )
        assert_soft_a2(witness, require_evidence=require_evidence)
        mapping = _structured_rows_mapping(
            rows, type_name, key_field, attribute_allowlist=allowlist
        )
        return await self.ingest_mapped_records(
            rows, mapping, tenant_id, source=source,
            instance_graph=instance_graph, key_join=key_join,
            run_id=run_id,
        )

    async def _resolve_key_join(
        self,
        entities: list[ExtractedEntity],
        resolved_types: dict[str, str],
        entity_uri_map: dict[str, str],
        instance_graph: str,
        key_join: KeyJoin,
        result: IngestResult,
    ) -> set[str]:
        """Join-by-exact-key (ONTA-250): rebind each row-entity whose key value
        matches an EXISTING entity onto that existing node's URI, so Pass 2 merges
        the row's attributes onto it (via the shared write path) instead of
        minting a duplicate.

        The key value is the resolved value of ``key_join.key_attribute`` carried
        on the entity (the CSV key column lands the key as a regular attribute —
        ADR 0003 §2 "key-as-attribute"). For every entity of a type that has that
        attribute, we look up the existing entity(ies) whose
        ``attrs/<key_attribute>`` equals it (one batched SPARQL per type), and:

        - exactly one match → rebind ``entity_uri_map[id]`` to that URI (MERGE),
        - no match → leave the freshly-minted URI in place; the caller mints it
          only if ``mint_unmatched`` (else the id is returned as *skip*),
        - several matches → the key is not unique; leave as-is + log (treated as
          unmatched so we never silently merge onto an arbitrary one).

        Returns the set of entity ids to SKIP (unmatched + ``mint_unmatched`` is
        false). Mutates ``entity_uri_map`` in place for merged rows and records the
        merged/minted/unmatched counts on ``result``. Fully general over any
        (type, key-attribute); best-effort — a lookup failure degrades to
        ordinary minting (never blocks ingest)."""
        key_attr = _normalize_attr_name(key_join.key_attribute)

        # Group the incoming key value per entity id, bucketed by resolved type
        # (the lookup query is per-type). An entity with no value for the key
        # attribute cannot be joined — it is treated as unmatched.
        from infona_client.resolver.entity_map import map_key

        by_type: dict[str, dict[str, str]] = {}  # type -> {map_key: key_value}
        no_key: set[str] = set()
        for entity in entities:
            q = map_key(entity)
            if q not in resolved_types and entity.id not in resolved_types:
                continue
            rtype = resolved_types.get(q) or resolved_types[entity.id]
            val = next(
                (a.value for a in entity.attributes
                 if _normalize_attr_name(a.name) == key_attr and (a.value or "").strip()),
                None,
            )
            if val is None:
                no_key.add(q)
                continue
            by_type.setdefault(rtype, {})[q] = val.strip()

        # value -> existing URI(s), resolved per type via one batched query.
        matched_uri: dict[str, str] = {}   # entity.id -> existing URI
        ambiguous: set[str] = set()
        BATCH = 300
        for rtype, id_to_val in by_type.items():
            # Distinct values to look up (many rows may share a key value).
            distinct_vals = sorted({v for v in id_to_val.values()})
            val_to_uris: dict[str, list[str]] = {}
            for i in range(0, len(distinct_vals), BATCH):
                chunk = distinct_vals[i : i + BATCH]
                try:
                    sparql = entities_by_key_value_query(
                        instance_graph, rtype, key_attr, chunk,
                    )
                    res = await self._neptune.query(sparql)
                except Exception as e:  # best-effort — degrade to ordinary mint
                    _sr.logger.warning("key_join_lookup_failed", type=rtype, error=str(e))
                    continue
                for b in res.get("results", {}).get("bindings", []):
                    v = b.get("v", {}).get("value")
                    ent = b.get("entity", {}).get("value")
                    if v is not None and ent:
                        val_to_uris.setdefault(v, []).append(ent)
            for eid, val in id_to_val.items():
                uris = val_to_uris.get(val, [])
                if len(uris) == 1:
                    matched_uri[eid] = uris[0]
                elif len(uris) > 1:
                    ambiguous.add(eid)

        if ambiguous:
            _sr.logger.warning(
                "key_join_ambiguous",
                key_attribute=key_attr,
                count=len(ambiguous),
            )

        # Rebind merged rows onto the existing node; tally outcomes. Entities with
        # NO key value (``no_key`` — e.g. the mapping's relationship-target stubs,
        # or a row missing the key column) were never join CANDIDATES, so they
        # always mint and are NEVER force-skipped by mint_unmatched=false — that
        # flag governs only rows that HAD a key value but matched nothing (or
        # matched ambiguously). Otherwise strict mode would silently drop
        # relationship targets.
        skip: set[str] = set()
        for entity in entities:
            q = map_key(entity)
            if q not in resolved_types and entity.id not in resolved_types:
                continue
            if q in matched_uri:
                entity_uri_map[q] = matched_uri[q]
                # Sync the raw slot only when this id is still unique in-batch
                # (collided raw ids were popped from the map by register_entity).
                if entity.id in entity_uri_map:
                    entity_uri_map[entity.id] = matched_uri[q]
                result.rows_key_merged += 1
            elif q in no_key:
                # No key to join on → ordinary mint, unaffected by mint_unmatched.
                pass
            else:
                # Had a key value but no unique match (missed or ambiguous).
                if key_join.mint_unmatched:
                    result.rows_key_minted += 1
                else:
                    result.rows_key_unmatched += 1
                    skip.add(q)
                    if entity.id in entity_uri_map:
                        skip.add(entity.id)

        if result.rows_key_unmatched:
            _sr.logger.warning(
                "key_join_unmatched_skipped",
                key_attribute=key_attr,
                skipped=result.rows_key_unmatched,
            )
        _sr.logger.info(
            "key_join_resolved",
            key_attribute=key_attr,
            merged=result.rows_key_merged,
            minted=result.rows_key_minted,
            unmatched=result.rows_key_unmatched,
        )
        return skip
