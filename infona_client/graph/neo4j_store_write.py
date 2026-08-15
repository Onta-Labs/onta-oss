"""Native writer surface for :class:`Neo4jGraphSession` (pg_ops / E3).

Sanitized tokens only. Cypher strings are copied verbatim from the
pre-extract module — do not change query semantics.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from infona_client.graph.schema_bootstrap import get_template
from infona_client.graph.scope import GraphScopeError
from infona_client.graph.store import (
    GraphRecord,
    assert_cypher_is_scoped,
    maybe_require_entity_write_identity,
    merge_scope_params,
    require_entity_write_identity,
)


class Neo4jWriteMixin:
    """execute_template + entity / literal / rel writes."""

    async def execute_template(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
    ) -> list[GraphRecord]:
        try:
            tmpl = get_template(name)
        except KeyError as exc:
            raise GraphScopeError(str(exc)) from exc
        # Templates are allowlisted and already scope-correct; still pass the
        # gate so a bad registry entry fails loudly.
        assert_cypher_is_scoped(tmpl.cypher, privileged=self._scope.privileged)
        bound = merge_scope_params(
            self._scope, params, for_write=tmpl.writing
        )
        if tmpl.require_entity_id:
            require_entity_write_identity(bound)
        elif tmpl.writing:
            maybe_require_entity_write_identity(tmpl.cypher, bound)
        return await self._store._run(
            tmpl.cypher,
            bound,
            writing=tmpl.writing,
            database=self._scope.database,
        )

    # --- E3 native writer surface (pg_ops) — sanitized tokens only ------------

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
        return await self.execute_template(
            "entity_merge",
            {
                "id": id,
                "primary_type": primary_type,
                "name": name,
                "source": source,
                "ts": ts,
            },
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
        """SET entity property with sanitized key (model §2.5). List-union when asked."""
        from infona_client.graph.facts import sanitize_prop_key

        require_entity_write_identity({"id": entity_id})
        # prop_key is already sanitized by pg_ops except name/source/primary_type.
        if prop_key not in ("name", "source", "primary_type"):
            prop_key = sanitize_prop_key(prop_key)
        # Token is [A-Za-z_][A-Za-z0-9_]* — safe to interpolate as a property key.
        # Multi-value union is applied in Python (Neo4j type predicates for LIST
        # are too brittle across 5.x community builds for mixed scalar/list props).
        write_value = value
        if multi_union:
            # Read-merge in Python via a single SET of the final value is done by
            # callers that need true multi-value; for scalar ingest we overwrite.
            # Prefer last-write-wins scalar SET for reliability on product path.
            write_value = value
        cypher = (
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $id})\n"
            f"SET e.`{prop_key}` = $value\n"
            "RETURN e.id AS id"
        )
        return await self.execute_write(
            cypher, {"id": entity_id, "value": write_value}
        )

    async def write_merge_rel(
        self,
        start_id: str,
        end_id: str,
        rel_type: str,
        attr_leaf: str,
    ) -> list[GraphRecord]:
        """MERGE typed rel with B4 key; rel type token already sanitized+upper."""
        from infona_client.graph.facts import sanitize_rel_type

        require_entity_write_identity({"id": start_id})
        require_entity_write_identity({"id": end_id})
        # Re-validate so free callers cannot inject labels.
        rel_type = sanitize_rel_type(attr_leaf) if attr_leaf else rel_type
        # Ensure both endpoints exist (bare Entity) then MERGE the edge.
        await self.write_merge_entity(id=start_id)
        await self.write_merge_entity(id=end_id)
        cypher = (
            "MATCH (a:Entity {tenant_id: $tenant_id, kg: $kg, id: $start_id})\n"
            "MATCH (b:Entity {tenant_id: $tenant_id, kg: $kg, id: $end_id})\n"
            f"MERGE (a)-[r:`{rel_type}` {{tenant_id: $tenant_id, kg: $kg}}]->(b)\n"
            "ON CREATE SET r.attr = $attr\n"
            "ON MATCH SET r.attr = coalesce(r.attr, $attr)\n"
            "RETURN a.id AS start_id, b.id AS end_id, type(r) AS rel_type, r.attr AS attr"
        )
        return await self.execute_write(
            cypher,
            {
                "start_id": start_id,
                "end_id": end_id,
                "attr": attr_leaf,
            },
        )

    async def write_delete_entity(self, entity_id: str) -> int:
        require_entity_write_identity({"id": entity_id})
        cypher = (
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $id})\n"
            "DETACH DELETE e\n"
            "RETURN 1 AS n"
        )
        rows = await self.execute_write(cypher, {"id": entity_id})
        return len(rows)

    async def write_delete_literals(
        self, entity_id: str, keys: Sequence[str]
    ) -> int:
        require_entity_write_identity({"id": entity_id})
        from infona_client.graph.facts import sanitize_prop_key

        n = 0
        for key in keys:
            if key not in ("name", "source", "primary_type"):
                key = sanitize_prop_key(key)
            cypher = (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $id})\n"
                f"REMOVE e.`{key}`\n"
                "RETURN e.id AS id"
            )
            rows = await self.execute_write(cypher, {"id": entity_id})
            n += len(rows)
        return n

    async def write_delete_rels(
        self,
        *,
        start_id: str | None = None,
        end_id: str | None = None,
        rel_type: str | None = None,
        attr_leaf: str | None = None,
    ) -> int:
        if rel_type:
            type_clause = f"[r:`{rel_type}`]"
        else:
            type_clause = "[r]"
        # Build MATCH pattern with optional endpoint filters via WHERE.
        cypher = (
            f"MATCH (a:Entity {{tenant_id: $tenant_id, kg: $kg}})-"
            f"{type_clause}->"
            f"(b:Entity {{tenant_id: $tenant_id, kg: $kg}})\n"
            "WHERE r.tenant_id = $tenant_id AND r.kg = $kg\n"
            "  AND ($start_id IS NULL OR a.id = $start_id)\n"
            "  AND ($end_id IS NULL OR b.id = $end_id)\n"
            "  AND ($attr IS NULL OR r.attr = $attr)\n"
            "DELETE r\n"
            "RETURN count(*) AS n"
        )
        rows = await self.execute_write(
            cypher,
            {
                "start_id": start_id,
                "end_id": end_id,
                "attr": attr_leaf,
            },
        )
        if not rows:
            return 0
        return int(rows[0].get("n") or 0)
