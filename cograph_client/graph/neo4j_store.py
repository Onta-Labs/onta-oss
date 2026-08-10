"""Neo4j async driver implementation of :class:`GraphStore` (E2.2).

BYOK: credentials come only from constructor args / ``NEO4J_*`` env via
:func:`cograph_client.graph.store.get_graph_store`. No platform keys.

Scope enforcement (model §3):

* every read/write merges session ``tenant_id`` / ``kg`` over caller params
* Cypher must reference ``$tenant_id`` and ``$kg`` (word-boundary) or reject
* non-privileged sessions also require ``tenant_id:`` / ``kg:`` property keys
  on MATCH/MERGE/CREATE (heuristic — not a rewriter)
* global-catalog writes require a privileged scope
* application writers should use :meth:`execute_template` (allowlisted Cypher)
* free-form execute_read/write remains for admin/bootstrap/tests only

Transient transport failures on the **read** path are retried with bounded
backoff (spirit of :class:`NeptuneClient`); writes stay single-shot for
at-most-once mutation semantics until the write path gains idempotent keys.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Mapping, Sequence

import structlog

from cograph_client.graph.schema_bootstrap import SCHEMA_STATEMENTS, get_template
from cograph_client.graph.scope import GraphScope, GraphScopeError
from cograph_client.graph.store import (
    GraphConfigError,
    GraphQueryError,
    GraphRecord,
    GraphSession,
    assert_cypher_is_scoped,
    maybe_require_entity_write_identity,
    merge_scope_params,
    require_entity_write_identity,
    scrub_store_detail,
)

logger = structlog.stdlib.get_logger("cograph.graph.neo4j")

_MAX_TRANSPORT_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.5


def _import_neo4j():
    """Import the official driver; raise a clear config error if missing."""
    try:
        import neo4j
        from neo4j.exceptions import (
            AuthError,
            ClientError,
            DriverError,
            Neo4jError,
            ServiceUnavailable,
            SessionExpired,
            TransientError,
        )
    except ImportError as exc:  # pragma: no cover - env dependent
        raise GraphConfigError(
            "The neo4j package is not installed. Install the optional extra: "
            "pip install 'onta-client[neo4j]' (or pip install neo4j)"
        ) from exc
    return neo4j, {
        "AuthError": AuthError,
        "ClientError": ClientError,
        "DriverError": DriverError,
        "Neo4jError": Neo4jError,
        "ServiceUnavailable": ServiceUnavailable,
        "SessionExpired": SessionExpired,
        "TransientError": TransientError,
    }


def _node_to_plain(value: Any) -> Any:
    """Convert neo4j driver values to plain Python for :class:`GraphRecord`."""
    # Avoid importing neo4j types at module level for protocol purity of store.py;
    # isinstance checks use duck typing on common attributes.
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_node_to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _node_to_plain(v) for k, v in value.items()}
    # neo4j.graph.Node / Relationship / Path — expose as dict-ish
    labels = getattr(value, "labels", None)
    if labels is not None and hasattr(value, "items"):
        data = {k: _node_to_plain(v) for k, v in value.items()}
        data["_labels"] = list(labels)
        if hasattr(value, "element_id"):
            data["_element_id"] = value.element_id
        return data
    if hasattr(value, "type") and hasattr(value, "items") and hasattr(value, "start_node"):
        data = {k: _node_to_plain(v) for k, v in value.items()}
        data["_type"] = value.type
        return data
    # DateTime / Date / etc. — stringify for portable Wave-1 ISO preference
    iso = getattr(value, "iso_format", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            pass
    return value


class Neo4jGraphSession:
    """Scoped session over a shared async driver."""

    def __init__(
        self,
        store: "Neo4jGraphStore",
        scope: GraphScope,
    ) -> None:
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
        return await self._store._run(
            cypher,
            bound,
            writing=False,
            database=self._scope.database,
        )

    async def execute_write(
        self,
        cypher: str,
        params: Mapping[str, Any] | None = None,
    ) -> list[GraphRecord]:
        assert_cypher_is_scoped(cypher, privileged=self._scope.privileged)
        bound = merge_scope_params(self._scope, params, for_write=True)
        maybe_require_entity_write_identity(cypher, bound)
        return await self._store._run(
            cypher,
            bound,
            writing=True,
            database=self._scope.database,
        )

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
        from cograph_client.graph.facts import sanitize_prop_key

        require_entity_write_identity({"id": entity_id})
        # prop_key is already sanitized by pg_ops except name/source/primary_type.
        if prop_key not in ("name", "source", "primary_type"):
            prop_key = sanitize_prop_key(prop_key)
        # Token is [A-Za-z_][A-Za-z0-9_]* — safe to interpolate as a property key.
        if multi_union:
            cypher = (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $id})\n"
                f"WITH e, e.`{prop_key}` AS cur\n"
                "WITH e, cur, $value AS incoming\n"
                "SET e.`"
                + prop_key
                + "` = CASE\n"
                "  WHEN cur IS NULL THEN incoming\n"
                "  WHEN cur = incoming THEN cur\n"
                "  WHEN cur IS :: LIST<ANY> AND incoming IS :: LIST<ANY> THEN\n"
                "    cur + [x IN incoming WHERE NOT x IN cur]\n"
                "  WHEN cur IS :: LIST<ANY> AND NOT incoming IN cur THEN cur + [incoming]\n"
                "  WHEN cur IS :: LIST<ANY> THEN cur\n"
                "  WHEN incoming IS :: LIST<ANY> THEN\n"
                "    CASE WHEN cur IN incoming THEN incoming ELSE [cur] + incoming END\n"
                "  ELSE [cur, incoming]\n"
                "END\n"
                "RETURN e.id AS id"
            )
        else:
            cypher = (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $id})\n"
                f"SET e.`{prop_key}` = $value\n"
                "RETURN e.id AS id"
            )
        return await self.execute_write(
            cypher, {"id": entity_id, "value": value}
        )

    async def write_merge_rel(
        self,
        start_id: str,
        end_id: str,
        rel_type: str,
        attr_leaf: str,
    ) -> list[GraphRecord]:
        """MERGE typed rel with B4 key; rel type token already sanitized+upper."""
        from cograph_client.graph.facts import sanitize_rel_type

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
        from cograph_client.graph.facts import sanitize_prop_key

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

    async def write_rewrite_entity_id(self, old_id: str, new_id: str) -> None:
        """Re-key Entity ``id``; rebind incident rels + ProvEvent (ADR 0007).

        * **Free ``new_id``:** ``SET old.id = new_id`` (relationships stay on the
          same node). ``ProvEvent.subject_id`` is rewritten to match.
        * **``new_id`` already exists (ER merge):** rebind every incident
          relationship onto the survivor, re-point ``:ABOUT`` / ``subject_id``
          on ``:ProvEvent``, coalesce display props onto survivor, then
          ``DETACH DELETE`` the loser — never drop edges with the node.
        """
        require_entity_write_identity({"id": old_id})
        require_entity_write_identity({"id": new_id})
        if old_id == new_id:
            return

        old_rows = await self.execute_read(
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})\n"
            "RETURN e.id AS id",
            {"old_id": old_id},
        )
        if not old_rows:
            return

        neu_rows = await self.execute_read(
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $new_id})\n"
            "RETURN e.id AS id",
            {"new_id": new_id},
        )
        if not neu_rows:
            # Free id: re-key in place; relationships stay bound to the node.
            await self.execute_write(
                "MATCH (old:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})\n"
                "SET old.id = $new_id\n"
                "RETURN old.id AS id",
                {"old_id": old_id, "new_id": new_id},
            )
            await self._rebind_prov_subject_ids(old_id, new_id)
            return

        # Target exists — rebind endpoints onto survivor, then drop loser.
        # List outbound + inbound (self-loops only once via outbound arm).
        rel_rows = await self.execute_read(
            "MATCH (a:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})"
            "-[r]->(b:Entity {tenant_id: $tenant_id, kg: $kg})\n"
            "RETURN a.id AS start_id, b.id AS end_id, type(r) AS rel_type, "
            "coalesce(r.attr, '') AS attr\n"
            "UNION\n"
            "MATCH (a:Entity {tenant_id: $tenant_id, kg: $kg})"
            "-[r]->(b:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})\n"
            "WHERE a.id <> $old_id\n"
            "RETURN a.id AS start_id, b.id AS end_id, type(r) AS rel_type, "
            "coalesce(r.attr, '') AS attr",
            {"old_id": old_id},
        )
        for row in rel_rows:
            start = str(row.get("start_id") or "")
            end = str(row.get("end_id") or "")
            rel_type = str(row.get("rel_type") or "")
            attr = str(row.get("attr") or "")
            if not start or not end or not rel_type:
                continue
            new_start = new_id if start == old_id else start
            new_end = new_id if end == old_id else end
            # Prefer original attr leaf when present so sanitize_rel_type is stable.
            attr_leaf = attr if attr else rel_type.lower()
            await self.write_merge_rel(new_start, new_end, rel_type, attr_leaf)

        # Re-point :ABOUT edges and subject_id before DETACH DELETE removes them.
        await self.execute_write(
            "MATCH (p:ProvEvent {tenant_id: $tenant_id, kg: $kg})"
            "-[a:ABOUT]->(old:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})\n"
            "MATCH (neu:Entity {tenant_id: $tenant_id, kg: $kg, id: $new_id})\n"
            "SET p.subject_id = $new_id\n"
            "CREATE (p)-[:ABOUT]->(neu)\n"
            "DELETE a\n"
            "RETURN p.subject_id AS subject_id",
            {"old_id": old_id, "new_id": new_id},
        )
        await self._rebind_prov_subject_ids(old_id, new_id)

        # Survivor wins on conflict; fill gaps from loser (display props).
        await self.execute_write(
            "MATCH (old:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})\n"
            "MATCH (neu:Entity {tenant_id: $tenant_id, kg: $kg, id: $new_id})\n"
            "SET neu.primary_type = coalesce(neu.primary_type, old.primary_type),\n"
            "    neu.name = coalesce(neu.name, old.name),\n"
            "    neu.source = coalesce(neu.source, old.source)\n"
            "RETURN neu.id AS id",
            {"old_id": old_id, "new_id": new_id},
        )
        # DETACH DELETE only after rebind — edges already live on survivor.
        await self.execute_write(
            "MATCH (old:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})\n"
            "DETACH DELETE old\n"
            "RETURN $new_id AS id",
            {"old_id": old_id, "new_id": new_id},
        )

    async def _rebind_prov_subject_ids(self, old_id: str, new_id: str) -> None:
        """Rewrite ``ProvEvent.subject_id`` (and leave ABOUT for free-id path)."""
        await self.execute_write(
            "MATCH (p:ProvEvent {tenant_id: $tenant_id, kg: $kg})\n"
            "WHERE p.subject_id = $old_id\n"
            "SET p.subject_id = $new_id\n"
            "RETURN count(p) AS n",
            {"old_id": old_id, "new_id": new_id},
        )

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
        cypher = (
            "MERGE (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $subject_id})\n"
            "CREATE (p:ProvEvent {\n"
            "  tenant_id: $tenant_id, kg: $kg,\n"
            "  event_type: $event_type, subject_id: $subject_id,\n"
            "  attr: $attr, object_repr: $object_repr,\n"
            "  old_id: $old_id, new_id: $new_id,\n"
            "  reason: $reason, source: $source, ts: $ts\n"
            "})\n"
            "CREATE (p)-[:ABOUT]->(e)\n"
            "RETURN p.subject_id AS subject_id"
        )
        await self.execute_write(
            cypher,
            {
                "subject_id": subject_id,
                "event_type": event_type,
                "attr": attr,
                "object_repr": object_repr,
                "old_id": old_id,
                "new_id": new_id,
                "reason": reason,
                "source": source,
                "ts": ts,
            },
        )

    async def write_get_entity(self, entity_id: str) -> Mapping[str, Any] | None:
        rows = await self.execute_template("entity_get", {"id": entity_id})
        if not rows:
            return None
        return rows[0].to_dict()

    async def read_list_entities_by_label(
        self,
        label: str,
        *,
        after_id: str | None = None,
        limit: int = 50,
    ) -> list[GraphRecord]:
        """List entities carrying a sanitized domain label (E5 explore).

        Label tokens are re-validated via :func:`sanitize_domain_label` before
        interpolation — Neo4j cannot parameterize labels.
        """
        from cograph_client.graph.labels import sanitize_domain_label

        safe = sanitize_domain_label(label)
        cypher = (
            f"MATCH (e:Entity:`{safe}` {{tenant_id: $tenant_id, kg: $kg}})\n"
            "WHERE $after_id IS NULL OR e.id > $after_id\n"
            "RETURN e.id AS id, e.tenant_id AS tenant_id, e.kg AS kg,\n"
            "       e.primary_type AS primary_type, e.name AS name, e.source AS source\n"
            "ORDER BY e.id\n"
            "LIMIT $limit"
        )
        return await self.execute_read(
            cypher, {"after_id": after_id, "limit": int(limit)}
        )


class Neo4jGraphStore:
    """Official ``neo4j`` async driver backed :class:`GraphStore`."""

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        *,
        database: str | None = None,
        max_connection_pool_size: int = 50,
    ) -> None:
        if not uri or not str(uri).strip():
            raise GraphConfigError("NEO4J_URI / uri must be non-empty")
        if password is None:
            raise GraphConfigError("NEO4J_PASSWORD / password is required")
        neo4j, _exc = _import_neo4j()
        self._neo4j = neo4j
        self._exc_types = _exc
        self._uri = uri.strip()
        self._user = user
        self._database = database
        self._driver = neo4j.AsyncGraphDatabase.driver(
            self._uri,
            auth=(user, password),
            max_connection_pool_size=max_connection_pool_size,
        )

    @property
    def default_database(self) -> str | None:
        return self._database

    def session(self, scope: GraphScope) -> GraphSession:
        if not isinstance(scope, GraphScope):
            raise GraphScopeError("session() requires a GraphScope instance")
        # Scope may pin a database; otherwise inherit store default at run time.
        if scope.database is None and self._database is not None:
            scope = GraphScope(
                tenant_id=scope.tenant_id,
                kg=scope.kg,
                database=self._database,
                privileged=scope.privileged,
            )
        return Neo4jGraphSession(self, scope)

    async def health(self) -> bool:
        try:
            await self._driver.verify_connectivity()
            return True
        except Exception as exc:
            logger.warning(
                "neo4j_health_failed",
                error_type=type(exc).__name__,
                detail=scrub_store_detail(str(exc)),
            )
            return False

    async def bootstrap_schema(self) -> Sequence[str]:
        """Create model §7 constraints/indexes (IF NOT EXISTS)."""
        applied: list[str] = []
        database = self._database
        async with self._driver.session(database=database) as session:
            for name, cypher in SCHEMA_STATEMENTS:
                try:
                    result = await session.run(cypher)
                    await result.consume()
                    applied.append(name)
                except Exception as exc:
                    raise GraphQueryError(
                        f"bootstrap failed on {name}: {exc}",
                        code=getattr(exc, "code", None),
                    ) from exc
        logger.info("neo4j_schema_bootstrap", statements=len(applied))
        return applied

    async def close(self) -> None:
        await self._driver.close()

    async def _run(
        self,
        cypher: str,
        params: dict[str, Any],
        *,
        writing: bool,
        database: str | None,
    ) -> list[GraphRecord]:
        db = database if database is not None else self._database
        start = time.monotonic()
        last_exc: BaseException | None = None
        attempts = 1 if writing else _MAX_TRANSPORT_ATTEMPTS

        async def _unit_of_work(tx) -> list[GraphRecord]:
            result = await tx.run(cypher, params)
            records = await result.data()
            return [
                GraphRecord(data={k: _node_to_plain(v) for k, v in row.items()})
                for row in records
            ]

        for attempt in range(1, attempts + 1):
            try:
                async with self._driver.session(database=db) as session:
                    if writing:
                        rows = await session.execute_write(_unit_of_work)
                    else:
                        rows = await session.execute_read(_unit_of_work)
                duration_ms = round((time.monotonic() - start) * 1000, 1)
                logger.info(
                    "neo4j_query",
                    writing=writing,
                    duration_ms=duration_ms,
                    rows=len(rows),
                    attempt=attempt,
                )
                return rows
            except Exception as exc:
                last_exc = exc
                if self._is_auth_error(exc):
                    raise GraphConfigError(
                        scrub_store_detail(f"Neo4j authentication failed: {exc}")
                    ) from exc
                if self._is_client_error(exc) and not self._is_transient(exc):
                    code = getattr(exc, "code", None)
                    raise GraphQueryError(str(exc), code=code) from exc
                if writing or attempt >= attempts or not self._is_transient(exc):
                    break
                logger.warning(
                    "neo4j_transport_retry",
                    attempt=attempt,
                    max_attempts=attempts,
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(_RETRY_BACKOFF_S * attempt)

        assert last_exc is not None
        code = getattr(last_exc, "code", None)
        raise GraphQueryError(str(last_exc), code=code) from last_exc

    def _is_transient(self, exc: BaseException) -> bool:
        TransientError = self._exc_types["TransientError"]
        ServiceUnavailable = self._exc_types["ServiceUnavailable"]
        SessionExpired = self._exc_types["SessionExpired"]
        return isinstance(exc, (TransientError, ServiceUnavailable, SessionExpired))

    def _is_client_error(self, exc: BaseException) -> bool:
        ClientError = self._exc_types["ClientError"]
        Neo4jError = self._exc_types["Neo4jError"]
        return isinstance(exc, (ClientError, Neo4jError))

    def _is_auth_error(self, exc: BaseException) -> bool:
        AuthError = self._exc_types["AuthError"]
        return isinstance(exc, AuthError)

    def _is_driver_error(self, exc: BaseException) -> bool:
        DriverError = self._exc_types["DriverError"]
        return isinstance(exc, DriverError)
