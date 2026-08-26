"""Graph-structure NL (exists / degree / path) — coverage skip + seeds.

Analytic COUNT-with-filters (how-many X that are Y) must still fail-closed.
"""
from __future__ import annotations

from infona_client.nlp.cypher_example_seeds_data import (
    CYPHER_SEEDS,
    SHAPE_GRAPH_DEGREE,
    SHAPE_GRAPH_EXISTS,
    SHAPE_GRAPH_PATH,
)
from infona_client.nlp.query_constraint_coverage_check import check_constraint_coverage
from infona_client.nlp.query_intent import (
    question_has_graph_structure_intent,
    sketch_query_intent,
)

_DEGREE_Q = (
    "Which entity has the highest number of outgoing edges in the "
    "provided knowledge graph? If there is a tie, choose one. "
    "Answer with the entity label using the format 'Answer: <entity label>'."
)
_EXISTS_Q = (
    "Your answer must consist of either \"Yes\" or \"No\", nothing else. "
    "Is the following triplet fact present in the knowledge graph (Yes/No)? "
    "(Widget A, made_by, Acme)"
)
_PATH_Q = "What is the shortest path between Widget A and Widget B?"
_FILTERED_COUNT_Q = "How many unique authors are involved in books?"

_DEGREE_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(e)
WITH e, count(a) AS deg
ORDER BY deg DESC
LIMIT 1
RETURN coalesce(e.display_name, e.name) AS name
""".strip()

_UNFILTERED_COUNT = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names
RETURN count(e) AS n
""".strip()


def test_degree_exists_path_are_graph_structure():
    assert question_has_graph_structure_intent(_DEGREE_Q)
    assert question_has_graph_structure_intent(_EXISTS_Q)
    assert question_has_graph_structure_intent(_PATH_Q)
    assert sketch_query_intent(_DEGREE_Q).has_graph_structure_intent
    assert sketch_query_intent(_EXISTS_Q).has_graph_structure_intent
    assert sketch_query_intent(_PATH_Q).has_graph_structure_intent


def test_filtered_how_many_is_not_graph_structure():
    assert not question_has_graph_structure_intent(_FILTERED_COUNT_Q)
    assert not question_has_graph_structure_intent(
        "How many outgoing relations of type 'made_by' does Acme have?"
    )
    sk = sketch_query_intent(_FILTERED_COUNT_Q)
    assert not sk.has_graph_structure_intent
    r = check_constraint_coverage(
        _FILTERED_COUNT_Q, _UNFILTERED_COUNT, params={"type_names": ["Book"]}
    )
    assert not r.ok
    assert r.fail_closed


def test_highest_degree_count_does_not_fail_closed_as_silent_total():
    r = check_constraint_coverage(_DEGREE_Q, _DEGREE_CYPHER)
    assert r.ok
    assert not r.fail_closed


def test_exists_question_does_not_fail_closed_as_silent_total():
    cypher = """
MATCH (from_e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE toLower(coalesce(from_e.display_name, from_e.name, '')) = toLower($from_name)
MATCH (to_e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE toLower(coalesce(to_e.display_name, to_e.name, '')) = toLower($to_name)
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(from_e)
MATCH (a)-[:OBJECT]->(to_e)
MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $rel_attr
RETURN CASE WHEN count(a) > 0 THEN 'Yes' ELSE 'No' END AS answer
""".strip()
    r = check_constraint_coverage(
        _EXISTS_Q,
        cypher,
        params={"from_name": "Widget A", "to_name": "Acme", "rel_attr": "made_by"},
    )
    assert r.ok
    assert not r.fail_closed


def test_seed_table_includes_graph_structure_shapes():
    present = {s["shape"] for s in CYPHER_SEEDS}
    assert SHAPE_GRAPH_EXISTS in present
    assert SHAPE_GRAPH_DEGREE in present
    assert SHAPE_GRAPH_PATH in present
    for shape in (SHAPE_GRAPH_EXISTS, SHAPE_GRAPH_DEGREE, SHAPE_GRAPH_PATH):
        body = next(s["cypher"] for s in CYPHER_SEEDS if s["shape"] == shape)
        assert "$tenant_id" in body
        assert "$kg" in body
        assert "HAS_ASSERTION" not in body
        assert "apoc" not in body.lower()
        if shape == SHAPE_GRAPH_PATH:
            assert "shortestPath" in body
