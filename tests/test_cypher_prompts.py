"""E6 — Cypher generation prompt content snapshots (hermetic)."""

from __future__ import annotations

from cograph_client.nlp.prompts import (
    CYPHER_GENERATION_SYSTEM,
    SPARQL_GENERATION_SYSTEM,
    build_cypher_generation_prompt,
    build_generation_prompt,
)


def test_cypher_system_teaches_params_not_literals():
    s = CYPHER_GENERATION_SYSTEM
    assert "$tenant_id" in s
    assert "$kg" in s
    assert "NEVER hardcode" in s or "never invent" in s.lower() or "NEVER invent" in s
    assert "primary_type" in s
    assert ":Entity" in s


def test_cypher_system_forbids_sparql_constructs():
    s = CYPHER_GENERATION_SYSTEM.upper()
    assert "NOT SPARQL" in s or "DO NOT EMIT SPARQL" in s
    assert "FROM" in s  # teaches not to use FROM
    assert "CREATE" in s  # forbids writes
    assert "MERGE" in s


def test_cypher_system_response_shape():
    assert '"cypher"' in CYPHER_GENERATION_SYSTEM
    assert "params" in CYPHER_GENERATION_SYSTEM


def test_sparql_prompt_unchanged_still_present():
    """Default Neptune path must keep SPARQL rules (do not delete SPARQL)."""
    assert "FROM" in SPARQL_GENERATION_SYSTEM
    assert "SPARQL" in SPARQL_GENERATION_SYSTEM
    assert build_generation_prompt("q", "Type: X", graph_uri="g").endswith(
        "Generate a SPARQL query to answer this question."
    )


def test_build_cypher_user_prompt_names_params_not_literal_scope():
    user = build_cypher_generation_prompt(
        "How many books?",
        "Type: Book\n  - title",
        tenant_id="demo-tenant",
        kg_name="bookstore",
    )
    assert "How many books?" in user
    assert "Type: Book" in user
    assert "$tenant_id" in user
    assert "$kg" in user
    # Must not tell the model to embed the real id as a Cypher string literal
    # in the MATCH map (scope line may mention kg name in prose only).
    assert "tenant_id: 'demo-tenant'" not in user
    assert 'tenant_id: "demo-tenant"' not in user
    assert "Generate a read-only Cypher" in user
