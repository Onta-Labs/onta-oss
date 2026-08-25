"""GraphStore ingest-batch rollback (ONTA-528).

Housekeeping ``onto/batch_id`` classifies as a literal Entity / Assertion
property (``graph.facts`` onto_leaf). On ingest failure, those subjects in
this KG are removed via :func:`delete_facts` — never SPARQL HTTP update.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

import structlog

from infona_client.graph.assertion_model import property_uri
from infona_client.graph.kg_writer import (
    _resolve_graph_session,
    delete_facts,
    refresh_after_write,
)
from infona_client.graph.queries import parse_kg_graph_uri
from infona_client.graph.store import GraphStoreError

if TYPE_CHECKING:
    from infona_client.graph.store import GraphSession, GraphStore

logger = structlog.stdlib.get_logger("infona.resolver.batch_rollback")

# Same leaf classify_triple uses for ``…/onto/batch_id``.
_BATCH_LEAF = "batch_id"

_ENTITY_BY_BATCH_CYPHER = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})\n"
    "WHERE e.batch_id = $batch_id\n"
    "RETURN e.id AS id"
)


def _as_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    to_dict = getattr(row, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    get = getattr(row, "get", None)
    if callable(get):
        return {
            "id": get("id"),
            "subject_id": get("subject_id"),
            "literal_value": get("literal_value"),
        }
    return {}


def _literal_matches(value: Any, batch_id: str) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        return any(_literal_matches(v, batch_id) for v in value)
    return str(value) == batch_id


async def _subjects_from_assertions(
    session: "GraphSession", batch_id: str
) -> set[str]:
    """Assertion SoT: datatype assertions whose leaf is ``batch_id``."""
    found: set[str] = set()
    read_hist = getattr(session, "read_assertion_history", None)
    if not callable(read_hist):
        return found
    rows = await read_hist(prop_id=property_uri(_BATCH_LEAF), limit=10000)
    for row in rows:
        data = _as_dict(row)
        if not _literal_matches(data.get("literal_value"), batch_id):
            continue
        sid = data.get("subject_id")
        if sid:
            found.add(str(sid))
    return found


async def _subjects_from_entity_props(
    session: "GraphSession", batch_id: str
) -> set[str]:
    """Entity property cache dual-written from the same housekeeping literal."""
    found: set[str] = set()
    try:
        rows = await session.execute_read(
            _ENTITY_BY_BATCH_CYPHER, {"batch_id": batch_id}
        )
    except GraphStoreError:
        rows = None
    if rows is not None:
        for row in rows:
            eid = _as_dict(row).get("id")
            if eid:
                found.add(str(eid))
        return found
    store = getattr(session, "_store", None)
    scope = getattr(session, "scope", None)
    snapshot = getattr(store, "snapshot_entities", None)
    if not callable(snapshot) or scope is None:
        return found
    for row in snapshot():
        if row.get("tenant_id") != scope.tenant_id or row.get("kg") != scope.kg:
            continue
        props = row.get("props") or {}
        if _literal_matches(props.get(_BATCH_LEAF), batch_id):
            found.add(str(row["id"]))
    return found


async def subjects_for_batch(session: "GraphSession", batch_id: str) -> list[str]:
    """Subject IRIs in this session's KG stamped with ``batch_id``."""
    found = await _subjects_from_assertions(session, batch_id)
    found |= await _subjects_from_entity_props(session, batch_id)
    return sorted(found)


async def rollback_ingest_batch(
    instance_graph: str,
    batch_id: str,
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    neptune: Any = None,
) -> int:
    """Delete every subject in ``instance_graph`` tagged with ``batch_id``.

    Returns the ``delete_facts`` removal count. No-op when ``batch_id`` is
    empty or no matching subjects exist. Never issues SPARQL HTTP update.
    """
    if not batch_id or not instance_graph:
        return 0
    gs = _resolve_graph_session(
        store=store, session=session, instance_graph=instance_graph
    )
    subjects = await subjects_for_batch(gs, batch_id)
    if not subjects:
        logger.info(
            "batch_rollback_noop",
            batch_id=batch_id,
            instance_graph=instance_graph,
        )
        return 0
    removed = await delete_facts(
        neptune,
        instance_graph,
        subjects=subjects,
        store=store,
        session=gs,
        reason="ingest_batch_rollback",
    )
    scope = parse_kg_graph_uri(instance_graph)
    if scope is not None:
        tenant_id, kg_name = scope
        try:
            await refresh_after_write(
                None,
                tenant_id=tenant_id,
                kg_name=kg_name,
                deleted_subjects=subjects,
                store=store,
                session=gs,
                recompute_stats=False,
            )
        except Exception:  # noqa: BLE001 — rollback must still report delete
            logger.warning(
                "batch_rollback_refresh_failed",
                batch_id=batch_id,
                instance_graph=instance_graph,
                exc_info=True,
            )
    logger.info(
        "batch_rollback",
        batch_id=batch_id,
        instance_graph=instance_graph,
        subjects=len(subjects),
        removed=removed,
    )
    return removed
