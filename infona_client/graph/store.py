"""GraphStore protocol — Neo4j-era storage seam (ADR 0012 §2.2).

Replaces the SPARQL client surface for instance + catalog work once the
cutover lands. This module defines:

* result / error types (records/dicts, **not** SPARQL JSON)
* the :class:`GraphStore` / :class:`GraphSession` protocols
* shared scope-enforcement helpers used by every implementation
* the process-level factory hook :func:`get_graph_store`

**No ``neo4j`` import here** — the driver adapter lives in
``neo4j_store.py`` so hermetic tests and the in-memory fake stay free of the
optional dependency.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import (
    Any,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from infona_client.graph.scope import (
    GLOBAL_TENANT_ID,
    GraphScope,
    GraphScopeError,
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphRecord:
    """One result row from a Cypher query.

    Keys are the RETURN aliases; values are Python-native (scalars, lists,
    dicts for node/rel projections). Never SPARQL-style ``{type, value}``
    bindings.
    """

    data: Mapping[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def keys(self):
        return self.data.keys()

    def __contains__(self, key: object) -> bool:
        return key in self.data

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

# Cap on how much of a store error body we surface (mirrors NeptuneClient).
_MAX_ERROR_DETAIL_CHARS = 600

# Scrub endpoint-shaped strings so host / credentials never reach NL retry
# prompts or user-facing logs. Covers bolt/neo4j URIs, host:port, and Aura hosts.
_SCRUB_RES = (
    re.compile(r"(?:bolt|neo4j|https?)://\S+", re.IGNORECASE),
    re.compile(r"\b(?:[\w-]+\.)+[\w-]+:\d+\b"),
    re.compile(r"\b(?:[\w-]+\.)+(?:databases\.neo4j\.io|neo4j\.io)\b", re.IGNORECASE),
    re.compile(r"(?i)\bpassword\s*[=:]\s*\S+"),
)


def scrub_store_detail(detail: str) -> str:
    """Host-/secret-scrubbed, length-capped diagnostic for NL retry / logs."""
    text = (detail or "").strip()
    for scrub in _SCRUB_RES:
        text = scrub.sub("[endpoint]", text)
    if len(text) > _MAX_ERROR_DETAIL_CHARS:
        text = text[:_MAX_ERROR_DETAIL_CHARS] + "…(truncated)"
    return text or "unknown store error"


class GraphStoreError(RuntimeError):
    """Base for GraphStore failures."""


class GraphQueryError(GraphStoreError):
    """A Cypher request failed at the store.

    Carries a scrubbed diagnostic so an NL→Cypher retry loop can feed
    ``str(err)`` back to the generator without leaking hosts or passwords
    (same spirit as :class:`infona_client.graph.client.SparqlQueryError`).
    """

    def __init__(self, detail: str, *, code: str | None = None):
        self.detail = scrub_store_detail(detail)
        self.code = code
        suffix = f" ({code})" if code else ""
        super().__init__(f"Graph query failed{suffix}: {self.detail}")


class GraphConfigError(GraphStoreError):
    """Missing or invalid store configuration (env / credentials)."""


# ---------------------------------------------------------------------------
# Scope enforcement helpers (shared by Neo4j + in-memory impls)
# ---------------------------------------------------------------------------

# Cypher must mention these parameters so MATCH/MERGE patterns cannot silently
# omit isolation. Word-boundary match: `$kg` must not match inside `$kg_name`.
_SCOPE_PARAM_NAMES = ("tenant_id", "kg")
_SCOPE_PARAM_RES: dict[str, re.Pattern[str]] = {
    name: re.compile(rf"\${name}\b") for name in _SCOPE_PARAM_NAMES
}

# Graph-touching clauses that should carry map-form isolation property keys.
_GRAPH_CLAUSE_RE = re.compile(r"\b(?:MATCH|MERGE|CREATE)\b", re.IGNORECASE)
# Property-key form in node/rel maps: `tenant_id: $tenant_id` / `kg: $kg`.
_TENANT_PROP_KEY_RE = re.compile(r"\btenant_id\s*:")
_KG_PROP_KEY_RE = re.compile(r"\bkg\s*:")
# Entity write identity: MERGE/CREATE of Entity that binds `$id`.
_ENTITY_WRITE_RE = re.compile(r"\b(?:MERGE|CREATE)\b", re.IGNORECASE)
_ENTITY_LABEL_RE = re.compile(r"\bEntity\b")
_ID_PARAM_RE = re.compile(r"\$id\b")


def cypher_has_scope_param(cypher: str, name: str) -> bool:
    """True if ``cypher`` references ``$name`` as a whole parameter token."""
    pattern = _SCOPE_PARAM_RES.get(name)
    if pattern is None:
        pattern = re.compile(rf"\${re.escape(name)}\b")
    return bool(pattern.search(cypher))


def assert_cypher_is_scoped(cypher: str, *, privileged: bool = False) -> None:
    """Reject Cypher that is not isolation-scoped (model §3.3 T1).

    Always required
      * non-empty string
      * word-boundary ``$tenant_id`` and ``$kg`` parameters (so ``$kg_name``
        does **not** satisfy the ``$kg`` check)

    Non-privileged sessions (app path) additionally require that any
    ``MATCH`` / ``MERGE`` / ``CREATE`` clause appears together with map-style
    property keys ``tenant_id:`` and ``kg:`` in the query text. This is a
    **heuristic**, not a Cypher rewriter: a query can still mention those
    keys and never bind the session params into the pattern. Prefer
    :meth:`GraphSession.execute_template` for application writers.

    Privileged sessions keep free-form Cypher for admin/bootstrap/tests after
    the parameter-token check only — document the residual risk in
    ``docs/neo4j-local.md``.
    """
    if not isinstance(cypher, str) or not cypher.strip():
        raise GraphScopeError("Cypher query must be a non-empty string")
    missing = [
        name for name in _SCOPE_PARAM_NAMES if not cypher_has_scope_param(cypher, name)
    ]
    if missing:
        raise GraphScopeError(
            "Cypher must reference $tenant_id and $kg parameters so every "
            f"pattern is isolation-scoped; missing: {', '.join('$' + m for m in missing)}"
        )
    if privileged:
        return
    if _GRAPH_CLAUSE_RE.search(cypher):
        if not _TENANT_PROP_KEY_RE.search(cypher) or not _KG_PROP_KEY_RE.search(cypher):
            raise GraphScopeError(
                "Non-privileged Cypher with MATCH/MERGE/CREATE must include "
                "tenant_id: and kg: property keys in the pattern (map form). "
                "Prefer execute_template() for application writers; free-form "
                "execute_read/execute_write is not isolation-complete"
            )


def maybe_require_entity_write_identity(cypher: str, params: Mapping[str, Any]) -> None:
    """Fail closed on Entity MERGE/CREATE that bind ``$id`` without a value.

    Detects writes that mention ``Entity`` and the ``$id`` parameter token.
    Templates with ``require_entity_id`` also call
    :func:`require_entity_write_identity` directly.
    """
    if not _ENTITY_WRITE_RE.search(cypher):
        return
    if not _ENTITY_LABEL_RE.search(cypher):
        return
    if not _ID_PARAM_RE.search(cypher):
        return
    require_entity_write_identity(params)


def merge_scope_params(
    scope: GraphScope,
    params: Mapping[str, Any] | None,
    *,
    for_write: bool,
) -> dict[str, Any]:
    """Build the parameter map for a scoped call.

    * Session scope **overwrites** any caller-supplied ``tenant_id`` / ``kg``
      (model §3.3 T2 — never trust model- or client-supplied scope).
    * Global-catalog **writes** require ``scope.privileged`` (T7).
    * Empty / missing kg is already impossible: :class:`GraphScope` validates.
    """
    if for_write and not scope.allows_global_write():
        raise GraphScopeError(
            "Writes to the global catalog (tenant_id=__global__) require a "
            "privileged GraphScope; app sessions cannot set __global__"
        )
    out: dict[str, Any] = dict(params or {})
    # Force scope last so callers cannot smuggle a different workspace.
    out.update(scope.as_params())
    return out


def require_entity_write_identity(params: Mapping[str, Any]) -> None:
    """Application-level existence check for Entity creates (model §7.3 / G7).

    Uniqueness constraints need the properties present; this fails closed with
    a clear error before the driver round-trip when ``id`` is missing.
    ``tenant_id`` / ``kg`` are already forced by :func:`merge_scope_params`.
    """
    entity_id = params.get("id")
    if entity_id is None or (isinstance(entity_id, str) and not entity_id.strip()):
        raise GraphScopeError(
            "Entity writes require a non-empty id parameter "
            "(stable entity_uri string from entity_uri())"
        )


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class GraphSession(Protocol):
    """Request-scoped unit of work bound to an immutable :class:`GraphScope`.

    All reads/writes inject and overwrite ``$tenant_id`` / ``$kg``. The
    session must not expose a mutable "current graph" field (model §3.3 T6).

    **Isolation note (Wave 1):** free-form :meth:`execute_read` /
    :meth:`execute_write` apply heuristic scope gates only — they are **not**
    a full Cypher rewriter. Application writers **must** use
    :meth:`execute_template` (allowlisted Cypher) or the future ``kg_writer``
    port. See ``docs/neo4j-local.md``.
    """

    @property
    def scope(self) -> GraphScope:
        """The immutable scope this session was opened with."""
        ...

    async def execute_read(
        self,
        cypher: str,
        params: Mapping[str, Any] | None = None,
    ) -> list[GraphRecord]:
        """Run a read (auto-commit or read transaction). Parameterized only.

        Free-form path for admin/bootstrap/tests. Prefer :meth:`execute_template`.
        """
        ...

    async def execute_write(
        self,
        cypher: str,
        params: Mapping[str, Any] | None = None,
    ) -> list[GraphRecord]:
        """Run a write (auto-commit or write transaction). Parameterized only.

        Free-form path for admin/bootstrap/tests. Prefer :meth:`execute_template`.
        Entity MERGE/CREATE with ``$id`` fails closed if ``id`` is missing.
        """
        ...

    async def execute_template(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
    ) -> list[GraphRecord]:
        """Run allowlisted Cypher from :data:`schema_bootstrap.TEMPLATES`.

        Safe path for application code: only registered templates execute;
        entity-write templates enforce :func:`require_entity_write_identity`.
        """
        ...


@runtime_checkable
class GraphStore(Protocol):
    """Process-level graph connection (pool). Open sessions per request/scope."""

    async def health(self) -> bool:
        """True if the store answers a trivial connectivity check."""
        ...

    def session(self, scope: GraphScope) -> GraphSession:
        """Open a scoped session. Scope is fixed for the session lifetime."""
        ...

    async def bootstrap_schema(self) -> Sequence[str]:
        """Idempotent constraints + indexes from the property-graph model §7.

        Returns the statement names applied (or already present). Safe to call
        on every process start / test setup. **Must run before instance writes**
        that rely on uniqueness (G7).
        """
        ...

    async def close(self) -> None:
        """Release the underlying pool / driver."""
        ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_store_singleton: GraphStore | None = None


def reset_graph_store_for_tests() -> None:
    """Drop the process singleton (test isolation only)."""
    global _store_singleton
    _store_singleton = None


def configure_graph_store(store: GraphStore | None) -> None:
    """Install (or clear) the process-level store used by :func:`get_graph_store`."""
    global _store_singleton
    _store_singleton = store


def get_graph_store() -> GraphStore:
    """Return the process GraphStore, constructing a Neo4j one from env if needed.

    Env (BYOK — user supplies credentials; no platform keys):

    * ``NEO4J_URI`` — bolt/neo4j URI (required to auto-construct)
    * ``NEO4J_USER`` — default ``neo4j``
    * ``NEO4J_PASSWORD`` — required with URI
    * ``NEO4J_DATABASE`` — optional database name

    Raises :class:`GraphConfigError` when no store was configured and env is
    incomplete. Callers that want an explicit in-memory store for tests should
    :func:`configure_graph_store` first.
    """
    global _store_singleton
    if _store_singleton is not None:
        return _store_singleton

    uri = (os.environ.get("NEO4J_URI") or "").strip()
    if not uri:
        raise GraphConfigError(
            "No GraphStore configured: set NEO4J_URI (+ NEO4J_PASSWORD) or "
            "call configure_graph_store(...) for tests"
        )
    password = os.environ.get("NEO4J_PASSWORD")
    if password is None or password == "":
        raise GraphConfigError(
            "NEO4J_PASSWORD is required when NEO4J_URI is set (BYOK)"
        )
    user = (os.environ.get("NEO4J_USER") or "neo4j").strip() or "neo4j"
    database = (os.environ.get("NEO4J_DATABASE") or "").strip() or None

    # Lazy import so installing the OSS package without the neo4j extra still
    # loads this module (memory store / protocol-only use).
    from infona_client.graph.neo4j_store import Neo4jGraphStore

    _store_singleton = Neo4jGraphStore(
        uri=uri,
        user=user,
        password=password,
        database=database,
    )
    return _store_singleton


def env_neo4j_configured() -> bool:
    """True when NEO4J_URI and NEO4J_PASSWORD are present (integration tests)."""
    return bool(
        (os.environ.get("NEO4J_URI") or "").strip()
        and (os.environ.get("NEO4J_PASSWORD") or "") != ""
    )


def graph_backend() -> str:
    """Active instance backend: ``neptune`` (default) or ``neo4j``.

    Same switch as :func:`infona_client.graph.kg_writer.graph_backend` — kept
    here so write rails can resolve an optional store without importing
    ``kg_writer`` (avoids circular imports at rail module load).
    """
    return (os.environ.get("INFONA_GRAPH_BACKEND") or "neptune").strip().lower()


def get_optional_graph_store() -> GraphStore | None:
    """Return the process GraphStore when the neo4j backend is active, else None.

    **Neptune default (no env / ``neptune``):** returns ``None`` so callers keep
    the SPARQL path and never require ``NEO4J_*`` credentials.

    **``INFONA_GRAPH_BACKEND=neo4j``:** delegates to :func:`get_graph_store`
    (process singleton from :func:`configure_graph_store`, or BYOK Neo4j from
    env). Raises :class:`GraphConfigError` if neither is configured — fail
    closed rather than silently writing to Neptune under a neo4j flag.

    Write rails (ingest, enrichment, normalization, ER) call this once per
    write batch and pass the result as ``store=`` into
    :func:`infona_client.graph.kg_writer.insert_facts` /
    ``delete_facts`` / ``rewrite_subject``. Prefer the alias
    :func:`resolve_optional_graph_store` at those call sites.
    """
    if graph_backend() != "neo4j":
        return None
    return get_graph_store()


# Preferred name at write-site call sites (E7 — wire rails to Neo4j write path).
resolve_optional_graph_store = get_optional_graph_store


# Re-export scope symbols that callers commonly need from one place.
__all__ = [
    "GLOBAL_TENANT_ID",
    "GraphConfigError",
    "GraphQueryError",
    "GraphRecord",
    "GraphScope",
    "GraphScopeError",
    "GraphSession",
    "GraphStore",
    "GraphStoreError",
    "assert_cypher_is_scoped",
    "configure_graph_store",
    "cypher_has_scope_param",
    "env_neo4j_configured",
    "get_graph_store",
    "get_optional_graph_store",
    "graph_backend",
    "maybe_require_entity_write_identity",
    "merge_scope_params",
    "require_entity_write_identity",
    "resolve_optional_graph_store",
    "reset_graph_store_for_tests",
    "scrub_store_detail",
]
