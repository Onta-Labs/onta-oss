"""Deterministic CSV row → entity mapping (no LLM).

Ids are slugged via the shared ``_safe_id`` (entity IRIs via ``entity_uri``
on the write path). This module does not write to the graph.
"""

from __future__ import annotations

import structlog

from infona_client.graph.ontology_queries import _safe_id
from infona_client.resolver.csv_extensions import _ExtensionApplier
from infona_client.resolver.csv_helpers import (
    _cell,
    _is_opaque_identifier,
    _rel_values,
    _snake_case,
    _synthetic_key,
)
from infona_client.resolver.csv_mapping import AppliedMapping
from infona_client.resolver.models import (
    ColumnMapping,
    ColumnRole,
    CSVSchemaMapping,
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
)


logger = structlog.stdlib.get_logger("infona.resolver.csv")


def _host():
    from infona_client.resolver import csv_resolver as _mod

    return _mod


class CSVApplyMixin:
    """apply_mapping + multi-entity expansion."""

    @staticmethod
    def apply_mapping(
        mapping: CSVSchemaMapping,
        rows: list[dict[str, str]],
    ) -> AppliedMapping:
        """Deterministically convert all CSV rows to entities + relationships. No LLM.

        Returns an :class:`AppliedMapping`, which unpacks as the legacy
        ``(entities, relationships)`` tuple and additionally carries
        row-conservation accounting (ADR 0003 §2): input rows are never
        silently dropped — an empty natural key falls back to a deterministic
        content-hash synthetic key, and a row is skipped (and counted) only
        when every owned value is empty.

        ``mapping.ontology_extensions`` (ADR 0003 Pass D, COG-52) is consumed
        here too: a promoted attribute's values become instances of the
        promoted type with identifies-edges back to their owner entity, and a
        relationship core slot carrying a dataset constant materializes ONE
        instance of its target type plus per-instance edges. held_for_review
        items are NOT filtered — the confirm gate is client-side (whatever
        mapping the client posts to /ingest/csv/rows is applied).
        """
        # Multi-entity mode: one row expands into several fully-attributed,
        # linked entities. Legacy single-entity path below is untouched.
        if mapping.entities:
            return CSVApplyMixin._apply_multi_entity(mapping, rows)

        id_col = next((c for c in mapping.columns if c.role == ColumnRole.TYPE_ID), None)
        if not id_col:
            # Degenerate mapping (no key column at all): nothing can be minted.
            # Account for every row so the mismatch is loud, not silent.
            drops = {mapping.entity_type: len(rows)} if rows else {}
            if rows:
                logger.warning(
                    "csv_rows_dropped",
                    rows_in=len(rows),
                    rows_dropped=len(rows),
                    drops_by_entity=drops,
                    reason="mapping has no type_id column",
                )
            return AppliedMapping(
                [], [], rows_in=len(rows), rows_dropped=len(rows), drops_by_entity=drops,
            )

        entities: list[ExtractedEntity] = []
        relationships: list[ExtractedRelationship] = []
        seen_rel_entities: dict[str, str] = {}  # safe_id → type for relationship targets
        rel_entity_names: dict[str, str] = {}  # safe_id → original value for name attr
        rows_dropped = 0
        drops_by_entity: dict[str, int] = {}
        # ADR 0003 Pass D: promoted-type instances + dataset-constant edges.
        applier = _ExtensionApplier(mapping)

        for row in rows:
            # Owned non-empty values, keyed by column name — feed both the
            # synthetic-key fallback and the nothing-to-assert skip. Emptiness
            # matches the attribute loop below (strip-if-str, falsy = empty).
            owned_values: dict[str, str] = {}
            for col in mapping.columns:
                raw = row.get(col.column_name, "")
                if isinstance(raw, str):
                    raw = raw.strip()
                if raw:
                    owned_values[col.column_name] = raw if isinstance(raw, str) else str(raw)

            entity_id = _cell(row, id_col.column_name)
            if entity_id:
                safe_id = _safe_id(entity_id)
            elif owned_values:
                # ADR 0003 §2: never silently drop a row. Empty natural key
                # with values to assert → deterministic content-hash key.
                safe_id = _synthetic_key(mapping.entity_type, owned_values)
            else:
                # Empty key AND every owned value empty: nothing to assert.
                # Principled skip — counted and logged, never silent.
                rows_dropped += 1
                drops_by_entity[mapping.entity_type] = (
                    drops_by_entity.get(mapping.entity_type, 0) + 1
                )
                continue

            attrs: list[ExtractedAttribute] = []
            entity_rels: list[ExtractedRelationship] = []

            for col in mapping.columns:
                raw_value = row.get(col.column_name, "")
                if isinstance(raw_value, str):
                    raw_value = raw_value.strip()
                if not raw_value:
                    continue

                attr_name = col.attribute_name or _snake_case(col.column_name)

                if col.role == ColumnRole.TYPE_ID:
                    # The key is URI + label material AND a regular attribute.
                    # Consuming it as URI-only made the key unqueryable
                    # (ADR 0003 §2 — "key consumed, not kept").
                    attrs.append(ExtractedAttribute(
                        name=attr_name,
                        value=raw_value if isinstance(raw_value, str) else str(raw_value),
                        datatype=col.datatype,
                    ))

                # Handle JSON arrays, pipe-delimited, and comma-delimited strings
                # by expanding into multiple values for relationships
                elif col.role == ColumnRole.RELATIONSHIP and col.target_type:
                    values: list[str] = []
                    if isinstance(raw_value, list):
                        values = [v.strip() for v in raw_value if isinstance(v, str) and v.strip()]
                    elif "|" in raw_value:
                        values = [v.strip() for v in raw_value.split("|") if v.strip()]
                    elif ", " in raw_value:
                        # Comma-delimited: split if parts are short (not addresses)
                        parts = [v.strip() for v in raw_value.split(", ") if v.strip()]
                        if all(len(p) < 30 for p in parts) and len(parts) >= 2:
                            values = parts
                        else:
                            values = [raw_value]
                    else:
                        values = [raw_value]

                    for value in values:
                        target_id = _safe_id(value)
                        entity_rels.append(ExtractedRelationship(
                            source_id=safe_id,
                            predicate=attr_name,
                            target_id=target_id,
                        ))
                        if target_id not in seen_rel_entities:
                            seen_rel_entities[target_id] = col.target_type
                            rel_entity_names[target_id] = value

                elif col.role == ColumnRole.ATTRIBUTE:
                    value = str(raw_value) if not isinstance(raw_value, str) else raw_value
                    # Split pipe-delimited attribute values into multiple triples.
                    # "PHASE1|PHASE2" becomes two separate attribute triples so that
                    # exact-match SPARQL filters work without CONTAINS.
                    if "|" in value and col.datatype == "string":
                        for v in value.split("|"):
                            v = v.strip()
                            if v:
                                attrs.append(ExtractedAttribute(
                                    name=attr_name,
                                    value=v,
                                    datatype=col.datatype,
                                ))
                    else:
                        attrs.append(ExtractedAttribute(
                            name=attr_name,
                            value=value,
                            datatype=col.datatype,
                        ))

            entities.append(ExtractedEntity(
                type_name=mapping.entity_type,
                id=safe_id,
                attributes=attrs,
            ))
            relationships.extend(entity_rels)

            if applier.active:
                # The single main entity is the only owner handle (None).
                applier.process_row(row, {None: safe_id})

        # Create stub entities for relationship targets (so they exist in the graph).
        # Opaque machine ids (C1001, R-WEST, P77) must NOT be written as attrs/name:
        # the full dimension-table row later adds the human name (Alice, West), and
        # multi-valued name then makes FILTER(CONTAINS(...)) fan-out SUM/AVG
        # rows (dogfood S5: West revenue 2×). Stubs still get rdfs:label=id at
        # write time in schema_resolver. Human-readable relationship cells
        # (Austin, Acme Corp) keep a name attr so NL can filter them before a
        # dim table arrives.
        for target_id, target_type in seen_rel_entities.items():
            raw_name = rel_entity_names.get(target_id, target_id.replace("_", " "))
            stub_attrs: list[ExtractedAttribute] = []
            if not _is_opaque_identifier(raw_name):
                stub_attrs.append(
                    ExtractedAttribute(name="name", value=raw_name, datatype="string")
                )
            entities.append(ExtractedEntity(
                type_name=target_type,
                id=target_id,
                attributes=stub_attrs,
            ))

        # ADR 0003 Pass D: merge materialized extension instances + edges.
        entities.extend(applier.entities)
        relationships.extend(applier.relationships)

        if rows_dropped:
            logger.warning(
                "csv_rows_dropped",
                rows_in=len(rows),
                rows_dropped=rows_dropped,
                drops_by_entity=drops_by_entity,
                reason="all owned values empty (nothing to assert)",
            )
        return AppliedMapping(
            entities,
            relationships,
            rows_in=len(rows),
            rows_dropped=rows_dropped,
            drops_by_entity=drops_by_entity,
        )

    @staticmethod
    def _entity_key(spec, row: dict) -> str | None:
        """Deterministic key for one in-row entity: its id_column value, or a
        composite of id_from columns. None when the key resolves empty."""
        if spec.id_column:
            v = (row.get(spec.id_column) or "").strip()
            return _safe_id(v) if v else None
        if spec.id_from:
            parts = [(row.get(c) or "").strip() for c in spec.id_from]
            if not any(parts):
                return None
            return _safe_id("|".join(parts))
        return None

    @staticmethod
    def _apply_multi_entity(
        mapping: CSVSchemaMapping,
        rows: list[dict[str, str]],
    ) -> AppliedMapping:
        """Multi-entity mode: one row → several fully-attributed, linked entities.

        Each `EntitySpec` is keyed by its id_column or an id_from composite.
        Columns route to their owner entity (`ColumnMapping.entity`). Inter-entity
        relationships reference the same deterministic ids the entities are minted
        from, so edges resolve to real URIs (not stubs). Entities dedup across
        rows by (type, id) with attribute union — collapsing repeated keys (e.g.
        many reservations → 5 Properties) into one entity. ER fires per
        ER-enabled type downstream (schema_resolver); nothing ER-specific here.

        Row conservation (ADR 0003 §2): an entity whose natural key resolves
        empty gets a deterministic content-hash synthetic key from its owned
        non-empty values; it is skipped (and counted in `drops_by_entity`)
        only when ALL of its owned values are empty. A row counts in
        `rows_dropped` only when it minted no entity at all.

        Ontology extensions (ADR 0003 Pass D): promotions and dataset
        constants are materialized per row against the entity keys minted
        above — see :class:`_ExtensionApplier`.
        """
        specs = {e.name: e for e in (mapping.entities or [])}

        # Route columns to their owner entity; drop (and log) unowned columns.
        cols_by_entity: dict[str, list[ColumnMapping]] = {name: [] for name in specs}
        for col in mapping.columns:
            if col.role == ColumnRole.TYPE_ID:
                continue  # in multi-entity mode, ids come from EntitySpec
            owner = col.entity
            if owner is None or owner not in specs:
                logger.warning(
                    "csv_multi_unowned_column", column=col.column_name, entity=owner,
                )
                continue
            cols_by_entity[owner].append(col)

        entities_by_key: dict[tuple[str, str], ExtractedEntity] = {}
        relationships: list[ExtractedRelationship] = []

        def add_entity(type_name: str, key: str, attrs: list[ExtractedAttribute]) -> None:
            ekey = (type_name, key)
            ent = entities_by_key.get(ekey)
            if ent is None:
                entities_by_key[ekey] = ExtractedEntity(
                    type_name=type_name, id=key, attributes=list(attrs),
                )
                return
            seen = {(a.name, a.value) for a in ent.attributes}
            for a in attrs:
                if (a.name, a.value) not in seen:
                    ent.attributes.append(a)
                    seen.add((a.name, a.value))

        rows_dropped = 0
        drops_by_entity: dict[str, int] = {}
        # ADR 0003 Pass D: promoted-type instances + dataset-constant edges.
        applier = _ExtensionApplier(mapping)

        for row in rows:
            row_ids: dict[str, str] = {}
            for name, spec in specs.items():
                # Owned non-empty values for this entity: its key column(s)
                # plus the columns routed to it — never another entity's
                # columns (unowned data must not leak into the key hash).
                owned_values: dict[str, str] = {}
                key_columns = ([spec.id_column] if spec.id_column else []) + list(spec.id_from or [])
                for column in key_columns:
                    v = _cell(row, column)
                    if v:
                        owned_values[column] = v
                for col in cols_by_entity[name]:
                    raw = row.get(col.column_name, "")
                    if isinstance(raw, str):
                        raw = raw.strip()
                    if raw:
                        owned_values[col.column_name] = raw if isinstance(raw, str) else str(raw)

                key = CSVApplyMixin._entity_key(spec, row)
                if key is None:
                    if not owned_values:
                        # Empty key AND every owned value empty: nothing to
                        # assert. Principled skip — counted, never silent.
                        drops_by_entity[name] = drops_by_entity.get(name, 0) + 1
                        continue
                    # ADR 0003 §2: never silently drop an entity. Empty natural
                    # key with values to assert → deterministic content-hash key.
                    key = _synthetic_key(spec.type_name, owned_values)
                row_ids[name] = key
                attrs: list[ExtractedAttribute] = []
                # The key column's value is also a regular attribute, not just
                # URI + label material (ADR 0003 §2 — "key consumed, not
                # kept"). When the id_column is routed to this entity it flows
                # through the column loop below under its mapped name;
                # otherwise emit it here.
                if spec.id_column and not any(
                    c.column_name == spec.id_column for c in cols_by_entity[name]
                ):
                    key_value = _cell(row, spec.id_column)
                    if key_value:
                        key_col = next(
                            (c for c in mapping.columns if c.column_name == spec.id_column),
                            None,
                        )
                        attrs.append(ExtractedAttribute(
                            name=(
                                key_col.attribute_name
                                if key_col and key_col.attribute_name
                                else _snake_case(spec.id_column)
                            ),
                            value=key_value,
                            datatype=key_col.datatype if key_col else "string",
                        ))
                for col in cols_by_entity[name]:
                    raw = row.get(col.column_name, "")
                    if isinstance(raw, str):
                        raw = raw.strip()
                    if not raw:
                        continue
                    attr_name = col.attribute_name or _snake_case(col.column_name)
                    if col.role == ColumnRole.RELATIONSHIP and col.target_type:
                        # Out-of-row reference (e.g. country) → stub target + edge.
                        # Same opaque-id rule as single-entity stubs (dogfood S5).
                        for value in _rel_values(raw):
                            tid = _safe_id(value)
                            relationships.append(ExtractedRelationship(
                                source_id=key, predicate=attr_name, target_id=tid,
                            ))
                            stub_attrs: list[ExtractedAttribute] = []
                            if not _is_opaque_identifier(value):
                                stub_attrs.append(ExtractedAttribute(
                                    name="name", value=value, datatype="string",
                                ))
                            add_entity(col.target_type, tid, stub_attrs)
                    elif col.role == ColumnRole.ATTRIBUTE:
                        value = str(raw)
                        if "|" in value and col.datatype == "string":
                            for v in value.split("|"):
                                v = v.strip()
                                if v:
                                    attrs.append(ExtractedAttribute(
                                        name=attr_name, value=v, datatype=col.datatype,
                                    ))
                        else:
                            attrs.append(ExtractedAttribute(
                                name=attr_name, value=value, datatype=col.datatype,
                            ))
                add_entity(spec.type_name, key, attrs)

            if specs and not row_ids:
                # The whole row minted nothing — every entity was all-empty.
                rows_dropped += 1

            # Inter-entity edges — only when both endpoints exist this row.
            for rel in (mapping.relationships or []):
                s = row_ids.get(rel.subject)
                o = row_ids.get(rel.object)
                if s and o:
                    relationships.append(ExtractedRelationship(
                        source_id=s, predicate=rel.predicate, target_id=o,
                    ))

            if applier.active:
                applier.process_row(row, row_ids)

        if rows_dropped:
            logger.warning(
                "csv_rows_dropped",
                rows_in=len(rows),
                rows_dropped=rows_dropped,
                drops_by_entity=drops_by_entity,
                reason="all owned values empty (nothing to assert)",
            )
        elif drops_by_entity:
            logger.warning(
                "csv_entities_skipped",
                rows_in=len(rows),
                drops_by_entity=drops_by_entity,
                reason="all owned values empty (nothing to assert)",
            )
        return AppliedMapping(
            # ADR 0003 Pass D: materialized extension instances merge in last.
            list(entities_by_key.values()) + applier.entities,
            relationships + applier.relationships,
            rows_in=len(rows),
            rows_dropped=rows_dropped,
            drops_by_entity=drops_by_entity,
        )


