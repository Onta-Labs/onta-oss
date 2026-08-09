"""Live Neo4j smoke for GraphStore (E2.2 / E2.4 / G6).

Skipped unless ``NEO4J_URI`` and ``NEO4J_PASSWORD`` are set **and** the
``neo4j`` package is importable. Default CI without compose stays green.

Run::

    docker compose up -d neo4j
    export NEO4J_URI=bolt://localhost:7687
    export NEO4J_USER=neo4j
    export NEO4J_PASSWORD=onta-dev-password
    pytest -m neo4j -q
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from cograph_client.graph.schema_bootstrap import (
    ENTITY_GET_CYPHER,
    ENTITY_MERGE_CYPHER,
)
from cograph_client.graph.scope import GraphScope, GraphScopeError
from cograph_client.graph.store import env_neo4j_configured

pytestmark = pytest.mark.neo4j


def _neo4j_available() -> bool:
    if not env_neo4j_configured():
        return False
    try:
        import neo4j  # noqa: F401
    except ImportError:
        return False
    return True


requires_neo4j = pytest.mark.skipif(
    not _neo4j_available(),
    reason="NEO4J_URI + NEO4J_PASSWORD and neo4j package required",
)


@pytest.fixture
async def neo4j_store():
    from cograph_client.graph.neo4j_store import Neo4jGraphStore

    uri = os.environ["NEO4J_URI"].strip()
    user = (os.environ.get("NEO4J_USER") or "neo4j").strip() or "neo4j"
    password = os.environ["NEO4J_PASSWORD"]
    database = (os.environ.get("NEO4J_DATABASE") or "").strip() or None
    store = Neo4jGraphStore(uri=uri, user=user, password=password, database=database)
    if not await store.health():
        await store.close()
        pytest.skip("Neo4j not reachable at NEO4J_URI")
    try:
        yield store
    finally:
        await store.close()


@requires_neo4j
@pytest.mark.asyncio
async def test_bootstrap_create_entity_read_back(neo4j_store):
    applied = await neo4j_store.bootstrap_schema()
    assert "entity_tenant_kg_id_unique" in applied
    # Second bootstrap is idempotent (IF NOT EXISTS).
    applied2 = await neo4j_store.bootstrap_schema()
    assert "entity_tenant_kg_id_unique" in applied2

    # Unique per run so re-runs on a dirty volume still pass.
    tenant = f"itest-{uuid.uuid4().hex[:10]}"
    kg = "smoke"
    entity_id = f"https://cograph.tech/entities/Person/smoke-{uuid.uuid4().hex[:8]}"
    scope = GraphScope.for_instance(tenant, kg)
    session = neo4j_store.session(scope)
    ts = datetime.now(timezone.utc).isoformat()

    written = await session.execute_write(
        ENTITY_MERGE_CYPHER,
        {
            "id": entity_id,
            "primary_type": "Person",
            "name": "Smoke Test",
            "source": "neo4j-integration",
            "ts": ts,
            # Overwritten by session:
            "tenant_id": "should-not-land",
            "kg": "should-not-land",
        },
    )
    assert len(written) == 1
    assert written[0]["id"] == entity_id
    assert written[0]["tenant_id"] == tenant
    assert written[0]["kg"] == kg

    rows = await session.execute_read(ENTITY_GET_CYPHER, {"id": entity_id})
    assert len(rows) == 1
    assert rows[0]["name"] == "Smoke Test"
    assert rows[0]["primary_type"] == "Person"

    # Sibling scope must not see the entity.
    other = neo4j_store.session(GraphScope.for_instance(tenant, "other-kg"))
    leak = await other.execute_read(ENTITY_GET_CYPHER, {"id": entity_id})
    assert leak == []


@requires_neo4j
@pytest.mark.asyncio
async def test_live_rejects_unscoped_cypher(neo4j_store):
    session = neo4j_store.session(GraphScope.for_instance("t", "k"))
    with pytest.raises(GraphScopeError):
        await session.execute_read("MATCH (n) RETURN count(n) AS c")
