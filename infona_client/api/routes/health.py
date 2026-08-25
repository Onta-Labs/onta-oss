"""Readiness for the API process.

Neo4j is the only graph backend (ONTA-527), so health is exactly "does the
GraphStore answer". The ``backend`` field is kept in the body — deploy smoke
checks and the ops runbook assert on it — but it is a constant, not a report
of which of several stores got selected.

HTTP contract: **200** only when Neo4j is up. A down graph used to return
**200** with ``status: degraded``, so an ALB health check kept routing
Explorer traffic into Bolt connect timeouts (2026-08-24: API pinned to a
stale Neo4j ENI IP). Degraded now returns **503** so the load balancer
stops sending work at a task that cannot answer. The probe still never
raises (no 500 / restart loop): a missing store is 503 + ``neo4j: false``.
"""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from infona_client.graph.bolt_uri import classify_bolt_uri
from infona_client.graph.store import NEO4J_BACKEND

router = APIRouter()


def _uri_kind() -> str:
    try:
        return classify_bolt_uri(os.environ.get("NEO4J_URI"))
    except Exception:  # noqa: BLE001 — kind is diagnostic, never 500 the probe
        return "missing"


@router.get(
    "/health",
    summary="Health",
    description=(
        "Graph-store readiness. **200** only when Neo4j answers; **503** with "
        "`status: degraded` when it does not (so a load balancer stops routing). "
        "`neo4j_uri_kind` is `hostname` / `private_ip` / `loopback` / "
        "`public_ip` / `missing` — never the raw host."
    ),
    responses={
        200: {"description": "Graph store up"},
        503: {"description": "Graph store down or unconfigured"},
    },
)
async def health():
    neo4j_ok = False
    try:
        from infona_client.graph.store import get_graph_store

        store = get_graph_store()
        neo4j_ok = bool(await store.health())
    except Exception:  # noqa: BLE001 — never crash the probe
        neo4j_ok = False
    body = {
        "status": "healthy" if neo4j_ok else "degraded",
        "backend": NEO4J_BACKEND,
        "neo4j": neo4j_ok,
        # Kind only — never the raw host. Hosted must be ``hostname``
        # (Cloud Map). ``private_ip`` is the pin that dies on ENI replace.
        "neo4j_uri_kind": _uri_kind(),
    }
    return JSONResponse(status_code=200 if neo4j_ok else 503, content=body)
