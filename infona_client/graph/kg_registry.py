"""Knowledge-graph registry for the Neo4j GraphStore path.

SPARQL ``list_kgs`` / ``create_kg`` / ``ensure_kg_registered`` read/write
``onto/kg_name`` in the tenant metadata named graph. On Neo4j there is no
SPARQL — registration is a ``:KnowledgeGraph`` node keyed by
``(tenant_id, name)``.

Hermetic tests use :class:`MemoryGraphStore`, which keeps an in-process map
via the same helpers (duck-typed ``kg_registry_*`` methods).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from infona_client.graph.queries import is_valid_kg_name
from infona_client.graph.store import GraphStore, get_graph_store, graph_backend


@runtime_checkable
class _KgRegistryStore(Protocol):
    async def kg_registry_list(self, tenant_id: str) -> list[dict[str, Any]]: ...
    async def kg_registry_upsert(
        self,
        tenant_id: str,
        name: str,
        *,
        description: str = "",
        triple_count: int | None = None,
        only_if_absent: bool = False,
    ) -> dict[str, Any]: ...
    async def kg_registry_delete(self, tenant_id: str, name: str) -> None: ...


_LIST_CYPHER = """
MATCH (k:KnowledgeGraph {tenant_id: $tenant_id})
RETURN k.name AS name,
       coalesce(k.description, '') AS description,
       coalesce(k.triple_count, 0) AS triple_count
ORDER BY k.name
"""

_UPSERT_CYPHER = """
MERGE (k:KnowledgeGraph {tenant_id: $tenant_id, name: $name})
ON CREATE SET
  k.description = $description,
  k.triple_count = coalesce($triple_count, 0),
  k.created_at = datetime()
ON MATCH SET
  k.description = CASE
    WHEN $only_if_absent THEN k.description
    WHEN $description = '' THEN k.description
    ELSE $description
  END,
  k.triple_count = CASE
    WHEN $triple_count IS NULL THEN k.triple_count
    ELSE $triple_count
  END
RETURN k.name AS name,
       coalesce(k.description, '') AS description,
       coalesce(k.triple_count, 0) AS triple_count
"""

_UPSERT_ABSENT_CYPHER = """
MERGE (k:KnowledgeGraph {tenant_id: $tenant_id, name: $name})
ON CREATE SET
  k.description = $description,
  k.triple_count = coalesce($triple_count, 0),
  k.created_at = datetime()
RETURN k.name AS name,
       coalesce(k.description, '') AS description,
       coalesce(k.triple_count, 0) AS triple_count
"""

_DELETE_CYPHER = """
MATCH (k:KnowledgeGraph {tenant_id: $tenant_id, name: $name})
DETACH DELETE k
"""

_DISTINCT_ENTITY_KG_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id})
WHERE e.kg IS NOT NULL AND e.kg <> ''
RETURN DISTINCT e.kg AS name
"""


def neo4j_kg_registry_active() -> bool:
    return graph_backend() == "neo4j"


async def list_registered_kgs(tenant_id: str) -> list[dict[str, Any]]:
    """Return ``[{name, description, triple_count}, ...]`` for a tenant."""
    store = get_graph_store()
    native = getattr(store, "kg_registry_list", None)
    if callable(native):
        return list(await native(tenant_id))

    run = getattr(store, "_run", None)
    if not callable(run):
        return []

    rows = await run(_LIST_CYPHER, {"tenant_id": tenant_id}, writing=False, database=None)
    by_name: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = str(r.get("name") or "")
        if not name:
            continue
        by_name[name] = {
            "name": name,
            "description": str(r.get("description") or ""),
            "triple_count": int(r.get("triple_count") or 0),
        }

    # Surface KGs that only exist as entity scopes (pre-registry writes).
    try:
        orphan = await run(
            _DISTINCT_ENTITY_KG_CYPHER,
            {"tenant_id": tenant_id},
            writing=False,
            database=None,
        )
        for r in orphan:
            name = str(r.get("name") or "")
            if name and name not in by_name and is_valid_kg_name(name):
                by_name[name] = {
                    "name": name,
                    "description": "",
                    "triple_count": 0,
                }
    except Exception:  # noqa: BLE001 — listing must not fail on orphan probe
        pass

    return [by_name[k] for k in sorted(by_name)]


async def upsert_registered_kg(
    tenant_id: str,
    name: str,
    *,
    description: str = "",
    triple_count: int | None = 0,
    only_if_absent: bool = False,
) -> dict[str, Any]:
    """Create or update a KG registry row. Returns the stored row."""
    if not name or not is_valid_kg_name(name):
        raise ValueError(f"invalid kg name: {name!r}")

    store = get_graph_store()
    native = getattr(store, "kg_registry_upsert", None)
    if callable(native):
        return dict(
            await native(
                tenant_id,
                name,
                description=description,
                triple_count=triple_count,
                only_if_absent=only_if_absent,
            )
        )

    run = getattr(store, "_run", None)
    if not callable(run):
        raise RuntimeError("GraphStore cannot register knowledge graphs")

    cypher = _UPSERT_ABSENT_CYPHER if only_if_absent else _UPSERT_CYPHER
    rows = await run(
        cypher,
        {
            "tenant_id": tenant_id,
            "name": name,
            "description": description or "",
            "triple_count": triple_count,
            "only_if_absent": only_if_absent,
        },
        writing=True,
        database=None,
    )
    if not rows:
        return {
            "name": name,
            "description": description or "",
            "triple_count": int(triple_count or 0),
        }
    r = rows[0]
    return {
        "name": str(r.get("name") or name),
        "description": str(r.get("description") or ""),
        "triple_count": int(r.get("triple_count") or 0),
    }


async def delete_registered_kg(tenant_id: str, name: str) -> None:
    store = get_graph_store()
    native = getattr(store, "kg_registry_delete", None)
    if callable(native):
        await native(tenant_id, name)
        return
    run = getattr(store, "_run", None)
    if not callable(run):
        return
    await run(
        _DELETE_CYPHER,
        {"tenant_id": tenant_id, "name": name},
        writing=True,
        database=None,
    )


async def ensure_kg_registered_store(tenant_id: str, kg_name: str) -> None:
    """Best-effort registry write for the GraphStore path (mirrors SPARQL ensure)."""
    if not kg_name or not is_valid_kg_name(kg_name):
        return
    try:
        await upsert_registered_kg(
            tenant_id,
            kg_name,
            description="",
            triple_count=None,
            only_if_absent=True,
        )
    except Exception:  # noqa: BLE001 — never fail a write on registration
        return
