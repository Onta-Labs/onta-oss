"""E6 / ADR 0013 — Cypher generation prompt content snapshots (hermetic)."""

from __future__ import annotations

from infona_client.nlp.prompts import (
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
    assert ":Entity" in s


def test_cypher_system_forbids_sparql_constructs():
    s = CYPHER_GENERATION_SYSTEM.upper()
    assert "NOT SPARQL" in s or "DO NOT EMIT SPARQL" in s
    assert "FROM" in s  # teaches not to use FROM
    assert "CREATE" in s  # forbids writes
    assert "MERGE" in s
    assert "PREFIX" in s
    assert "INSERT DATA" in s or "INSERT DATA" in CYPHER_GENERATION_SYSTEM


def test_cypher_system_forbids_sparql_translation():
    """ADR 0013: NL must not be framed as SPARQL→Cypher translation."""
    s = CYPHER_GENERATION_SYSTEM.lower()
    assert "do not translate sparql" in s or "not translate sparql" in s
    assert "translate this sparql" not in s
    assert "sparql→cypher" not in s.replace(" ", "") or "not" in s
    # Positive: teaches Assertion model + helpers
    assert "assertion" in s
    assert "entities_of_type" in s
    assert "literal_values" in s
    assert "related_entities" in s
    assert "helper" in s


def test_cypher_system_response_shape():
    assert '"cypher"' in CYPHER_GENERATION_SYSTEM
    assert "params" in CYPHER_GENERATION_SYSTEM
    assert "template" in CYPHER_GENERATION_SYSTEM


def test_cypher_system_mentions_golden_answers_not_string_match():
    s = CYPHER_GENERATION_SYSTEM.lower()
    assert "answer" in s
    assert "sparql look-alike" in s or "not sparql" in s


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
    assert "semantic helper" in user.lower() or "entities_of_type" in user
    assert "do not translate sparql" in user.lower()
    assert "Generate a SPARQL" not in user


def test_build_cypher_user_prompt_includes_conversation_only_when_given():
    bare = build_cypher_generation_prompt(
        "How many books?",
        "Type: Book",
        tenant_id="t",
        kg_name="kg",
    )
    assert "Prior conversation" not in bare
    from infona_client.nlp.query_ambiguity import format_conversation_for_prompt

    convo = format_conversation_for_prompt(
        [
            {"role": "user", "text": "when did I last meet Ada Example?"},
            {"role": "assistant", "text": "2026-08-12"},
        ]
    )
    with_hist = build_cypher_generation_prompt(
        "what did we talk about?",
        "Type: Book",
        tenant_id="t",
        kg_name="kg",
        conversation_text=convo,
    )
    assert "Ada Example" in with_hist
    assert "what did we talk about?" in with_hist
    assert "follow-up" in with_hist.lower()
    assert "do not switch" in with_hist.lower()


def test_build_cypher_user_prompt_retry_forbids_sparql_fallback():
    user = build_cypher_generation_prompt(
        "How many books?",
        "Type: Book",
        tenant_id="t",
        kg_name="k",
        error_feedback="SyntaxError: unexpected token",
    )
    assert "Previous Cypher attempt failed" in user
    assert "do not switch to SPARQL" in user
    assert "SyntaxError" in user
