"""E6 foundation — Cypher scope injector / scrubber (hermetic)."""

from __future__ import annotations

import pytest

from infona_client.nlp.cypher_scope import (
    CypherScopeError,
    confine_generated_cypher,
    has_sparql_leftovers,
    is_read_only_cypher,
    normalize_cypher,
    scrub_cypher_error,
)


def test_normalize_strips_markdown_fence():
    raw = "```cypher\nMATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) RETURN e\n```"
    out = normalize_cypher(raw)
    assert out.startswith("MATCH")
    assert "```" not in out


def test_rejects_empty():
    with pytest.raises(CypherScopeError, match="empty"):
        confine_generated_cypher("", tenant_id="t1", kg="kg1")


def test_rejects_write_clauses():
    with pytest.raises(CypherScopeError, match="read-only"):
        confine_generated_cypher(
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
            "SET e.x = 1 RETURN e",
            tenant_id="t1",
            kg="kg1",
        )


def test_rejects_create_merge_delete():
    for bad in (
        "CREATE (e:Entity {tenant_id: $tenant_id, kg: $kg}) RETURN e",
        "MERGE (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $id}) RETURN e",
        "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) DELETE e",
    ):
        with pytest.raises(CypherScopeError, match="read-only"):
            confine_generated_cypher(bad, tenant_id="t1", kg="kg1")


def test_rejects_sparql_leftovers():
    assert has_sparql_leftovers(
        "SELECT ?s FROM <https://graph.onta.sh/graphs/t/kg/x> WHERE { ?s ?p ?o }"
    )
    with pytest.raises(CypherScopeError, match="SPARQL"):
        confine_generated_cypher(
            "SELECT ?s FROM <https://graph.onta.sh/graphs/t/kg/x> WHERE { ?s ?p ?o }",
            tenant_id="t1",
            kg="kg1",
        )


def test_rejects_unscoped_without_entity_pattern():
    with pytest.raises(CypherScopeError):
        confine_generated_cypher(
            "MATCH (n) RETURN n LIMIT 1",
            tenant_id="t1",
            kg="kg1",
        )


def test_repairs_bare_entity_match():
    cypher, params = confine_generated_cypher(
        "MATCH (e:Entity) RETURN count(*) AS n",
        tenant_id="demo-tenant",
        kg="bookstore",
    )
    assert "$tenant_id" in cypher
    assert "$kg" in cypher
    assert "tenant_id:" in cypher
    assert "kg:" in cypher
    assert params == {"tenant_id": "demo-tenant", "kg": "bookstore"}


def test_forces_session_params_over_model_values():
    cypher, params = confine_generated_cypher(
        "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
        "WHERE e.primary_type = $primary_type RETURN count(*) AS n",
        tenant_id="real-tenant",
        kg="real-kg",
        params={
            "tenant_id": "evil-tenant",
            "kg": "other-kg",
            "primary_type": "Book",
        },
    )
    assert params["tenant_id"] == "real-tenant"
    assert params["kg"] == "real-kg"
    assert params["primary_type"] == "Book"
    assert "evil" not in cypher  # params only — cypher uses $ tokens


def test_accepts_already_scoped_count():
    q = (
        "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
        "WHERE e.primary_type = $primary_type "
        "RETURN count(*) AS n"
    )
    out, params = confine_generated_cypher(
        q, tenant_id="t1", kg="kg1", params={"primary_type": "Person"}
    )
    assert out == q.strip() or "$tenant_id" in out
    assert params["primary_type"] == "Person"


def test_scrub_strips_bolt_and_password():
    dirty = (
        "Failed bolt://secret-host.neo4j.io:7687 password=supersecret "
        "for user neo4j"
    )
    clean = scrub_cypher_error(dirty)
    assert "secret-host" not in clean
    assert "supersecret" not in clean
    assert "password" not in clean.lower() or "[endpoint]" in clean


def test_is_read_only_helpers():
    assert is_read_only_cypher("MATCH (e:Entity) RETURN e")
    assert not is_read_only_cypher("MATCH (e) DETACH DELETE e")
