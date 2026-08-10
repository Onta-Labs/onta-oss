"""E6 — example bank optional cypher field + mixed-bank load (hermetic)."""

from __future__ import annotations

import json
from pathlib import Path

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
    # Literals rewritten to params
    assert "demo-tenant" not in text
    assert "$tenant_id" in text
    assert "$kg" in text


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
