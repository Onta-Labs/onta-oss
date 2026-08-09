"""Wave 1 / E2 — GraphStore protocol, scope enforcement, in-memory store.

Hermetic: no Neo4j process required. Live-driver smoke lives in
``test_graph_store_neo4j_integration.py`` (``@pytest.mark.neo4j``).
"""

from __future__ import annotations

import pytest

from cograph_client.graph.memory_store import MemoryGraphStore
from cograph_client.graph.queries import InvalidKGName, InvalidTenantId
from cograph_client.graph.schema_bootstrap import (
    ENTITY_GET_CYPHER,
    ENTITY_LIST_BY_TYPE_CYPHER,
    ENTITY_MERGE_CYPHER,
    SCHEMA_STATEMENTS,
    bootstrap_schema_statements,
)
from cograph_client.graph.scope import (
    ENHANCED_KG,
    GLOBAL_TENANT_ID,
    ONTOLOGY_KG,
    PUBLIC_KG,
    GraphScope,
    GraphScopeError,
)
from cograph_client.graph.store import (
    GraphConfigError,
    GraphQueryError,
    GraphRecord,
    assert_cypher_is_scoped,
    configure_graph_store,
    env_neo4j_configured,
    get_graph_store,
    merge_scope_params,
    require_entity_write_identity,
    reset_graph_store_for_tests,
    scrub_store_detail,
)


# ---------------------------------------------------------------------------
# Scope construction
# ---------------------------------------------------------------------------


def test_graph_scope_for_instance_ok():
    s = GraphScope.for_instance("demo-tenant", "bookstore")
    assert s.tenant_id == "demo-tenant"
    assert s.kg == "bookstore"
    assert s.privileged is False
    assert s.as_params() == {"tenant_id": "demo-tenant", "kg": "bookstore"}


def test_graph_scope_for_instance_rejects_global_tenant():
    with pytest.raises(GraphScopeError, match="global catalog"):
        GraphScope.for_instance(GLOBAL_TENANT_ID, "bookstore")


def test_graph_scope_for_instance_rejects_ontology_kg():
    with pytest.raises(GraphScopeError, match="reserved kg"):
        GraphScope.for_instance("demo-tenant", ONTOLOGY_KG)


def test_graph_scope_for_instance_rejects_bad_kg():
    with pytest.raises(InvalidKGName):
        GraphScope.for_instance("demo-tenant", "bad kg!")


def test_graph_scope_for_instance_rejects_bad_tenant():
    with pytest.raises(InvalidTenantId):
        GraphScope.for_instance("tenant/with/slash", "bookstore")


def test_graph_scope_for_catalog_layers():
    pub = GraphScope.for_catalog(layer="public")
    assert pub.tenant_id == GLOBAL_TENANT_ID and pub.kg == PUBLIC_KG

    enh = GraphScope.for_catalog(layer="enhanced")
    assert enh.tenant_id == GLOBAL_TENANT_ID and enh.kg == ENHANCED_KG

    ten = GraphScope.for_catalog(layer="tenant", tenant_id="acme")
    assert ten.tenant_id == "acme" and ten.kg == ONTOLOGY_KG


def test_graph_scope_for_catalog_tenant_requires_id():
    with pytest.raises(GraphScopeError, match="requires tenant_id"):
        GraphScope.for_catalog(layer="tenant")


def test_graph_scope_for_catalog_unknown_layer():
    with pytest.raises(GraphScopeError, match="Unknown catalog layer"):
        GraphScope.for_catalog(layer="mystery")


# ---------------------------------------------------------------------------
# Scope enforcement helpers
# ---------------------------------------------------------------------------


def test_assert_cypher_is_scoped_accepts_both_params():
    assert_cypher_is_scoped(
        "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) RETURN e"
    )


def test_assert_cypher_is_scoped_rejects_unscoped():
    with pytest.raises(GraphScopeError, match="unscoped|missing"):
        assert_cypher_is_scoped("MATCH (n) RETURN n")


def test_assert_cypher_is_scoped_rejects_partial():
    with pytest.raises(GraphScopeError, match=r"\$kg"):
        assert_cypher_is_scoped(
            "MATCH (e:Entity {tenant_id: $tenant_id}) RETURN e"
        )


def test_assert_cypher_rejects_empty():
    with pytest.raises(GraphScopeError, match="non-empty"):
        assert_cypher_is_scoped("   ")


def test_merge_scope_params_overwrites_caller_scope():
    scope = GraphScope.for_instance("real-tenant", "real-kg")
    bound = merge_scope_params(
        scope,
        {"tenant_id": "evil", "kg": "other", "id": "x"},
        for_write=False,
    )
    assert bound["tenant_id"] == "real-tenant"
    assert bound["kg"] == "real-kg"
    assert bound["id"] == "x"


def test_merge_scope_params_rejects_global_write_without_privilege():
    scope = GraphScope(
        tenant_id=GLOBAL_TENANT_ID, kg=PUBLIC_KG, privileged=False
    )
    with pytest.raises(GraphScopeError, match="privileged"):
        merge_scope_params(scope, {}, for_write=True)


def test_merge_scope_params_allows_global_write_when_privileged():
    scope = GraphScope(
        tenant_id=GLOBAL_TENANT_ID, kg=PUBLIC_KG, privileged=True
    )
    bound = merge_scope_params(scope, {"name": "Person"}, for_write=True)
    assert bound["tenant_id"] == GLOBAL_TENANT_ID
    assert bound["kg"] == PUBLIC_KG


def test_merge_scope_params_allows_global_read_without_privilege():
    scope = GraphScope(
        tenant_id=GLOBAL_TENANT_ID, kg=PUBLIC_KG, privileged=False
    )
    bound = merge_scope_params(scope, {}, for_write=False)
    assert bound["tenant_id"] == GLOBAL_TENANT_ID


def test_require_entity_write_identity():
    require_entity_write_identity({"id": "https://cograph.tech/entities/Book/1"})
    with pytest.raises(GraphScopeError, match="non-empty id"):
        require_entity_write_identity({})
    with pytest.raises(GraphScopeError, match="non-empty id"):
        require_entity_write_identity({"id": "  "})


def test_scrub_store_detail_removes_hosts_and_passwords():
    raw = (
        "Failed to connect to bolt://neo4j.databases.neo4j.io:7687 "
        "password=super-secret also https://example.com/path"
    )
    cleaned = scrub_store_detail(raw)
    assert "neo4j.io" not in cleaned
    assert "super-secret" not in cleaned
    assert "example.com" not in cleaned
    assert "[endpoint]" in cleaned


def test_graph_query_error_scrubs_detail():
    err = GraphQueryError("boom at bolt://db.internal:7687")
    assert "db.internal" not in str(err)
    assert "[endpoint]" in err.detail


# ---------------------------------------------------------------------------
# Schema bootstrap plan (pure)
# ---------------------------------------------------------------------------


def test_bootstrap_statements_include_entity_uniqueness():
    names = [n for n, _ in bootstrap_schema_statements()]
    assert "entity_tenant_kg_id_unique" in names
    assert "onto_type_scope_unique" in names
    assert names == [n for n, _ in SCHEMA_STATEMENTS]
    # Every DDL statement is IF NOT EXISTS (idempotent).
    for name, cypher in SCHEMA_STATEMENTS:
        assert "IF NOT EXISTS" in cypher, name


def test_smoke_cypher_templates_are_scoped():
    for template in (
        ENTITY_MERGE_CYPHER,
        ENTITY_GET_CYPHER,
        ENTITY_LIST_BY_TYPE_CYPHER,
    ):
        assert_cypher_is_scoped(template)


# ---------------------------------------------------------------------------
# In-memory GraphStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_store_health_and_bootstrap():
    store = MemoryGraphStore()
    assert await store.health() is True
    names = await store.bootstrap_schema()
    assert "entity_tenant_kg_id_unique" in names
    # Idempotent
    names2 = await store.bootstrap_schema()
    assert names2 == names
    await store.close()


@pytest.mark.asyncio
async def test_memory_store_merge_and_get_entity():
    store = MemoryGraphStore()
    await store.bootstrap_schema()
    scope = GraphScope.for_instance("t1", "kg1")
    session = store.session(scope)

    written = await session.execute_write(
        ENTITY_MERGE_CYPHER,
        {
            "id": "https://cograph.tech/entities/Person/alice",
            "primary_type": "Person",
            "name": "Alice",
            "source": "unit",
            "ts": "2026-08-09T00:00:00Z",
            # Must be overwritten by session:
            "tenant_id": "attacker",
            "kg": "other",
        },
    )
    assert len(written) == 1
    assert written[0]["tenant_id"] == "t1"
    assert written[0]["kg"] == "kg1"
    assert written[0]["name"] == "Alice"

    rows = await session.execute_read(
        ENTITY_GET_CYPHER,
        {"id": "https://cograph.tech/entities/Person/alice"},
    )
    assert len(rows) == 1
    assert rows[0]["primary_type"] == "Person"
    await store.close()


@pytest.mark.asyncio
async def test_memory_store_isolation_across_scopes():
    store = MemoryGraphStore()
    a = store.session(GraphScope.for_instance("tenant-a", "kg-a"))
    b = store.session(GraphScope.for_instance("tenant-b", "kg-b"))

    await a.execute_write(
        ENTITY_MERGE_CYPHER,
        {
            "id": "ent-1",
            "primary_type": "Thing",
            "name": "A only",
            "source": "a",
            "ts": "t",
        },
    )
    # Same id string in B must not see A's node (scope is part of identity).
    rows_b = await b.execute_read(ENTITY_GET_CYPHER, {"id": "ent-1"})
    assert rows_b == []

    await b.execute_write(
        ENTITY_MERGE_CYPHER,
        {
            "id": "ent-1",
            "primary_type": "Thing",
            "name": "B only",
            "source": "b",
            "ts": "t",
        },
    )
    rows_a = await a.execute_read(ENTITY_GET_CYPHER, {"id": "ent-1"})
    rows_b = await b.execute_read(ENTITY_GET_CYPHER, {"id": "ent-1"})
    assert rows_a[0]["name"] == "A only"
    assert rows_b[0]["name"] == "B only"
    assert store.entity_count() == 2
    await store.close()


@pytest.mark.asyncio
async def test_memory_store_rejects_unscoped_cypher():
    store = MemoryGraphStore()
    session = store.session(GraphScope.for_instance("t", "k"))
    with pytest.raises(GraphScopeError, match="tenant_id"):
        await session.execute_read("MATCH (n) RETURN n LIMIT 1")
    with pytest.raises(GraphScopeError):
        await session.execute_write("CREATE (n:Entity) RETURN n")
    await store.close()


@pytest.mark.asyncio
async def test_memory_store_rejects_global_write_without_privilege():
    store = MemoryGraphStore()
    session = store.session(
        GraphScope(tenant_id=GLOBAL_TENANT_ID, kg=PUBLIC_KG, privileged=False)
    )
    with pytest.raises(GraphScopeError, match="privileged"):
        await session.execute_write(
            ENTITY_MERGE_CYPHER,
            {
                "id": "x",
                "primary_type": "T",
                "name": "n",
                "source": "s",
                "ts": "t",
            },
        )
    await store.close()


@pytest.mark.asyncio
async def test_memory_store_merge_requires_id():
    store = MemoryGraphStore()
    session = store.session(GraphScope.for_instance("t", "k"))
    with pytest.raises(GraphScopeError, match="non-empty id"):
        await session.execute_write(
            ENTITY_MERGE_CYPHER,
            {
                "primary_type": "T",
                "name": "n",
                "source": "s",
                "ts": "t",
            },
        )
    await store.close()


@pytest.mark.asyncio
async def test_memory_store_list_by_type():
    store = MemoryGraphStore()
    session = store.session(GraphScope.for_instance("t", "k"))
    for i, ptype in enumerate(["Book", "Book", "Author"]):
        await session.execute_write(
            ENTITY_MERGE_CYPHER,
            {
                "id": f"id-{i}",
                "primary_type": ptype,
                "name": ptype,
                "source": "s",
                "ts": "t",
            },
        )
    books = await session.execute_read(
        ENTITY_LIST_BY_TYPE_CYPHER, {"primary_type": "Book"}
    )
    assert len(books) == 2
    assert all(r["primary_type"] == "Book" for r in books)
    await store.close()


@pytest.mark.asyncio
async def test_memory_store_echo_scope_params():
    """Diagnostic RETURN of forced scope params (wrong caller values overwritten)."""
    store = MemoryGraphStore()
    session = store.session(GraphScope.for_instance("scoped-t", "scoped-k"))
    rows = await session.execute_read(
        "RETURN $tenant_id AS tenant_id, $kg AS kg",
        {"tenant_id": "nope", "kg": "nope"},
    )
    assert rows[0].to_dict() == {"tenant_id": "scoped-t", "kg": "scoped-k"}
    await store.close()


def test_graph_record_mapping_api():
    rec = GraphRecord(data={"id": "x", "n": 1})
    assert rec["id"] == "x"
    assert rec.get("missing") is None
    assert "n" in rec
    assert rec.to_dict() == {"id": "x", "n": 1}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_get_graph_store_requires_config(monkeypatch):
    reset_graph_store_for_tests()
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    with pytest.raises(GraphConfigError, match="NEO4J_URI"):
        get_graph_store()


def test_get_graph_store_requires_password(monkeypatch):
    reset_graph_store_for_tests()
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    with pytest.raises(GraphConfigError, match="NEO4J_PASSWORD"):
        get_graph_store()


def test_configure_graph_store_memory(monkeypatch):
    reset_graph_store_for_tests()
    mem = MemoryGraphStore()
    configure_graph_store(mem)
    assert get_graph_store() is mem
    reset_graph_store_for_tests()
    monkeypatch.delenv("NEO4J_URI", raising=False)
    with pytest.raises(GraphConfigError):
        get_graph_store()


def test_env_neo4j_configured(monkeypatch):
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    assert env_neo4j_configured() is False
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_PASSWORD", "x")
    assert env_neo4j_configured() is True


def test_store_module_does_not_import_neo4j():
    """Protocol module must stay free of the optional neo4j dependency."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "cograph_client" / "graph" / "store.py"
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "neo4j" and not alias.name.startswith("neo4j.")
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module != "neo4j" and not node.module.startswith("neo4j.")


def test_scope_module_does_not_import_neo4j():
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "cograph_client" / "graph" / "scope.py"
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "neo4j" and not alias.name.startswith("neo4j.")
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module != "neo4j" and not node.module.startswith("neo4j.")
