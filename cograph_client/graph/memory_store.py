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

Anything outside the smoke Cypher subset raises :class:`GraphQueryError` —
prefer the native ``write_*`` methods for instance mutations.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from cograph_client.graph.schema_bootstrap import (
    ENTITY_COUNT_BY_PRIMARY_TYPE_CYPHER,
    ENTITY_GET_CYPHER,
    ENTITY_LIST_BY_TYPE_CYPHER,
    ENTITY_MERGE_CYPHER,
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
    ts: str | None = None


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
        ts: str | None = None,
    ) -> None:
        t, k = self._scope_tk()
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
                ts=ts,
            )
        )

    async def write_get_entity(self, entity_id: str) -> Mapping[str, Any] | None:
        t, k = self._scope_tk()
        row = self._store._entities.get((t, k, str(entity_id)))
        if row is None:
            return None
        return row.as_record().to_dict()


class MemoryGraphStore:
    """Process-local fake store; not safe for concurrent multi-process use."""

    def __init__(self) -> None:
        # key: (tenant_id, kg, id)
        self._entities: dict[tuple[str, str, str], _EntityRow] = {}
        # B4 key: (tenant_id, kg, start_id, end_id, rel_type)
        self._rels: dict[tuple[str, str, str, str, str], _RelRow] = {}
        self._prov: list[_ProvRow] = []
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
        self._onto_types.clear()
        self._onto_attrs.clear()
        self._bootstrapped.clear()

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

    def _apply_domain_labels(
        self,
        tenant_id: str,
        kg: str,
        entity_id: str,
        safe_labels: Sequence[str],
    ) -> list[GraphRecord]:
        row = self._entities.get((tenant_id, kg, entity_id))
        if row is None:
            return []
        labels = ["Entity"]
        for lab in safe_labels:
            if lab not in labels:
                labels.append(lab)
        row.labels = labels
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

    def _add_prov(self, row: _ProvRow) -> None:
        self._prov.append(row)

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
