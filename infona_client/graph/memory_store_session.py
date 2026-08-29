"""Scoped session over :class:`MemoryGraphStore`.

Mirrors the :class:`~infona_client.graph.store.GraphSession` surface so
isolation tests exercise the same ``write_*`` / ``execute_*`` contract
as the Neo4j store without a live database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Sequence

from infona_client.graph.memory_store_rows import (
    _CitationRow,
    _ProvRow,
    _SuppressionRow,
    _ValueHistoryRow,
)
from infona_client.graph.memory_store_validity import MemoryValiditySessionMixin
from infona_client.graph.schema_bootstrap import get_template
from infona_client.graph.scope import GraphScope, GraphScopeError
from infona_client.graph.store import (
    GraphRecord,
    assert_cypher_is_scoped,
    maybe_require_entity_write_identity,
    merge_scope_params,
    require_entity_write_identity,
)

if TYPE_CHECKING:
    from infona_client.graph.memory_store import MemoryGraphStore


class MemoryGraphSession(MemoryValiditySessionMixin):
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

    async def write_suppression(
        self,
        *,
        mark_id: str,
        kind: str,
        statement_id: str = "",
        subject: str = "",
        predicate: str = "",
        object_repr: str = "",
        reason: str = "",
        suppressed_at: str = "",
        graph_uri: str = "",
    ) -> None:
        """Persist one sticky suppression mark (ONTA-279).

        Deliberately does NOT merge an ``:Entity`` for ``subject`` (unlike
        ``write_prov_event``'s assert/rewrite branch): a mark is written for a
        value that may have just been hard-deleted, and for an ENTITY tombstone
        whose whole point is that the entity must not come back.
        """
        t, k = self._scope_tk()
        self._store._upsert_suppression(
            _SuppressionRow(
                tenant_id=t,
                kg=k,
                mark_id=mark_id,
                kind=kind,
                statement_id=statement_id,
                subject=subject,
                predicate=predicate,
                object_repr=object_repr,
                reason=reason,
                suppressed_at=suppressed_at,
                graph_uri=graph_uri,
            )
        )

    async def read_suppressions(
        self,
        *,
        kind: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
    ) -> list[dict[str, Any]]:
        """Suppression marks in THIS session's ``(tenant_id, kg)`` scope."""
        t, k = self._scope_tk()
        return self._store._list_suppressions(
            t, k, kind=kind, subject=subject, predicate=predicate
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

    async def write_fact_batch(self, batch: Any) -> int:
        """Same semantics as the Neo4j UNWIND path, via in-process natives."""
        from infona_client.graph.fact_batch import FactBatch

        if not isinstance(batch, FactBatch) or batch.n_facts == 0:
            return 0
        for row in batch.entities:
            await self.write_merge_entity(
                id=row["id"],
                primary_type=row.get("primary_type"),
                name=row.get("name"),
                source=row.get("source"),
                ts=row.get("ts"),
            )
        for row in batch.properties:
            await self.write_merge_property(
                property_id=row["id"], name=row["name"], kind=row["kind"]
            )
        for row in batch.classes:
            await self.write_merge_class(class_id=row["id"], name=row["name"])
        for row in batch.assertions:
            await self.write_assertion(
                assertion_id=row["assertion_id"],
                subject_id=row["subject_id"],
                property_id=row["property_id"],
                property_name="",
                object_id=row.get("object_id"),
                object_class_id=row.get("object_class_id"),
                literal_value=row.get("literal_value"),
                literal_datatype=row.get("literal_datatype"),
                source_url=row.get("source_url"),
                verified_at=row.get("verified_at"),
                run_id=row.get("run_id"),
                confidence=row.get("confidence"),
                provenance=row.get("provenance"),
                ts=row.get("ts"),
            )
        for eid, props in batch.entity_props.items():
            for key, value in props.items():
                await self.write_set_literal(eid, key, value, multi_union=True)
        for row in batch.instance_of:
            await self.write_instance_of(row["entity_id"], row["class_id"])
        for label, ids in batch.labels.items():
            for eid in ids:
                await self.apply_entity_domain_labels(eid, [label])
        for row in batch.rels:
            await self.write_merge_rel(
                row["start_id"], row["end_id"], row["rel_type"], row["attr"]
            )
        for row in batch.prov_events:
            await self.write_prov_event(
                event_type=row["event_type"],
                subject_id=row["subject_id"],
                attr=row.get("attr"),
                object_repr=row.get("object_repr"),
                old_id=row.get("old_id"),
                new_id=row.get("new_id"),
                reason=row.get("reason") or "",
                source=row.get("source"),
                fact_hash=row.get("fact_hash"),
                ts=row.get("ts"),
                confidence=row.get("confidence"),
            )
        return batch.n_facts

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
