"""In-memory :class:`GraphStore` for hermetic unit tests.

Implements the same scope-enforcement surface as :class:`Neo4jGraphStore` so
isolation tests do not need a live database. Supports:

* Entity MERGE / MATCH by ``(tenant_id, kg, id)`` (templates)
* Entity list filtered by ``primary_type``
* Domain-label SET via :func:`cograph_client.graph.labels.set_entity_type_labels`
* Native writer methods used by :mod:`cograph_client.graph.pg_ops` (E3):
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

from cograph_client.graph.rdfs_helpers import (
    ENTITIES_OF_TYPE_COUNT_CYPHER,
    ENTITIES_OF_TYPE_CYPHER,
    LITERAL_VALUES_CYPHER,
    RELATED_ENTITIES_CYPHER,
    SUBCLASS_OF_CLOSURE_CYPHER,
)
from cograph_client.graph.schema_bootstrap import (
    ENTITY_1HOP_OUT_CYPHER,
    ENTITY_COUNT_BY_PRIMARY_TYPE_CYPHER,
    ENTITY_COUNT_BY_TYPE_CYPHER,
    ENTITY_COUNT_TOTAL_CYPHER,
    ENTITY_DETAIL_CYPHER,
    ENTITY_FILTER_PROP_EQ_CYPHER,
    ENTITY_GET_CYPHER,
    ENTITY_LIST_BY_TYPE_CYPHER,
    ENTITY_LIST_BY_TYPE_PAGE_CYPHER,
    ENTITY_MERGE_CYPHER,
    ENTITY_RELS_CYPHER,
    ONTO_ATTR_LIST_CYPHER,
    ONTO_ATTR_RANGE_TYPE_CYPHER,
    ONTO_ATTR_UPSERT_CYPHER,
    ONTO_SUBCLASS_CLEAR_CYPHER,
    ONTO_SUBCLASS_SET_CYPHER,
    ONTO_TYPE_GET_CYPHER,
    ONTO_TYPE_LIST_CYPHER,
    ONTO_TYPE_UPSERT_CYPHER,
    bootstrap_schema_statements,
    get_template,
)
from cograph_client.graph.scope import GraphScope, GraphScopeError
from cograph_client.graph.store import (
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
_FILTER_PROP_EQ_NORM = _norm_cypher(ENTITY_FILTER_PROP_EQ_CYPHER)
_HOP_OUT_NORM = _norm_cypher(ENTITY_1HOP_OUT_CYPHER)
_ENTITIES_OF_TYPE_NORM = _norm_cypher(ENTITIES_OF_TYPE_CYPHER)
_ENTITIES_OF_TYPE_COUNT_NORM = _norm_cypher(ENTITIES_OF_TYPE_COUNT_CYPHER)
_LITERAL_VALUES_NORM = _norm_cypher(LITERAL_VALUES_CYPHER)
_RELATED_ENTITIES_NORM = _norm_cypher(RELATED_ENTITIES_CYPHER)
_SUBCLASS_OF_CLOSURE_NORM = _norm_cypher(SUBCLASS_OF_CLOSURE_CYPHER)
_ONTO_TYPE_UPSERT_NORM = _norm_cypher(ONTO_TYPE_UPSERT_CYPHER)
_ONTO_SUBCLASS_SET_NORM = _norm_cypher(ONTO_SUBCLASS_SET_CYPHER)
_ONTO_SUBCLASS_CLEAR_NORM = _norm_cypher(ONTO_SUBCLASS_CLEAR_CYPHER)
_ONTO_TYPE_LIST_NORM = _norm_cypher(ONTO_TYPE_LIST_CYPHER)
_ONTO_TYPE_GET_NORM = _norm_cypher(ONTO_TYPE_GET_CYPHER)
_ONTO_ATTR_UPSERT_NORM = _norm_cypher(ONTO_ATTR_UPSERT_CYPHER)
_ONTO_ATTR_RANGE_NORM = _norm_cypher(ONTO_ATTR_RANGE_TYPE_CYPHER)
_ONTO_ATTR_LIST_NORM = _norm_cypher(ONTO_ATTR_LIST_CYPHER)
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
    # ABOUT is implied: event is always about subject_id when entity exists.


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
    label_token: str | None = None
    uri: str | None = None
    parent_type: str | None = None

    def as_record(self) -> GraphRecord:
        return GraphRecord(
            data={
                "name": self.name,
                "layer": self.layer,
                "description": self.description,
                "label_token": self.label_token,
                "uri": self.uri,
                "parent_type": self.parent_type,
                "tenant_id": self.tenant_id,
                "kg": self.kg,
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
    prop_key: str | None = None

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
                "prop_key": self.prop_key,
                "layer": self.layer,
                "tenant_id": self.tenant_id,
                "kg": self.kg,
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
        """Native path for :func:`cograph_client.graph.labels.set_entity_type_labels`."""
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
            )
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
        self._citations.clear()
        self._classes.clear()
        self._properties.clear()
        self._assertions.clear()
        self._subclass_of.clear()
        self._subproperty_of.clear()
        self._instance_of.clear()
        self._onto_types.clear()
        self._onto_attrs.clear()
        self._bootstrapped.clear()

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
        drop: list[tuple[str, str, str]] = []
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
        for key in drop:
            del self._assertions[key]
            # Drop INSTANCE_OF when type assertion removed (best-effort)
        return len(drop)

    def _subclass_closure(
        self, tenant_id: str, kg: str, class_id: str
    ) -> list[str]:
        # Build child→parent for this scope, then descendants of class_id.
        child_to_parent: dict[str, str] = {}
        for (t, k, child), parent in self._subclass_of.items():
            if t == tenant_id and k == kg:
                child_to_parent[child] = parent
        from cograph_client.graph.rdfs_helpers import descendants_of

        return descendants_of(class_id, child_to_parent)

    def _subproperty_closure(
        self, tenant_id: str, kg: str, prop_id: str
    ) -> list[str]:
        child_to_parent: dict[str, str] = {}
        for (t, k, child), parent in self._subproperty_of.items():
            if t == tenant_id and k == kg:
                child_to_parent[child] = parent
        from cograph_client.graph.rdfs_helpers import descendants_of

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

    # --- Ontology catalog (E4) ----------------------------------------------

    def _upsert_onto_type(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        name: str,
        *,
        description: str = "",
        label_token: str | None = None,
        uri: str | None = None,
    ) -> list[GraphRecord]:
        key = (tenant_id, kg, layer, name)
        existing = self._onto_types.get(key)
        if existing is None:
            row = _OntoTypeRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                name=name,
                description=description or "",
                label_token=label_token,
                uri=uri,
            )
            self._onto_types[key] = row
        else:
            if description:
                existing.description = description
            if label_token is not None:
                existing.label_token = label_token
            if uri is not None:
                existing.uri = uri
            row = existing
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
            )
        child.parent_type = parent_name
        return [
            GraphRecord(data={"name": name, "parent_type": parent_name})
        ]

    def _clear_subclass(
        self, tenant_id: str, kg: str, layer: str, name: str
    ) -> list[GraphRecord]:
        child = self._onto_types.get((tenant_id, kg, layer, name))
        if child is None:
            return []
        child.parent_type = None
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
        prop_key: str | None,
        domain_label_token: str | None,
    ) -> list[GraphRecord]:
        # Ensure domain type exists (DECLARES target).
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
        existing = self._onto_attrs.get(akey)
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
                prop_key=prop_key,
            )
            self._onto_attrs[akey] = row
        else:
            existing.kind = kind
            existing.datatype = datatype
            existing.range_type = range_type
            if cardinality:
                existing.cardinality = cardinality
            if description:
                existing.description = description
            if prop_key is not None:
                existing.prop_key = prop_key
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

    def _entity_counts_by_primary_type(
        self, tenant_id: str, kg: str
    ) -> list[GraphRecord]:
        counts: dict[str, int] = {}
        for (t, k, _id), row in self._entities.items():
            if t != tenant_id or k != kg:
                continue
            if row.primary_type:
                counts[row.primary_type] = counts.get(row.primary_type, 0) + 1
        return [
            GraphRecord(data={"primary_type": pt, "n": n})
            for pt, n in sorted(counts.items())
        ]

    def _list_entities_by_type_page(
        self,
        tenant_id: str,
        kg: str,
        primary_type: str,
        after_id: str | None,
        limit: int,
    ) -> list[GraphRecord]:
        rows = [
            r
            for (t, k, _), r in sorted(self._entities.items(), key=lambda x: x[0][2])
            if t == tenant_id and k == kg and r.primary_type == primary_type
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
        n = sum(
            1
            for (t, k, _), r in self._entities.items()
            if t == tenant_id and k == kg and r.primary_type == primary_type
        )
        return [GraphRecord(data={"n": n})]

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
        from cograph_client.graph.ontology_queries import type_uri

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
        from cograph_client.graph.assertion_model import property_uri

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
            if a.literal_value != prop_value:
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
            if actual != prop_value:
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
        # Build child→parent from OntoType rows, then expand descendants.
        child_to_parent: dict[str, str] = {}
        for (t, k, lyr, name), row in self._onto_types.items():
            if t != tenant_id or k != kg:
                continue
            if layer is not None and lyr != layer:
                continue
            parent = getattr(row, "parent_type", None) or getattr(row, "parent", None)
            if parent:
                child_to_parent[name] = str(parent)
        from cograph_client.graph.rdfs_helpers import descendants_of

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

    def _entity_prop_value(self, row: _EntityRow, prop_key: str) -> Any:
        """Read a property the way Neo4j ``e[$prop_key]`` would for Entity."""
        if prop_key in ("id", "tenant_id", "kg", "primary_type", "name", "source"):
            return getattr(row, prop_key, None)
        return row.props.get(prop_key)

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
            rows = [
                r.as_record()
                for (t, k, _), r in sorted(self._entities.items(), key=lambda x: x[0][2])
                if t == tenant_id and k == kg and r.primary_type == primary_type
            ]
            return rows

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
                prop_key=params.get("prop_key"),
                domain_label_token=params.get("domain_label_token"),
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

        if norm == _ONTO_ATTR_LIST_NORM:
            return self._list_onto_attrs(
                tenant_id, kg, params.get("domain"), params.get("layer")
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
