"""Graph-structure NL (exists / degree / path) — coverage skip + seeds.

Analytic COUNT-with-filters (how-many X that are Y) must still fail-closed.
"""
from __future__ import annotations

from infona_client.nlp.cypher_example_seeds_data import (
    CYPHER_SEEDS,
    SHAPE_GRAPH_DEGREE,
    SHAPE_GRAPH_EXISTS,
    SHAPE_GRAPH_NEIGHBOR,
    SHAPE_GRAPH_PATH,
    SHAPE_GRAPH_REL_COUNT,
)
from infona_client.nlp.query_constraint_coverage_check import check_constraint_coverage
from infona_client.nlp.query_constraint_coverage_dim import plan_has_dimension_filter
from infona_client.nlp.query_intent import (
    extract_filter_tokens,
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
    rel_q = "How many outgoing relations of type 'made_by' does Acme have?"
    assert not question_has_graph_structure_intent(rel_q)
    sk = sketch_query_intent(_FILTERED_COUNT_Q)
    assert not sk.has_graph_structure_intent
    r = check_constraint_coverage(
        _FILTERED_COUNT_Q, _UNFILTERED_COUNT, params={"type_names": ["Book"]}
    )
    assert not r.ok
    assert r.fail_closed
    r2 = check_constraint_coverage(
        rel_q, _UNFILTERED_COUNT, params={"type_names": ["Entity"]}
    )
    assert not r2.ok
    assert r2.fail_closed


def test_highest_degree_count_does_not_fail_closed_as_silent_total():
    r = check_constraint_coverage(_DEGREE_Q, _DEGREE_CYPHER)
    assert r.ok
    assert not r.fail_closed
    # RETURN count(...) is the agg regex; skip must still apply.
    r_count = check_constraint_coverage(
        _DEGREE_Q,
        """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(e)
RETURN count(a) AS n, coalesce(e.display_name, e.name) AS name
ORDER BY n DESC
LIMIT 1
""".strip(),
    )
    assert r_count.ok
    assert not r_count.fail_closed


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
    assert SHAPE_GRAPH_REL_COUNT in present
    assert SHAPE_GRAPH_NEIGHBOR in present
    for shape in (
        SHAPE_GRAPH_EXISTS,
        SHAPE_GRAPH_DEGREE,
        SHAPE_GRAPH_PATH,
        SHAPE_GRAPH_REL_COUNT,
        SHAPE_GRAPH_NEIGHBOR,
    ):
        body = next(s["cypher"] for s in CYPHER_SEEDS if s["shape"] == shape)
        assert "$tenant_id" in body
        assert "$kg" in body
        assert "HAS_ASSERTION" not in body
        assert "apoc" not in body.lower()
        assert ":KgNode" not in body
        if shape == SHAPE_GRAPH_PATH:
            assert "shortestPath" in body
            assert "AS answer" in body
            assert "toString([" not in body
        if shape == SHAPE_GRAPH_DEGREE:
            assert "[:OBJECT]" in body
            assert "Answer:" in body
        if shape in (SHAPE_GRAPH_EXISTS, SHAPE_GRAPH_REL_COUNT, SHAPE_GRAPH_NEIGHBOR):
            assert "replace(toLower(" in body
        if shape == SHAPE_GRAPH_NEIGHBOR:
            assert "[:SUBJECT|OBJECT]" in body


_REL_COUNT_Q = (
    "How many outgoing relations of type 'made_by' does Acme have? "
    "Answer in the format 'Answer: <number>'. Do not output anything other "
    "than 'Answer: <number>'"
)
_REL_COUNT_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE replace(toLower(coalesce(e.display_name, e.display_label, e.name, '')), '_', ' ')
  = replace(toLower($entity_name), '_', ' ')
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(e)
MATCH (a)-[:OBJECT]->(:Entity {tenant_id: $tenant_id, kg: $kg})
MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE replace(toLower(p.name), '_', ' ') = replace(toLower($rel_attr), '_', ' ')
RETURN 'Answer: ' + toString(count(DISTINCT a)) AS answer
""".strip()

_NEIGHBOR_Q = (
    "How many of the directly connected entities to Widget A have an outgoing "
    "property of type 'made_by' in the knowledge graph? You must respond in "
    "the format 'Answer: <number>'."
)
_NEIGHBOR_CYPHER = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE replace(toLower(coalesce(e.display_name, e.name, '')), '_', ' ')
  = replace(toLower($entity_name), '_', ' ')
MATCH (hop:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT|OBJECT]->(e)
MATCH (hop)-[:SUBJECT|OBJECT]->(nbr:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE nbr <> e
WITH DISTINCT nbr
MATCH (out:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(nbr)
MATCH (out)-[:OBJECT]->(:Entity {tenant_id: $tenant_id, kg: $kg})
MATCH (out)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE replace(toLower(p.name), '_', ' ') = replace(toLower($rel_attr), '_', ' ')
RETURN 'Answer: ' + toString(count(DISTINCT nbr)) AS answer
""".strip()


def test_answer_format_prose_is_not_a_filter_token():
    lows = {t.lower() for t in extract_filter_tokens(_REL_COUNT_Q)}
    assert "answer: <number>" not in lows
    assert "format" not in lows
    assert "made_by" in lows or "made by" in lows


def test_knowledge_graph_boilerplate_is_not_a_filter_token():
    lows = {t.lower() for t in extract_filter_tokens(_NEIGHBOR_Q)}
    assert "knowledge graph" not in lows
    assert "answer: <number>" not in lows
    assert "format" not in lows


def test_typed_rel_count_plan_does_not_fail_closed():
    r = check_constraint_coverage(
        _REL_COUNT_Q,
        _REL_COUNT_CYPHER,
        params={"entity_name": "Acme", "rel_attr": "made_by"},
    )
    assert r.ok
    assert not r.fail_closed


def test_typed_rel_count_unfiltered_still_fail_closes():
    r = check_constraint_coverage(
        _REL_COUNT_Q,
        _UNFILTERED_COUNT,
        params={"type_names": ["Entity"]},
    )
    assert not r.ok
    assert r.fail_closed


def test_space_underscore_rel_attr_is_a_dim_filter():
    cypher = (
        "MATCH (a:Assertion)-[:PREDICATE]->(p:Property) "
        "WHERE replace(toLower(p.name), '_', ' ') = replace(toLower($rel_attr), '_', ' ') "
        "RETURN count(a) AS n"
    )
    assert plan_has_dimension_filter(
        cypher, params={"rel_attr": "member_of"}
    )
    r = check_constraint_coverage(
        "How many incoming relations of type 'member of' does Widget A have?",
        """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE toLower(coalesce(e.display_name, e.name, '')) = toLower($entity_name)
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:OBJECT]->(e)
MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE replace(toLower(p.name), '_', ' ') = replace(toLower($rel_attr), '_', ' ')
RETURN count(DISTINCT a) AS n
""".strip(),
        params={"entity_name": "Widget A", "rel_attr": "member_of"},
    )
    assert r.ok
    assert not r.fail_closed


def test_neighbor_outgoing_plan_does_not_fail_closed():
    r = check_constraint_coverage(
        _NEIGHBOR_Q,
        _NEIGHBOR_CYPHER,
        params={"entity_name": "Widget A", "rel_attr": "made_by"},
    )
    assert r.ok
    assert not r.fail_closed


def test_repair_shortest_path_filters_entity_nodes():
    from infona_client.nlp.graph_structure_cypher import repair_graph_structure_cypher

    raw = (
        "MATCH p = shortestPath((s)-[:SUBJECT|OBJECT*..12]-(t)) "
        "WITH nodes(p) AS path_nodes "
        "RETURN 'SHORTEST PATH: [' + reduce(acc = '', x IN path_nodes | "
        "acc + coalesce(x.display_name, x.name)) + ']' AS answer"
    )
    out, changed, extra = repair_graph_structure_cypher(raw, "")
    assert changed
    assert "[n IN nodes(p) WHERE n:Entity]" in out
    assert "WITH nodes(p) AS" not in out
    assert "coalesce(x.display_name, x.name, '')" in out
    assert extra is None


def test_repair_neighbor_first_hop_is_undirected():
    from infona_client.nlp.graph_structure_cypher import repair_graph_structure_cypher

    q = "How many of the directly connected entities to Widget A have an outgoing property of type 'made_by'?"
    raw = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(e)
MATCH (a)-[:OBJECT]->(neighbor:Entity)
MATCH (out:Assertion)-[:SUBJECT]->(neighbor)
RETURN count(DISTINCT neighbor) AS n
""".strip()
    out, changed, extra = repair_graph_structure_cypher(
        raw, "which widgets are directly connected?"
    )
    assert changed
    assert out.count("[:SUBJECT|OBJECT]") >= 2
    assert "-[:SUBJECT]->(neighbor)" in out or "-[:SUBJECT]->(neighbor" in out
    assert "neighbor <> e" in out or "<> e" in out


class _FakeDim:
    leaf = "located_in"
    kind = "entity_dim"
    subject_type = "Widget"
    range_type = "Yard"


class _FakeVal:
    display = "YardX"
    normalized = "yardx"


class _FakeBind:
    token = "YardX"
    dim = _FakeDim()
    matched_value = _FakeVal()


def test_entity_name_without_rel_attr_does_not_cover_related_entity_dim():
    """Name-only COUNT must not satisfy a registry related-entity bind."""
    from infona_client.nlp.query_constraint_coverage_dim import plan_covers_dim_bind

    cypher = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE toLower(coalesce(e.display_name, e.name, '')) = toLower($entity_name)
RETURN count(e) AS n
""".strip()
    assert not plan_covers_dim_bind(
        _FakeBind(),
        cypher,
        params={"entity_name": "YardX"},
    )
    r = check_constraint_coverage(
        "How many Widgets targeting YardX?",
        cypher,
        params={"entity_name": "YardX"},
        dim_binds=[_FakeBind()],
    )
    assert not r.ok
    assert r.fail_closed


def test_unused_rel_attr_param_does_not_save_unfiltered_count():
    """params.rel_attr with no $rel_attr in Cypher is not a dim filter."""
    r = check_constraint_coverage(
        _REL_COUNT_Q,
        _UNFILTERED_COUNT,
        params={"type_names": ["Entity"], "rel_attr": "made_by", "entity_name": "Acme"},
    )
    assert not r.ok
    assert r.fail_closed


def test_path_seed_returns_answer_string_not_path_alias():
    body = next(s["cypher"] for s in CYPHER_SEEDS if s["shape"] == SHAPE_GRAPH_PATH)
    assert "AS answer" in body
    assert "AS path" not in body
    assert "reduce(" in body


def test_compile_exists_widget_slots():
    from infona_client.nlp.graph_structure_cypher import compile_graph_op_cypher

    planned = compile_graph_op_cypher(_EXISTS_Q)
    assert planned is not None
    body, extra = planned
    assert extra == {"from_name": "Widget A", "to_name": "Acme", "rel_attr": "made_by"}
    assert ":KgNode" not in body
    assert "[:SUBJECT]->(from_e)" in body
    r = check_constraint_coverage(_EXISTS_Q, body, params=extra)
    assert r.ok


def test_compile_exists_skips_yes_no_parens():
    from infona_client.nlp.graph_structure_cypher import compile_graph_op_cypher

    q = (
        'Your answer must consist of either "Yes" or "No", nothing else. '
        "Is the following triplet fact present in the knowledge graph (Yes/No)? "
        "(Kakwa, member of, International Centre for Settlement of Investment Disputes)"
    )
    planned = compile_graph_op_cypher(q)
    assert planned is not None
    _, extra = planned
    assert extra["from_name"] == "Kakwa"
    assert extra["rel_attr"] == "member of"
    assert extra["to_name"].startswith("International Centre")


def test_compile_path_ignores_argentina_mexico_example():
    from infona_client.nlp.graph_structure_cypher import compile_graph_op_cypher

    q = (
        "Your task is to find the shortest path from Faithful Empire to "
        "Imperial State of Neo-Japan. For example, if the shortest path in "
        "the knowledge graph between Argentina and Mexico is through Bolivia "
        "and Colombia, then your response should be \"SHORTEST PATH: "
        "['Argentina', 'Bolivia', 'Colombia', 'Mexico']\". Your response must "
        "begin with 'SHORTEST_PATH:' followed by the list of entities in the "
        "path following the format shown before. Note that you can use both "
        "incoming and outgoing edges to form a path. \n\n What is the shortest "
        "path from Faithful Empire to Imperial State of Neo-Japan? \n "
        "Answer: SHORTEST PATH:"
    )
    planned = compile_graph_op_cypher(q)
    assert planned is not None
    body, extra = planned
    assert extra["start_name"] == "Faithful Empire"
    assert extra["end_name"] == "Imperial State of Neo-Japan"
    assert "Argentina" not in extra.values()
    assert "shortestPath" in body
    assert "AS answer" in body


def test_compile_path_between_widgets():
    from infona_client.nlp.graph_structure_cypher import compile_graph_op_cypher

    planned = compile_graph_op_cypher(_PATH_Q)
    assert planned is not None
    _, extra = planned
    assert extra == {"start_name": "Widget A", "end_name": "Widget B"}


def test_compile_degree_directions():
    from infona_client.nlp.graph_structure_cypher import compile_graph_op_cypher

    for direction, needle in (
        ("outgoing", "[:SUBJECT]->(e)"),
        ("incoming", "[:OBJECT]->(e)"),
        ("total", "[:SUBJECT|OBJECT]->(e)"),
    ):
        q = (
            f"Which entity has the highest number of {direction} edges in the "
            "provided knowledge graph?"
        )
        planned = compile_graph_op_cypher(q)
        assert planned is not None, direction
        body, extra = planned
        assert extra == {}
        assert needle in body
        assert "Answer:" in body


def test_compile_rel_count_and_neighbor_pass_coverage():
    from infona_client.nlp.graph_structure_cypher import compile_graph_op_cypher

    planned = compile_graph_op_cypher(_REL_COUNT_Q)
    assert planned is not None
    body, extra = planned
    assert extra == {"entity_name": "Acme", "rel_attr": "made_by"}
    r = check_constraint_coverage(_REL_COUNT_Q, body, params=extra)
    assert r.ok
    assert not r.fail_closed

    planned_n = compile_graph_op_cypher(_NEIGHBOR_Q)
    assert planned_n is not None
    body_n, extra_n = planned_n
    assert extra_n == {"entity_name": "Widget A", "rel_attr": "made_by"}
    assert "[:SUBJECT|OBJECT]" in body_n
    r_n = check_constraint_coverage(_NEIGHBOR_Q, body_n, params=extra_n)
    assert r_n.ok


def test_compile_does_not_swallow_analytic_count():
    from infona_client.nlp.graph_structure_cypher import compile_graph_op_cypher

    assert compile_graph_op_cypher(_FILTERED_COUNT_Q) is None
    assert compile_graph_op_cypher("which widgets are directly connected?") is None


def test_repair_replaces_kgnode_body_when_question_compiles():
    from infona_client.nlp.graph_structure_cypher import repair_graph_structure_cypher

    garbage = "MATCH (e:KgNode)-[:member_of]->(x) RETURN count(*)"
    out, changed, extra = repair_graph_structure_cypher(garbage, _EXISTS_Q)
    assert changed
    assert ":KgNode" not in out
    assert extra is not None
    assert extra["from_name"] == "Widget A"
    assert "[:SUBJECT]->(from_e)" in out


def test_compiled_params_drop_llm_invented_keys():
    from infona_client.nlp.graph_structure_cypher import compiled_graph_op_params

    out = compiled_graph_op_params(
        {
            "tenant_id": "t",
            "kg": "k",
            "rel_attr": "invented_leaf",
            "member_of": "nope",
        },
        {},
    )
    assert out == {"tenant_id": "t", "kg": "k"}
    out2 = compiled_graph_op_params(
        {"tenant_id": "t", "rel_attr": "wrong"},
        {"entity_name": "Acme", "rel_attr": "made_by"},
    )
    assert out2 == {
        "tenant_id": "t",
        "entity_name": "Acme",
        "rel_attr": "made_by",
    }
