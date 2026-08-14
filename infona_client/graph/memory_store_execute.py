"""Cypher-template dispatcher for the in-memory store.

Only the smoke subset in :mod:`schema_bootstrap` / :mod:`rdfs_helpers`
is implemented. Anything else raises ``GraphQueryError`` — prefer the
native ``write_*`` methods for instance mutations.
"""

from __future__ import annotations

import re
from typing import Any

from infona_client.graph.memory_store_norms import (
    _COUNT_BY_TYPE_SINGLE_NORM,
    _COUNT_TOTAL_NORM,
    _DETAIL_NORM,
    _ENTITIES_OF_TYPE_COUNT_NORM,
    _ENTITIES_OF_TYPE_NORM,
    _ENTITY_COUNT_BY_TYPE_NORM,
    _FILTER_PROP_EQ_NORM,
    _GET_NORM,
    _HOP_OUT_NORM,
    _LIST_NORM,
    _LIST_PAGE_NORM,
    _LITERAL_AGGREGATE_NORM,
    _LITERAL_COMPARE_NORM,
    _LITERAL_GREP_NORM,
    _LITERAL_VALUES_NORM,
    _MERGE_NORM,
    _ONTO_ATTR_DELETE_NORM,
    _ONTO_ATTR_LIST_NORM,
    _ONTO_ATTR_RANGE_NORM,
    _ONTO_ATTR_SET_MARKERS_NORM,
    _ONTO_ATTR_SET_TEXT_KIND_NORM,
    _ONTO_ATTR_UPSERT_NORM,
    _ONTO_SUBCLASS_CLEAR_NORM,
    _ONTO_SUBCLASS_SET_NORM,
    _ONTO_TYPE_DELETE_NORM,
    _ONTO_TYPE_GET_NORM,
    _ONTO_TYPE_LIST_NORM,
    _ONTO_TYPE_SET_MARKERS_NORM,
    _ONTO_TYPE_UPSERT_NORM,
    _RELATED_ENTITIES_NORM,
    _RELATED_ENTITY_NAME_FILTER_NORM,
    _RELS_NORM,
    _SUBCLASS_OF_CLOSURE_NORM,
    _TYPE_ATTR_COVERAGE_NORM,
    _TYPE_PROP_DISTINCT_NORM,
    _TYPE_REL_COVERAGE_NORM,
    _TYPE_REL_TARGET_DISTINCT_NORM,
    _norm_cypher,
)
from infona_client.graph.store import (
    GraphQueryError,
    GraphRecord,
    require_entity_write_identity,
)


class MemoryExecuteMixin:
    """Dispatch normalized smoke Cypher onto the native helpers."""

    def _execute(
        self,
        cypher: str,
        params: dict[str, Any],
        *,
        writing: bool,
    ) -> list[GraphRecord]:
        norm = _norm_cypher(cypher)
        tenant_id = str(params["tenant_id"])
        kg = str(params["kg"])

        if norm == _MERGE_NORM:
            if not writing:
                raise GraphQueryError("MERGE entity template requires execute_write")
            require_entity_write_identity(params)
            entity_id = params.get("id")
            return self._merge_entity(
                tenant_id,
                kg,
                str(entity_id),
                primary_type=params.get("primary_type"),
                name=params.get("name"),
                source=params.get("source"),
            )

        if norm == _GET_NORM:
            entity_id = params.get("id")
            if entity_id is None:
                return []
            row = self._entities.get((tenant_id, kg, str(entity_id)))
            return [row.as_record()] if row else []

        if norm == _LIST_NORM:
            primary_type = params.get("primary_type")
            return self._list_entities_by_types_page(
                tenant_id,
                kg,
                [str(primary_type or "")],
                None,
                -1,  # no limit
            )

        if norm == _LIST_PAGE_NORM:
            return self._list_entities_by_type_page(
                tenant_id,
                kg,
                str(params.get("primary_type") or ""),
                params.get("after_id"),
                int(params.get("limit") if params.get("limit") is not None else 50),
            )

        if norm == _COUNT_BY_TYPE_SINGLE_NORM:
            return self._entity_count_by_type(
                tenant_id, kg, str(params.get("primary_type") or "")
            )

        if norm == _COUNT_TOTAL_NORM:
            return self._entity_count_total(tenant_id, kg)

        if norm == _DETAIL_NORM:
            entity_id = params.get("id")
            if entity_id is None:
                return []
            return self._entity_detail(tenant_id, kg, str(entity_id))

        if norm == _RELS_NORM:
            entity_id = params.get("id")
            if entity_id is None:
                return []
            return self._entity_rels(tenant_id, kg, str(entity_id))

        if norm == _TYPE_ATTR_COVERAGE_NORM:
            return self._entity_type_attr_coverage(
                tenant_id, kg, str(params.get("primary_type") or "")
            )

        if norm == _TYPE_REL_COVERAGE_NORM:
            return self._entity_type_rel_coverage(
                tenant_id, kg, str(params.get("primary_type") or "")
            )

        if norm == _TYPE_PROP_DISTINCT_NORM:
            lim = params.get("limit")
            return self._entity_type_prop_distinct(
                tenant_id,
                kg,
                str(params.get("primary_type") or ""),
                str(params.get("prop_key") or ""),
                int(lim) if lim is not None else 50,
            )

        if norm == _TYPE_REL_TARGET_DISTINCT_NORM:
            lim = params.get("limit")
            return self._entity_type_rel_target_distinct(
                tenant_id,
                kg,
                str(params.get("primary_type") or ""),
                str(params.get("rel_attr") or ""),
                int(lim) if lim is not None else 50,
            )

        if norm == _LITERAL_GREP_NORM:
            lim = params.get("limit")
            type_name = params.get("type_name")
            pred = params.get("predicate_leaf")
            return self._entity_literal_grep(
                tenant_id,
                kg,
                str(params.get("needle") or ""),
                case_sensitive=bool(params.get("case_sensitive")),
                type_name=None if type_name is None else str(type_name),
                predicate_leaf=None if pred is None else str(pred),
                limit=int(lim) if lim is not None else 51,
            )

        if norm == _FILTER_PROP_EQ_NORM:
            lim = params.get("limit")
            return self._entity_filter_prop_eq(
                tenant_id,
                kg,
                str(params.get("primary_type") or ""),
                str(params.get("prop_key") or ""),
                params.get("prop_value"),
                int(lim) if lim is not None else 25,
            )

        if norm == _HOP_OUT_NORM:
            lim = params.get("limit")
            to_type = params.get("to_type")
            rel_attr = params.get("rel_attr")
            return self._entity_1hop_out(
                tenant_id,
                kg,
                str(params.get("from_type") or ""),
                None if to_type is None else str(to_type),
                None if rel_attr is None else str(rel_attr),
                int(lim) if lim is not None else 25,
            )

        # ADR 0013 semantic helpers
        if norm == _ENTITIES_OF_TYPE_NORM:
            lim = params.get("limit")
            return self._list_entities_by_types_page(
                tenant_id,
                kg,
                params.get("type_names"),
                params.get("after_id"),
                int(lim) if lim is not None else 50,
            )

        if norm == _ENTITIES_OF_TYPE_COUNT_NORM:
            return self._entity_count_by_types(
                tenant_id, kg, params.get("type_names")
            )

        if norm == _LITERAL_VALUES_NORM:
            lim = params.get("limit")
            return self._literal_values_eq(
                tenant_id,
                kg,
                params.get("type_names"),
                str(params.get("prop_key") or ""),
                params.get("prop_value"),
                int(lim) if lim is not None else 25,
            )

        if norm == _LITERAL_COMPARE_NORM:
            lim = params.get("limit")
            return self._literal_compare(
                tenant_id,
                kg,
                params.get("type_names"),
                str(params.get("prop_key") or ""),
                str(params.get("op") or "lt"),
                params.get("threshold"),
                int(lim) if lim is not None else 25,
            )

        if norm == _LITERAL_AGGREGATE_NORM:
            return self._literal_aggregate(
                tenant_id,
                kg,
                params.get("type_names"),
                str(params.get("prop_key") or ""),
                str(params.get("agg_op") or "sum"),
            )

        if norm == _RELATED_ENTITIES_NORM:
            lim = params.get("limit")
            to_types = params.get("to_types")
            rel_attr = params.get("rel_attr")
            return self._related_entities(
                tenant_id,
                kg,
                params.get("from_types"),
                to_types,
                None if rel_attr is None else str(rel_attr),
                int(lim) if lim is not None else 25,
            )

        if norm == _RELATED_ENTITY_NAME_FILTER_NORM:
            lim = params.get("limit")
            return self._related_entity_name_filter(
                tenant_id,
                kg,
                params.get("type_names"),
                str(params.get("rel_attr") or ""),
                str(params.get("target_name") or ""),
                int(lim) if lim is not None else 25,
            )

        if norm == _SUBCLASS_OF_CLOSURE_NORM:
            return self._subclass_of_closure_names(
                tenant_id,
                kg,
                str(params.get("type_name") or ""),
                params.get("layer"),
            )

        # --- Ontology catalog templates ------------------------------------
        if norm == _ONTO_TYPE_UPSERT_NORM:
            if not writing:
                raise GraphQueryError("onto_type_upsert requires execute_write")
            return self._upsert_onto_type(
                tenant_id,
                kg,
                str(params["layer"]),
                str(params["name"]),
                description=str(params.get("description") or ""),
                description_updated_at=params.get("description_updated_at"),
                description_provided=bool(params.get("description_provided")),
                label_token=params.get("label_token"),
                uri=params.get("uri"),
            )

        if norm == _ONTO_SUBCLASS_SET_NORM:
            if not writing:
                raise GraphQueryError("onto_subclass_set requires execute_write")
            return self._set_subclass(
                tenant_id,
                kg,
                str(params["layer"]),
                str(params["name"]),
                str(params["parent_name"]),
                params.get("parent_label_token"),
            )

        if norm == _ONTO_SUBCLASS_CLEAR_NORM:
            if not writing:
                raise GraphQueryError("onto_subclass_clear requires execute_write")
            return self._clear_subclass(
                tenant_id, kg, str(params["layer"]), str(params["name"])
            )

        if norm == _ONTO_TYPE_LIST_NORM:
            return self._list_onto_types(tenant_id, kg, params.get("layer"))

        if norm == _ONTO_TYPE_GET_NORM:
            return self._get_onto_type(
                tenant_id, kg, str(params["layer"]), str(params["name"])
            )

        if norm == _ONTO_ATTR_UPSERT_NORM:
            if not writing:
                raise GraphQueryError("onto_attr_upsert requires execute_write")
            return self._upsert_onto_attr(
                tenant_id,
                kg,
                str(params["layer"]),
                str(params["domain"]),
                str(params["name"]),
                kind=str(params.get("kind") or "literal"),
                datatype=params.get("datatype"),
                range_type=params.get("range_type"),
                cardinality=str(params.get("cardinality") or "1:1"),
                description=str(params.get("description") or ""),
                description_updated_at=params.get("description_updated_at"),
                description_provided=bool(params.get("description_provided")),
                prop_key=params.get("prop_key"),
                domain_label_token=params.get("domain_label_token"),
                domain_description=str(params.get("domain_description") or ""),
                domain_description_updated_at=params.get(
                    "domain_description_updated_at"
                ),
            )

        if norm == _ONTO_ATTR_RANGE_NORM:
            if not writing:
                raise GraphQueryError("onto_attr_range_type requires execute_write")
            return self._set_attr_range_type(
                tenant_id,
                kg,
                str(params["layer"]),
                str(params["domain"]),
                str(params["name"]),
                str(params["range_type"]),
                params.get("range_label_token"),
            )

        if norm == _ONTO_ATTR_SET_TEXT_KIND_NORM:
            if not writing:
                raise GraphQueryError("onto_attr_set_text_kind requires execute_write")
            return self._set_onto_attr_text_kind(
                tenant_id,
                kg,
                str(params["layer"]),
                str(params["domain"]),
                str(params["name"]),
                str(params.get("text_kind") or ""),
                params.get("domain_label_token"),
            )

        if norm == _ONTO_ATTR_LIST_NORM:
            return self._list_onto_attrs(
                tenant_id, kg, params.get("domain"), params.get("layer")
            )

        if norm == _ONTO_ATTR_DELETE_NORM:
            if not writing:
                raise GraphQueryError("onto_attr_delete requires execute_write")
            return self._delete_onto_attr(
                tenant_id,
                kg,
                str(params["layer"]),
                str(params["domain"]),
                str(params["name"]),
            )

        if norm == _ONTO_TYPE_DELETE_NORM:
            if not writing:
                raise GraphQueryError("onto_type_delete requires execute_write")
            return self._delete_onto_type(
                tenant_id, kg, str(params["layer"]), str(params["name"])
            )

        if norm == _ONTO_ATTR_SET_MARKERS_NORM:
            if not writing:
                raise GraphQueryError("onto_attr_set_markers requires execute_write")
            return self._set_onto_attr_markers(
                tenant_id,
                kg,
                str(params["layer"]),
                str(params["domain"]),
                str(params["name"]),
                core_slot=params.get("core_slot"),
                text_kind=params.get("text_kind"),
                clear_text_kind=bool(params.get("clear_text_kind")),
                deprecated_at=params.get("deprecated_at"),
                superseded_by=params.get("superseded_by"),
                clear_deprecation=bool(params.get("clear_deprecation")),
            )

        if norm == _ONTO_TYPE_SET_MARKERS_NORM:
            if not writing:
                raise GraphQueryError("onto_type_set_markers requires execute_write")
            return self._set_onto_type_markers(
                tenant_id,
                kg,
                str(params["layer"]),
                str(params["name"]),
                description=params.get("description"),
                deprecated_at=params.get("deprecated_at"),
                superseded_by=params.get("superseded_by"),
                clear_deprecation=bool(params.get("clear_deprecation")),
            )

        if norm == _ENTITY_COUNT_BY_TYPE_NORM:
            return self._entity_counts_by_primary_type(tenant_id, kg)

        set_labels_m = re.search(
            r"MATCH\s+\(e:Entity\s*\{[^}]*\}\)\s*SET\s+e:([A-Za-z][A-Za-z0-9_]*(?::[A-Za-z][A-Za-z0-9_]*)*)",
            norm,
            re.IGNORECASE,
        )
        if set_labels_m:
            if not writing:
                raise GraphQueryError("SET entity labels requires execute_write")
            require_entity_write_identity(params)
            entity_id = str(params["id"])
            raw_labels = [p for p in set_labels_m.group(1).split(":") if p]
            return self._apply_domain_labels(tenant_id, kg, entity_id, raw_labels)

        if re.search(
            r"RETURN\s+\$tenant_id\s+AS\s+tenant_id\s*,\s*\$kg\s+AS\s+kg",
            cypher,
            re.IGNORECASE,
        ):
            return [
                GraphRecord(
                    data={"tenant_id": tenant_id, "kg": kg}
                )
            ]

        raise GraphQueryError(
            "MemoryGraphStore does not implement this Cypher; use the smoke "
            "templates in schema_bootstrap, native write_* methods, or the "
            "Neo4j integration store"
        )
