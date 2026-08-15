"""AssertionMemoryStore helpers for ADR 0013 semantic queries.

Implementation sibling of :mod:`infona_client.graph.rdfs_helpers`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from infona_client.graph.assertion_model import (
    AssertionNode,
    canonical_literal,
    type_membership_property_id,
)
from infona_client.graph.scope import GraphScopeError

if TYPE_CHECKING:
    from infona_client.graph.assertion_memory import AssertionMemoryStore

def subclass_of(
    store: AssertionMemoryStore,
    class_id: str,
    *,
    tenant_id: str,
    kg: str,
    direction: str = "ancestors",
    transitive: bool = True,
) -> list[str]:
    """Walk ``SUBCLASS_OF`` from ``class_id``.

    Parameters
    ----------
    direction:
        ``ancestors`` — parents of this class (toward superclasses).
        ``descendants`` — classes that subclass this class (transitive children).
    transitive:
        When False, only the immediate parent (ancestors) or children
        (descendants).
    """
    if direction not in ("ancestors", "descendants"):
        raise GraphScopeError("direction must be ancestors|descendants")
    if direction == "ancestors":
        out: list[str] = []
        cur = class_id
        seen: set[str] = set()
        while True:
            parent = store.subclass_parent(tenant_id, kg, cur)
            if not parent or parent in seen:
                break
            out.append(parent)
            seen.add(parent)
            if not transitive:
                break
            cur = parent
        return out

    # descendants: invert the parent map
    children: dict[str, list[str]] = {}
    for cid in store.all_class_ids(tenant_id, kg):
        parent = store.subclass_parent(tenant_id, kg, cid)
        if parent:
            children.setdefault(parent, []).append(cid)

    out = []
    stack = list(children.get(class_id, []))
    seen = set()
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
        if transitive:
            stack.extend(children.get(c, []))
    return out


def subclass_of_closure(
    store: AssertionMemoryStore,
    class_ids: Sequence[str],
    *,
    tenant_id: str,
    kg: str,
    include_self: bool = True,
) -> set[str]:
    """Set of Class ids in the descendant closure of ``class_ids`` (for type query).

    ``entities_of_type(T, include_subclasses=True)`` uses this: an entity whose
    asserted type is a **descendant** of T matches T.
    """
    result: set[str] = set()
    for cid in class_ids:
        if include_self:
            result.add(cid)
        for d in subclass_of(
            store, cid, tenant_id=tenant_id, kg=kg, direction="descendants", transitive=True
        ):
            result.add(d)
    return result


def asserted_types(
    store: AssertionMemoryStore,
    entity_id: str,
    *,
    tenant_id: str,
    kg: str,
) -> list[dict[str, Any]]:
    """Asserted type Class ids / names for one entity (no ancestor fill)."""
    type_prop = type_membership_property_id()
    rows: list[dict[str, Any]] = []
    for a in store.list_assertions(tenant_id=tenant_id, kg=kg):
        if a.subject_id != entity_id:
            continue
        if a.property_id != type_prop:
            continue
        cid = a.object_class_id
        if not cid:
            continue
        rows.append(
            {
                "entity_id": entity_id,
                "class_id": cid,
                "type_name": store.class_name(tenant_id, kg, cid),
            }
        )
    return rows


def entities_of_type(
    store: AssertionMemoryStore,
    class_id_or_name: str,
    *,
    tenant_id: str,
    kg: str,
    include_subclasses: bool = True,
) -> list[dict[str, Any]]:
    """Entities with a type Assertion matching ``class_id`` (optional subclass fill).

    Uses type Assertions / INSTANCE_OF cache; never invents ancestor type
    Assertions. When ``include_subclasses`` is True, an entity asserted as a
    **descendant** of the query class is included.
    """
    class_id = store.resolve_class_id(tenant_id, kg, class_id_or_name)
    if not class_id:
        return []

    if include_subclasses:
        allowed = subclass_of_closure(
            store, [class_id], tenant_id=tenant_id, kg=kg, include_self=True
        )
    else:
        allowed = {class_id}

    type_prop = type_membership_property_id()
    # entity_id → set of matching class ids from type assertions
    matched: dict[str, set[str]] = {}
    for a in store.list_assertions(tenant_id=tenant_id, kg=kg):
        if a.property_id != type_prop:
            continue
        if not a.object_class_id or a.object_class_id not in allowed:
            continue
        matched.setdefault(a.subject_id, set()).add(a.object_class_id)

    # Cross-check INSTANCE_OF cache (derived) — Assertions win if skew, but
    # include cache-only only when assertion already listed (never invent).
    rows: list[dict[str, Any]] = []
    for eid, cids in sorted(matched.items()):
        ent = store.get_entity(tenant_id, kg, eid)
        rows.append(
            {
                "entity_id": eid,
                "name": ent.name if ent else None,
                "type_ids": sorted(cids),
                "type_names": sorted(
                    n
                    for n in (
                        store.class_name(tenant_id, kg, c) for c in cids
                    )
                    if n
                ),
            }
        )
    return rows


def count_entities_of_type(
    store: AssertionMemoryStore,
    class_id_or_name: str,
    *,
    tenant_id: str,
    kg: str,
    include_subclasses: bool = True,
) -> list[dict[str, Any]]:
    """``[{count: N}]`` — answer shape for GQ-01 / GQ-02."""
    ents = entities_of_type(
        store,
        class_id_or_name,
        tenant_id=tenant_id,
        kg=kg,
        include_subclasses=include_subclasses,
    )
    return [{"count": len(ents)}]


def assertions_for_subject(
    store: AssertionMemoryStore,
    entity_id: str,
    *,
    tenant_id: str,
    kg: str,
    property_id: str | None = None,
    property_name: str | None = None,
) -> list[dict[str, Any]]:
    """Assertion rows for one subject, optionally filtered by property."""
    prop_filter: str | None = property_id
    if prop_filter is None and property_name:
        prop_filter = store.resolve_property_id(tenant_id, kg, property_name)

    rows: list[dict[str, Any]] = []
    for a in store.list_assertions(tenant_id=tenant_id, kg=kg):
        if a.subject_id != entity_id:
            continue
        if prop_filter is not None and a.property_id != prop_filter:
            continue
        rows.append(_project_assertion(store, a, tenant_id=tenant_id, kg=kg))
    return rows


def literal_value(assertion: AssertionNode | MappingLike) -> Any:
    """Project the datatype object of an Assertion."""
    if isinstance(assertion, AssertionNode):
        return assertion.literal_value
    return assertion.get("literal_value")  # type: ignore[union-attr]


def object_value(assertion: AssertionNode | MappingLike) -> str | None:
    """Project the object Entity id of an object-property Assertion."""
    if isinstance(assertion, AssertionNode):
        return assertion.object_id
    return assertion.get("object_id")  # type: ignore[union-attr]


def fact_provenance(
    store: AssertionMemoryStore,
    assertion_id: str,
    *,
    tenant_id: str,
    kg: str,
) -> list[dict[str, Any]]:
    """Provenance fields for one Assertion (GQ-06)."""
    a = store.get_assertion(tenant_id, kg, assertion_id)
    if a is None:
        return []
    return [
        {
            "assertion_id": a.id,
            "subject_id": a.subject_id,
            "property": store.property_name(tenant_id, kg, a.property_id)
            or a.property_id,
            "property_id": a.property_id,
            "value": a.literal_value if a.literal_value is not None else a.object_id,
            "source_url": a.source_url,
            "verified_at": a.verified_at,
            "run_id": a.run_id,
            "confidence": a.confidence,
            "provenance": a.provenance,
        }
    ]


def reverse_object_assertions(
    store: AssertionMemoryStore,
    object_entity_id: str,
    *,
    tenant_id: str,
    kg: str,
    property_id: str | None = None,
    property_name: str | None = None,
) -> list[dict[str, Any]]:
    """Subjects that point at ``object_entity_id`` via an object property (GQ-05)."""
    prop_filter: str | None = property_id
    if prop_filter is None and property_name:
        prop_filter = store.resolve_property_id(tenant_id, kg, property_name)

    rows: list[dict[str, Any]] = []
    for a in store.list_assertions(tenant_id=tenant_id, kg=kg):
        if a.object_id != object_entity_id:
            continue
        if prop_filter is not None and a.property_id != prop_filter:
            continue
        rows.append(
            {
                "subject_id": a.subject_id,
                "object_id": a.object_id,
                "property": store.property_name(tenant_id, kg, a.property_id)
                or a.property_id,
            }
        )
    return rows


def parent_classes(
    store: AssertionMemoryStore,
    class_id_or_name: str,
    *,
    tenant_id: str,
    kg: str,
    transitive: bool = True,
) -> list[dict[str, Any]]:
    """Parent Class ids/names for catalog hierarchy (GQ-10)."""
    class_id = store.resolve_class_id(tenant_id, kg, class_id_or_name)
    if not class_id:
        return []
    parents = subclass_of(
        store,
        class_id,
        tenant_id=tenant_id,
        kg=kg,
        direction="ancestors",
        transitive=transitive,
    )
    return [
        {
            "class_id": pid,
            "type_name": store.class_name(tenant_id, kg, pid),
        }
        for pid in parents
    ]


def entities_with_literal_filter(
    store: AssertionMemoryStore,
    class_id_or_name: str,
    property_name: str,
    *,
    tenant_id: str,
    kg: str,
    op: str = ">",
    value: Any = None,
    include_subclasses: bool = True,
) -> list[dict[str, Any]]:
    """Compose type membership + literal assertion filter (GQ-12)."""
    candidates = entities_of_type(
        store,
        class_id_or_name,
        tenant_id=tenant_id,
        kg=kg,
        include_subclasses=include_subclasses,
    )
    prop_id = store.resolve_property_id(tenant_id, kg, property_name)
    if not prop_id:
        return []

    out: list[dict[str, Any]] = []
    for row in candidates:
        eid = row["entity_id"]
        for a in store.list_assertions(tenant_id=tenant_id, kg=kg):
            if a.subject_id != eid or a.property_id != prop_id:
                continue
            lit = a.literal_value
            if lit is None:
                continue
            if _compare(lit, op, value):
                out.append({"entity_id": eid, "value": lit})
                break
    return out


# --- internal ---------------------------------------------------------------

# Typing alias for duck-typed assertion maps
MappingLike = Any


def _project_assertion(
    store: AssertionMemoryStore,
    a: AssertionNode,
    *,
    tenant_id: str,
    kg: str,
) -> dict[str, Any]:
    return {
        "assertion_id": a.id,
        "subject_id": a.subject_id,
        "entity_id": a.subject_id,
        "property_id": a.property_id,
        "property": store.property_name(tenant_id, kg, a.property_id) or a.property_id,
        "literal_value": a.literal_value,
        "value": a.literal_value if a.literal_value is not None else a.object_id,
        "object_id": a.object_id,
        "object_class_id": a.object_class_id,
        "source_url": a.source_url,
        "verified_at": a.verified_at,
        "run_id": a.run_id,
        "confidence": a.confidence,
        "provenance": a.provenance,
    }


def _compare(left: Any, op: str, right: Any) -> bool:
    try:
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op in ("=", "==", "eq"):
            return canonical_literal(left) == canonical_literal(right)
        if op in ("!=", "ne"):
            return canonical_literal(left) != canonical_literal(right)
    except TypeError:
        return False
    raise GraphScopeError(f"unsupported compare op {op!r}")


def project_rows(
    rows: Sequence[dict[str, Any]],
    columns: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """Strip helper debug columns to the gold-defined column set."""
    if not columns:
        return [dict(r) for r in rows]
    return [{c: r.get(c) for c in columns} for r in rows]
