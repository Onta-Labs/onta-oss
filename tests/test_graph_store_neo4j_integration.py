"""Live Neo4j smoke for GraphStore (E2.2 / E2.4 / G6).

Skipped unless ``NEO4J_URI`` and ``NEO4J_PASSWORD`` are set **and** the
``neo4j`` package is importable. Default CI without compose stays green.

Run::

    docker compose up -d neo4j
    export NEO4J_URI=bolt://localhost:7687
    export NEO4J_USER=neo4j
    export NEO4J_PASSWORD=infona-dev-password
    pytest -m neo4j -q
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from infona_client.graph.schema_bootstrap import (
    ENTITY_GET_CYPHER,
    ENTITY_MERGE_CYPHER,
)
from infona_client.graph.scope import GraphScope, GraphScopeError
from infona_client.graph.store import env_neo4j_configured, require_entity_write_identity

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
    from infona_client.graph.neo4j_store import Neo4jGraphStore

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
    entity_id = f"https://graph.infona.ai/entities/Person/smoke-{uuid.uuid4().hex[:8]}"
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


@requires_neo4j
@pytest.mark.asyncio
async def test_live_cross_tenant_isolation(neo4j_store):
    """F4: live Neo4j — tenant A must not read tenant B's entity with same id."""
    await neo4j_store.bootstrap_schema()
    shared_kg = "iso-kg"
    entity_id = f"https://graph.infona.ai/entities/Thing/iso-{uuid.uuid4().hex[:8]}"
    tenant_a = f"iso-a-{uuid.uuid4().hex[:8]}"
    tenant_b = f"iso-b-{uuid.uuid4().hex[:8]}"
    ts = datetime.now(timezone.utc).isoformat()

    sess_a = neo4j_store.session(GraphScope.for_instance(tenant_a, shared_kg))
    sess_b = neo4j_store.session(GraphScope.for_instance(tenant_b, shared_kg))

    await sess_a.execute_template(
        "entity_merge",
        {
            "id": entity_id,
            "primary_type": "Thing",
            "name": "tenant-a-only",
            "source": "iso-test",
            "ts": ts,
        },
    )
    leak = await sess_b.execute_template("entity_get", {"id": entity_id})
    assert leak == []

    await sess_b.execute_template(
        "entity_merge",
        {
            "id": entity_id,
            "primary_type": "Thing",
            "name": "tenant-b-only",
            "source": "iso-test",
            "ts": ts,
        },
    )
    a_rows = await sess_a.execute_template("entity_get", {"id": entity_id})
    b_rows = await sess_b.execute_template("entity_get", {"id": entity_id})
    assert a_rows[0]["name"] == "tenant-a-only"
    assert b_rows[0]["name"] == "tenant-b-only"
    assert a_rows[0]["tenant_id"] == tenant_a
    assert b_rows[0]["tenant_id"] == tenant_b


@requires_neo4j
@pytest.mark.asyncio
async def test_live_red_team_param_only_bypass_rejected(neo4j_store):
    session = neo4j_store.session(GraphScope.for_instance("t", "k"))
    bypass = "MATCH (n) WHERE $tenant_id = $tenant_id AND $kg = $kg RETURN n"
    with pytest.raises(GraphScopeError, match="property keys|execute_template"):
        await session.execute_read(bypass)


@requires_neo4j
@pytest.mark.asyncio
async def test_live_entity_merge_missing_id_fail_closed(neo4j_store):
    session = neo4j_store.session(GraphScope.for_instance("t", "k"))
    with pytest.raises(GraphScopeError, match="non-empty id"):
        await session.execute_template(
            "entity_merge",
            {
                "primary_type": "T",
                "name": "n",
                "source": "s",
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )
    # Helper itself is the contract (also covered hermetically).
    with pytest.raises(GraphScopeError, match="non-empty id"):
        require_entity_write_identity({})