"""Property-graph instance mutations for GraphSession (E3 / model §8).

All instance writes go through these helpers (called only from
:mod:`cograph_client.graph.kg_writer`). Callers never hand-build Cypher.

Strategy:
* Prefer **session-native** methods when the store implements them
  (``MemoryGraphStore`` — hermetic tests; ``Neo4jGraphStore`` — live).
* Fall back to allowlisted :meth:`GraphSession.execute_template` where a static
  template exists (``entity_merge`` / ``entity_get``).
* Dynamic prop keys / rel types use sanitizers from :mod:`facts` + :mod:`labels`
  so tokens are never free-form user strings.

Scope is always forced by the session. Missing entity ``id`` fails closed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from cograph_client.graph.facts import (
    Fact,
    group_facts_by_subject,
    primary_type_from_facts,
    sanitize_prop_key,
    sanitize_rel_type,
)
from cograph_client.graph.labels import sanitize_domain_labels, set_entity_type_labels
from cograph_client.graph.scope import GraphScopeError
from cograph_client.graph.store import require_entity_write_identity

if TYPE_CHECKING:
    from cograph_client.graph.store import GraphRecord, GraphSession


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def merge_entity(
    session: "GraphSession",
    entity_id: str,
    *,
    primary_type: str | None = None,
    name: str | None = None,
    source: str | None = None,
    ts: str | None = None,
) -> list["GraphRecord"]:
    """MERGE ``:Entity`` by ``(tenant_id, kg, id)`` (model §1 / §7)."""
    require_entity_write_identity({"id": entity_id})
    native = getattr(session, "write_merge_entity", None)
    params = {
        "id": entity_id,
        "primary_type": primary_type,
        "name": name,
        "source": source,
        "ts": ts or _ts(),
    }
    if callable(native):
        return await native(**params)
    return await session.execute_template("entity_merge", params)


async def set_literal(
    session: "GraphSession",
    entity_id: str,
    leaf: str,
    value: Any,
    *,
    multi_union: bool = True,
) -> list["GraphRecord"]:
    """Set an entity-scoped literal property (model §2.1 / §2.3).

    ``name`` / ``source`` map to reserved Entity display/source props.
    Other leaves go through :func:`sanitize_prop_key`. Multi-value = list union
    when ``multi_union`` is True and a second value arrives for the same key.
    """
    require_entity_write_identity({"id": entity_id})
    if leaf in ("name", "source", "primary_type"):
        prop_key = leaf
    else:
        prop_key = sanitize_prop_key(leaf)
    native = getattr(session, "write_set_literal", None)
    if callable(native):
        return await native(
            entity_id, prop_key, value, multi_union=multi_union, original_leaf=leaf
        )
    raise GraphScopeError(
        "GraphSession does not implement write_set_literal; use MemoryGraphStore "
        "or Neo4jGraphStore for property-graph instance writes"
    )


async def merge_rel(
    session: "GraphSession",
    start_id: str,
    end_id: str,
    attr_leaf: str,
) -> list["GraphRecord"]:
    """MERGE a typed relationship with B4 identity key + ``attr`` property.

    MERGE key = ``(start.id, end.id, rel type, tenant_id, kg)``.
    """
    require_entity_write_identity({"id": start_id})
    require_entity_write_identity({"id": end_id})
    if not attr_leaf or not str(attr_leaf).strip():
        raise GraphScopeError("Relationship attr leaf must be non-empty")
    rel_type = sanitize_rel_type(attr_leaf)
    native = getattr(session, "write_merge_rel", None)
    if callable(native):
        return await native(start_id, end_id, rel_type, attr_leaf)
    raise GraphScopeError(
        "GraphSession does not implement write_merge_rel; use MemoryGraphStore "
        "or Neo4jGraphStore for property-graph instance writes"
    )


async def delete_entity(session: "GraphSession", entity_id: str) -> int:
    """Delete an Entity node and its incident relationships within session scope."""
    require_entity_write_identity({"id": entity_id})
    native = getattr(session, "write_delete_entity", None)
    if callable(native):
        return int(await native(entity_id))
    raise GraphScopeError(
        "GraphSession does not implement write_delete_entity; use MemoryGraphStore "
        "or Neo4jGraphStore for property-graph instance writes"
    )


async def delete_literals(
    session: "GraphSession",
    entity_id: str,
    leaves: Sequence[str],
) -> int:
    """Remove named literal properties from an Entity (predicate-scoped clear)."""
    require_entity_write_identity({"id": entity_id})
    keys: list[str] = []
    for leaf in leaves:
        if leaf in ("name", "source", "primary_type"):
            keys.append(leaf)
        else:
            keys.append(sanitize_prop_key(leaf))
    native = getattr(session, "write_delete_literals", None)
    if callable(native):
        return int(await native(entity_id, keys))
    raise GraphScopeError(
        "GraphSession does not implement write_delete_literals"
    )


async def delete_rels(
    session: "GraphSession",
    *,
    start_id: str | None = None,
    end_id: str | None = None,
    attr_leaf: str | None = None,
    end_id_exact: str | None = None,
) -> int:
    """Delete relationships in scope filtered by start / end / attr leaf.

    ``end_id_exact`` is the object endpoint when deleting a concrete edge;
    ``end_id`` is kept as an alias for the same filter.
    """
    end = end_id_exact if end_id_exact is not None else end_id
    rel_type = sanitize_rel_type(attr_leaf) if attr_leaf else None
    native = getattr(session, "write_delete_rels", None)
    if callable(native):
        return int(
            await native(
                start_id=start_id,
                end_id=end,
                rel_type=rel_type,
                attr_leaf=attr_leaf,
            )
        )
    raise GraphScopeError("GraphSession does not implement write_delete_rels")


async def rewrite_entity_id(
    session: "GraphSession",
    old_id: str,
    new_id: str,
) -> None:
    """Re-key Entity ``id`` and rebind relationship endpoints (not delete+insert)."""
    require_entity_write_identity({"id": old_id})
    require_entity_write_identity({"id": new_id})
    if old_id == new_id:
        return
    native = getattr(session, "write_rewrite_entity_id", None)
    if callable(native):
        await native(old_id, new_id)
        return
    raise GraphScopeError(
        "GraphSession does not implement write_rewrite_entity_id"
    )


async def create_prov_event(
    session: "GraphSession",
    *,
    event_type: str,
    subject_id: str,
    attr: str | None = None,
    object_repr: str | None = None,
    old_id: str | None = None,
    new_id: str | None = None,
    reason: str = "",
    source: str | None = None,
) -> None:
    """Minimal ``:ProvEvent`` + ``[:ABOUT]->(:Entity)`` (model §4.1). Best-effort caller."""
    native = getattr(session, "write_prov_event", None)
    if not callable(native):
        return  # optional on stores that do not implement companions yet
    await native(
        event_type=event_type,
        subject_id=subject_id,
        attr=attr,
        object_repr=object_repr,
        old_id=old_id,
        new_id=new_id,
        reason=reason,
        source=source,
        ts=_ts(),
    )


async def apply_facts(
    session: "GraphSession",
    facts: Sequence[Fact],
    *,
    provenance_enabled: bool = False,
) -> int:
    """Apply a batch of Facts: MERGE entities, types/labels, literals, rels.

    Returns the number of Facts applied. Ensures target entities exist for rels.
    """
    if not facts:
        return 0
    grouped = group_facts_by_subject(facts)
    applied = 0

    # First pass: ensure all subjects (and rel targets) exist as Entity nodes.
    target_ids: set[str] = set(grouped)
    for f in facts:
        if f.kind == "rel" and isinstance(f.value, str) and f.value:
            target_ids.add(f.value)

    for sid in target_ids:
        sub_facts = grouped.get(sid, [])
        primary = primary_type_from_facts(sub_facts)
        name = None
        source = None
        for f in sub_facts:
            if f.kind == "literal" and f.key == "name" and f.value is not None:
                name = f.value
            if f.kind == "literal" and f.key == "source" and f.value is not None:
                source = f.value
            if f.source:
                source = f.source
        await merge_entity(
            session, sid, primary_type=primary, name=name, source=source
        )

    # Second pass: types (labels), literals, rels per subject.
    for sid, sub_facts in grouped.items():
        type_leaves = [f.key for f in sub_facts if f.kind == "type"]
        if type_leaves:
            safe = sanitize_domain_labels(type_leaves)
            await set_entity_type_labels(session, sid, safe)
            applied += len(type_leaves)

        for f in sub_facts:
            if f.kind == "literal":
                if f.key == "name":
                    await merge_entity(session, sid, name=f.value)
                elif f.key == "source":
                    await merge_entity(session, sid, source=f.value)
                else:
                    await set_literal(session, sid, f.key, f.value, multi_union=True)
                applied += 1
                if provenance_enabled:
                    await create_prov_event(
                        session,
                        event_type="assert",
                        subject_id=sid,
                        attr=f.key,
                        object_repr=str(f.value) if f.value is not None else None,
                        source=f.source,
                    )
            elif f.kind == "rel":
                if not isinstance(f.value, str) or not f.value:
                    raise GraphScopeError(
                        f"rel Fact requires target entity id string, got {f.value!r}"
                    )
                await merge_rel(session, sid, f.value, f.key)
                applied += 1
                if provenance_enabled:
                    await create_prov_event(
                        session,
                        event_type="assert",
                        subject_id=sid,
                        attr=f.key,
                        object_repr=f.value,
                        source=f.source,
                    )
            # type facts already counted above
    return applied


async def get_entity(
    session: "GraphSession", entity_id: str
) -> Mapping[str, Any] | None:
    """Fetch one entity record (test/helper)."""
    require_entity_write_identity({"id": entity_id})
    native = getattr(session, "write_get_entity", None)
    if callable(native):
        return await native(entity_id)
    rows = await session.execute_template("entity_get", {"id": entity_id})
    if not rows:
        return None
    return rows[0].to_dict()


__all__ = [
    "apply_facts",
    "create_prov_event",
    "delete_entity",
    "delete_literals",
    "delete_rels",
    "get_entity",
    "merge_entity",
    "merge_rel",
    "rewrite_entity_id",
    "set_literal",
]
