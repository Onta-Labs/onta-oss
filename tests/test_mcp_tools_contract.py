"""Contract tests for the MCP tools added in COG-129 parity work:
``create_knowledge_graph``, ``delete_knowledge_graph``, ``list_jobs`` and
``get_job``.

Like ``test_mcp_agent_route.py``, the OSS MCP server (``packages/cograph-mcp``)
is a thin TypeScript client over the HTTP API — each tool calls a canonical
route via the ``cograph`` SDK. The tool itself is exercised by the npm
typecheck + build in CI; here we lock the request/response *contract* each tool
depends on, through the FastAPI ``TestClient`` (the same path the SDK hits), with
Neptune mocked so the suite is deterministic and offline.

Tool → backend route (via the SDK):
  * create_knowledge_graph → ``POST   /graphs/{tenant}/kgs``            (SDK createKg)
  * delete_knowledge_graph → ``DELETE /graphs/{tenant}/kgs/{name}``     (SDK deleteKg)
  * list_jobs              → ``GET    /graphs/{tenant}/jobs``           (SDK jobs)
  * get_job                → ``GET    /graphs/{tenant}/enrich/jobs/{id}`` (SDK enrichJob)
  * search                 → ``POST   /graphs/{tenant}/search``         (SDK search, ONTA-178)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("OMNIX_API_KEYS", '{"test-key": "test-tenant"}')
os.environ.setdefault("OMNIX_NEPTUNE_ENDPOINT", "http://fake:8182")

HEADERS = {"X-API-Key": "test-key"}
TENANT = "test-tenant"

_MCP_INDEX = (
    Path(__file__).resolve().parent.parent
    / "packages"
    / "cograph-mcp"
    / "src"
    / "index.ts"
)


def test_create_kg_tool_target_exists(client, mock_neptune, auth_headers):
    """create_knowledge_graph → POST /kgs creates a graph and echoes its name."""
    resp = client.post(
        f"/graphs/{TENANT}/kgs",
        json={"name": "mcp-created-kg", "description": "made by the MCP tool"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "mcp-created-kg"


def test_delete_kg_tool_target_exists(client, mock_neptune, auth_headers):
    """delete_knowledge_graph → DELETE /kgs/{name} is mounted + reachable
    (dispatched, not a 404/405 from a missing route)."""
    resp = client.delete(f"/graphs/{TENANT}/kgs/mcp-created-kg", headers=auth_headers)
    assert resp.status_code in (200, 202, 204), resp.text


def test_list_jobs_tool_target_returns_a_list(client, mock_neptune, auth_headers):
    """list_jobs → GET /jobs returns a JSON array (empty when no jobs)."""
    resp = client.get(f"/graphs/{TENANT}/jobs", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


def test_list_jobs_tool_accepts_category_filter(client, mock_neptune, auth_headers):
    """The `category` arg the tool forwards is a valid query param on /jobs."""
    resp = client.get(
        f"/graphs/{TENANT}/jobs", params={"category": "enrichment"}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


def test_get_job_tool_target_404s_for_unknown_id(client, mock_neptune, auth_headers):
    """get_job → GET /enrich/jobs/{id} is mounted and 404s for a missing id
    (proving the route exists and is owner-scoped, not that it's unreachable)."""
    resp = client.get(f"/graphs/{TENANT}/enrich/jobs/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404, resp.text


def test_search_tool_target_contract(monkeypatch, client, auth_headers):
    """search → POST /graphs/{tenant}/search returns the exact envelope the
    MCP tool renders: hits[{entity_uri, attrs, snippet, attr, score}] + count +
    degraded + top_k. The tool forwards query/kg_name/type/top_k verbatim —
    this test proves those are the route's accepted body fields (ONTA-178).
    Deeper behavior (clamping, filters, auth) is locked in
    ``tests/test_search_route.py``."""
    import asyncio

    from cograph_client.semantic.extract import content_hash
    from cograph_client.semantic.memory import InMemorySemanticIndex
    from cograph_client.semantic.protocol import SemanticChunk
    from cograph_client.semantic.registry import (
        register_semantic_index,
        reset_semantic_index,
    )

    monkeypatch.setenv("COGRAPH_SEMANTIC_INDEX_ENABLED", "true")
    reset_semantic_index()
    index = InMemorySemanticIndex()
    register_semantic_index(index)
    try:
        text = "Rooftop solar subsidies for residential homes."
        asyncio.run(
            index.upsert_chunks(
                [
                    SemanticChunk(
                        tenant_id=TENANT,
                        kg_name="mcp-kg",
                        entity_uri="e:solar",
                        attr="description",
                        chunk_ix=0,
                        chunk_text=text,
                        content_hash=content_hash(text),
                        attrs={"label": "Solar", "type": "Report"},
                    )
                ]
            )
        )
        resp = client.post(
            f"/graphs/{TENANT}/search",
            json={
                "query": "solar subsidies",
                "kg_name": "mcp-kg",
                "type": "Report",
                "top_k": 5,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {"hits", "count", "degraded", "top_k"}
        assert body["count"] == len(body["hits"]) == 1
        hit = body["hits"][0]
        assert set(hit) == {"entity_uri", "attrs", "snippet", "attr", "score"}
        assert hit["entity_uri"] == "e:solar"
    finally:
        reset_semantic_index()


def test_search_tool_disabled_deployment_degrades_not_503(monkeypatch, client, auth_headers):
    """With the semantic index gate off, the route degrades to lexical keyword
    search (200 + degraded=true) instead of 503 — so the MCP `search` tool
    still returns a usable, honestly-badged result instead of a hard failure.
    The response keeps the canonical envelope the tool renders."""
    monkeypatch.delenv("COGRAPH_SEMANTIC_INDEX_ENABLED", raising=False)
    resp = client.post(
        f"/graphs/{TENANT}/search", json={"query": "anything"}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"hits", "count", "degraded", "top_k"}
    assert body["degraded"] is True


# --- list_jobs discovery category (ONTA-243) --------------------------------- #


def test_list_jobs_tool_accepts_discovery_category(client, mock_neptune, auth_headers):
    """The `category` arg forwards `discovery` to /jobs as a valid query param —
    the value the MCP enum used to omit, which made a web-ingest job unfindable via
    the category filter (the persona's `category:'enrichment'` guess filtered it OUT)."""
    resp = client.get(
        f"/graphs/{TENANT}/jobs", params={"category": "discovery"}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


def _mcp_list_jobs_categories() -> set[str]:
    """Parse the `JOB_CATEGORIES` runtime array the MCP `list_jobs` tool enum is
    built from, straight out of the TypeScript source. Deliberately reads the SOURCE
    (not a built artifact) so this guard runs with no npm build in CI."""
    src = _MCP_INDEX.read_text()
    m = re.search(r"const\s+JOB_CATEGORIES\s*=\s*\[(.*?)\]\s*as\s+const", src, re.DOTALL)
    assert m, "could not find JOB_CATEGORIES array in the MCP index.ts"
    return set(re.findall(r'"([a-z_]+)"', m.group(1)))


def test_mcp_list_jobs_enum_matches_backend_job_category():
    """Drift guard (ONTA-243): the MCP `list_jobs` category enum MUST equal the
    backend `JobCategory` set — no more, no less. A missing member silently hides
    that category's jobs from the agent (the exact `discovery`-omitted bug). The
    TypeScript side also enforces this at compile time via an exhaustiveness check
    against the SDK's `JobCategory`; this asserts it from Python so a backend change
    that forgets the MCP enum fails CI here too."""
    from cograph_client.enrichment.models import JobCategory

    backend = {c.value for c in JobCategory}
    assert _mcp_list_jobs_categories() == backend, (
        "MCP list_jobs enum drifted from backend JobCategory: "
        f"mcp={_mcp_list_jobs_categories()} backend={backend}"
    )
    # Explicit: discovery is present on both sides (the regression this guards).
    assert "discovery" in backend
    assert "discovery" in _mcp_list_jobs_categories()
