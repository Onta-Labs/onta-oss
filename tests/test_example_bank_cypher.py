"""E6 / ONTA-539 — example bank optional cypher field + mixed-bank load (hermetic)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from infona_client.nlp.example_bank import (
    Example,
    ExampleBank,
    detect_pattern_tags_cypher,
    format_examples_for_prompt,
    sanitize_example_cypher,
)


def test_from_dict_sparql_only_backward_compatible():
    ex = Example.from_dict(
        {
            "question": "How many movies?",
            "sparql": "SELECT (COUNT(?m) AS ?c) FROM <g> WHERE { ?m a <T> }",
            "kg_name": "imdb",
            "ontology_context": "Type: Movie",
            "pattern_tags": ["count"],
            "embedding": [0.1, 0.2],
        }
    )
    assert ex.sparql.startswith("SELECT")
    assert ex.cypher == ""
    d = ex.to_dict()
    assert "cypher" not in d  # omit empty for stable SPARQL bank lines


def test_from_dict_cypher_only():
    ex = Example.from_dict(
        {
            "question": "How many books?",
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                "WHERE e.primary_type = $primary_type RETURN count(*) AS n"
            ),
            "kg_name": "bookstore",
            "ontology_context": "Type: Book",
        }
    )
    assert ex.sparql == ""
    assert "MATCH" in ex.cypher
    assert ex.to_dict()["cypher"].startswith("MATCH")


def test_from_dict_mixed_both_fields():
    ex = Example.from_dict(
        {
            "question": "Count people",
            "sparql": "SELECT (COUNT(?p) AS ?c) WHERE { ?p a <Person> }",
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                "WHERE e.primary_type = 'Person' RETURN count(*) AS n"
            ),
            "kg_name": "crm",
        }
    )
    assert ex.sparql
    assert ex.cypher


def test_from_dict_requires_sparql_or_cypher():
    try:
        Example.from_dict({"question": "x", "kg_name": "k"})
        assert False, "expected KeyError"
    except KeyError as exc:
        assert "sparql" in str(exc) or "cypher" in str(exc)


def test_load_mixed_bank_jsonl(tmp_path: Path):
    path = tmp_path / "bank.jsonl"
    rows = [
        {
            "question": "SPARQL only",
            "sparql": "SELECT * WHERE { ?s ?p ?o }",
            "kg_name": "a",
            "ontology_context": "",
            "pattern_tags": [],
            "embedding": [1.0],
        },
        {
            "question": "Cypher only",
            "cypher": "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) RETURN e",
            "kg_name": "b",
            "ontology_context": "",
            "pattern_tags": ["count"],
            "embedding": [0.0],
        },
        {
            "question": "Both",
            "sparql": "SELECT ?x WHERE { ?x ?p ?o }",
            "cypher": "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) RETURN e.id",
            "kg_name": "c",
            "ontology_context": "",
            "pattern_tags": [],
            "embedding": [0.5],
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    bank = ExampleBank(openrouter_api_key="test-key", bank_path=path)
    n = bank.load()
    assert n == 3
    assert bank._examples[0].cypher == ""
    assert bank._examples[1].sparql == ""
    assert bank._examples[2].sparql and bank._examples[2].cypher


def test_format_examples_sparql_mode_skips_cypher_only():
    examples = [
        Example(
            question="Q1",
            sparql="SELECT ?s FROM <https://graph.infona.ai/graphs/t/kg/x> WHERE { ?s ?p ?o }",
            kg_name="x",
            ontology_context="",
            pattern_tags=["basic"],
        ),
        Example(
            question="Q2",
            sparql="",
            cypher="MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) RETURN e",
            kg_name="y",
            ontology_context="",
            pattern_tags=["count"],
        ),
    ]
    text = format_examples_for_prompt(
        examples,
        "https://graph.infona.ai/graphs/acme/kg/app",
        language="sparql",
    )
    assert "SPARQL:" in text
    assert "Q1" in text
    assert "Q2" not in text
    assert "Cypher:" not in text


def test_format_examples_cypher_mode_skips_sparql_only():
    examples = [
        Example(
            question="Q1",
            sparql="SELECT ?s WHERE { ?s ?p ?o }",
            kg_name="x",
            ontology_context="",
        ),
        Example(
            question="How many books?",
            sparql="",
            cypher=(
                "MATCH (e:Entity {tenant_id: 'demo-tenant', kg: 'bookstore'}) "
                "RETURN count(*) AS n"
            ),
            kg_name="bookstore",
            ontology_context="Type: Book",
            pattern_tags=["count"],
        ),
    ]
    text = format_examples_for_prompt(examples, language="cypher")
    assert "Cypher:" in text
    assert "How many books?" in text
    assert "Q1" not in text
    assert "SPARQL:" not in text
    # No SPARQL query body in the few-shot block (header may mention the word).
    assert "WHERE {" not in text
    assert "FROM <" not in text
    # Literals rewritten to params
    assert "demo-tenant" not in text
    assert "$tenant_id" in text
    assert "$kg" in text


def test_format_examples_cypher_mode_empty_when_all_sparql():
    """Cypher mode must prefer empty few-shot over injecting SPARQL examples."""
    examples = [
        Example(
            question="Q1",
            sparql="SELECT ?s FROM <https://graph.infona.ai/graphs/t/kg/x> WHERE { ?s ?p ?o }",
            kg_name="x",
            ontology_context="",
        ),
        Example(
            question="Q2",
            sparql="SELECT (COUNT(?m) AS ?c) WHERE { ?m a <Movie> }",
            kg_name="imdb",
            ontology_context="",
        ),
    ]
    text = format_examples_for_prompt(examples, language="cypher")
    assert text == ""


def test_sanitize_example_cypher_rewrites_literals():
    raw = "MATCH (e:Entity {tenant_id: 'evil', kg: \"other\"}) RETURN e"
    out = sanitize_example_cypher(raw)
    assert "evil" not in out
    assert "other" not in out
    assert "tenant_id: $tenant_id" in out
    assert "kg: $kg" in out


def test_detect_pattern_tags_cypher_count():
    tags = detect_pattern_tags_cypher(
        "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) RETURN count(*) AS n"
    )
    assert "count" in tags


def test_format_examples_cypher_mode_never_emits_sparql_body():
    """Success criterion: every few-shot body is non-empty Cypher, never SPARQL."""
    examples = [
        Example(
            question="SPARQL only",
            sparql="SELECT (COUNT(?m) AS ?c) FROM <https://graph.infona.ai/graphs/t/kg/x> WHERE { ?m a <Movie> }",
            kg_name="x",
            ontology_context="",
        ),
        Example(
            question="Count books",
            sparql="SELECT * WHERE { ?s ?p ?o }",
            cypher=(
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
                "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
                "WHERE c.name IN $type_names RETURN count(DISTINCT e) AS n"
            ),
            kg_name="bookstore",
            ontology_context="Type: Book",
            pattern_tags=["count"],
        ),
        Example(
            question="Also SPARQL",
            sparql="PREFIX ex: <http://ex/> SELECT ?x WHERE { ?x a ex:T }",
            kg_name="y",
            ontology_context="",
        ),
    ]
    text = format_examples_for_prompt(examples, language="cypher")
    assert text
    assert "Cypher:" in text
    assert "Count books" in text
    assert "SPARQL only" not in text
    assert "Also SPARQL" not in text
    assert "SPARQL:" not in text
    # No SPARQL query body fragments in the few-shot block.
    assert "SELECT" not in text
    assert "PREFIX" not in text
    assert "WHERE {" not in text
    assert "FROM <" not in text
    # Every labeled body is Cypher MATCH...
    for line in text.splitlines():
        if line.strip().startswith("Cypher:"):
            body = line.split("Cypher:", 1)[1].strip()
            assert body, "empty Cypher body"
            assert body.upper().startswith("MATCH") or "MATCH" in body.upper()


@pytest.mark.asyncio
async def test_retrieve_cypher_language_skips_sparql_only(tmp_path: Path):
    """retrieve(language='cypher') must not return SPARQL-only rows (ONTA-539)."""
    path = tmp_path / "bank.jsonl"
    emb_a = [1.0] + [0.0] * 1535
    emb_b = [0.0, 1.0] + [0.0] * 1534
    emb_c = [0.7, 0.7] + [0.0] * 1534
    rows = [
        {
            "question": "How many movies?",
            "sparql": "SELECT (COUNT(?m) AS ?c) WHERE { ?m a <Movie> }",
            "kg_name": "imdb",
            "ontology_context": "",
            "pattern_tags": ["count"],
            "embedding": emb_a,
        },
        {
            "question": "How many books are there?",
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                "RETURN count(*) AS n"
            ),
            "kg_name": "bookstore",
            "ontology_context": "",
            "pattern_tags": ["count"],
            "embedding": emb_b,
        },
        {
            "question": "Count products",
            "sparql": "SELECT * WHERE { ?s ?p ?o }",
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
                "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
                "WHERE c.name IN $type_names RETURN count(DISTINCT e) AS n"
            ),
            "kg_name": "catalog",
            "ontology_context": "",
            "pattern_tags": ["count"],
            "embedding": emb_c,
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    bank = ExampleBank(openrouter_api_key="test-key", bank_path=path)
    bank.load()

    async def _fake_embed(texts, **_kw):
        # Point the query at the SPARQL-only row; language filter must still drop it.
        return [emb_a for _ in texts]

    bank._embed_texts = _fake_embed  # type: ignore[method-assign]
    got = await bank.retrieve("How many movies?", language="cypher", top_k=3)
    assert got
    assert all((ex.cypher or "").strip() for ex in got)
    assert all(not (ex.question == "How many movies?" and not ex.cypher) for ex in got)
    assert "How many movies?" not in {ex.question for ex in got}

    sparql_got = await bank.retrieve("How many movies?", language="sparql", top_k=3)
    assert any(ex.question == "How many movies?" for ex in sparql_got)


def test_apply_cypher_seeds_refuses_benchmark_kg():
    from infona_client.nlp.cypher_example_seeds import apply_cypher_seeds_to_examples

    seeds = [
        {
            "shape": "count_by_type",
            "question": "How many singers?",
            "kg_name": "spider-concert-singer",
            "cypher": "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) RETURN count(*) AS n",
            "sparql": "",
            "ontology_context": "",
            "pattern_tags": ["count"],
        }
    ]
    out, stats = apply_cypher_seeds_to_examples([], seeds=seeds)
    assert out == []
    assert stats["skipped_benchmark"] == 1
    assert stats["appended"] == 0
