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
