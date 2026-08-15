"""GraphSession async helpers for ADR 0013 semantic queries.

Implementation sibling of :mod:`infona_client.graph.rdfs_helpers`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from infona_client.graph.rdfs_helpers_templates import (
    ASSERTIONS_FOR_SUBJECT_CYPHER,
    CLASS_SUBCLASS_DESCENDANTS_CYPHER,
    SUBPROPERTY_DESCENDANTS_CYPHER,
)

if TYPE_CHECKING:
    from infona_client.graph.store import GraphSession

# Typing alias for duck-typed assertion maps
MappingLike = Any

# ---------------------------------------------------------------------------
# GraphSession async helpers (Memory / Neo4j — native methods preferred)
# ---------------------------------------------------------------------------


async def subclass_closure(
    session: "GraphSession",
    class_id: str,
    *,
    include_self: bool = True,
) -> list[str]:
    """Class IRIs: query class plus descendants (Person ⊑ Agent → Person in Agent)."""
    native = getattr(session, "read_subclass_closure", None)
    if callable(native):
        ids = list(await native(class_id))
    else:
        rows = await session.execute_read(
            CLASS_SUBCLASS_DESCENDANTS_CYPHER, {"class_id": class_id}
        )
        ids = [str(r.get("id")) for r in rows if r.get("id")]
    if include_self and class_id not in ids:
        ids = [class_id, *ids]
    if not include_self:
        ids = [i for i in ids if i != class_id]
    return ids


async def subproperty_closure(
    session: "GraphSession",
    prop_id: str,
    *,
    include_self: bool = True,
) -> list[str]:
    """Property IRIs: query property plus sub-properties."""
    native = getattr(session, "read_subproperty_closure", None)
    if callable(native):
        ids = list(await native(prop_id))
    else:
        rows = await session.execute_read(
            SUBPROPERTY_DESCENDANTS_CYPHER, {"prop_id": prop_id}
        )
        ids = [str(r.get("id")) for r in rows if r.get("id")]
    if include_self and prop_id not in ids:
        ids = [prop_id, *ids]
    if not include_self:
        ids = [i for i in ids if i != prop_id]
    return ids


async def session_entities_of_type(
    session: "GraphSession",
    class_id: str,
    *,
    include_subclasses: bool = True,
) -> list[str]:
    """Entity IRIs typed as ``class_id`` (optional subclass closure)."""
    if include_subclasses:
        class_ids = await subclass_closure(session, class_id, include_self=True)
    else:
        class_ids = [class_id]
    native = getattr(session, "read_entities_of_type", None)
    if callable(native):
        return list(await native(class_ids))
    return []


async def session_assertions_for_subject(
    session: "GraphSession",
    entity_id: str,
    *,
    prop_id: str | None = None,
) -> list[dict[str, Any]]:
    """Assertion dicts for one subject within session scope."""
    native = getattr(session, "read_assertions_for_subject", None)
    if callable(native):
        return list(await native(entity_id, prop_id=prop_id))
    rows = await session.execute_read(
        ASSERTIONS_FOR_SUBJECT_CYPHER,
        {"entity_id": entity_id, "prop_id": prop_id},
    )
    return [r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in rows]


def assertion_to_history_row(row: MappingLike) -> dict[str, Any]:
    """Project an Assertion provenance dict into the GET ``/history`` change shape.

    Neo4j has no companion value-history graph yet (temporal ``old → new``
    :ValueHistory is deferred). Until that lands, the store path treats
    **current Assertion provenance** as the history feed:

    * ``subject`` / ``predicate`` — Assertion subject + property IRIs
    * ``new_value`` — current literal / object id
    * ``old_value`` — empty (no prior-value log on Assertion SoT alone)
    * ``changed_at`` — ``verified_at`` when present, else empty

    Same keys as :class:`infona_client.graph.history.ValueChange` so the dual-
    backend route can reuse one response builder.
    """
    if hasattr(row, "to_dict"):
        row = row.to_dict()  # type: ignore[assignment]
    data = dict(row) if not isinstance(row, dict) else row
    value = data.get("literal_value")
    if value is None:
        value = data.get("object_id") or data.get("object_class_id") or ""
    return {
        "subject": str(data.get("subject_id") or ""),
        "predicate": str(data.get("property_id") or ""),
        "old_value": "",
        "new_value": "" if value is None else str(value),
        "changed_at": str(data.get("verified_at") or ""),
        # Provenance extras (route may omit; helpers / clients may use):
        "source_url": data.get("source_url"),
        "provenance": data.get("provenance"),
        "assertion_id": data.get("assertion_id") or data.get("id"),
    }


def _since_passes(verified_at: str | None, since: str | None) -> bool:
    """``True`` when the row is strictly after ``since`` (Neptune FILTER ``>``)."""
    if not since:
        return True
    va = (verified_at or "").strip()
    if not va:
        return False
    return va > since


async def session_assertion_history(
    session: "GraphSession",
    *,
    entity_id: str | None = None,
    prop_id: str | None = None,
    since: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """List Assertion provenance as history-shaped rows (neo4j dual-backend).

    Preferred path: subject-scoped via :func:`session_assertions_for_subject`
    (or native ``read_assertion_history`` when the session implements a full-KG
    scan). Without a subject and without a native scan, returns ``[]`` — never
    invents cross-scope rows.
    """
    lim = max(1, min(int(limit), 10000))
    native = getattr(session, "read_assertion_history", None)
    raw_rows: list[Any]
    if callable(native):
        raw_rows = list(
            await native(
                entity_id=entity_id,
                prop_id=prop_id,
                since=since,
                limit=lim,
            )
        )
    elif entity_id:
        raw_rows = await session_assertions_for_subject(
            session, entity_id, prop_id=prop_id
        )
    else:
        return []

    projected: list[dict[str, Any]] = []
    for r in raw_rows:
        d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        if prop_id is not None and d.get("property_id") != prop_id:
            continue
        if not _since_passes(d.get("verified_at"), since):
            continue
        projected.append(assertion_to_history_row(d))

    projected.sort(
        key=lambda x: (
            x.get("changed_at") or "",
            x.get("predicate") or "",
            x.get("subject") or "",
            str(x.get("assertion_id") or ""),
        )
    )
    return projected[:lim]


async def session_literal_values(
    session: "GraphSession",
    entity_id: str,
    prop_id: str,
) -> list[Any]:
    rows = await session_assertions_for_subject(session, entity_id, prop_id=prop_id)
    return [r["literal_value"] for r in rows if r.get("literal_value") is not None]


async def session_object_values(
    session: "GraphSession",
    entity_id: str,
    prop_id: str,
) -> list[str]:
    rows = await session_assertions_for_subject(session, entity_id, prop_id=prop_id)
    return [str(r["object_id"]) for r in rows if r.get("object_id")]

