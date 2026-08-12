"""Explore / KG-admin read path on the property-graph store (E5 partial).

Minimal GraphStore-backed readers that Explorer / KG-admin routes can call:

* :func:`list_entities_by_type` — paged instances by ``INSTANCE_OF`` → Class
  (match mode ``primary_type`` is historical; semantic membership is Class)
  or domain label
* :func:`get_entity_detail` — properties + outgoing / incoming relationships
* :func:`type_counts` — per-Class counts via ``INSTANCE_OF`` (type-stats input)
* :func:`type_summary` — per-type inventory for ``vis <Type>`` (P-A1a): entity
  count + attribute/relationship coverage, same count source as type-counts
* :func:`count_entities` — total Entity nodes in a tenant+kg (minimal KG size)

**ADR 0013:** type list/count prefer Assertion-derived ``INSTANCE_OF`` + Class
over the denorm ``Entity.primary_type`` property alone. Optional subclass
expansion walks ``:Class``-``SUBCLASS_OF`` when ``include_subclasses=True``.

**Dual-backend:** when an explicit ``store`` / ``session`` is passed, or
``INFONA_GRAPH_BACKEND=neo4j``, reads run through GraphStore templates /
native session methods. Raises :class:`GraphConfigError` when no store is
configured (ONTA-527 — Neo4j is the only backend; no SPARQL hand-back).

Type-name path safety (ONTA-425): every public type-leaf argument goes through
:func:`require_valid_type_name` (and domain-label sanitization when matching
labels) so unsafe path segments never reach Cypher interpolation or IRI
construction.

API sketch (route wiring)::

    GET  /graphs/{tenant}/explore/kgs/{kg}/types/{type}/records
         → list_entities_by_type(...)   # columns/rows assembled by route
    GET  /graphs/{tenant}/explore/kgs/{kg}/entities/{id}
         → get_entity_detail(...)
    GET  /graphs/{tenant}/kgs/{kg}/type-counts
         → type_counts(...)
    GET  /graphs/{tenant}/explore/kgs/{kg}/types/{type}/summary
         → type_summary(...)            # P-A1a vis drill-in
    GET  /graphs/{tenant}/kgs            # entity_count field
         → count_entities(...) per kg when registry deferred
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Mapping, Optional

from infona_client.graph.facts import (
    RESERVED_ENTITY_PROPERTY_KEYS,
    is_internal_property_key,
)
from infona_client.graph.iri import ONTO_PRED_PREFIX
from infona_client.graph.labels import sanitize_domain_label
from infona_client.graph.ontology_queries import attr_uri
from infona_client.graph.predicates import companion_leaves
from infona_client.graph.queries import (
    InvalidTypeName,
    require_valid_type_name,
)
from infona_client.graph.scope import GraphScope, GraphScopeError

if TYPE_CHECKING:
    from infona_client.graph.store import GraphSession, GraphStore

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
class TypeAttrSummary:
    """One populated literal attribute on a type summary panel."""

    name: str
    predicate_uri: str
    datatype: str
    count: int
    coverage_pct: float


@dataclass(frozen=True, slots=True)
class TypeRelSummary:
    """One populated relationship on a type summary panel."""

    name: str
    predicate_uri: str
    target_type: str | None
    count: int
    coverage_pct: float
    avg_degree: float


@dataclass(frozen=True, slots=True)
class TypeSummaryRow:
    """Per-KG type inventory for ``vis <Type>`` / Explorer type panel (P-A1a).

    Instance inventory for the selected KG (projection of the tenant-global
    ontology onto this KG's data). ``entity_count`` uses the same
    ``INSTANCE_OF`` path as :func:`type_counts` so overview and drill-in agree.
    """

    name: str
    entity_count: int
    description: str = ""
    parent_type: str | None = None
    attributes: tuple[TypeAttrSummary, ...] = ()
    relationships: tuple[TypeRelSummary, ...] = ()
    spatially_indexed: bool = False
    temporally_indexed: bool = False

    def as_api_dict(self) -> dict[str, Any]:
        """Wire shape matching ``GET …/types/{type}/summary`` / CLI TypeSummary."""
        return {
            "name": self.name,
            "description": self.description,
            "parent_type": self.parent_type,
            "entity_count": self.entity_count,
            "attributes": [
                {
                    "name": a.name,
                    "predicate_uri": a.predicate_uri,
                    "datatype": a.datatype,
                    "count": a.count,
                    "coverage_pct": a.coverage_pct,
                }
                for a in self.attributes
            ],
            "relationships": [
                {
                    "name": r.name,
                    "predicate_uri": r.predicate_uri,
                    "target_type": r.target_type,
                    "count": r.count,
                    "coverage_pct": r.coverage_pct,
                    "avg_degree": r.avg_degree,
                }
                for r in self.relationships
            ],
            "spatially_indexed": self.spatially_indexed,
            "temporally_indexed": self.temporally_indexed,
        }


@dataclass(frozen=True, slots=True)
class EntityPage:
    """Paged list result with keyset cursor."""

    entities: tuple[EntitySummary, ...]
    total: int
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Dual-backend session resolution
# ---------------------------------------------------------------------------


# The ONE backend switch lives in `graph/store.py`. This module used to define a
# copy of `graph_backend()` (as did kg_writer and ontology_catalog); ONTA-527
# removed all three duplicates and `tests/test_neo4j_only_backend.py` fails if a
# new one appears.


def resolve_explore_session(
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    tenant_id: str | None = None,
    kg: str | None = None,
    kg_name: str | None = None,
) -> "GraphSession":
    """Return an instance-scoped session for the explore read path.

    Priority: explicit ``session`` → explicit ``store`` → the process store.
    Never returns ``None``: Neo4j is the only backend (ONTA-527), so there is
    no SPARQL path to hand back to. Raises :class:`GraphConfigError` when no
    store is configured.

    ``kg`` and ``kg_name`` are aliases (``kg_name`` matches REST path params).
    """
    if session is not None:
        return session
    if store is None:
        from infona_client.graph.store import get_optional_graph_store

        store = get_optional_graph_store()
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


async def _type_names_for_explore(
    session: "GraphSession",
    leaf: str,
    *,
    include_subclasses: bool,
) -> list[str]:
    """Resolve explore type filter; optionally expand via Class SUBCLASS_OF."""
    if not include_subclasses:
        return [leaf]
    try:
        rows = await session.execute_template(
            "subclass_of_closure",
            {"type_name": leaf, "layer": None},
        )
    except Exception:
        return [leaf]
    names = [str(r.get("type_name")) for r in rows if r.get("type_name")]
    return names if names else [leaf]


async def list_entities_by_type_pg(
    session: "GraphSession",
    type_name: str,
    *,
    match: MatchMode = "primary_type",
    limit: int = DEFAULT_PAGE_LIMIT,
    after_id: str | None = None,
    include_subclasses: bool = False,
) -> EntityPage:
    """Paged Entity list filtered by type membership or domain label.

    Parameters
    ----------
    type_name:
        Ontology type leaf. Validated (ONTA-425) before query.
    match:
        ``primary_type`` (historical name) matches ``INSTANCE_OF`` → Class
        (ADR 0013 semantic membership). ``label`` matches the sanitized Neo4j
        domain label.
    include_subclasses:
        When True and match is semantic, expand the type filter via Class
        ``SUBCLASS_OF`` closure (Person includes Employee, etc.).
    limit / after_id:
        Keyset pagination ordered by ``id`` ascending.
    """
    leaf = _validate_type_name(type_name)
    page_limit = _clamp_limit(limit)
    cursor = after_id if after_id else None
    type_names = await _type_names_for_explore(
        session, leaf, include_subclasses=include_subclasses
    )

    if match == "label":
        safe_label = sanitize_domain_label(leaf)
        native = getattr(session, "read_list_entities_by_label", None)
        if not callable(native):
            raise GraphScopeError(
                "GraphSession does not implement read_list_entities_by_label; "
                "use MemoryGraphStore or Neo4jGraphStore"
            )
        rows = await native(safe_label, after_id=cursor, limit=page_limit)
        # Total via INSTANCE_OF Class (semantic), not denorm primary_type alone.
        total_rows = await session.execute_template(
            "entities_of_type_count", {"type_names": type_names}
        )
        total = int(total_rows[0].get("n") or 0) if total_rows else len(rows)
    elif match == "primary_type":
        # Semantic path: INSTANCE_OF → Class (+ optional subclass-expanded names).
        if len(type_names) == 1 and not include_subclasses:
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
        else:
            rows = await session.execute_template(
                "entities_of_type",
                {
                    "type_names": type_names,
                    "after_id": cursor,
                    "limit": page_limit,
                },
            )
            total_rows = await session.execute_template(
                "entities_of_type_count", {"type_names": type_names}
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
    """Count Entity nodes grouped by Class name via ``INSTANCE_OF`` (ADR 0013).

    Multi-typed entities contribute to each asserted Class. The denorm
    ``Entity.primary_type`` property is not used as the sole grouping key.
    """
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


def _coverage_pct(count: int, entity_count: int) -> float:
    if not entity_count:
        return 0.0
    return round(count / entity_count * 100, 1)


async def type_summary_pg(
    session: "GraphSession",
    type_name: str,
    *,
    catalog_session: Optional["GraphSession"] = None,
) -> TypeSummaryRow | None:
    """Build a type summary for one type leaf from GraphStore instance data.

    Returns
    -------
    TypeSummaryRow
        When the type has instances in this KG, or is declared in the tenant
        ontology catalog (even with zero instances).
    None
        When the type is neither declared nor has instances — route maps this
        to 404. Matches the product contract for ``vis <Type>``.

    ``entity_count`` uses ``entity_count_by_type`` (``INSTANCE_OF`` → Class),
    the same membership path as :func:`type_counts_pg`, so overview bars and
    drill-in numbers cannot diverge.
    """
    leaf = _validate_type_name(type_name)

    count_rows = await session.execute_template(
        "entity_count_by_type", {"primary_type": leaf}
    )
    try:
        entity_count = int(count_rows[0].get("n") or 0) if count_rows else 0
    except (TypeError, ValueError, IndexError):
        entity_count = 0

    description = ""
    parent_type: str | None = None
    attr_meta: dict[str, dict[str, str]] = {}
    declared = False
    if catalog_session is not None:
        try:
            from infona_client.graph.ontology_catalog import (
                get_type_pg,
                list_attributes_pg,
            )

            onto = await get_type_pg(catalog_session, leaf)
            if onto is not None:
                declared = True
                description = onto.description or ""
                parent_type = onto.parent_type
            for a in await list_attributes_pg(catalog_session, domain=leaf):
                attr_meta[a.name] = {
                    "datatype": a.datatype or "string",
                    "range_type": a.range_type or "",
                    "kind": a.kind or "literal",
                }
                declared = True
        except Exception:
            # Ontology catalog is best-effort; instance inventory still works.
            pass

    if entity_count == 0 and not declared:
        return None

    attributes: list[TypeAttrSummary] = []
    relationships: list[TypeRelSummary] = []

    if entity_count > 0:
        attr_rows = await session.execute_template(
            "entity_type_attr_coverage", {"primary_type": leaf}
        )
        raw_attr_names: list[str] = []
        attr_counts: dict[str, int] = {}
        for r in attr_rows:
            d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
            name = str(d.get("attr") or "")
            if not name:
                continue
            if name in RESERVED_ENTITY_PROPERTY_KEYS or is_internal_property_key(name):
                continue
            try:
                n = int(d.get("n") or 0)
            except (TypeError, ValueError):
                n = 0
            if n <= 0:
                continue
            raw_attr_names.append(name)
            attr_counts[name] = n

        # LEGACY companion hygiene (ONTA-262): hide ``email_source_url`` when
        # ``email`` is also present.
        legacy = companion_leaves(raw_attr_names)
        for name, n in sorted(attr_counts.items(), key=lambda x: (-x[1], x[0])):
            if name in legacy:
                continue
            meta = attr_meta.get(name, {})
            datatype = meta.get("datatype") or "string"
            try:
                p_uri = attr_uri(leaf, name)
            except InvalidTypeName:
                p_uri = f"{ONTO_PRED_PREFIX}{name}"
            attributes.append(
                TypeAttrSummary(
                    name=name,
                    predicate_uri=p_uri,
                    datatype=datatype,
                    count=n,
                    coverage_pct=_coverage_pct(n, entity_count),
                )
            )

        rel_rows = await session.execute_template(
            "entity_type_rel_coverage", {"primary_type": leaf}
        )
        for r in rel_rows:
            d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
            name = str(d.get("attr") or "")
            if not name:
                continue
            if is_internal_property_key(name) and name not in attr_meta:
                continue
            try:
                n = int(d.get("n") or 0)
            except (TypeError, ValueError):
                n = 0
            try:
                rel_total = int(d.get("rel_total") or 0)
            except (TypeError, ValueError):
                rel_total = 0
            if n <= 0:
                continue
            target = d.get("target_type")
            if target is not None:
                target = str(target) or None
            meta = attr_meta.get(name, {})
            if not target and meta.get("range_type"):
                target = meta["range_type"] or None
            p_uri = f"{ONTO_PRED_PREFIX}{name}"
            avg = round(rel_total / entity_count, 2) if entity_count else 0.0
            relationships.append(
                TypeRelSummary(
                    name=name,
                    predicate_uri=p_uri,
                    target_type=target,
                    count=n,
                    coverage_pct=_coverage_pct(n, entity_count),
                    avg_degree=avg,
                )
            )

    return TypeSummaryRow(
        name=leaf,
        entity_count=entity_count,
        description=description,
        parent_type=parent_type,
        attributes=tuple(attributes),
        relationships=tuple(relationships),
    )


@dataclass(frozen=True, slots=True)
class GrepHit:
    """One literal property match from :func:`grep_literals`."""

    entity_uri: str
    label: str | None
    type: str | None
    attr: str
    value: str


async def grep_literals_pg(
    session: "GraphSession",
    needle: str,
    *,
    case_sensitive: bool = False,
    type_name: str | None = None,
    predicate_leaf: str | None = None,
    limit: int = 50,
) -> tuple[list[GrepHit], bool]:
    """Substring scan over Entity property values (Neo4j / MemoryGraphStore).

    Returns ``(hits, truncated)`` where ``truncated`` is True when the store
    produced more than ``limit`` rows (caller asked for ``limit + 1``).

    **Internal keys never reach a caller.** The scan already excludes them (the
    ``entity_literal_grep`` template and the Memory store both filter on
    :func:`~infona_client.graph.facts.is_internal_property_key`); this repeats
    the check as the authority, in the ONE place that owns the page, so a store
    whose scan-level exclusion drifts still cannot leak. Order matters: the
    check runs BEFORE the page is cut, so an internal row can never occupy a
    slot the caller paid for and hand back a short page marked ``truncated:
    false``.
    """
    if not isinstance(needle, str) or not needle:
        raise GraphScopeError("grep needle must be a non-empty string")
    page_limit = _clamp_limit(limit)
    # Over-fetch one row for honest truncation (mirrors SPARQL grep).
    fetch_limit = page_limit + 1
    type_leaf: str | None = None
    if type_name:
        type_leaf = _validate_type_name(type_name)
    pred = predicate_leaf.strip() if predicate_leaf else None
    if pred is not None and not pred:
        pred = None

    rows = await session.execute_template(
        "entity_literal_grep",
        {
            "needle": needle,
            "case_sensitive": bool(case_sensitive),
            "type_name": type_leaf,
            "predicate_leaf": pred,
            "limit": fetch_limit,
        },
    )
    kept: list[GrepHit] = []
    for r in rows:
        d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        attr = str(d.get("attr") or "")
        if not attr or is_internal_property_key(attr):
            continue
        kept.append(
            GrepHit(
                entity_uri=str(d.get("entity_uri") or d.get("id") or ""),
                label=d.get("label"),
                type=d.get("type") or d.get("primary_type"),
                attr=attr,
                value=str(d.get("value") if d.get("value") is not None else ""),
            )
        )
    truncated = len(kept) > page_limit
    return kept[:page_limit], truncated


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
    include_subclasses: bool = False,
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
        include_subclasses=include_subclasses,
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


async def grep_literals(
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    tenant_id: str | None = None,
    kg: str | None = None,
    kg_name: str | None = None,
    needle: str,
    case_sensitive: bool = False,
    type_name: str | None = None,
    predicate_leaf: str | None = None,
    limit: int = 50,
) -> tuple[list[GrepHit], bool] | None:
    """Literal property grep on GraphStore, or ``None`` for SPARQL fallback."""
    gs = resolve_explore_session(
        store=store,
        session=session,
        tenant_id=tenant_id,
        kg=kg,
        kg_name=kg_name,
    )
    if gs is None:
        return None
    return await grep_literals_pg(
        gs,
        needle,
        case_sensitive=case_sensitive,
        type_name=type_name,
        predicate_leaf=predicate_leaf,
        limit=limit,
    )


async def type_summary(
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    catalog_session: Optional["GraphSession"] = None,
    tenant_id: str | None = None,
    kg: str | None = None,
    kg_name: str | None = None,
    type_name: str,
) -> TypeSummaryRow | None:
    """Per-type inventory for one KG (P-A1a).

    Returns :class:`TypeSummaryRow` when the type has instances or is declared
    in the tenant ontology; ``None`` when neither (caller → 404).

    Raises :class:`InvalidTypeName` on unsafe ``type_name`` (ONTA-425) before
    any store touch. Raises :class:`GraphConfigError` when no store is
    configured (Neo4j-only; no SPARQL fallback).
    """
    _validate_type_name(type_name)
    gs = resolve_explore_session(
        store=store,
        session=session,
        tenant_id=tenant_id,
        kg=kg,
        kg_name=kg_name,
    )
    cat = catalog_session
    if cat is None and tenant_id:
        try:
            from infona_client.graph.ontology_catalog import resolve_catalog_session

            cat = resolve_catalog_session(
                store=store,
                layer="tenant",
                tenant_id=tenant_id,
            )
        except Exception:
            cat = None
    return await type_summary_pg(gs, type_name, catalog_session=cat)


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "EntityDetail",
    "EntityPage",
    "EntityRel",
    "EntitySummary",
    "GrepHit",
    "TypeAttrSummary",
    "TypeCountRow",
    "TypeRelSummary",
    "TypeSummaryRow",
    "count_entities",
    "count_entities_pg",
    "get_entity_detail",
    "get_entity_detail_pg",
    "grep_literals",
    "grep_literals_pg",
    "list_entities_by_type",
    "list_entities_by_type_pg",
    "resolve_explore_session",
    "type_counts",
    "type_counts_pg",
    "type_summary",
    "type_summary_pg",
]
