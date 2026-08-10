"""In-memory :class:`GraphStore` for hermetic unit tests.

Implements the same scope-enforcement surface as :class:`Neo4jGraphStore` so
isolation tests do not need a live database. Supports a **small** Cypher
subset sufficient for smoke helpers in ``schema_bootstrap``:

* Entity MERGE / MATCH by ``(tenant_id, kg, id)``
* Entity list filtered by ``primary_type``
* Domain-label SET via :func:`cograph_client.graph.labels.set_entity_type_labels`

Anything outside that subset raises :class:`GraphQueryError` — tests that need
richer behavior should either extend this deliberately or use the Neo4j
integration marker.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from cograph_client.graph.schema_bootstrap import (
    ENTITY_GET_CYPHER,
    ENTITY_LIST_BY_TYPE_CYPHER,
    ENTITY_MERGE_CYPHER,
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
        return GraphRecord(
            data={
                "id": self.id,
                "tenant_id": self.tenant_id,
                "kg": self.kg,
                "primary_type": self.primary_type,
                "name": self.name,
                "source": self.source,
            }
        )


class MemoryGraphSession:
    def __init__(self, store: "MemoryGraphStore", scope: GraphScope) -> None:
        self._store = store
        self._scope = scope

    @property
    def scope(self) -> GraphScope:
        return self._scope

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


class MemoryGraphStore:
    """Process-local fake store; not safe for concurrent multi-process use."""

    def __init__(self) -> None:
        # key: (tenant_id, kg, id)
        self._entities: dict[tuple[str, str, str], _EntityRow] = {}
        self._bootstrapped: list[str] = []

    def session(self, scope: GraphScope) -> GraphSession:
        if not isinstance(scope, GraphScope):
            raise GraphScopeError("session() requires a GraphScope instance")
        return MemoryGraphSession(self, scope)

    async def health(self) -> bool:
        return True

    async def bootstrap_schema(self) -> Sequence[str]:
        # Mirror Neo4j IF NOT EXISTS: record statement names once.
        if not self._bootstrapped:
            self._bootstrapped = [name for name, _ in bootstrap_schema_statements()]
        return list(self._bootstrapped)

    async def close(self) -> None:
        self._entities.clear()
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

    def snapshot_entities(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(row.as_record().to_dict()) for row in self._entities.values()]

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
            # Identity already enforced by session for free-form/template paths;
            # keep a local fail-closed for direct _execute callers in tests.
            require_entity_write_identity(params)
            entity_id = params.get("id")
            key = (tenant_id, kg, str(entity_id))
            existing = self._entities.get(key)
            if existing is None:
                row = _EntityRow(
                    tenant_id=tenant_id,
                    kg=kg,
                    id=str(entity_id),
                    primary_type=params.get("primary_type"),
                    name=params.get("name"),
                    source=params.get("source"),
                )
                self._entities[key] = row
            else:
                if params.get("primary_type") is not None:
                    existing.primary_type = params.get("primary_type")
                if params.get("name") is not None:
                    existing.name = params.get("name")
                if params.get("source") is not None:
                    existing.source = params.get("source")
                row = existing
            return [row.as_record()]

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

        # SET e:Label1:Label2 after MATCH Entity map (from entity_set_labels_cypher).
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

        # Allow a few diagnostic patterns used by unit tests of enforcement.
        if "RETURN $tenant_id" in cypher and "RETURN $kg" in cypher.replace(" ", ""):
            # too brittle — skip
            pass
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
            "templates in schema_bootstrap or the Neo4j integration store"
        )
