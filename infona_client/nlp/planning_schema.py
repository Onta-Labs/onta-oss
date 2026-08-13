"""Population-aware ontology text for NL planning (/ask).

The tenant catalog is shared across KGs and often still declares relationship
leaves that this KG never populated (e.g. ``has_sponsor→Sponsor``) while the
instance data uses a different leaf (``sponsored_by→Organization``). Handing
the LLM the declaration list as primary plan context makes it walk dead edges.

**Rule (this module):** when instance inventory is available, *prefer* edges
and attributes that are populated in the active KG. Declared-but-empty slots
stay visible (ONTA-248 / ONTA-258) but are secondary — listed after populated
slots and marked ``[no instances]``. Instance-only slots (present in data, not
in the catalog) are first-class primary plan context.

Shared pure merge/format only. GraphStore I/O lives in
:func:`infona_client.nlp.cypher_generate.ontology_from_graph_store`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

NO_INSTANCES_MARK = "[no instances]"

# Cap how many zero-instance *types* we still list after populated ones when
# the tenant ontology is polluted with prior-run shells. Named filters bypass
# the cap (caller already chose those types).
DEFAULT_MAX_EMPTY_TYPES = 12


@dataclass(frozen=True, slots=True)
class PlanningSlot:
    """One attribute or relationship leaf for planning ontology text."""

    name: str
    kind: str  # "literal" | "relationship"
    datatype: str | None = None
    range_type: str | None = None
    prop_key: str | None = None
    populated: bool = False
    count: int = 0


@dataclass(frozen=True, slots=True)
class PlanningType:
    """One type row for planning ontology text."""

    name: str
    entity_count: int = 0
    description: str = ""
    parent_type: str | None = None
    slots: tuple[PlanningSlot, ...] = ()


def _leaf(obj: Any, *keys: str) -> str:
    for k in keys:
        v = getattr(obj, k, None)
        if v is None and isinstance(obj, dict):
            v = obj.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _intish(obj: Any, *keys: str, default: int = 0) -> int:
    for k in keys:
        v = getattr(obj, k, None)
        if v is None and isinstance(obj, dict):
            v = obj.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return default


def merge_declared_and_populated(
    *,
    declared: Sequence[Any] = (),
    populated_literals: Sequence[Any] = (),
    populated_relationships: Sequence[Any] = (),
    default_declared_populated: bool = False,
) -> tuple[PlanningSlot, ...]:
    """Merge catalog declarations with per-KG instance inventory.

    * Instance-populated leaves win for ``populated=True`` / ``count``.
    * Instance-only leaves (not declared) are included as primary.
    * Declared leaves with no instances remain with ``populated=False``
      (unless ``default_declared_populated`` — catalog-only fallback when
      instance inventory was not probed).
    * Order: populated first (count desc, name), then empty declared (name).
    """
    by_name: dict[str, PlanningSlot] = {}

    for a in declared or ():
        name = _leaf(a, "name")
        if not name:
            continue
        kind = (_leaf(a, "kind") or "literal").lower()
        range_type = _leaf(a, "range_type") or None
        datatype = _leaf(a, "datatype") or None
        prop_key = _leaf(a, "prop_key") or name
        if kind == "relationship" or range_type:
            kind = "relationship"
        else:
            kind = "literal"
            if not datatype:
                datatype = "string"
        by_name[name] = PlanningSlot(
            name=name,
            kind=kind,
            datatype=datatype if kind == "literal" else None,
            range_type=range_type if kind == "relationship" else None,
            prop_key=prop_key,
            populated=bool(default_declared_populated),
            count=0,
        )

    for a in populated_literals or ():
        name = _leaf(a, "name")
        if not name:
            continue
        count = _intish(a, "count", "n")
        if count <= 0:
            continue
        datatype = _leaf(a, "datatype") or "string"
        prev = by_name.get(name)
        by_name[name] = PlanningSlot(
            name=name,
            kind="literal",
            datatype=datatype or (prev.datatype if prev else "string") or "string",
            range_type=None,
            prop_key=(prev.prop_key if prev else None) or name,
            populated=True,
            count=count,
        )

    for r in populated_relationships or ():
        name = _leaf(r, "name")
        if not name:
            continue
        count = _intish(r, "count", "n")
        if count <= 0:
            continue
        range_type = _leaf(r, "target_type", "range_type") or None
        prev = by_name.get(name)
        # Prefer instance target when present; fall back to declared range.
        if not range_type and prev and prev.range_type:
            range_type = prev.range_type
        by_name[name] = PlanningSlot(
            name=name,
            kind="relationship",
            datatype=None,
            range_type=range_type,
            prop_key=(prev.prop_key if prev else None) or name,
            populated=True,
            count=count,
        )

    def _sort_key(s: PlanningSlot) -> tuple:
        # populated first, higher count first, then stable name
        return (0 if s.populated else 1, -s.count, s.name.lower())

    return tuple(sorted(by_name.values(), key=_sort_key))


def build_planning_type(
    *,
    name: str,
    entity_count: int = 0,
    description: str = "",
    parent_type: str | None = None,
    declared: Sequence[Any] = (),
    populated_literals: Sequence[Any] = (),
    populated_relationships: Sequence[Any] = (),
    default_declared_populated: bool = False,
) -> PlanningType:
    """Assemble one planning type from declared + populated sources.

    Empty types keep their declared slots so the LLM can write a valid
    zero-row query (ONTA-258). Tenant pollution is reduced by capping how
    many empty *types* appear (:func:`order_planning_types`), not by
    stripping slots from types we chose to include.
    """
    slots = merge_declared_and_populated(
        declared=declared,
        populated_literals=populated_literals,
        populated_relationships=populated_relationships,
        default_declared_populated=default_declared_populated,
    )
    return PlanningType(
        name=name,
        entity_count=int(entity_count or 0),
        description=description or "",
        parent_type=parent_type,
        slots=slots,
    )


def order_planning_types(
    types: Sequence[PlanningType],
    *,
    max_empty_types: int = DEFAULT_MAX_EMPTY_TYPES,
    force_include: Iterable[str] | None = None,
) -> list[PlanningType]:
    """Populated types first; cap trailing empty types (unless force-included).

    Does not hide force-included empty types (ONTA-258 named-type honesty).
    """
    force = {n for n in (force_include or ()) if n}
    populated = [t for t in types if t.entity_count > 0]
    empty = [t for t in types if t.entity_count <= 0]
    populated.sort(key=lambda t: (-t.entity_count, t.name.lower()))
    empty.sort(key=lambda t: t.name.lower())

    kept_empty: list[PlanningType] = []
    extra = 0
    for t in empty:
        if t.name in force:
            kept_empty.append(t)
            continue
        if extra < max_empty_types:
            kept_empty.append(t)
            extra += 1
    return populated + kept_empty


def format_planning_slot(slot: PlanningSlot) -> str:
    """One Cypher-oriented slot line (matches format_schema_types_for_cypher)."""
    prop_key = slot.prop_key or slot.name
    mark = f" {NO_INSTANCES_MARK}" if not slot.populated else ""
    if slot.kind == "relationship" or slot.range_type:
        return (
            f"  - {slot.name} -> {slot.range_type or '?'} "
            f"(relationship, key={prop_key}){mark}"
        )
    dtype = slot.datatype or "string"
    return f"  - {slot.name}: {dtype} (literal, key={prop_key}){mark}"


def format_planning_type(ptype: PlanningType) -> str:
    """Format one type block for the Cypher /ask ontology schema section."""
    count = int(ptype.entity_count or 0)
    if count == 0:
        header = f"Type: {ptype.name} {NO_INSTANCES_MARK}"
    else:
        header = f"Type: {ptype.name} ({count} entities)"
    lines = [header]
    if ptype.description:
        lines.append(f"  description: {ptype.description}")
    if ptype.parent_type:
        lines.append(f"  parent: {ptype.parent_type}")
    for s in ptype.slots:
        lines.append(format_planning_slot(s))
    return "\n".join(lines)


def format_planning_ontology(
    types: Sequence[PlanningType],
    *,
    max_empty_types: int = DEFAULT_MAX_EMPTY_TYPES,
    force_include: Iterable[str] | None = None,
    preface: bool = True,
) -> str:
    """Full ontology text for NL planning, population-preferred.

    Populated slots appear before declared-empty ones within each type.
    Empty types trail populated ones (capped). Optional one-line preface
    teaches the model to plan on unmarked slots first.
    """
    ordered = order_planning_types(
        types, max_empty_types=max_empty_types, force_include=force_include
    )
    if not ordered:
        return ""
    blocks = [format_planning_type(t) for t in ordered]
    body = "\n".join(blocks)
    if not preface:
        return body
    # Compact planning rule — complements prompts.py [no instances] guidance.
    note = (
        "Planning note: prefer attributes/relationships WITHOUT "
        f'"{NO_INSTANCES_MARK}" (they hold data in this KG). '
        f"Slots marked {NO_INSTANCES_MARK} are declared in the tenant ontology "
        "but unpopulated here — use them only when the question requires that "
        "exact declared leaf; otherwise plan on the populated leaves above them."
    )
    return f"{note}\n{body}"


def planning_types_from_schema_and_summaries(
    schema_rows: Sequence[Any],
    summaries_by_name: dict[str, Any] | None = None,
    *,
    max_empty_types: int = DEFAULT_MAX_EMPTY_TYPES,
    force_include: Iterable[str] | None = None,
    inventory_probed: bool = True,
) -> list[PlanningType]:
    """Build ordered planning types from catalog rows + type_summary inventory.

    ``schema_rows``: :class:`~infona_client.graph.ontology_catalog.SchemaTypeSummary`
    (or duck-typed). ``summaries_by_name``: optional
    :class:`~infona_client.graph.explore_store.TypeSummaryRow` by type name —
    supplies instance-populated attrs/rels (including undeclared leaves).

    When ``inventory_probed`` is True (default for the GraphStore planning path)
    a populated type *without* a summary row treats declared slots as unknown
    → usable (legacy catalog behaviour). A type *with* a summary treats only
    inventory-backed leaves as populated; remaining declared leaves are
    secondary ``[no instances]`` slots.
    """
    summaries = summaries_by_name or {}
    out: list[PlanningType] = []
    for row in schema_rows or ():
        name = _leaf(row, "name")
        if not name:
            continue
        entity_count = _intish(row, "entity_count")
        summary = summaries.get(name)
        if summary is not None:
            # Prefer live summary count when present (source of population truth).
            sc = _intish(summary, "entity_count")
            if sc > 0 or entity_count == 0:
                entity_count = sc
            pop_lits = getattr(summary, "attributes", None) or ()
            pop_rels = getattr(summary, "relationships", None) or ()
            if isinstance(summary, dict):
                pop_lits = summary.get("attributes") or ()
                pop_rels = summary.get("relationships") or ()
            # Inventory is authoritative: declared-only leaves stay secondary.
            default_decl_pop = False
        else:
            pop_lits = ()
            pop_rels = ()
            # No inventory for this type: if the type has entities, keep declared
            # slots primary so a summary failure does not mark every leaf empty.
            default_decl_pop = bool(inventory_probed and entity_count > 0) or (
                not inventory_probed and entity_count > 0
            )
        declared = getattr(row, "attributes", None) or ()
        if isinstance(row, dict):
            declared = row.get("attributes") or ()
        out.append(
            build_planning_type(
                name=name,
                entity_count=entity_count,
                description=_leaf(row, "description")
                or (getattr(summary, "description", None) or ""),
                parent_type=(_leaf(row, "parent_type") or None)
                or (getattr(summary, "parent_type", None) or None),
                declared=declared,
                populated_literals=pop_lits,
                populated_relationships=pop_rels,
                default_declared_populated=default_decl_pop,
            )
        )
    # Instance-only types present in summaries but missing from catalog.
    seen = {t.name for t in out}
    for name, summary in summaries.items():
        if not name or name in seen:
            continue
        entity_count = _intish(summary, "entity_count")
        if entity_count <= 0:
            continue
        pop_lits = getattr(summary, "attributes", None) or ()
        pop_rels = getattr(summary, "relationships", None) or ()
        if isinstance(summary, dict):
            pop_lits = summary.get("attributes") or ()
            pop_rels = summary.get("relationships") or ()
        out.append(
            build_planning_type(
                name=name,
                entity_count=entity_count,
                description=getattr(summary, "description", None) or "",
                parent_type=getattr(summary, "parent_type", None),
                declared=(),
                populated_literals=pop_lits,
                populated_relationships=pop_rels,
            )
        )
    return order_planning_types(
        out, max_empty_types=max_empty_types, force_include=force_include
    )


__all__ = [
    "DEFAULT_MAX_EMPTY_TYPES",
    "NO_INSTANCES_MARK",
    "PlanningSlot",
    "PlanningType",
    "build_planning_type",
    "format_planning_ontology",
    "format_planning_slot",
    "format_planning_type",
    "merge_declared_and_populated",
    "order_planning_types",
    "planning_types_from_schema_and_summaries",
]
