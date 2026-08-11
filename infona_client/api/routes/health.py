"""Liveness / readiness for the API process.

When ``INFONA_GRAPH_BACKEND=neo4j``, health is driven by the GraphStore
(Neo4j). Neptune is not required. When the backend is Neptune (default on
legacy deploys), health still probes the SPARQL endpoint.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request

from infona_client.api.deps import get_neptune_client
from infona_client.graph.client import NeptuneClient

router = APIRouter()


def _graph_backend() -> str:
    return (os.environ.get("INFONA_GRAPH_BACKEND") or "neptune").strip().lower()


@router.get("/health")
async def health(request: Request):
    backend = _graph_backend()
    body: dict = {"status": "healthy", "backend": backend}

    if backend == "neo4j":
        try:
            from infona_client.graph.store import get_graph_store

            store = get_graph_store()
            neo4j_ok = bool(await store.health())
        except Exception:  # noqa: BLE001 — never crash the probe
            neo4j_ok = False
        body["neo4j"] = neo4j_ok
        body["status"] = "healthy" if neo4j_ok else "degraded"
        return body

    # Neptune / Fuseki path
    try:
        client: NeptuneClient = get_neptune_client(request)
        neptune_ok = await client.health()
    except Exception:  # noqa: BLE001
        neptune_ok = False
    body["neptune"] = neptune_ok
    body["status"] = "healthy" if neptune_ok else "degraded"
    return body
