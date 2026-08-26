from infona_client.graph.parser import (
    apply_unbound_confidence,
    cypher_return_aliases,
    dropped_projection_aliases,
    parse_sparql_results,
    unbound_projection_vars,
)


def test_parse_empty_results():
    raw = {"head": {"vars": ["s", "p", "o"]}, "results": {"bindings": []}}
    vars, bindings = parse_sparql_results(raw)
    assert vars == ["s", "p", "o"]
    assert bindings == []


def test_parse_single_result():
    raw = {
        "head": {"vars": ["s", "p", "o"]},
        "results": {
            "bindings": [
                {
                    "s": {"type": "uri", "value": "https://example.com/place/1"},
                    "p": {"type": "uri", "value": "https://schema.org/name"},
                    "o": {"type": "literal", "value": "Central Park"},
                }
            ]
        },
    }
    vars, bindings = parse_sparql_results(raw)
    assert len(bindings) == 1
    assert bindings[0]["s"] == "https://example.com/place/1"
    assert bindings[0]["o"] == "Central Park"


def test_parse_multiple_results():
    raw = {
        "head": {"vars": ["name"]},
        "results": {
            "bindings": [
                {"name": {"type": "literal", "value": "Park A"}},
                {"name": {"type": "literal", "value": "Park B"}},
            ]
        },
    }
    vars, bindings = parse_sparql_results(raw)
    assert len(bindings) == 2
    assert bindings[0]["name"] == "Park A"
    assert bindings[1]["name"] == "Park B"


def test_parse_missing_optional_var():
    raw = {
        "head": {"vars": ["name", "desc"]},
        "results": {
            "bindings": [
                {"name": {"type": "literal", "value": "func1"}},
            ]
        },
    }
    vars, bindings = parse_sparql_results(raw)
    assert "name" in bindings[0]
    assert "desc" not in bindings[0]


def test_parse_malformed_input():
    vars, bindings = parse_sparql_results({})
    assert vars == []
    assert bindings == []


def test_unbound_projection_vars_detects_zero_bind_column():
    # `desc` is projected but binds in no row → reported as unbound.
    variables = ["name", "desc"]
    bindings = [{"name": "A"}, {"name": "B"}]
    assert unbound_projection_vars(variables, bindings) == ["desc"]


def test_unbound_projection_vars_none_when_all_bound():
    variables = ["name", "desc"]
    bindings = [{"name": "A", "desc": "x"}, {"name": "B"}]  # desc binds in row 0
    assert unbound_projection_vars(variables, bindings) == []


def test_unbound_projection_vars_empty_result_is_no_signal():
    # With zero rows we can't tell "unbound" from "empty result" → return [].
    assert unbound_projection_vars(["name", "desc"], []) == []


def test_unbound_projection_vars_preserves_projection_order():
    variables = ["a", "b", "c"]
    bindings = [{"b": "1"}]
    assert unbound_projection_vars(variables, bindings) == ["a", "c"]


def test_cypher_return_aliases_from_as_and_bare_idents():
    cypher = (
        "MATCH (a:Assertion)-[:SUBJECT]->(e) "
        "RETURN to_e.name AS person_name, da.literal_value AS date "
        "ORDER BY date LIMIT 10"
    )
    assert cypher_return_aliases(cypher) == ["person_name", "date"]


def test_cypher_return_aliases_coalesce_as():
    cypher = (
        "RETURN coalesce(from_e.display_name, from_e.title, from_e.name) "
        "AS from_name, to_e.id AS to_id"
    )
    assert cypher_return_aliases(cypher) == ["from_name", "to_id"]


def test_dropped_projection_aliases_empty_result_is_no_signal():
    cypher = (
        "MATCH p = shortestPath((s)-[:SUBJECT|OBJECT*..12]-(t)) "
        "RETURN [n IN nodes(p) WHERE n:Entity | n.name] AS path"
    )
    assert dropped_projection_aliases(cypher, [], []) == []
    assert dropped_projection_aliases(cypher, ["path"], []) == []


def test_dropped_projection_aliases_when_template_omits_return_keys():
    cypher = (
        "MATCH (a:Assertion)-[:SUBJECT]->(e) "
        "RETURN to_e.name AS person_name, da.literal_value AS date"
    )
    # related_entities template rows have from_name/to_name, not the gen RETURN.
    variables = ["from_id", "from_name", "to_id", "to_name"]
    bindings = [{"from_name": "Ada_Lovelace", "to_name": "Acme"}]
    assert dropped_projection_aliases(cypher, variables, bindings) == [
        "person_name",
        "date",
    ]


def test_apply_unbound_confidence_never_stays_high():
    conf, reason = apply_unbound_confidence(["date"], "high", "coverage ok")
    assert conf == "low"
    assert "date" in reason
    conf2, _ = apply_unbound_confidence(["date"], "medium", "partial")
    assert conf2 == "medium"
    conf3, _ = apply_unbound_confidence([], "high", "ok")
    assert conf3 == "high"
