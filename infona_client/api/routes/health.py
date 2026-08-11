"""Liveness / readiness for the API process.

Neo4j is the only graph backend (ONTA-527), so health is exactly "does the
GraphStore answer". The ``backend`` field is kept in the body — deploy smoke
checks and the ops runbook assert on it — but it is now a constant, not a
report of which of several stores got selected.
"""

from __future__ import annotations

from fastapi import APIRouter

from infona_client.graph.store import NEO4J_BACKEND

router = APIRouter()


@router.get("/health")
async def health():
    neo4j_ok = False
    try:
        from infona_client.graph.store import get_graph_store

        store = get_graph_store()
        neo4j_ok = bool(await store.health())
    except Exception:  # noqa: BLE001 — never crash the probe
        neo4j_ok = False
    return {
        "status": "healthy" if neo4j_ok else "degraded",
        "backend": NEO4J_BACKEND,
        "neo4j": neo4j_ok,
    }
