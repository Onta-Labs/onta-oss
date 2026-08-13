"""In-memory :class:`GraphStore` for hermetic unit tests.

Implements the same scope-enforcement surface as :class:`Neo4jGraphStore` so
isolation tests do not need a live database. Supports:

* Entity MERGE / MATCH by ``(tenant_id, kg, id)`` (templates)
* Entity list filtered by ``INSTANCE_OF`` → Class (ADR 0013; not primary_type alone)
* Domain-label SET via :func:`infona_client.graph.labels.set_entity_type_labels`
* Native writer methods used by :mod:`infona_client.graph.pg_ops` (E3):
  literals (list-union), typed rels (B4 MERGE key), delete, rewrite, ProvEvent
* Ontology catalog (E4): ``:OntoType`` / ``:OntoAttr`` + hierarchy / DECLARES /
  RANGE_TYPE templates
* Explore reads (E5): paged list-by-type, entity detail + rels, type / total counts

Anything outside the smoke Cypher subset raises :class:`GraphQueryError` —
prefer the native ``write_*`` methods for instance mutations.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from infona_client.graph.rdfs_helpers import (
    ENTITIES_OF_TYPE_COUNT_CYPHER,
    ENTITIES_OF_TYPE_CYPHER,
    LITERAL_COMPARE_CYPHER,
    LITERAL_AGGREGATE_CYPHER,
    LITERAL_VALUES_CYPHER,
    RELATED_ENTITIES_CYPHER,
    RELATED_ENTITY_NAME_FILTER_CYPHER,
    SUBCLASS_OF_CLOSURE_CYPHER,
)
from infona_client.graph.schema_bootstrap import (
    ENTITY_1HOP_OUT_CYPHER,
    ENTITY_COUNT_BY_PRIMARY_TYPE_CYPHER,
    ENTITY_COUNT_BY_TYPE_CYPHER,
    ENTITY_COUNT_TOTAL_CYPHER,
    ENTITY_DETAIL_CYPHER,
    ENTITY_FILTER_PROP_EQ_CYPHER,
    ENTITY_GET_CYPHER,
    ENTITY_LIST_BY_TYPE_CYPHER,
    ENTITY_LIST_BY_TYPE_PAGE_CYPHER,
    ENTITY_LITERAL_GREP_CYPHER,
    ENTITY_MERGE_CYPHER,
    ENTITY_RELS_CYPHER,
    ENTITY_TYPE_ATTR_COVERAGE_CYPHER,
    ENTITY_TYPE_PROP_DISTINCT_CYPHER,
    ENTITY_TYPE_REL_COVERAGE_CYPHER,
    ENTITY_TYPE_REL_TARGET_DISTINCT_CYPHER,
    ONTO_ATTR_DELETE_CYPHER,
    ONTO_ATTR_LIST_CYPHER,
    ONTO_ATTR_RANGE_TYPE_CYPHER,
    ONTO_ATTR_SET_MARKERS_CYPHER,
    ONTO_ATTR_SET_TEXT_KIND_CYPHER,
    ONTO_ATTR_UPSERT_CYPHER,
    ONTO_SUBCLASS_CLEAR_CYPHER,
    ONTO_SUBCLASS_SET_CYPHER,
    ONTO_TYPE_DELETE_CYPHER,
    ONTO_TYPE_GET_CYPHER,
    ONTO_TYPE_LIST_CYPHER,
    ONTO_TYPE_SET_MARKERS_CYPHER,
    ONTO_TYPE_UPSERT_CYPHER,
    bootstrap_schema_statements,
    get_template,
)
from infona_client.graph.scope import GraphScope, GraphScopeError
from infona_client.graph.store import (
    GraphQueryError,
    GraphRecord,
    GraphSession,
    assert_cypher_is_scoped,
    maybe_require_entity_write_identity,
    merge_scope_params,
    require_entity_write_identity,
)


def _norm_cypher(cypher: str) -> str:
    return re.sub(r"\s+", " ", cypher.strip())


_MERGE_NORM = _norm_cypher(ENTITY_MERGE_CYPHER)
_GET_NORM = _norm_cypher(ENTITY_GET_CYPHER)
_LIST_NORM = _norm_cypher(ENTITY_LIST_BY_TYPE_CYPHER)
_LIST_PAGE_NORM = _norm_cypher(ENTITY_LIST_BY_TYPE_PAGE_CYPHER)
_COUNT_BY_TYPE_SINGLE_NORM = _norm_cypher(ENTITY_COUNT_BY_TYPE_CYPHER)
_COUNT_TOTAL_NORM = _norm_cypher(ENTITY_COUNT_TOTAL_CYPHER)
_DETAIL_NORM = _norm_cypher(ENTITY_DETAIL_CYPHER)
_RELS_NORM = _norm_cypher(ENTITY_RELS_CYPHER)
_TYPE_ATTR_COVERAGE_NORM = _norm_cypher(ENTITY_TYPE_ATTR_COVERAGE_CYPHER)
_TYPE_REL_COVERAGE_NORM = _norm_cypher(ENTITY_TYPE_REL_COVERAGE_CYPHER)
_TYPE_PROP_DISTINCT_NORM = _norm_cypher(ENTITY_TYPE_PROP_DISTINCT_CYPHER)
_TYPE_REL_TARGET_DISTINCT_NORM = _norm_cypher(ENTITY_TYPE_REL_TARGET_DISTINCT_CYPHER)
_LITERAL_GREP_NORM = _norm_cypher(ENTITY_LITERAL_GREP_CYPHER)
_FILTER_PROP_EQ_NORM = _norm_cypher(ENTITY_FILTER_PROP_EQ_CYPHER)
_HOP_OUT_NORM = _norm_cypher(ENTITY_1HOP_OUT_CYPHER)
_ENTITIES_OF_TYPE_NORM = _norm_cypher(ENTITIES_OF_TYPE_CYPHER)
_ENTITIES_OF_TYPE_COUNT_NORM = _norm_cypher(ENTITIES_OF_TYPE_COUNT_CYPHER)
_LITERAL_VALUES_NORM = _norm_cypher(LITERAL_VALUES_CYPHER)
_LITERAL_COMPARE_NORM = _norm_cypher(LITERAL_COMPARE_CYPHER)
_LITERAL_AGGREGATE_NORM = _norm_cypher(LITERAL_AGGREGATE_CYPHER)
_RELATED_ENTITIES_NORM = _norm_cypher(RELATED_ENTITIES_CYPHER)
_RELATED_ENTITY_NAME_FILTER_NORM = _norm_cypher(RELATED_ENTITY_NAME_FILTER_CYPHER)
_SUBCLASS_OF_CLOSURE_NORM = _norm_cypher(SUBCLASS_OF_CLOSURE_CYPHER)
_ONTO_TYPE_UPSERT_NORM = _norm_cypher(ONTO_TYPE_UPSERT_CYPHER)
_ONTO_SUBCLASS_SET_NORM = _norm_cypher(ONTO_SUBCLASS_SET_CYPHER)
_ONTO_SUBCLASS_CLEAR_NORM = _norm_cypher(ONTO_SUBCLASS_CLEAR_CYPHER)
_ONTO_TYPE_LIST_NORM = _norm_cypher(ONTO_TYPE_LIST_CYPHER)
_ONTO_TYPE_GET_NORM = _norm_cypher(ONTO_TYPE_GET_CYPHER)
_ONTO_ATTR_UPSERT_NORM = _norm_cypher(ONTO_ATTR_UPSERT_CYPHER)
_ONTO_ATTR_RANGE_NORM = _norm_cypher(ONTO_ATTR_RANGE_TYPE_CYPHER)
_ONTO_ATTR_SET_TEXT_KIND_NORM = _norm_cypher(ONTO_ATTR_SET_TEXT_KIND_CYPHER)
_ONTO_ATTR_LIST_NORM = _norm_cypher(ONTO_ATTR_LIST_CYPHER)
_ONTO_ATTR_DELETE_NORM = _norm_cypher(ONTO_ATTR_DELETE_CYPHER)
_ONTO_TYPE_DELETE_NORM = _norm_cypher(ONTO_TYPE_DELETE_CYPHER)
_ONTO_ATTR_SET_MARKERS_NORM = _norm_cypher(ONTO_ATTR_SET_MARKERS_CYPHER)
_ONTO_TYPE_SET_MARKERS_NORM = _norm_cypher(ONTO_TYPE_SET_MARKERS_CYPHER)
_ENTITY_COUNT_BY_TYPE_NORM = _norm_cypher(ENTITY_COUNT_BY_PRIMARY_TYPE_CYPHER)


@dataclass
class _EntityRow:
    tenant_id: str
    kg: str
    id: str
    primary_type: str | None = None
    name: str | None = None
    source: str | None = None
    labels: list[str] = field(default_factory=lambda: ["Entity"])
    props: dict[str, Any] = field(default_factory=dict)

    def as_record(self) -> GraphRecord:
        data = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "kg": self.kg,
            "primary_type": self.primary_type,
            "name": self.name,
            "source": self.source,
            "labels": list(self.labels),
            "props": copy.deepcopy(self.props),
        }
        return GraphRecord(data=data)


@dataclass
class _RelRow:
    tenant_id: str
    kg: str
    start_id: str
    end_id: str
    rel_type: str
    attr: str


@dataclass
class _ProvRow:
    tenant_id: str
    kg: str
    event_type: str
    subject_id: str
    attr: str | None = None
    object_repr: str | None = None
    old_id: str | None = None
    new_id: str | None = None
    reason: str = ""
    source: str | None = None
    fact_hash: str | None = None
    ts: str | None = None
    # ONTA-536: ADR 0002 governance fields on assert events.
    confidence: float | None = None
    # ABOUT is implied: event is always about subject_id when entity exists.


@dataclass
class _ValueHistoryRow:
    """One old→new value transition (ONTA-236 / ONTA-536 property-graph port)."""

    tenant_id: str
    kg: str
    subject_id: str
    predicate: str
    old_value: str
    new_value: str
    changed_at: str


@dataclass
class _CitationRow:
    tenant_id: str
    kg: str
    entity_id: str
    attr: str
    source_url: str | None = None
    provenance: str | None = None
    verified_at: str | None = None
    value_hash: str = ""


@dataclass
class _ClassRow:
    tenant_id: str
    kg: str
    id: str
    name: str
    layer: str = "tenant"


@dataclass
class _PropertyRow:
    tenant_id: str
    kg: str
    id: str
    name: str
    kind: str = "datatype"  # datatype | object
    layer: str = "tenant"


@dataclass
class _AssertionRow:
    tenant_id: str
    kg: str
    id: str
    subject_id: str
    property_id: str
    literal_value: Any = None
    literal_datatype: str | None = None
    object_id: str | None = None  # Entity object
    object_class_id: str | None = None  # Class object (type)
    source_url: str | None = None
    verified_at: str | None = None
    run_id: str | None = None
    confidence: float | None = None
    provenance: str | None = None

    def as_record(self) -> GraphRecord:
        return GraphRecord(
            data={
                "assertion_id": self.id,
                "id": self.id,
                "tenant_id": self.tenant_id,
                "kg": self.kg,
                "subject_id": self.subject_id,
                "property_id": self.property_id,
                "literal_value": self.literal_value,
                "literal_datatype": self.literal_datatype,
                "object_id": self.object_id,
                "object_class_id": self.object_class_id,
                "source_url": self.source_url,
                "verified_at": self.verified_at,
                "run_id": self.run_id,
                "confidence": self.confidence,
                "provenance": self.provenance,
            }
        )


@dataclass
class _OntoTypeRow:
    tenant_id: str
    kg: str
    layer: str
    name: str
    description: str = ""
    description_updated_at: str | None = None
    label_token: str | None = None
    uri: str | None = None
    parent_type: str | None = None
    deprecated_at: str | None = None
    superseded_by: str | None = None

    def as_record(self) -> GraphRecord:
        return GraphRecord(
            data={
                "name": self.name,
                "layer": self.layer,
                "description": self.description,
                "description_updated_at": self.description_updated_at,
                "label_token": self.label_token,
                "uri": self.uri,
                "parent_type": self.parent_type,
                "tenant_id": self.tenant_id,
                "kg": self.kg,
                "deprecated_at": self.deprecated_at,
                "superseded_by": self.superseded_by,
            }
        )


@dataclass
class _OntoAttrRow:
    tenant_id: str
    kg: str
    layer: str
    domain: str
    name: str
    kind: str = "literal"
    datatype: str | None = None
    range_type: str | None = None
    cardinality: str = "1:1"
    description: str = ""
    description_updated_at: str | None = None
    prop_key: str | None = None
    core_slot: bool = False
    text_kind: str | None = None
    deprecated_at: str | None = None
    superseded_by: str | None = None

    def as_record(self) -> GraphRecord:
        return GraphRecord(
            data={
                "name": self.name,
                "domain": self.domain,
                "kind": self.kind,
                "datatype": self.datatype,
                "range_type": self.range_type,
                "cardinality": self.cardinality,
                "description": self.description,
                "description_updated_at": self.description_updated_at,
                "prop_key": self.prop_key,
                "layer": self.layer,
                "tenant_id": self.tenant_id,
                "kg": self.kg,
                "core_slot": self.core_slot,
                "text_kind": self.text_kind,
                "deprecated_at": self.deprecated_at,
                "superseded_by": self.superseded_by,
            }
        )


class MemoryGraphSession:
    def __init__(self, store: "MemoryGraphStore", scope: GraphScope) -> None:
        self._store = store
        self._scope = scope

    @property
    def scope(self) -> GraphScope:
        return self._scope

    def _scope_tk(self) -> tuple[str, str]:
        return self._scope.tenant_id, self._scope.kg

    async def execute_read(
        self,
        cypher: str,
        params: Mapping[str, Any] | None = None,
    ) -> list[GraphRecord]:
        assert_cypher_is_scoped(cypher, privileged=self._scope.privileged)
        bound = merge_scope_params(self._scope, params, for_write=False)
        return self._store._execute(cypher, bound, writing=False)

    async def execute_write(
        self,
        cypher: str,
        params: Mapping[str, Any] | None = None,
    ) -> list[GraphRecord]:
        assert_cypher_is_scoped(cypher, privileged=self._scope.privileged)
        bound = merge_scope_params(self._scope, params, for_write=True)
        maybe_require_entity_write_identity(cypher, bound)
        return self._store._execute(cypher, bound, writing=True)

    async def execute_template(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
    ) -> list[GraphRecord]:
        try:
            tmpl = get_template(name)
        except KeyError as exc:
            raise GraphScopeError(str(exc)) from exc
        assert_cypher_is_scoped(tmpl.cypher, privileged=self._scope.privileged)
        bound = merge_scope_params(
            self._scope, params, for_write=tmpl.writing
        )
        if tmpl.require_entity_id:
            require_entity_write_identity(bound)
        elif tmpl.writing:
            maybe_require_entity_write_identity(tmpl.cypher, bound)
        return self._store._execute(tmpl.cypher, bound, writing=tmpl.writing)

    async def apply_entity_domain_labels(
        self,
        entity_id: str,
        safe_labels: Sequence[str],
    ) -> list[GraphRecord]:
        """Native path for :func:`infona_client.graph.labels.set_entity_type_labels`."""
        require_entity_write_identity({"id": entity_id})
        bound = merge_scope_params(
            self._scope, {"id": entity_id}, for_write=True
        )
        return self._store._apply_domain_labels(
            str(bound["tenant_id"]),
            str(bound["kg"]),
            str(entity_id),
            list(safe_labels),
        )

    # --- E3 native writer surface (pg_ops) ------------------------------------

    async def write_merge_entity(
        self,
        *,
        id: str,
        primary_type: str | None = None,
        name: str | None = None,
        source: str | None = None,
        ts: str | None = None,
    ) -> list[GraphRecord]:
        require_entity_write_identity({"id": id})
        t, k = self._scope_tk()
        return self._store._merge_entity(
            t, k, str(id), primary_type=primary_type, name=name, source=source
        )

    async def write_set_literal(
        self,
        entity_id: str,
        prop_key: str,
        value: Any,
        *,
        multi_union: bool = True,
        original_leaf: str | None = None,
    ) -> list[GraphRecord]:
        require_entity_write_identity({"id": entity_id})
        t, k = self._scope_tk()
        return self._store._set_literal(
            t, k, str(entity_id), prop_key, value, multi_union=multi_union
        )

    async def write_merge_rel(
        self,
        start_id: str,
        end_id: str,
        rel_type: str,
        attr_leaf: str,
    ) -> list[GraphRecord]:
        require_entity_write_identity({"id": start_id})
        require_entity_write_identity({"id": end_id})
        t, k = self._scope_tk()
        return self._store._merge_rel(
            t, k, str(start_id), str(end_id), rel_type, attr_leaf
        )

    async def write_delete_entity(self, entity_id: str) -> int:
        require_entity_write_identity({"id": entity_id})
        t, k = self._scope_tk()
        return self._store._delete_entity(t, k, str(entity_id))

    async def write_delete_literals(
        self, entity_id: str, keys: Sequence[str]
    ) -> int:
        require_entity_write_identity({"id": entity_id})
        t, k = self._scope_tk()
        return self._store._delete_literals(t, k, str(entity_id), list(keys))

    async def write_delete_rels(
        self,
        *,
        start_id: str | None = None,
        end_id: str | None = None,
        rel_type: str | None = None,
        attr_leaf: str | None = None,
    ) -> int:
        t, k = self._scope_tk()
        return self._store._delete_rels(
            t,
            k,
            start_id=start_id,
            end_id=end_id,
            rel_type=rel_type,
            attr_leaf=attr_leaf,
        )

    async def write_rewrite_entity_id(self, old_id: str, new_id: str) -> None:
        require_entity_write_identity({"id": old_id})
        require_entity_write_identity({"id": new_id})
        t, k = self._scope_tk()
        self._store._rewrite_entity_id(t, k, str(old_id), str(new_id))

    async def write_prov_event(
        self,
        *,
        event_type: str,
        subject_id: str,
        attr: str | None = None,
        object_repr: str | None = None,
        old_id: str | None = None,
        new_id: str | None = None,
        reason: str = "",
        source: str | None = None,
        fact_hash: str | None = None,
        ts: str | None = None,
        confidence: float | None = None,
    ) -> None:
        t, k = self._scope_tk()
        # Ensure ABOUT target for assert/rewrite only — never re-mint a deleted
        # Entity when writing a post-removal tombstone (subject_id is enough).
        if event_type in ("assert", "rewrite"):
            self._store._merge_entity(t, k, subject_id)
        self._store._add_prov(
            _ProvRow(
                tenant_id=t,
                kg=k,
                event_type=event_type,
                subject_id=subject_id,
                attr=attr,
                object_repr=object_repr,
                old_id=old_id,
                new_id=new_id,
                reason=reason,
                source=source,
                fact_hash=fact_hash,
                ts=ts,
                confidence=confidence,
            )
        )

    async def write_value_history(
        self,
        *,
        subject_id: str,
        predicate: str,
        old_value: str,
        new_value: str,
        changed_at: str,
    ) -> None:
        t, k = self._scope_tk()
        self._store._add_value_history(
            _ValueHistoryRow(
                tenant_id=t,
                kg=k,
                subject_id=subject_id,
                predicate=predicate,
                old_value=old_value,
                new_value=new_value,
                changed_at=changed_at,
            )
        )

    async def read_value_history(
        self,
        *,
        subject_id: str | None = None,
        predicate: str | None = None,
        since: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        t, k = self._scope_tk()
        return self._store._list_value_history(
            t,
            k,
            subject_id=subject_id,
            predicate=predicate,
            since=since,
            limit=limit,
        )

    async def write_attr_citation(
        self,
        *,
        entity_id: str,
        attr: str,
        source_url: str | None = None,
        provenance: str | None = None,
        verified_at: str | None = None,
        value_hash: str = "",
    ) -> None:
        t, k = self._scope_tk()
        self._store._merge_entity(t, k, entity_id)
        self._store._upsert_citation(
            _CitationRow(
                tenant_id=t,
                kg=k,
                entity_id=entity_id,
                attr=attr,
                source_url=source_url,
                provenance=provenance,
                verified_at=verified_at,
                value_hash=value_hash or "",
            )
        )

    async def write_get_entity(self, entity_id: str) -> Mapping[str, Any] | None:
        t, k = self._scope_tk()
        row = self._store._entities.get((t, k, str(entity_id)))
        if row is None:
            return None
        return row.as_record().to_dict()

    # --- ADR 0013 Assertion model natives ------------------------------------

    async def write_merge_class(
        self,
        *,
        class_id: str,
        name: str,
        layer: str = "tenant",
    ) -> list[GraphRecord]:
        t, k = self._scope_tk()
        return self._store._merge_class(t, k, class_id, name=name, layer=layer)

    async def write_merge_property(
        self,
        *,
        property_id: str,
        name: str,
        kind: str = "datatype",
        layer: str = "tenant",
    ) -> list[GraphRecord]:
        t, k = self._scope_tk()
        return self._store._merge_property(
            t, k, property_id, name=name, kind=kind, layer=layer
        )

    async def write_subclass_of(
        self, child_class_id: str, parent_class_id: str
    ) -> None:
        t, k = self._scope_tk()
        self._store._set_class_subclass(t, k, child_class_id, parent_class_id)

    async def write_subproperty_of(
        self, child_prop_id: str, parent_prop_id: str
    ) -> None:
        t, k = self._scope_tk()
        self._store._set_subproperty(t, k, child_prop_id, parent_prop_id)

    async def write_instance_of(self, entity_id: str, class_id: str) -> None:
        t, k = self._scope_tk()
        self._store._set_instance_of(t, k, entity_id, class_id)

    async def write_clear_class_subclass(self, child_class_id: str) -> None:
        """Drop Class-level SUBCLASS_OF for ``child_class_id`` (catalog clear)."""
        t, k = self._scope_tk()
        self._store._clear_class_subclass(t, k, child_class_id)

    async def write_assertion(
        self,
        *,
        assertion_id: str,
        subject_id: str,
        property_id: str,
        property_name: str,
        property_kind: str = "datatype",
        object_id: str | None = None,
        object_class_id: str | None = None,
        literal_value: Any = None,
        literal_datatype: str | None = None,
        source_url: str | None = None,
        verified_at: str | None = None,
        run_id: str | None = None,
        confidence: float | None = None,
        provenance: str | None = None,
        ts: str | None = None,
    ) -> list[GraphRecord]:
        t, k = self._scope_tk()
        return self._store._upsert_assertion(
            t,
            k,
            assertion_id=assertion_id,
            subject_id=subject_id,
            property_id=property_id,
            object_id=object_id,
            object_class_id=object_class_id,
            literal_value=literal_value,
            literal_datatype=literal_datatype,
            source_url=source_url,
            verified_at=verified_at,
            run_id=run_id,
            confidence=confidence,
            provenance=provenance,
        )

    async def write_delete_assertions(
        self,
        *,
        subject_id: str,
        property_id: str | None = None,
        object_key: str | None = None,
    ) -> int:
        t, k = self._scope_tk()
        return self._store._delete_assertions(
            t,
            k,
            subject_id=subject_id,
            property_id=property_id,
            object_key=object_key,
        )

    async def read_subclass_closure(self, class_id: str) -> list[str]:
        t, k = self._scope_tk()
        return self._store._subclass_closure(t, k, class_id)

    async def read_subproperty_closure(self, prop_id: str) -> list[str]:
        t, k = self._scope_tk()
        return self._store._subproperty_closure(t, k, prop_id)

    async def read_entities_of_type(self, class_ids: Sequence[str]) -> list[str]:
        t, k = self._scope_tk()
        return self._store._entities_of_type_ids(t, k, list(class_ids))

    async def read_assertions_for_subject(
        self, entity_id: str, *, prop_id: str | None = None
    ) -> list[dict[str, Any]]:
        t, k = self._scope_tk()
        return self._store._assertions_for_subject(t, k, entity_id, prop_id=prop_id)

    async def read_assertion_history(
        self,
        *,
        entity_id: str | None = None,
        prop_id: str | None = None,
        since: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Assertion rows for history feed (optional subject / since filter)."""
        t, k = self._scope_tk()
        return self._store._assertion_history(
            t,
            k,
            entity_id=entity_id,
            prop_id=prop_id,
            since=since,
            limit=limit,
        )

    async def read_list_entities_by_label(
        self,
        label: str,
        *,
        after_id: str | None = None,
        limit: int = 50,
    ) -> list[GraphRecord]:
        """List entities carrying a sanitized domain label (E5 explore)."""
        t, k = self._scope_tk()
        return self._store._list_entities_by_label(
            t, k, label, after_id, int(limit)
        )


class MemoryGraphStore:
    """Process-local fake store; not safe for concurrent multi-process use."""

    def __init__(self) -> None:
        # key: (tenant_id, kg, id)
        self._entities: dict[tuple[str, str, str], _EntityRow] = {}
        # B4 key: (tenant_id, kg, start_id, end_id, rel_type)
        self._rels: dict[tuple[str, str, str, str, str], _RelRow] = {}
        self._prov: list[_ProvRow] = []
        # ValueHistory (ONTA-236/536): ordered old→new transitions
        self._value_history: list[_ValueHistoryRow] = []
        # AttrCitation MERGE key: (tenant_id, kg, entity_id, attr, value_hash)
        self._citations: dict[tuple[str, str, str, str, str], _CitationRow] = {}
        # ADR 0013: Class / Property / Assertion
        self._classes: dict[tuple[str, str, str], _ClassRow] = {}
        self._properties: dict[tuple[str, str, str], _PropertyRow] = {}
        self._assertions: dict[tuple[str, str, str], _AssertionRow] = {}
        # SUBCLASS_OF: (tenant, kg, child_id) → parent_id
        self._subclass_of: dict[tuple[str, str, str], str] = {}
        # SUBPROPERTY_OF: (tenant, kg, child_id) → parent_id
        self._subproperty_of: dict[tuple[str, str, str], str] = {}
        # INSTANCE_OF cache: (tenant, kg, entity_id) → set(class_id)
        self._instance_of: dict[tuple[str, str, str], set[str]] = {}
        # Catalog: (tenant_id, kg, layer, name)
        self._onto_types: dict[tuple[str, str, str, str], _OntoTypeRow] = {}
        # Catalog: (tenant_id, kg, layer, domain, name)
        self._onto_attrs: dict[tuple[str, str, str, str, str], _OntoAttrRow] = {}
        # KG registry: (tenant_id, name) → {name, description, triple_count}
        self._kg_registry: dict[tuple[str, str], dict[str, Any]] = {}
        self._bootstrapped: list[str] = []

    def session(self, scope: GraphScope) -> GraphSession:
        if not isinstance(scope, GraphScope):
            raise GraphScopeError("session() requires a GraphScope instance")
        return MemoryGraphSession(self, scope)

    async def health(self) -> bool:
        return True

    async def bootstrap_schema(self) -> Sequence[str]:
        if not self._bootstrapped:
            self._bootstrapped = [name for name, _ in bootstrap_schema_statements()]
        return list(self._bootstrapped)

    async def close(self) -> None:
        self._entities.clear()
        self._rels.clear()
        self._prov.clear()
        self._value_history.clear()
        self._citations.clear()
        self._classes.clear()
        self._properties.clear()
        self._assertions.clear()
        self._subclass_of.clear()
        self._subproperty_of.clear()
        self._instance_of.clear()
        self._onto_types.clear()
        self._onto_attrs.clear()
        self._kg_registry.clear()
        self._bootstrapped.clear()
        bag = getattr(self, "_ontology_companion", None)
        if bag is not None and hasattr(bag, "clear"):
            bag.clear()

    async def kg_registry_list(self, tenant_id: str) -> list[dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for (t, name), row in self._kg_registry.items():
            if t == tenant_id:
                out[name] = dict(row)
        for (t, k, _), _ in self._entities.items():
            if t == tenant_id and k and k not in out:
                out[k] = {"name": k, "description": "", "triple_count": 0}
        return [out[n] for n in sorted(out)]

    async def kg_registry_upsert(
        self,
        tenant_id: str,
        name: str,
        *,
        description: str = "",
        triple_count: int | None = None,
        only_if_absent: bool = False,
    ) -> dict[str, Any]:
        key = (tenant_id, name)
        existing = self._kg_registry.get(key)
        if existing is not None and only_if_absent:
            return dict(existing)
        if existing is None:
            row = {
                "name": name,
                "description": description or "",
                "triple_count": int(triple_count or 0),
            }
        else:
            row = dict(existing)
            if description:
                row["description"] = description
            if triple_count is not None:
                row["triple_count"] = int(triple_count)
        self._kg_registry[key] = row
        return dict(row)

    async def kg_registry_delete(self, tenant_id: str, name: str) -> None:
        self._kg_registry.pop((tenant_id, name), None)

    def assertion_count(
        self, *, tenant_id: str | None = None, kg: str | None = None
    ) -> int:
        n = 0
        for (t, k, _), _ in self._assertions.items():
            if tenant_id is not None and t != tenant_id:
                continue
            if kg is not None and k != kg:
                continue
            n += 1
        return n

    def snapshot_assertions(self) -> list[dict[str, Any]]:
        return [a.as_record().to_dict() for a in self._assertions.values()]

    # --- test helpers -------------------------------------------------------

    def entity_count(self, *, tenant_id: str | None = None, kg: str | None = None) -> int:
        n = 0
        for (t, k, _id), _row in self._entities.items():
            if tenant_id is not None and t != tenant_id:
                continue
            if kg is not None and k != kg:
                continue
            n += 1
        return n

    def rel_count(self, *, tenant_id: str | None = None, kg: str | None = None) -> int:
        n = 0
        for (t, k, *_), _row in self._rels.items():
            if tenant_id is not None and t != tenant_id:
                continue
            if kg is not None and k != kg:
                continue
            n += 1
        return n

    def snapshot_entities(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(row.as_record().to_dict()) for row in self._entities.values()]

    def snapshot_rels(self) -> list[dict[str, Any]]:
        out = []
        for r in self._rels.values():
            out.append(
                {
                    "tenant_id": r.tenant_id,
                    "kg": r.kg,
                    "start_id": r.start_id,
                    "end_id": r.end_id,
                    "rel_type": r.rel_type,
                    "attr": r.attr,
                }
            )
        return out

    def snapshot_prov(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(r.__dict__) for r in self._prov]

    def snapshot_value_history(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(r.__dict__) for r in self._value_history]

    def snapshot_citations(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(r.__dict__) for r in self._citations.values()]

    def _apply_domain_labels(
        self,
        tenant_id: str,
        kg: str,
        entity_id: str,
        safe_labels: Sequence[str],
    ) -> list[GraphRecord]:
        """Accumulate domain labels (Neo4j ``SET e:Label`` is additive).

        Prior domain labels are kept; only new tokens are appended. ``Entity``
        is always present as the system label.
        """
        row = self._entities.get((tenant_id, kg, entity_id))
        if row is None:
            return []
        # Union into existing labels — never replace (matches Neo4j SET e:X).
        if "Entity" not in row.labels:
            row.labels.insert(0, "Entity")
        for lab in safe_labels:
            if lab not in row.labels:
                row.labels.append(lab)
        return [
            GraphRecord(
                data={
                    "id": row.id,
                    "tenant_id": row.tenant_id,
                    "kg": row.kg,
                    "labels": list(row.labels),
                }
            )
        ]

    def _merge_entity(
        self,
        tenant_id: str,
        kg: str,
        entity_id: str,
        *,
        primary_type: str | None = None,
        name: str | None = None,
        source: str | None = None,
    ) -> list[GraphRecord]:
        key = (tenant_id, kg, entity_id)
        existing = self._entities.get(key)
        if existing is None:
            row = _EntityRow(
                tenant_id=tenant_id,
                kg=kg,
                id=entity_id,
                primary_type=primary_type,
                name=name,
                source=source,
            )
            self._entities[key] = row
        else:
            if primary_type is not None:
                existing.primary_type = primary_type
            if name is not None:
                existing.name = name
            if source is not None:
                existing.source = source
            row = existing
        return [row.as_record()]

    def _set_literal(
        self,
        tenant_id: str,
        kg: str,
        entity_id: str,
        prop_key: str,
        value: Any,
        *,
        multi_union: bool,
    ) -> list[GraphRecord]:
        key = (tenant_id, kg, entity_id)
        row = self._entities.get(key)
        if row is None:
            row = _EntityRow(tenant_id=tenant_id, kg=kg, id=entity_id)
            self._entities[key] = row
        if prop_key in ("name", "source", "primary_type"):
            setattr(row, prop_key, value)
            return [row.as_record()]
        if multi_union and prop_key in row.props:
            existing = row.props[prop_key]
            if isinstance(value, list):
                incoming = list(value)
            else:
                incoming = [value]
            if isinstance(existing, list):
                base = list(existing)
            else:
                base = [existing]
            for v in incoming:
                if v not in base:
                    base.append(v)
            row.props[prop_key] = base if len(base) > 1 else base[0]
        else:
            row.props[prop_key] = value
        return [row.as_record()]

    def _merge_rel(
        self,
        tenant_id: str,
        kg: str,
        start_id: str,
        end_id: str,
        rel_type: str,
        attr_leaf: str,
    ) -> list[GraphRecord]:
        # Ensure endpoints exist.
        self._merge_entity(tenant_id, kg, start_id)
        self._merge_entity(tenant_id, kg, end_id)
        rkey = (tenant_id, kg, start_id, end_id, rel_type)
        self._rels[rkey] = _RelRow(
            tenant_id=tenant_id,
            kg=kg,
            start_id=start_id,
            end_id=end_id,
            rel_type=rel_type,
            attr=attr_leaf,
        )
        return [
            GraphRecord(
                data={
                    "start_id": start_id,
                    "end_id": end_id,
                    "rel_type": rel_type,
                    "attr": attr_leaf,
                    "tenant_id": tenant_id,
                    "kg": kg,
                }
            )
        ]

    def _delete_entity(self, tenant_id: str, kg: str, entity_id: str) -> int:
        removed = 0
        if self._entities.pop((tenant_id, kg, entity_id), None) is not None:
            removed += 1
        # Drop incident rels in scope.
        drop = [
            k
            for k, r in self._rels.items()
            if r.tenant_id == tenant_id
            and r.kg == kg
            and (r.start_id == entity_id or r.end_id == entity_id)
        ]
        for k in drop:
            del self._rels[k]
            removed += 1
        # Drop Assertions where subject or object is this entity.
        a_drop = [
            k
            for k, a in self._assertions.items()
            if a.tenant_id == tenant_id
            and a.kg == kg
            and (a.subject_id == entity_id or a.object_id == entity_id)
        ]
        for k in a_drop:
            del self._assertions[k]
            removed += 1
        self._instance_of.pop((tenant_id, kg, entity_id), None)
        return removed

    def _delete_literals(
        self, tenant_id: str, kg: str, entity_id: str, keys: list[str]
    ) -> int:
        row = self._entities.get((tenant_id, kg, entity_id))
        if row is None:
            return 0
        n = 0
        for key in keys:
            if key == "name" and row.name is not None:
                row.name = None
                n += 1
            elif key == "source" and row.source is not None:
                row.source = None
                n += 1
            elif key in row.props:
                del row.props[key]
                n += 1
        return n

    def _delete_rels(
        self,
        tenant_id: str,
        kg: str,
        *,
        start_id: str | None,
        end_id: str | None,
        rel_type: str | None,
        attr_leaf: str | None,
    ) -> int:
        drop = []
        for k, r in self._rels.items():
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if start_id is not None and r.start_id != start_id:
                continue
            if end_id is not None and r.end_id != end_id:
                continue
            if rel_type is not None and r.rel_type != rel_type:
                continue
            if attr_leaf is not None and r.attr != attr_leaf:
                continue
            drop.append(k)
        for k in drop:
            del self._rels[k]
        return len(drop)

    def _rewrite_entity_id(
        self, tenant_id: str, kg: str, old_id: str, new_id: str
    ) -> None:
        old_key = (tenant_id, kg, old_id)
        new_key = (tenant_id, kg, new_id)
        row = self._entities.get(old_key)
        if row is None:
            return
        if new_key in self._entities and old_id != new_id:
            # Merge props into survivor (last-write: new keeps its own, then overlay old)
            survivor = self._entities[new_key]
            for pk, pv in row.props.items():
                if pk not in survivor.props:
                    survivor.props[pk] = pv
            if survivor.name is None and row.name is not None:
                survivor.name = row.name
            if survivor.source is None and row.source is not None:
                survivor.source = row.source
            if survivor.primary_type is None and row.primary_type is not None:
                survivor.primary_type = row.primary_type
            for lab in row.labels:
                if lab not in survivor.labels:
                    survivor.labels.append(lab)
            del self._entities[old_key]
        else:
            del self._entities[old_key]
            row.id = new_id
            self._entities[new_key] = row
        # Rebind rel endpoints.
        updates: list[tuple[tuple, _RelRow]] = []
        drop_keys = []
        for k, r in list(self._rels.items()):
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if r.start_id != old_id and r.end_id != old_id:
                continue
            drop_keys.append(k)
            nr = _RelRow(
                tenant_id=r.tenant_id,
                kg=r.kg,
                start_id=new_id if r.start_id == old_id else r.start_id,
                end_id=new_id if r.end_id == old_id else r.end_id,
                rel_type=r.rel_type,
                attr=r.attr,
            )
            updates.append(
                (
                    (nr.tenant_id, nr.kg, nr.start_id, nr.end_id, nr.rel_type),
                    nr,
                )
            )
        for k in drop_keys:
            del self._rels[k]
        for nk, nr in updates:
            self._rels[nk] = nr
        # Rebind prov subject ids.
        for p in self._prov:
            if p.tenant_id == tenant_id and p.kg == kg and p.subject_id == old_id:
                p.subject_id = new_id
        # Rebind AttrCitation entity_id keys.
        cite_moves: list[tuple[tuple, _CitationRow]] = []
        cite_drop: list[tuple] = []
        for ck, c in list(self._citations.items()):
            if c.tenant_id == tenant_id and c.kg == kg and c.entity_id == old_id:
                cite_drop.append(ck)
                c.entity_id = new_id
                cite_moves.append(
                    (
                        (c.tenant_id, c.kg, new_id, c.attr, c.value_hash or ""),
                        c,
                    )
                )
        for ck in cite_drop:
            del self._citations[ck]
        for nk, crow in cite_moves:
            self._citations[nk] = crow
        # Rebind Assertion subject_id / object_id + denormalized keys.
        a_moves: list[tuple[tuple, _AssertionRow]] = []
        a_drop: list[tuple] = []
        for ak, a in list(self._assertions.items()):
            if a.tenant_id != tenant_id or a.kg != kg:
                continue
            changed = False
            if a.subject_id == old_id:
                a.subject_id = new_id
                changed = True
            if a.object_id == old_id:
                a.object_id = new_id
                changed = True
            if changed:
                a_drop.append(ak)
                a_moves.append(((a.tenant_id, a.kg, a.id), a))
        for ak in a_drop:
            del self._assertions[ak]
        for nk, arow in a_moves:
            self._assertions[nk] = arow
        # Rebind INSTANCE_OF cache key.
        old_io = self._instance_of.pop((tenant_id, kg, old_id), None)
        if old_io:
            self._instance_of.setdefault((tenant_id, kg, new_id), set()).update(
                old_io
            )

    def _add_prov(self, row: _ProvRow) -> None:
        self._prov.append(row)

    def _add_value_history(self, row: _ValueHistoryRow) -> None:
        self._value_history.append(row)

    def _list_value_history(
        self,
        tenant_id: str,
        kg: str,
        *,
        subject_id: str | None = None,
        predicate: str | None = None,
        since: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self._value_history:
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if subject_id is not None and r.subject_id != subject_id:
                continue
            if predicate is not None and r.predicate != predicate:
                # Also allow leaf match (attrs URI vs bare leaf).
                if not (
                    r.predicate.endswith("/" + predicate.rstrip("/").rsplit("/", 1)[-1])
                    or predicate.endswith("/" + r.predicate)
                    or r.predicate == predicate
                ):
                    pred_leaf = predicate.rstrip("/").rsplit("/", 1)[-1]
                    row_leaf = r.predicate.rstrip("/").rsplit("/", 1)[-1]
                    if pred_leaf != row_leaf:
                        continue
            if since and not ((r.changed_at or "") > since):
                continue
            out.append(
                {
                    "subject_id": r.subject_id,
                    "subject": r.subject_id,
                    "predicate": r.predicate,
                    "old_value": r.old_value,
                    "new_value": r.new_value,
                    "changed_at": r.changed_at,
                }
            )
        out.sort(key=lambda d: d.get("changed_at") or "")
        return out[: max(1, min(int(limit), 10000))]

    def _upsert_citation(self, row: _CitationRow) -> None:
        key = (row.tenant_id, row.kg, row.entity_id, row.attr, row.value_hash or "")
        existing = self._citations.get(key)
        if existing is None:
            self._citations[key] = row
            return
        # Merge non-empty fields onto existing citation.
        if row.source_url:
            existing.source_url = row.source_url
        if row.provenance:
            existing.provenance = row.provenance
        if row.verified_at:
            existing.verified_at = row.verified_at

    # --- ADR 0013 Class / Property / Assertion ------------------------------

    def _merge_class(
        self,
        tenant_id: str,
        kg: str,
        class_id: str,
        *,
        name: str,
        layer: str = "tenant",
    ) -> list[GraphRecord]:
        key = (tenant_id, kg, class_id)
        row = self._classes.get(key)
        if row is None:
            row = _ClassRow(
                tenant_id=tenant_id, kg=kg, id=class_id, name=name, layer=layer
            )
            self._classes[key] = row
        else:
            if name:
                row.name = name
            if layer:
                row.layer = layer
        return [
            GraphRecord(
                data={
                    "id": row.id,
                    "name": row.name,
                    "layer": row.layer,
                    "tenant_id": tenant_id,
                    "kg": kg,
                }
            )
        ]

    def _merge_property(
        self,
        tenant_id: str,
        kg: str,
        property_id: str,
        *,
        name: str,
        kind: str = "datatype",
        layer: str = "tenant",
    ) -> list[GraphRecord]:
        key = (tenant_id, kg, property_id)
        row = self._properties.get(key)
        if row is None:
            row = _PropertyRow(
                tenant_id=tenant_id,
                kg=kg,
                id=property_id,
                name=name,
                kind=kind,
                layer=layer,
            )
            self._properties[key] = row
        else:
            if name:
                row.name = name
            if kind:
                row.kind = kind
            if layer:
                row.layer = layer
        return [
            GraphRecord(
                data={
                    "id": row.id,
                    "name": row.name,
                    "kind": row.kind,
                    "layer": row.layer,
                    "tenant_id": tenant_id,
                    "kg": kg,
                }
            )
        ]

    def _set_class_subclass(
        self,
        tenant_id: str,
        kg: str,
        child_class_id: str,
        parent_class_id: str,
    ) -> None:
        self._subclass_of[(tenant_id, kg, child_class_id)] = parent_class_id

    def _clear_class_subclass(
        self, tenant_id: str, kg: str, child_class_id: str
    ) -> None:
        self._subclass_of.pop((tenant_id, kg, child_class_id), None)

    def _set_subproperty(
        self,
        tenant_id: str,
        kg: str,
        child_prop_id: str,
        parent_prop_id: str,
    ) -> None:
        self._subproperty_of[(tenant_id, kg, child_prop_id)] = parent_prop_id

    def _set_instance_of(
        self, tenant_id: str, kg: str, entity_id: str, class_id: str
    ) -> None:
        self._instance_of.setdefault((tenant_id, kg, entity_id), set()).add(class_id)

    def _clear_instance_of(
        self, tenant_id: str, kg: str, entity_id: str, class_id: str
    ) -> None:
        ios = self._instance_of.get((tenant_id, kg, entity_id))
        if ios:
            ios.discard(class_id)
            if not ios:
                self._instance_of.pop((tenant_id, kg, entity_id), None)

    def _upsert_assertion(
        self,
        tenant_id: str,
        kg: str,
        *,
        assertion_id: str,
        subject_id: str,
        property_id: str,
        object_id: str | None = None,
        object_class_id: str | None = None,
        literal_value: Any = None,
        literal_datatype: str | None = None,
        source_url: str | None = None,
        verified_at: str | None = None,
        run_id: str | None = None,
        confidence: float | None = None,
        provenance: str | None = None,
    ) -> list[GraphRecord]:
        key = (tenant_id, kg, assertion_id)
        row = _AssertionRow(
            tenant_id=tenant_id,
            kg=kg,
            id=assertion_id,
            subject_id=subject_id,
            property_id=property_id,
            literal_value=literal_value,
            literal_datatype=literal_datatype,
            object_id=object_id,
            object_class_id=object_class_id,
            source_url=source_url,
            verified_at=verified_at,
            run_id=run_id,
            confidence=confidence,
            provenance=provenance,
        )
        self._assertions[key] = row
        return [row.as_record()]

    def _delete_assertions(
        self,
        tenant_id: str,
        kg: str,
        *,
        subject_id: str,
        property_id: str | None = None,
        object_key: str | None = None,
    ) -> int:
        from infona_client.graph.assertion_model import type_membership_property_id

        type_prop = type_membership_property_id()
        drop: list[tuple[str, str, str]] = []
        # (entity_id, class_id) pairs whose type Assertion is being removed
        type_pairs: list[tuple[str, str]] = []
        for key, a in self._assertions.items():
            if a.tenant_id != tenant_id or a.kg != kg:
                continue
            if a.subject_id != subject_id:
                continue
            if property_id is not None and a.property_id != property_id:
                continue
            if object_key is not None:
                ok = (
                    a.object_id
                    or a.object_class_id
                    or (
                        str(a.literal_value)
                        if a.literal_value is not None
                        else ""
                    )
                )
                if ok != object_key:
                    continue
            drop.append(key)
            if a.object_class_id and (
                property_id is None or a.property_id == type_prop
            ):
                type_pairs.append((a.subject_id, a.object_class_id))
        for key in drop:
            del self._assertions[key]
        # Evict derived INSTANCE_OF when no remaining type Assertion backs it.
        for eid, cid in type_pairs:
            still = any(
                a.tenant_id == tenant_id
                and a.kg == kg
                and a.subject_id == eid
                and a.object_class_id == cid
                for a in self._assertions.values()
            )
            if not still:
                self._clear_instance_of(tenant_id, kg, eid, cid)
        return len(drop)

    def _subclass_closure(
        self, tenant_id: str, kg: str, class_id: str
    ) -> list[str]:
        # Build child→parent for this scope, then descendants of class_id.
        child_to_parent: dict[str, str] = {}
        for (t, k, child), parent in self._subclass_of.items():
            if t == tenant_id and k == kg:
                child_to_parent[child] = parent
        from infona_client.graph.rdfs_helpers import descendants_of

        return descendants_of(class_id, child_to_parent)

    def _subproperty_closure(
        self, tenant_id: str, kg: str, prop_id: str
    ) -> list[str]:
        child_to_parent: dict[str, str] = {}
        for (t, k, child), parent in self._subproperty_of.items():
            if t == tenant_id and k == kg:
                child_to_parent[child] = parent
        from infona_client.graph.rdfs_helpers import descendants_of

        return descendants_of(prop_id, child_to_parent)

    def _entities_of_type_ids(
        self, tenant_id: str, kg: str, class_ids: list[str]
    ) -> list[str]:
        allowed = set(class_ids)
        out: set[str] = set()
        for (t, k, eid), cids in self._instance_of.items():
            if t != tenant_id or k != kg:
                continue
            if cids & allowed:
                out.add(eid)
        # Also from type Assertions if INSTANCE_OF missed
        for (t, k, _), a in self._assertions.items():
            if t != tenant_id or k != kg:
                continue
            if a.object_class_id and a.object_class_id in allowed:
                out.add(a.subject_id)
        return sorted(out)

    def _assertions_for_subject(
        self,
        tenant_id: str,
        kg: str,
        entity_id: str,
        *,
        prop_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for (t, k, _), a in sorted(
            self._assertions.items(), key=lambda x: (x[1].property_id, x[1].id)
        ):
            if t != tenant_id or k != kg:
                continue
            if a.subject_id != entity_id:
                continue
            if prop_id is not None and a.property_id != prop_id:
                continue
            rows.append(a.as_record().to_dict())
        return rows

    def _assertion_history(
        self,
        tenant_id: str,
        kg: str,
        *,
        entity_id: str | None = None,
        prop_id: str | None = None,
        since: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """All Assertion rows in scope (optional subject/property/since)."""
        lim = max(1, min(int(limit), 10000))
        rows: list[dict[str, Any]] = []
        for (t, k, _), a in sorted(
            self._assertions.items(),
            key=lambda x: (
                x[1].verified_at or "",
                x[1].property_id,
                x[1].subject_id,
                x[1].id,
            ),
        ):
            if t != tenant_id or k != kg:
                continue
            if entity_id is not None and a.subject_id != entity_id:
                continue
            if prop_id is not None and a.property_id != prop_id:
                continue
            if since:
                va = (a.verified_at or "").strip()
                if not va or va <= since:
                    continue
            rows.append(a.as_record().to_dict())
            if len(rows) >= lim:
                break
        return rows

    # --- Structural QC scans (E8) -------------------------------------------

    def scan_entities_missing_primary_type(
        self, tenant_id: str, kg: str
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for (t, k, eid), row in self._entities.items():
            if t != tenant_id or k != kg:
                continue
            if not row.primary_type:
                out.append({"id": eid, "tenant_id": t, "kg": k})
        return out

    def scan_rels_missing_scope(
        self, tenant_id: str, kg: str
    ) -> list[dict[str, Any]]:
        """Rels in this scope whose tenant_id/kg are empty (construction bug)."""
        out: list[dict[str, Any]] = []
        for r in self._rels.values():
            # Include unscoped rows that claim this scope via empty mismatch, and
            # any row with blank scope fields that would pollute isolation.
            if not r.tenant_id or not r.kg:
                out.append(
                    {
                        "start_id": r.start_id,
                        "end_id": r.end_id,
                        "attr": r.attr,
                        "tenant_id": r.tenant_id,
                        "kg": r.kg,
                    }
                )
            elif r.tenant_id == tenant_id and r.kg == kg:
                # In-scope but somehow blank after normalize — already covered.
                pass
        return out

    def scan_orphan_rel_targets(
        self, tenant_id: str, kg: str
    ) -> list[dict[str, Any]]:
        """Relationship endpoints whose Entity node is missing in scope."""
        out: list[dict[str, Any]] = []
        for r in self._rels.values():
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if (tenant_id, kg, r.end_id) not in self._entities:
                out.append(
                    {
                        "start_id": r.start_id,
                        "end_id": r.end_id,
                        "attr": r.attr,
                        "side": "end",
                    }
                )
            if (tenant_id, kg, r.start_id) not in self._entities:
                out.append(
                    {
                        "start_id": r.start_id,
                        "end_id": r.end_id,
                        "attr": r.attr,
                        "side": "start",
                    }
                )
        return out

    def scan_instance_of_without_type_assertion(
        self, tenant_id: str, kg: str
    ) -> list[dict[str, Any]]:
        """Derived ``INSTANCE_OF`` edges with no backing type Assertion (ADR 0013 skew).

        Assertion is SoT; ``INSTANCE_OF`` is cache only. Any cache edge without a
        type Assertion (``object_class_id`` → Class) is dual-write skew.
        """
        out: list[dict[str, Any]] = []
        for (t, k, eid), class_ids in self._instance_of.items():
            if t != tenant_id or k != kg:
                continue
            for cid in class_ids:
                backed = any(
                    a.tenant_id == tenant_id
                    and a.kg == kg
                    and a.subject_id == eid
                    and a.object_class_id == cid
                    for a in self._assertions.values()
                )
                if not backed:
                    out.append(
                        {
                            "entity_id": eid,
                            "class_id": cid,
                            "tenant_id": tenant_id,
                            "kg": kg,
                        }
                    )
        return out

    # --- Ontology catalog (E4) ----------------------------------------------

    def _upsert_onto_type(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        name: str,
        *,
        description: str = "",
        description_updated_at: str | None = None,
        description_provided: bool = False,
        label_token: str | None = None,
        uri: str | None = None,
    ) -> list[GraphRecord]:
        from infona_client.graph.ontology_queries import type_uri

        key = (tenant_id, kg, layer, name)
        class_id = uri or type_uri(name)
        existing = self._onto_types.get(key)
        dua = (
            str(description_updated_at)
            if description_updated_at
            else None
        )
        if existing is None:
            row = _OntoTypeRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                name=name,
                description=description or "",
                description_updated_at=dua,
                label_token=label_token,
                uri=class_id,
            )
            self._onto_types[key] = row
        else:
            if not (existing.description or "").strip() and description:
                existing.description = description
                if dua:
                    existing.description_updated_at = dua
            elif description_provided and description and description != existing.description:
                existing.description = description
                if dua:
                    existing.description_updated_at = dua
            elif existing.description_updated_at is None and dua:
                existing.description_updated_at = dua
            if label_token is not None:
                existing.label_token = label_token
            if uri is not None:
                existing.uri = uri
            elif existing.uri is None:
                existing.uri = class_id
            row = existing
            class_id = row.uri or class_id
        # ADR 0013 dual-write: Class node (id = type IRI) alongside OntoType.
        self._merge_class(tenant_id, kg, class_id, name=name, layer=layer)
        return [row.as_record()]

    def _set_subclass(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        name: str,
        parent_name: str,
        parent_label_token: str | None,
    ) -> list[GraphRecord]:
        from infona_client.graph.ontology_queries import type_uri

        child_key = (tenant_id, kg, layer, name)
        child = self._onto_types.get(child_key)
        if child is None:
            return []
        parent_key = (tenant_id, kg, layer, parent_name)
        if parent_key not in self._onto_types:
            self._onto_types[parent_key] = _OntoTypeRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                name=parent_name,
                label_token=parent_label_token,
                uri=type_uri(parent_name),
            )
        child.parent_type = parent_name
        # Dual-write Class SUBCLASS_OF (preferred NL hierarchy).
        child_id = child.uri or type_uri(name)
        parent_row = self._onto_types[parent_key]
        parent_id = parent_row.uri or type_uri(parent_name)
        self._merge_class(tenant_id, kg, child_id, name=name, layer=layer)
        self._merge_class(
            tenant_id, kg, parent_id, name=parent_name, layer=layer
        )
        self._set_class_subclass(tenant_id, kg, child_id, parent_id)
        return [
            GraphRecord(data={"name": name, "parent_type": parent_name})
        ]

    def _clear_subclass(
        self, tenant_id: str, kg: str, layer: str, name: str
    ) -> list[GraphRecord]:
        from infona_client.graph.ontology_queries import type_uri

        child = self._onto_types.get((tenant_id, kg, layer, name))
        if child is None:
            return []
        child.parent_type = None
        child_id = child.uri or type_uri(name)
        self._clear_class_subclass(tenant_id, kg, child_id)
        return [GraphRecord(data={"name": name, "parent_type": None})]

    def _list_onto_types(
        self, tenant_id: str, kg: str, layer: str | None
    ) -> list[GraphRecord]:
        rows: list[_OntoTypeRow] = []
        for (t, k, ly, _n), row in self._onto_types.items():
            if t != tenant_id or k != kg:
                continue
            if layer is not None and ly != layer:
                continue
            rows.append(row)
        rows.sort(key=lambda r: r.name)
        return [r.as_record() for r in rows]

    def _get_onto_type(
        self, tenant_id: str, kg: str, layer: str, name: str
    ) -> list[GraphRecord]:
        row = self._onto_types.get((tenant_id, kg, layer, name))
        return [row.as_record()] if row else []

    def _upsert_onto_attr(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        domain: str,
        name: str,
        *,
        kind: str,
        datatype: str | None,
        range_type: str | None,
        cardinality: str,
        description: str,
        description_updated_at: str | None = None,
        description_provided: bool = False,
        prop_key: str | None,
        domain_label_token: str | None,
        domain_description: str = "",
        domain_description_updated_at: str | None = None,
    ) -> list[GraphRecord]:
        # Ensure domain type exists (DECLARES target) with mandatory description.
        dkey = (tenant_id, kg, layer, domain)
        if dkey not in self._onto_types:
            self._onto_types[dkey] = _OntoTypeRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                name=domain,
                description=domain_description or "",
                description_updated_at=(
                    str(domain_description_updated_at)
                    if domain_description_updated_at
                    else None
                ),
                label_token=domain_label_token,
            )
        elif not (self._onto_types[dkey].description or "").strip() and domain_description:
            self._onto_types[dkey].description = domain_description
            if domain_description_updated_at:
                self._onto_types[dkey].description_updated_at = str(
                    domain_description_updated_at
                )
        akey = (tenant_id, kg, layer, domain, name)
        existing = self._onto_attrs.get(akey)
        dua = str(description_updated_at) if description_updated_at else None
        if existing is None:
            row = _OntoAttrRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                domain=domain,
                name=name,
                kind=kind,
                datatype=datatype,
                range_type=range_type,
                cardinality=cardinality or "1:1",
                description=description or "",
                description_updated_at=dua,
                prop_key=prop_key,
            )
            self._onto_attrs[akey] = row
        else:
            existing.kind = kind
            existing.datatype = datatype
            existing.range_type = range_type
            if cardinality:
                existing.cardinality = cardinality
            if not (existing.description or "").strip() and description:
                existing.description = description
                if dua:
                    existing.description_updated_at = dua
            elif description_provided and description and description != existing.description:
                existing.description = description
                if dua:
                    existing.description_updated_at = dua
            elif existing.description_updated_at is None and dua:
                existing.description_updated_at = dua
            if prop_key is not None:
                existing.prop_key = prop_key
            # text_kind is NOT touched by a general attribute upsert — only
            # set_text_kind / the dedicated template owns that field (ONTA-533).
            row = existing
        return [row.as_record()]

    def _set_onto_attr_text_kind(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        domain: str,
        name: str,
        text_kind: str,
        domain_label_token: str | None,
    ) -> list[GraphRecord]:
        """SET_TEXT_KIND: durable free-text candidacy on :OntoAttr (ONTA-533)."""
        dkey = (tenant_id, kg, layer, domain)
        if dkey not in self._onto_types:
            self._onto_types[dkey] = _OntoTypeRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                name=domain,
                label_token=domain_label_token,
            )
        akey = (tenant_id, kg, layer, domain, name)
        kind_val = (text_kind or "").strip() or None
        existing = self._onto_attrs.get(akey)
        if existing is None:
            row = _OntoAttrRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                domain=domain,
                name=name,
                kind="literal",
                datatype="string",
                cardinality="1:1",
                description="",
                text_kind=kind_val,
            )
            self._onto_attrs[akey] = row
        else:
            existing.text_kind = kind_val
            row = existing
        return [row.as_record()]

    def _set_attr_range_type(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        domain: str,
        name: str,
        range_type: str,
        range_label_token: str | None,
    ) -> list[GraphRecord]:
        akey = (tenant_id, kg, layer, domain, name)
        attr = self._onto_attrs.get(akey)
        if attr is None:
            return []
        rkey = (tenant_id, kg, layer, range_type)
        if rkey not in self._onto_types:
            self._onto_types[rkey] = _OntoTypeRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                name=range_type,
                label_token=range_label_token,
            )
        attr.range_type = range_type
        attr.kind = "relationship"
        return [GraphRecord(data={"name": name, "range_type": range_type})]

    def _list_onto_attrs(
        self,
        tenant_id: str,
        kg: str,
        domain: str | None,
        layer: str | None,
    ) -> list[GraphRecord]:
        rows: list[_OntoAttrRow] = []
        for (t, k, ly, dom, _n), row in self._onto_attrs.items():
            if t != tenant_id or k != kg:
                continue
            if layer is not None and ly != layer:
                continue
            if domain is not None and dom != domain:
                continue
            rows.append(row)
        rows.sort(key=lambda r: (r.domain, r.name))
        return [r.as_record() for r in rows]

    def _delete_onto_attr(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        domain: str,
        name: str,
    ) -> list[GraphRecord]:
        akey = (tenant_id, kg, layer, domain, name)
        if akey not in self._onto_attrs:
            return []
        del self._onto_attrs[akey]
        return [GraphRecord(data={"name": name, "domain": domain})]

    def _delete_onto_type(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        name: str,
    ) -> list[GraphRecord]:
        tkey = (tenant_id, kg, layer, name)
        if tkey not in self._onto_types:
            return []
        del self._onto_types[tkey]
        # Drop Class dual-write row when present.
        from infona_client.graph.ontology_queries import type_uri

        uri = type_uri(name)
        self._classes.pop((tenant_id, kg, uri), None)
        self._subclass_of.pop((tenant_id, kg, uri), None)
        return [GraphRecord(data={"name": name})]

    def _set_onto_attr_markers(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        domain: str,
        name: str,
        *,
        core_slot: Any = None,
        text_kind: Any = None,
        clear_text_kind: bool = False,
        deprecated_at: Any = None,
        superseded_by: Any = None,
        clear_deprecation: bool = False,
    ) -> list[GraphRecord]:
        akey = (tenant_id, kg, layer, domain, name)
        attr = self._onto_attrs.get(akey)
        if attr is None:
            return []
        if core_slot is not None:
            attr.core_slot = bool(core_slot)
        if clear_text_kind:
            attr.text_kind = None
        elif text_kind is not None:
            attr.text_kind = str(text_kind) if text_kind != "" else None
        if clear_deprecation:
            attr.deprecated_at = None
            attr.superseded_by = None
        else:
            if deprecated_at is not None:
                attr.deprecated_at = str(deprecated_at)
            if superseded_by is not None:
                attr.superseded_by = str(superseded_by) if superseded_by else None
        return [attr.as_record()]

    def _set_onto_type_markers(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        name: str,
        *,
        description: Any = None,
        deprecated_at: Any = None,
        superseded_by: Any = None,
        clear_deprecation: bool = False,
    ) -> list[GraphRecord]:
        tkey = (tenant_id, kg, layer, name)
        row = self._onto_types.get(tkey)
        if row is None:
            return []
        if description is not None:
            row.description = str(description)
        if clear_deprecation:
            row.deprecated_at = None
            row.superseded_by = None
        else:
            if deprecated_at is not None:
                row.deprecated_at = str(deprecated_at)
            if superseded_by is not None:
                row.superseded_by = str(superseded_by) if superseded_by else None
        return [row.as_record()]

    def _entity_counts_by_primary_type(
        self, tenant_id: str, kg: str
    ) -> list[GraphRecord]:
        """Count entities per Class.name via INSTANCE_OF (ADR 0013)."""
        # entity → set of class names (from INSTANCE_OF + Class rows; fall back
        # to type Assertions' object_class leaf; never primary_type alone).
        by_class: dict[str, set[str]] = {}
        for (t, k, eid), cids in self._instance_of.items():
            if t != tenant_id or k != kg:
                continue
            for cid in cids:
                crow = self._classes.get((t, k, cid))
                cname = crow.name if crow else cid.rstrip("/").rsplit("/", 1)[-1]
                if cname:
                    by_class.setdefault(cname, set()).add(eid)
        for (t, k, _), a in self._assertions.items():
            if t != tenant_id or k != kg or not a.object_class_id:
                continue
            cid = a.object_class_id
            crow = self._classes.get((t, k, cid))
            cname = crow.name if crow else cid.rstrip("/").rsplit("/", 1)[-1]
            if cname:
                by_class.setdefault(cname, set()).add(a.subject_id)
        return [
            GraphRecord(data={"primary_type": pt, "n": len(eids)})
            for pt, eids in sorted(by_class.items())
        ]

    def _list_entities_by_type_page(
        self,
        tenant_id: str,
        kg: str,
        primary_type: str,
        after_id: str | None,
        limit: int,
    ) -> list[GraphRecord]:
        # Semantic membership via INSTANCE_OF → Class (param name historical).
        return self._list_entities_by_types_page(
            tenant_id, kg, [primary_type], after_id, limit
        )

    def _list_entities_by_label(
        self,
        tenant_id: str,
        kg: str,
        label: str,
        after_id: str | None,
        limit: int,
    ) -> list[GraphRecord]:
        rows = [
            r
            for (t, k, _), r in sorted(self._entities.items(), key=lambda x: x[0][2])
            if t == tenant_id and k == kg and label in (r.labels or [])
        ]
        if after_id is not None:
            rows = [r for r in rows if r.id > after_id]
        if limit is not None and limit >= 0:
            rows = rows[: int(limit)]
        return [
            GraphRecord(
                data={
                    "id": r.id,
                    "tenant_id": r.tenant_id,
                    "kg": r.kg,
                    "primary_type": r.primary_type,
                    "name": r.name,
                    "source": r.source,
                }
            )
            for r in rows
        ]

    def _entity_count_by_type(
        self, tenant_id: str, kg: str, primary_type: str
    ) -> list[GraphRecord]:
        return self._entity_count_by_types(tenant_id, kg, [primary_type])

    @staticmethod
    def _as_type_name_set(raw: Any) -> set[str]:
        if raw is None:
            return set()
        if isinstance(raw, str):
            return {raw} if raw else set()
        return {str(x) for x in raw if x is not None and str(x)}

    def _class_ids_matching_type_names(
        self, tenant_id: str, kg: str, type_names: Any
    ) -> set[str]:
        """Resolve NL ``$type_names`` (leaves and/or Class IRIs) to Class ids."""
        names = self._as_type_name_set(type_names)
        if not names:
            return set()
        ids: set[str] = set()
        for (t, k, cid), row in self._classes.items():
            if t != tenant_id or k != kg:
                continue
            if row.name in names or cid in names:
                ids.add(cid)
        # Accept leaf names that have not yet been MERGEd as Class rows by
        # minting the shared type_uri (assert_fact / type Assertions use this).
        from infona_client.graph.ontology_queries import type_uri

        for n in names:
            if n.startswith("http://") or n.startswith("https://"):
                ids.add(n)
            else:
                ids.add(type_uri(n))
        return ids

    def _entity_ids_via_instance_of(
        self, tenant_id: str, kg: str, type_names: Any
    ) -> set[str]:
        """Entity ids with INSTANCE_OF (or type Assertion) in ``type_names``."""
        allowed = self._class_ids_matching_type_names(tenant_id, kg, type_names)
        if not allowed:
            return set()
        out: set[str] = set()
        for (t, k, eid), cids in self._instance_of.items():
            if t == tenant_id and k == kg and cids & allowed:
                out.add(eid)
        for (t, k, _), a in self._assertions.items():
            if t != tenant_id or k != kg:
                continue
            if a.object_class_id and a.object_class_id in allowed:
                out.add(a.subject_id)
        return out

    def _entity_count_by_types(
        self, tenant_id: str, kg: str, type_names: Any
    ) -> list[GraphRecord]:
        # Semantic path: INSTANCE_OF → Class (ADR 0013), not primary_type alone.
        matched = self._entity_ids_via_instance_of(tenant_id, kg, type_names)
        return [GraphRecord(data={"n": len(matched)})]

    def _list_entities_by_types_page(
        self,
        tenant_id: str,
        kg: str,
        type_names: Any,
        after_id: str | None,
        limit: int,
    ) -> list[GraphRecord]:
        matched = self._entity_ids_via_instance_of(tenant_id, kg, type_names)
        rows = [
            r
            for (t, k, eid), r in sorted(self._entities.items(), key=lambda x: x[0][2])
            if t == tenant_id and k == kg and eid in matched
        ]
        if after_id is not None:
            rows = [r for r in rows if r.id > after_id]
        if limit is not None and limit >= 0:
            rows = rows[: int(limit)]
        return [
            GraphRecord(
                data={
                    "id": r.id,
                    "tenant_id": r.tenant_id,
                    "kg": r.kg,
                    "primary_type": r.primary_type,
                    "name": r.name,
                    "source": r.source,
                }
            )
            for r in rows
        ]

    @staticmethod
    def _literal_eq(store_val: Any, query_val: Any) -> bool:
        """Equality with SPARQL-era ``lexical^^xsd`` stripping + numeric coerce.

        Mirrors LITERAL_VALUES_CYPHER: normalize both sides (strip legacy
        datatype suffixes), then accept raw ==, string form of normalized
        values, or float equality when both coerce to numbers.
        """
        from infona_client.graph.assertion_model import normalize_store_literal

        left = normalize_store_literal(store_val)
        right = normalize_store_literal(query_val)
        if left == right:
            return True
        if left is None or right is None:
            return False
        if str(left).strip() == str(right).strip():
            return True
        if isinstance(left, bool) or isinstance(right, bool):
            return False
        try:
            return float(left) == float(right)
        except (TypeError, ValueError):
            return False

    def _literal_values_eq(
        self,
        tenant_id: str,
        kg: str,
        type_names: Any,
        prop_key: str,
        prop_value: Any,
        limit: int,
    ) -> list[GraphRecord]:
        # Prefer Assertion literal SoT; Entity property cache is secondary.
        matched = self._entity_ids_via_instance_of(tenant_id, kg, type_names)
        from infona_client.graph.assertion_model import property_uri

        prop_id = property_uri(prop_key) if prop_key else None
        out: list[GraphRecord] = []
        seen: set[str] = set()
        for (t, k, _), a in sorted(
            self._assertions.items(), key=lambda x: x[1].subject_id
        ):
            if t != tenant_id or k != kg:
                continue
            if a.subject_id not in matched:
                continue
            if a.literal_value is None:
                continue
            if prop_id is not None and a.property_id != prop_id:
                # Also accept Property catalog name match.
                prop_row = self._properties.get((tenant_id, kg, a.property_id))
                if prop_row is None or prop_row.name != prop_key:
                    continue
            if not self._literal_eq(a.literal_value, prop_value):
                continue
            if a.subject_id in seen:
                continue
            r = self._entities.get((tenant_id, kg, a.subject_id))
            if r is None:
                continue
            seen.add(a.subject_id)
            out.append(
                GraphRecord(
                    data={
                        "id": r.id,
                        "name": r.name,
                        "primary_type": r.primary_type,
                        "literal_value": a.literal_value,
                    }
                )
            )
            if len(out) >= limit:
                return out
        # Secondary: Entity property cache (dual-written after Assertion).
        for eid in sorted(matched):
            if eid in seen:
                continue
            r = self._entities.get((tenant_id, kg, eid))
            if r is None:
                continue
            actual = self._entity_prop_value(r, prop_key)
            if actual is None or not self._literal_eq(actual, prop_value):
                continue
            out.append(
                GraphRecord(
                    data={
                        "id": r.id,
                        "name": r.name,
                        "primary_type": r.primary_type,
                        "literal_value": actual,
                    }
                )
            )
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _to_float_legacy(raw: Any) -> float | None:
        """Coerce store values to float; strip SPARQL-era ``^^xsd`` suffixes."""
        if raw is None:
            return None
        if isinstance(raw, bool):
            return None
        if isinstance(raw, (int, float)):
            return float(raw)
        text = str(raw).strip()
        if "^^" in text:
            text = text.split("^^", 1)[0].strip()
        try:
            return float(text)
        except ValueError:
            return None

    def _literal_compare(
        self,
        tenant_id: str,
        kg: str,
        type_names: Any,
        prop_key: str,
        op: str,
        threshold: Any,
        limit: int,
    ) -> list[GraphRecord]:
        """Numeric inequality over Assertion SoT + Entity property cache."""
        from infona_client.graph.assertion_model import property_uri

        try:
            thr = float(threshold)
        except (TypeError, ValueError):
            return []
        op = (op or "lt").strip().lower()
        matched = self._entity_ids_via_instance_of(tenant_id, kg, type_names)
        prop_id = property_uri(prop_key) if prop_key else None
        rows: list[tuple[float, GraphRecord]] = []
        seen: set[str] = set()

        def _op_ok(num: float) -> bool:
            if op == "lt":
                return num < thr
            if op == "le":
                return num <= thr
            if op == "gt":
                return num > thr
            if op == "ge":
                return num >= thr
            if op == "eq":
                return num == thr
            return False

        for (t, k, _), a in sorted(
            self._assertions.items(), key=lambda x: x[1].subject_id
        ):
            if t != tenant_id or k != kg:
                continue
            if a.subject_id not in matched or a.literal_value is None:
                continue
            if prop_id is not None and a.property_id != prop_id:
                prop_row = self._properties.get((tenant_id, kg, a.property_id))
                if prop_row is None or prop_row.name != prop_key:
                    continue
            num = self._to_float_legacy(a.literal_value)
            if num is None or not _op_ok(num):
                continue
            if a.subject_id in seen:
                continue
            r = self._entities.get((tenant_id, kg, a.subject_id))
            if r is None:
                continue
            seen.add(a.subject_id)
            title = self._entity_prop_value(r, "title") or r.name
            rows.append(
                (
                    num,
                    GraphRecord(
                        data={
                            "id": r.id,
                            "name": r.name,
                            "primary_type": r.primary_type,
                            "title": title,
                            "value": num,
                        }
                    ),
                )
            )

        for eid in sorted(matched):
            if eid in seen:
                continue
            r = self._entities.get((tenant_id, kg, eid))
            if r is None:
                continue
            raw = self._entity_prop_value(r, prop_key)
            num = self._to_float_legacy(raw)
            if num is None or not _op_ok(num):
                continue
            title = self._entity_prop_value(r, "title") or r.name
            rows.append(
                (
                    num,
                    GraphRecord(
                        data={
                            "id": r.id,
                            "name": r.name,
                            "primary_type": r.primary_type,
                            "title": title,
                            "value": num,
                        }
                    ),
                )
            )

        rows.sort(key=lambda x: (x[0], str(x[1].get("id") or "")))
        return [rec for _, rec in rows[:limit]]

    def _related_entity_name_filter(
        self,
        tenant_id: str,
        kg: str,
        type_names: Any,
        rel_attr: str,
        target_name: str,
        limit: int,
    ) -> list[GraphRecord]:
        """Subjects of type(s) linked to a related entity matching display name."""
        if not rel_attr or not target_name:
            return []
        needle = target_name.strip().lower()
        matched = self._entity_ids_via_instance_of(tenant_id, kg, type_names)
        out: list[GraphRecord] = []
        seen: set[str] = set()

        def _name_hits(ent: Any) -> bool:
            display = str(getattr(ent, "display_name", None) or "").strip().lower()
            # Entity cache may store display_name as a property.
            if not display:
                display = str(self._entity_prop_value(ent, "display_name") or "").strip().lower()
            name = str(getattr(ent, "name", None) or "").strip().lower()
            spaced = name.replace("_", " ")
            if needle in (display, name, spaced):
                return True
            # Substring: "Acme" matches "Acme Corp"
            return bool(needle) and (
                needle in display or needle in name or needle in spaced
            )

        for (t, k, _), a in sorted(
            self._assertions.items(), key=lambda x: x[1].subject_id
        ):
            if t != tenant_id or k != kg:
                continue
            if not a.object_id or a.subject_id not in matched:
                continue
            prop_row = self._properties.get((tenant_id, kg, a.property_id))
            prop_name = prop_row.name if prop_row else a.property_id
            if prop_name != rel_attr and a.property_id != rel_attr:
                continue
            target = self._entities.get((tenant_id, kg, a.object_id))
            subj = self._entities.get((tenant_id, kg, a.subject_id))
            if target is None or subj is None or not _name_hits(target):
                continue
            if subj.id in seen:
                continue
            seen.add(subj.id)
            related = (
                self._entity_prop_value(target, "display_name")
                or target.name
            )
            title = self._entity_prop_value(subj, "title") or subj.name
            out.append(
                GraphRecord(
                    data={
                        "id": subj.id,
                        "title": title,
                        "primary_type": subj.primary_type,
                        "related_name": related,
                    }
                )
            )
            if len(out) >= limit:
                return out
        return out


    def _literal_aggregate(
        self,
        tenant_id: str,
        kg: str,
        type_names: Any,
        prop_key: str,
        agg_op: str,
    ) -> list[GraphRecord]:
        """SUM/AVG/MIN/MAX over Assertion SoT + Entity property cache."""
        from infona_client.graph.assertion_model import property_uri

        op = (agg_op or "sum").strip().lower()
        if op not in {"sum", "avg", "min", "max"}:
            return [GraphRecord(data={"value": None})]
        matched = self._entity_ids_via_instance_of(tenant_id, kg, type_names)
        prop_id = property_uri(prop_key) if prop_key else None
        nums: list[float] = []
        seen: set[str] = set()

        for (t, k, _), a in self._assertions.items():
            if t != tenant_id or k != kg:
                continue
            if a.subject_id not in matched or a.literal_value is None:
                continue
            if prop_id is not None and a.property_id != prop_id:
                prop_row = self._properties.get((tenant_id, kg, a.property_id))
                if prop_row is None or prop_row.name != prop_key:
                    continue
            num = self._to_float_legacy(a.literal_value)
            if num is None:
                continue
            if a.subject_id in seen:
                continue
            seen.add(a.subject_id)
            nums.append(num)

        for eid in matched:
            if eid in seen:
                continue
            r = self._entities.get((tenant_id, kg, eid))
            if r is None:
                continue
            raw = self._entity_prop_value(r, prop_key)
            num = self._to_float_legacy(raw)
            if num is None:
                continue
            seen.add(eid)
            nums.append(num)

        if not nums:
            return [GraphRecord(data={"value": None})]
        if op == "sum":
            val = float(sum(nums))
        elif op == "avg":
            val = float(sum(nums)) / len(nums)
        elif op == "min":
            val = float(min(nums))
        else:
            val = float(max(nums))
        return [GraphRecord(data={"value": val})]

    def _related_entities(
        self,
        tenant_id: str,
        kg: str,
        from_types: Any,
        to_types: Any,
        rel_attr: str | None,
        limit: int,
    ) -> list[GraphRecord]:
        # Prefer object Assertions (SoT); shortcut rels are derived dual-write.
        from_ids = self._entity_ids_via_instance_of(tenant_id, kg, from_types)
        to_ids = (
            None
            if to_types is None
            else self._entity_ids_via_instance_of(tenant_id, kg, to_types)
        )
        out: list[GraphRecord] = []
        seen: set[tuple[str, str, str]] = set()
        for (t, k, _), a in sorted(
            self._assertions.items(),
            key=lambda x: (x[1].subject_id, x[1].object_id or ""),
        ):
            if t != tenant_id or k != kg:
                continue
            if not a.object_id or a.subject_id not in from_ids:
                continue
            if to_ids is not None and a.object_id not in to_ids:
                continue
            prop_row = self._properties.get((tenant_id, kg, a.property_id))
            prop_name = prop_row.name if prop_row else a.property_id
            if rel_attr is not None and prop_name != rel_attr and a.property_id != rel_attr:
                continue
            start = self._entities.get((tenant_id, kg, a.subject_id))
            end = self._entities.get((tenant_id, kg, a.object_id))
            if start is None or end is None:
                continue
            key = (start.id, end.id, prop_name)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                GraphRecord(
                    data={
                        "from_id": start.id,
                        "from_name": start.name,
                        "from_type": start.primary_type,
                        "to_id": end.id,
                        "to_name": end.name,
                        "to_type": end.primary_type,
                        "rel_type": prop_name,
                        "attr": prop_name,
                    }
                )
            )
            if len(out) >= limit:
                return out
        # Secondary derived shortcut rels (document as cache — dual-written).
        for rel in sorted(
            self._rels.values(),
            key=lambda r: (r.start_id, r.end_id),
        ):
            if rel.tenant_id != tenant_id or rel.kg != kg:
                continue
            if rel.start_id not in from_ids:
                continue
            if to_ids is not None and rel.end_id not in to_ids:
                continue
            if rel_attr is not None and rel.attr != rel_attr and rel.rel_type != rel_attr:
                continue
            a = self._entities.get((tenant_id, kg, rel.start_id))
            b = self._entities.get((tenant_id, kg, rel.end_id))
            if a is None or b is None:
                continue
            key = (a.id, b.id, rel.attr or rel.rel_type)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                GraphRecord(
                    data={
                        "from_id": a.id,
                        "from_name": a.name,
                        "from_type": a.primary_type,
                        "to_id": b.id,
                        "to_name": b.name,
                        "to_type": b.primary_type,
                        "rel_type": rel.rel_type,
                        "attr": rel.attr or rel.rel_type,
                    }
                )
            )
            if len(out) >= limit:
                break
        return out

    def _subclass_of_closure_names(
        self,
        tenant_id: str,
        kg: str,
        type_name: str,
        layer: str | None,
    ) -> list[GraphRecord]:
        """Descendant Class names including self (prefer Class SUBCLASS_OF).

        Falls back to OntoType parent map when no Class hierarchy is present
        in this scope (dual-write lag / legacy fixtures).
        """
        from infona_client.graph.rdfs_helpers import descendants_of

        # Prefer :Class hierarchy (ADR 0013).
        child_to_parent: dict[str, str] = {}
        id_to_name: dict[str, str] = {}
        for (t, k, cid), row in self._classes.items():
            if t != tenant_id or k != kg:
                continue
            if layer is not None and row.layer != layer:
                continue
            id_to_name[cid] = row.name
        for (t, k, child_id), parent_id in self._subclass_of.items():
            if t != tenant_id or k != kg:
                continue
            cname = id_to_name.get(child_id)
            pname = id_to_name.get(parent_id)
            if cname and pname:
                child_to_parent[cname] = pname
        if child_to_parent or any(
            n == type_name for n in id_to_name.values()
        ):
            names = descendants_of(type_name, child_to_parent)
            # If type_name is known as a Class but has no descendants map entry,
            # descendants_of still returns [type_name].
            if names:
                return [GraphRecord(data={"type_name": n}) for n in names]

        # Fallback: OntoType catalog rows (legacy dual surface).
        child_to_parent = {}
        for (t, k, lyr, name), row in self._onto_types.items():
            if t != tenant_id or k != kg:
                continue
            if layer is not None and lyr != layer:
                continue
            parent = getattr(row, "parent_type", None) or getattr(row, "parent", None)
            if parent:
                child_to_parent[name] = str(parent)
        names = descendants_of(type_name, child_to_parent)
        return [GraphRecord(data={"type_name": n}) for n in names]

    def _entity_count_total(self, tenant_id: str, kg: str) -> list[GraphRecord]:
        n = sum(
            1 for (t, k, _) in self._entities if t == tenant_id and k == kg
        )
        return [GraphRecord(data={"n": n})]

    def _entity_detail(
        self, tenant_id: str, kg: str, entity_id: str
    ) -> list[GraphRecord]:
        row = self._entities.get((tenant_id, kg, entity_id))
        if row is None:
            return []
        props = {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "kg": row.kg,
            "primary_type": row.primary_type,
            "name": row.name,
            "source": row.source,
            **copy.deepcopy(row.props),
        }
        return [
            GraphRecord(
                data={
                    "id": row.id,
                    "tenant_id": row.tenant_id,
                    "kg": row.kg,
                    "primary_type": row.primary_type,
                    "name": row.name,
                    "source": row.source,
                    "labels": list(row.labels),
                    "props": props,
                }
            )
        ]

    def _entity_rels(
        self, tenant_id: str, kg: str, entity_id: str
    ) -> list[GraphRecord]:
        out: list[GraphRecord] = []
        for r in self._rels.values():
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if r.start_id == entity_id:
                other = self._entities.get((tenant_id, kg, r.end_id))
                out.append(
                    GraphRecord(
                        data={
                            "attr": r.attr,
                            "rel_type": r.rel_type,
                            "other_id": r.end_id,
                            "other_name": other.name if other else None,
                            "other_type": other.primary_type if other else None,
                            "direction": "out",
                        }
                    )
                )
            if r.end_id == entity_id:
                other = self._entities.get((tenant_id, kg, r.start_id))
                out.append(
                    GraphRecord(
                        data={
                            "attr": r.attr,
                            "rel_type": r.rel_type,
                            "other_id": r.start_id,
                            "other_name": other.name if other else None,
                            "other_type": other.primary_type if other else None,
                            "direction": "in",
                        }
                    )
                )
        out.sort(key=lambda rec: (rec.get("direction"), rec.get("attr"), rec.get("other_id")))
        return out

    def _entity_type_attr_coverage(
        self, tenant_id: str, kg: str, primary_type: str
    ) -> list[GraphRecord]:
        """Property-key coverage for entities of ``primary_type`` (P-A1a)."""
        matched = self._entity_ids_via_instance_of(tenant_id, kg, [primary_type])
        counts: dict[str, int] = {}
        for eid in matched:
            row = self._entities.get((tenant_id, kg, eid))
            if row is None:
                continue
            # Mirror Neo4j keys(e): system fields + props map keys with values.
            keys: set[str] = set()
            for k in ("id", "tenant_id", "kg", "primary_type", "name", "source"):
                if getattr(row, k, None) is not None:
                    keys.add(k)
            for k, v in row.props.items():
                if v is not None:
                    keys.add(str(k))
            for k in keys:
                counts[k] = counts.get(k, 0) + 1
        return [
            GraphRecord(data={"attr": attr, "n": n})
            for attr, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
        ]

    def _entity_type_rel_coverage(
        self, tenant_id: str, kg: str, primary_type: str
    ) -> list[GraphRecord]:
        """Outgoing relationship coverage for entities of ``primary_type`` (P-A1a)."""
        matched = self._entity_ids_via_instance_of(tenant_id, kg, [primary_type])
        entity_sets: dict[str, set[str]] = {}
        edge_totals: dict[str, int] = {}
        targets: dict[str, str | None] = {}
        for r in self._rels.values():
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if r.start_id not in matched:
                continue
            attr = r.attr or r.rel_type
            if not attr:
                continue
            entity_sets.setdefault(attr, set()).add(r.start_id)
            edge_totals[attr] = edge_totals.get(attr, 0) + 1
            if attr not in targets:
                other = self._entities.get((tenant_id, kg, r.end_id))
                targets[attr] = other.primary_type if other else None
        out: list[GraphRecord] = []
        for attr in sorted(
            entity_sets.keys(),
            key=lambda a: (-len(entity_sets[a]), a),
        ):
            out.append(
                GraphRecord(
                    data={
                        "attr": attr,
                        "n": len(entity_sets[attr]),
                        "rel_total": edge_totals.get(attr, 0),
                        "target_type": targets.get(attr),
                    }
                )
            )
        return out

    def _entity_prop_value(self, row: _EntityRow, prop_key: str) -> Any:
        """Read a property the way Neo4j ``e[$prop_key]`` would for Entity."""
        if prop_key in ("id", "tenant_id", "kg", "primary_type", "name", "source"):
            return getattr(row, prop_key, None)
        return row.props.get(prop_key)

    def _entity_literal_grep(
        self,
        tenant_id: str,
        kg: str,
        needle: str,
        *,
        case_sensitive: bool,
        type_name: str | None,
        predicate_leaf: str | None,
        limit: int,
    ) -> list[GraphRecord]:
        """Substring scan over Entity name + props (grep dual-backend)."""
        from infona_client.graph.facts import (
            RESERVED_ENTITY_PROPERTY_KEYS,
            is_internal_property_key,
        )

        skip = set(RESERVED_ENTITY_PROPERTY_KEYS) | {
            "labels",
            "props",
            "created_at",
            "updated_at",
            "elementId",
        }
        # ``name`` is greppable (display label) even though reserved for writes.
        skip.discard("name")

        needle_cmp = needle if case_sensitive else needle.lower()
        type_filter = (type_name or "").strip() or None
        pred_filter = (predicate_leaf or "").strip() or None
        if type_filter:
            matched_ids = set(
                self._entity_ids_via_instance_of(tenant_id, kg, [type_filter])
            )
        else:
            matched_ids = None

        out: list[GraphRecord] = []
        for (t, k, eid), row in sorted(self._entities.items(), key=lambda x: x[0][2]):
            if t != tenant_id or k != kg:
                continue
            if matched_ids is not None and eid not in matched_ids:
                # Also allow primary_type denorm match when INSTANCE_OF missing.
                if row.primary_type != type_filter:
                    continue
            candidates: list[tuple[str, Any]] = []
            if row.name is not None:
                candidates.append(("name", row.name))
            for pk, pv in sorted(row.props.items()):
                if pk in skip:
                    continue
                # Internal/housekeeping keys (ER block index, ingest markers)
                # are dropped BEFORE the limit is counted, mirroring the
                # exclusion the Cypher template pushes into the scan — a page
                # must never be silently shortened by rows nobody may see.
                if is_internal_property_key(pk):
                    continue
                candidates.append((pk, pv))
            for attr, val in candidates:
                if pred_filter is not None and attr != pred_filter:
                    continue
                if val is None:
                    continue
                # Multi-value lists: any element may match (scan each).
                values: list[Any]
                if isinstance(val, (list, tuple)):
                    values = list(val)
                else:
                    values = [val]
                for item in values:
                    text = str(item)
                    hay = text if case_sensitive else text.lower()
                    if needle_cmp not in hay:
                        continue
                    out.append(
                        GraphRecord(
                            data={
                                "entity_uri": row.id,
                                "label": row.name,
                                "type": row.primary_type,
                                "attr": attr,
                                "value": text,
                            }
                        )
                    )
                    if len(out) >= limit:
                        return out
                    break  # one match per (entity, attr) is enough
        return out

    def _entity_type_prop_distinct(
        self,
        tenant_id: str,
        kg: str,
        primary_type: str,
        prop_key: str,
        limit: int,
    ) -> list[GraphRecord]:
        """Distinct stringified property values for entities of ``primary_type``."""
        matched = self._entity_ids_via_instance_of(tenant_id, kg, [primary_type])
        seen: set[str] = set()
        values: list[str] = []
        for eid in matched:
            row = self._entities.get((tenant_id, kg, eid))
            if row is None:
                continue
            raw = self._entity_prop_value(row, prop_key)
            if raw is None:
                continue
            items: list[Any]
            if isinstance(raw, (list, tuple)):
                items = list(raw)
            else:
                items = [raw]
            for item in items:
                if item is None:
                    continue
                text = str(item).strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                values.append(text)
        values.sort()
        if limit >= 0:
            values = values[:limit]
        return [GraphRecord(data={"value": v}) for v in values]

    def _entity_type_rel_target_distinct(
        self,
        tenant_id: str,
        kg: str,
        primary_type: str,
        rel_attr: str,
        limit: int,
    ) -> list[GraphRecord]:
        """Distinct related-entity names for one outgoing rel leaf."""
        matched = self._entity_ids_via_instance_of(tenant_id, kg, [primary_type])
        by_value: dict[str, str | None] = {}
        for r in self._rels.values():
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            if r.start_id not in matched:
                continue
            attr = r.attr or r.rel_type
            if attr != rel_attr and r.rel_type != rel_attr:
                continue
            other = self._entities.get((tenant_id, kg, r.end_id))
            display = None
            target_type = None
            if other is not None:
                display = other.name or other.id
                target_type = other.primary_type
            else:
                display = r.end_id
            if not display:
                continue
            key = str(display)
            if key not in by_value:
                by_value[key] = target_type
        ordered = sorted(by_value.items(), key=lambda x: x[0])
        if limit >= 0:
            ordered = ordered[:limit]
        return [
            GraphRecord(data={"value": val, "target_type": tt})
            for val, tt in ordered
        ]

    def _entity_filter_prop_eq(
        self,
        tenant_id: str,
        kg: str,
        primary_type: str,
        prop_key: str,
        prop_value: Any,
        limit: int,
    ) -> list[GraphRecord]:
        rows: list[_EntityRow] = [
            r
            for (t, k, _), r in sorted(self._entities.items(), key=lambda x: x[0][2])
            if t == tenant_id and k == kg and r.primary_type == primary_type
        ]
        out: list[GraphRecord] = []
        for r in rows:
            if self._entity_prop_value(r, prop_key) == prop_value:
                out.append(
                    GraphRecord(
                        data={
                            "id": r.id,
                            "name": r.name,
                            "primary_type": r.primary_type,
                        }
                    )
                )
            if limit >= 0 and len(out) >= limit:
                break
        return out

    def _entity_1hop_out(
        self,
        tenant_id: str,
        kg: str,
        from_type: str,
        to_type: str | None,
        rel_attr: str | None,
        limit: int,
    ) -> list[GraphRecord]:
        out: list[GraphRecord] = []
        rels = sorted(
            self._rels.values(),
            key=lambda r: (r.start_id, r.end_id, r.rel_type),
        )
        for r in rels:
            if r.tenant_id != tenant_id or r.kg != kg:
                continue
            a = self._entities.get((tenant_id, kg, r.start_id))
            b = self._entities.get((tenant_id, kg, r.end_id))
            if a is None or b is None:
                continue
            if a.primary_type != from_type:
                continue
            if to_type is not None and b.primary_type != to_type:
                continue
            if rel_attr is not None and r.attr != rel_attr and r.rel_type != rel_attr:
                continue
            out.append(
                GraphRecord(
                    data={
                        "from_id": a.id,
                        "from_name": a.name,
                        "from_type": a.primary_type,
                        "to_id": b.id,
                        "to_name": b.name,
                        "to_type": b.primary_type,
                        "rel_type": r.rel_type,
                        "attr": r.attr or r.rel_type,
                    }
                )
            )
            if limit >= 0 and len(out) >= limit:
                break
        return out

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
