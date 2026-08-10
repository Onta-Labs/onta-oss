"""Explore / KG-admin read path on the property-graph store (E5 partial).

Minimal GraphStore-backed readers that Explorer / KG-admin routes can call:

* :func:`list_entities_by_type` — paged instances by ``primary_type`` or domain label
* :func:`get_entity_detail` — properties + outgoing / incoming relationships
* :func:`type_counts` — per-``primary_type`` counts (type-stats input)
* :func:`count_entities` — total Entity nodes in a tenant+kg (minimal KG size)

**Dual-backend:** when an explicit ``store`` / ``session`` is passed, or
``COGRAPH_GRAPH_BACKEND=neo4j``, reads run through GraphStore templates /
native session methods. Otherwise helpers return ``None`` / raise so the
existing SPARQL explore routes remain the default until cutover.

**Out of scope (→ E9):** full ``api/routes/explore.py`` SPARQL rewrite
(aggregates, coverage %, drift history, grep fulltext, schema panel fan-out),
KgMeta registry, web UI.

Type-name path safety (ONTA-425): every public type-leaf argument goes through
:func:`require_valid_type_name` (and domain-label sanitization when matching
labels) so unsafe path segments never reach Cypher interpolation or IRI
construction.

API sketch (future route wiring — not registered here)::

    GET  /graphs/{tenant}/explore/kgs/{kg}/types/{type}/records
         → list_entities_by_type(...)   # columns/rows assembled by route
    GET  /graphs/{tenant}/explore/kgs/{kg}/entities/{id}
         → get_entity_detail(...)
    GET  /graphs/{tenant}/kgs/{kg}/type-counts
         → type_counts(...)             # dual-backend already optional
    GET  /graphs/{tenant}/kgs            # entity_count field
         → count_entities(...) per kg when registry deferred
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Mapping, Optional, Sequence

from cograph_client.graph.facts import RESERVED_ENTITY_PROPERTY_KEYS
from cograph_client.graph.labels import sanitize_domain_label
from cograph_client.graph.queries import (
    InvalidTypeName,
    require_valid_type_name,
)
from cograph_client.graph.scope import GraphScope, GraphScopeError

if TYPE_CHECKING:
    from cograph_client.graph.store import GraphSession, GraphStore

MatchMode = Literal["primary_type", "label"]

# System props that live on Entity nodes but are not ontology attributes.
_SYSTEM_PROP_KEYS: frozenset[str] = frozenset(RESERVED_ENTITY_PROPERTY_KEYS) | frozenset(
    {"labels", "props"}
)

# Default page size for entity lists (mirrors explore.get_type_records default).
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntitySummary:
    """One row for a paged entity list."""

    id: str
    tenant_id: str
    kg: str
    primary_type: str | None = None
    name: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class EntityRel:
    """One incident relationship edge on an entity."""

    attr: str
    rel_type: str
    other_id: str
    direction: Literal["out", "in"]
    other_name: str | None = None
    other_type: str | None = None


@dataclass(frozen=True, slots=True)
class EntityDetail:
    """Entity node + literal properties + incident relationships."""

    id: str
    tenant_id: str
    kg: str
    primary_type: str | None = None
    name: str | None = None
    source: str | None = None
    labels: tuple[str, ...] = ()
    properties: Mapping[str, Any] = field(default_factory=dict)
    outgoing: tuple[EntityRel, ...] = ()
    incoming: tuple[EntityRel, ...] = ()


@dataclass(frozen=True, slots=True)
class TypeCountRow:
    """One type with an instance count (Explorer type-stats / type-counts)."""

    name: str
    entity_count: int


@dataclass(frozen=True, slots=True)
class EntityPage:
    """Paged list result with keyset cursor."""

    entities: tuple[EntitySummary, ...]
    total: int
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Dual-backend session resolution
# ---------------------------------------------------------------------------


def graph_backend() -> str:
    """Same switch as :func:`cograph_client.graph.kg_writer.graph_backend`."""
    return (os.environ.get("COGRAPH_GRAPH_BACKEND") or "neptune").strip().lower()


def resolve_explore_session(
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    tenant_id: str | None = None,
    kg: str | None = None,
    kg_name: str | None = None,
) -> Optional["GraphSession"]:
    """Return an instance-scoped session when the Neo4j path should run.

    Priority: explicit ``session`` → explicit ``store`` → env ``neo4j`` backend.
    Returns ``None`` when the SPARQL path should be used instead.

    ``kg`` and ``kg_name`` are aliases (``kg_name`` matches REST path params).
    """
    if session is not None:
        return session
    if store is None and graph_backend() != "neo4j":
        return None
    if store is None:
        from cograph_client.graph.store import get_graph_store

        store = get_graph_store()
    kg_val = kg if kg is not None else kg_name
    if not tenant_id or not kg_val:
        raise GraphScopeError(
            "Explore GraphStore path requires tenant_id and kg "
            "(or pass an explicit session)"
        )
    return store.session(GraphScope.for_instance(tenant_id, kg_val))


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_LIMIT
    try:
        n = int(limit)
    except (TypeError, ValueError) as exc:
        raise GraphScopeError(f"Invalid limit {limit!r}") from exc
    if n < 1:
        raise GraphScopeError(f"limit must be >= 1, got {n}")
    return min(n, MAX_PAGE_LIMIT)


def _validate_type_name(type_name: str) -> str:
    """ONTA-425: reject unsafe type path segments before any store touch."""
    return require_valid_type_name(type_name, "type name")


def _summary_from_row(row: Mapping[str, Any]) -> EntitySummary:
    return EntitySummary(
        id=str(row["id"]),
        tenant_id=str(row.get("tenant_id") or ""),
        kg=str(row.get("kg") or ""),
        primary_type=row.get("primary_type"),
        name=row.get("name"),
        source=row.get("source"),
    )


def _public_properties(props: Mapping[str, Any] | None) -> dict[str, Any]:
    """Strip reserved / system Entity keys from a properties map."""
    if not props:
        return {}
    out: dict[str, Any] = {}
    for k, v in props.items():
        if k in _SYSTEM_PROP_KEYS:
            continue
        if v is None:
            continue
        out[str(k)] = v
    return out


def _rel_from_row(row: Mapping[str, Any]) -> EntityRel:
    direction = row.get("direction") or "out"
    if direction not in ("out", "in"):
        direction = "out"
    return EntityRel(
        attr=str(row.get("attr") or row.get("rel_type") or ""),
        rel_type=str(row.get("rel_type") or ""),
        other_id=str(row.get("other_id") or ""),
        direction=direction,  # type: ignore[arg-type]
        other_name=row.get("other_name"),
        other_type=row.get("other_type"),
    )


# ---------------------------------------------------------------------------
# Property-graph path
# ---------------------------------------------------------------------------


async def list_entities_by_type_pg(
    session: "GraphSession",
    type_name: str,
    *,
    match: MatchMode = "primary_type",
    limit: int = DEFAULT_PAGE_LIMIT,
    after_id: str | None = None,
) -> EntityPage:
    """Paged Entity list filtered by primary_type or domain label.

    Parameters
    ----------
    type_name:
        Ontology type leaf. Validated (ONTA-425) before query.
    match:
        ``primary_type`` uses the Entity property (Explorer counting path).
        ``label`` matches the sanitized Neo4j domain label (asserted type).
    limit / after_id:
        Keyset pagination ordered by ``id`` ascending.
    """
    leaf = _validate_type_name(type_name)
    page_limit = _clamp_limit(limit)
    cursor = after_id if after_id else None

    if match == "label":
        safe_label = sanitize_domain_label(leaf)
        native = getattr(session, "read_list_entities_by_label", None)
        if not callable(native):
            raise GraphScopeError(
                "GraphSession does not implement read_list_entities_by_label; "
                "use MemoryGraphStore or Neo4jGraphStore"
            )
        rows = await native(safe_label, after_id=cursor, limit=page_limit)
        # Total for label match: count page isn't free; approximate via full scan
        # only when needed — prefer primary_type total when labels align.
        total_rows = await session.execute_template(
            "entity_count_by_type", {"primary_type": leaf}
        )
        total = int(total_rows[0].get("n") or 0) if total_rows else len(rows)
    elif match == "primary_type":
        rows = await session.execute_template(
            "entity_list_by_type_page",
            {
                "primary_type": leaf,
                "after_id": cursor,
                "limit": page_limit,
            },
        )
        total_rows = await session.execute_template(
            "entity_count_by_type", {"primary_type": leaf}
        )
        total = int(total_rows[0].get("n") or 0) if total_rows else 0
    else:
        raise GraphScopeError(
            f"Unknown match mode {match!r}; expected primary_type|label"
        )

    entities = tuple(_summary_from_row(r.to_dict()) for r in rows)
    next_cursor = entities[-1].id if len(entities) == page_limit else None
    return EntityPage(entities=entities, total=total, next_cursor=next_cursor)


async def get_entity_detail_pg(
    session: "GraphSession",
    entity_id: str,
) -> EntityDetail | None:
    """Fetch one Entity with public properties and incident relationships."""
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise GraphScopeError("entity_id must be a non-empty string")
    eid = entity_id.strip()

    detail_rows = await session.execute_template("entity_detail", {"id": eid})
    if not detail_rows:
        return None
    row = detail_rows[0].to_dict()
    raw_props = row.get("props")
    if not isinstance(raw_props, Mapping):
        raw_props = {}
    labels_raw = row.get("labels") or []
    if isinstance(labels_raw, (list, tuple)):
        labels = tuple(str(x) for x in labels_raw)
    else:
        labels = ()

    rel_rows = await session.execute_template("entity_rels", {"id": eid})
    outgoing: list[EntityRel] = []
    incoming: list[EntityRel] = []
    for rr in rel_rows:
        rel = _rel_from_row(rr.to_dict())
        if not rel.other_id:
            continue
        if rel.direction == "in":
            incoming.append(rel)
        else:
            outgoing.append(rel)

    return EntityDetail(
        id=str(row.get("id") or eid),
        tenant_id=str(row.get("tenant_id") or session.scope.tenant_id),
        kg=str(row.get("kg") or session.scope.kg),
        primary_type=row.get("primary_type"),
        name=row.get("name"),
        source=row.get("source"),
        labels=labels,
        properties=_public_properties(raw_props),
        outgoing=tuple(outgoing),
        incoming=tuple(incoming),
    )


async def type_counts_pg(session: "GraphSession") -> list[TypeCountRow]:
    """Count Entity nodes grouped by ``primary_type`` (non-null only)."""
    rows = await session.execute_template("entity_count_by_primary_type", {})
    out: list[TypeCountRow] = []
    for r in rows:
        name = r.get("primary_type")
        if not name:
            continue
        # Fail-soft on corrupt stored type leaves (ONTA-425 enumeration rule).
        try:
            leaf = require_valid_type_name(name, "type name")
        except InvalidTypeName:
            continue
        try:
            n = int(r.get("n") or 0)
        except (TypeError, ValueError):
            n = 0
        out.append(TypeCountRow(name=leaf, entity_count=n))
    # Sort by count desc then name for stable Explorer rails.
    out.sort(key=lambda t: (-t.entity_count, t.name))
    return out


async def count_entities_pg(session: "GraphSession") -> int:
    """Total Entity nodes in the session's tenant+kg scope."""
    rows = await session.execute_template("entity_count_total", {})
    if not rows:
        return 0
    try:
        return int(rows[0].get("n") or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Dual-backend public API
# ---------------------------------------------------------------------------


async def list_entities_by_type(
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    tenant_id: str | None = None,
    kg: str | None = None,
    kg_name: str | None = None,
    type_name: str,
    match: MatchMode = "primary_type",
    limit: int = DEFAULT_PAGE_LIMIT,
    after_id: str | None = None,
) -> EntityPage | None:
    """List entities by type on GraphStore, or ``None`` for SPARQL fallback.

    Raises :class:`InvalidTypeName` on unsafe ``type_name`` even when the
    SPARQL path would have been chosen (fail closed before routing).
    """
    # Validate first so Neptune-default callers still get ONTA-425 protection
    # when they funnel path params through this helper.
    _validate_type_name(type_name)
    gs = resolve_explore_session(
        store=store,
        session=session,
        tenant_id=tenant_id,
        kg=kg,
        kg_name=kg_name,
    )
    if gs is None:
        return None
    return await list_entities_by_type_pg(
        gs,
        type_name,
        match=match,
        limit=limit,
        after_id=after_id,
    )


async def get_entity_detail(
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    tenant_id: str | None = None,
    kg: str | None = None,
    kg_name: str | None = None,
    entity_id: str,
) -> EntityDetail | None:
    """Entity detail on GraphStore.

    Returns ``None`` when the entity is missing **or** when the SPARQL path is
    active (no store). Callers that need to distinguish "use SPARQL" vs
    "missing" should call :func:`resolve_explore_session` first.
    """
    gs = resolve_explore_session(
        store=store,
        session=session,
        tenant_id=tenant_id,
        kg=kg,
        kg_name=kg_name,
    )
    if gs is None:
        return None
    return await get_entity_detail_pg(gs, entity_id)


async def type_counts(
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    tenant_id: str | None = None,
    kg: str | None = None,
    kg_name: str | None = None,
) -> list[TypeCountRow] | None:
    """Per-type entity counts on GraphStore, or ``None`` for SPARQL fallback."""
    gs = resolve_explore_session(
        store=store,
        session=session,
        tenant_id=tenant_id,
        kg=kg,
        kg_name=kg_name,
    )
    if gs is None:
        return None
    return await type_counts_pg(gs)


async def count_entities(
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    tenant_id: str | None = None,
    kg: str | None = None,
    kg_name: str | None = None,
) -> int | None:
    """Total entity count for a KG on GraphStore, or ``None`` for SPARQL.

    **KG list/count note:** a full KgMeta registry (names, descriptions,
    ownership) is deferred (model §10.1 B7). Until then, callers can use this
    as the minimal per-kg size signal when listing KGs from the instance store
    alone is enough.
    """
    gs = resolve_explore_session(
        store=store,
        session=session,
        tenant_id=tenant_id,
        kg=kg,
        kg_name=kg_name,
    )
    if gs is None:
        return None
    return await count_entities_pg(gs)


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "EntityDetail",
    "EntityPage",
    "EntityRel",
    "EntitySummary",
    "TypeCountRow",
    "count_entities",
    "count_entities_pg",
    "get_entity_detail",
    "get_entity_detail_pg",
    "graph_backend",
    "list_entities_by_type",
    "list_entities_by_type_pg",
    "resolve_explore_session",
    "type_counts",
    "type_counts_pg",
]
