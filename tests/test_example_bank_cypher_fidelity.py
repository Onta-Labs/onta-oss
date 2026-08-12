"""ONTA-539: hermetic Q ↔ Cypher answer-shape fidelity guard.

Poison few-shots (count Q → list Cypher; filtered sum → bare sum) mis-teach
the LLM. Every bank row that carries both sparql and cypher must agree on
coarse answer shape. Seed-table duals are checked the same way.
"""

from __future__ import annotations

import json
import re

import pytest

from infona_client.nlp.cypher_example_seeds import (
    CYPHER_SEEDS,
    classify_cypher_shape,
    classify_sparql_shape,
    sparql_cypher_shape_compatible,
)
from infona_client.nlp.example_bank import DEFAULT_BANK_PATH, is_benchmark_kg


def test_classify_count_vs_list():
    sp = "SELECT (COUNT(DISTINCT ?m) AS ?c) WHERE { ?m a <Movie> }"
    assert "count" in classify_sparql_shape(sp)
    assert "list" not in classify_sparql_shape(sp)

    list_cy = (
        "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
        "RETURN e.id AS id, e.name AS name ORDER BY e.id LIMIT $limit"
    )
    assert "list" in classify_cypher_shape(list_cy)
    assert "count" not in classify_cypher_shape(list_cy)

    ok, reason = sparql_cypher_shape_compatible(sp, list_cy)
    assert not ok, reason
    assert "COUNT" in reason or "count" in reason.lower() or "aggregation" in reason


def test_classify_filtered_sum_vs_bare_sum():
    sp = (
        "SELECT (SUM(?gross) AS ?t) WHERE { "
        "?m a <Movie> . ?m <onto/star> ?p . "
        '?p <attrs/name> ?n . FILTER(CONTAINS(LCASE(?n), "tom hanks")) '
        ". ?m <attrs/gross> ?gross }"
    )
    bare = (
        "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
        "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
        "WHERE c.name IN $type_names "
        "OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})"
        "-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg}) "
        "WHERE p.name = $prop_key "
        "WITH e, toFloat(a.literal_value) AS num WHERE num IS NOT NULL "
        "RETURN sum(num) AS value"
    )
    ok, reason = sparql_cypher_shape_compatible(sp, bare)
    assert not ok, reason
    assert "filter" in reason.lower()


def test_classify_filtered_sum_with_target_name_ok():
    sp = (
        "SELECT (SUM(?gross) AS ?t) WHERE { "
        "?m a <Movie> . ?m <onto/star> ?p . "
        '?p <attrs/name> ?n . FILTER(CONTAINS(LCASE(?n), "tom hanks")) '
        ". ?m <attrs/gross> ?gross }"
    )
    filtered = (
        "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
        "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
        "WHERE c.name IN $type_names "
        "MATCH (a:Assertion)-[:SUBJECT]->(e) "
        "MATCH (a)-[:OBJECT]->(t:Entity) "
        "WHERE toLower(t.name) CONTAINS toLower($target_name) "
        "WITH e, toFloat(e.gross) AS num RETURN sum(num) AS value"
    )
    ok, reason = sparql_cypher_shape_compatible(sp, filtered)
    assert ok, reason


def test_count_with_related_name_filter_ok():
    sp = (
        "SELECT (COUNT(DISTINCT ?movie) AS ?c) WHERE { "
        "?movie a <Movie> . ?movie <onto/director> ?d . "
        '?d <attrs/name> ?n . FILTER(CONTAINS(LCASE(?n), "nolan")) }'
    )
    cy = (
        "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
        "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
        "WHERE c.name IN $type_names "
        "MATCH (a:Assertion)-[:SUBJECT]->(e) "
        "MATCH (a)-[:OBJECT]->(t:Entity) "
        "WHERE toLower(t.name) CONTAINS toLower($target_name) "
        "RETURN count(DISTINCT e) AS n"
    )
    ok, reason = sparql_cypher_shape_compatible(sp, cy)
    assert ok, reason


@pytest.mark.parametrize(
    "seed",
    [s for s in CYPHER_SEEDS if (s.get("sparql") or "").strip()],
    ids=lambda s: f"{s['kg_name']}:{s['question'][:40]}",
)
def test_seed_table_dual_language_fidelity(seed: dict):
    """Any seed that carries both languages must be shape-compatible."""
    ok, reason = sparql_cypher_shape_compatible(seed["sparql"], seed["cypher"])
    assert ok, f"{seed['question']!r}: {reason}"


def test_shipped_bank_dual_language_fidelity():
    """Every committed dual-language row must agree on coarse answer shape."""
    assert DEFAULT_BANK_PATH.exists(), f"missing bank at {DEFAULT_BANK_PATH}"
    failures: list[str] = []
    dual = 0
    for line in DEFAULT_BANK_PATH.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if is_benchmark_kg(row.get("kg_name") or ""):
            continue
        sp = (row.get("sparql") or "").strip()
        cy = (row.get("cypher") or "").strip()
        if not sp or not cy:
            continue
        dual += 1
        ok, reason = sparql_cypher_shape_compatible(sp, cy)
        if not ok:
            failures.append(f"{row.get('question', '')!r} ({row.get('kg_name')}): {reason}")
    assert dual >= 1, "expected at least one dual-language open-data row"
    assert not failures, (
        "poison few-shot shape mismatch(es):\n  - " + "\n  - ".join(failures)
    )


def _is_how_many(q: str) -> bool:
    return bool(re.match(r"(?is)^\s*how\s+many\b", q or ""))


def test_open_data_how_many_have_count_cypher():
    """Regression: count questions must not ship list-return Cypher."""
    how_many = []
    for line in DEFAULT_BANK_PATH.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cy = (row.get("cypher") or "").strip()
        if not cy:
            continue
        q = (row.get("question") or "").strip()
        if _is_how_many(q):
            how_many.append(row)
            assert re.search(r"(?i)\bcount\s*\(", cy), (
                f"count question lacks count(: {q!r} → {cy[:120]!r}"
            )
    assert how_many, "expected at least one how-many cypher row"


def test_apply_seeds_refuses_mismatched_overwrite():
    """Merge path must not re-poison an existing dual-language row."""
    from infona_client.nlp.cypher_example_seeds import apply_cypher_seeds_to_examples
    from infona_client.nlp.example_bank import Example

    existing = Example(
        question="How many widgets have status Active?",
        sparql="SELECT (COUNT(?w) AS ?c) WHERE { ?w a <Widget> . FILTER(?s = 'Active') }",
        kg_name="synthetic-cypher-shapes",
        ontology_context="Type: Widget",
        pattern_tags=["count"],
        embedding=[0.1, 0.2],
        cypher=(
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
            "RETURN count(DISTINCT e) AS n"
        ),
    )
    # Poison seed: list return for a count SPARQL question.
    poison = [
        {
            "shape": "literal_filter",
            "question": existing.question,
            "kg_name": existing.kg_name,
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                "RETURN e.id AS id ORDER BY e.id LIMIT $limit"
            ),
            "sparql": "",
            "ontology_context": "",
            "pattern_tags": [],
        }
    ]
    out, stats = apply_cypher_seeds_to_examples([existing], seeds=poison)
    assert stats["refreshed"] == 0
    assert out[0].cypher.startswith("MATCH (e:Entity")
    assert "count(DISTINCT e)" in out[0].cypher
