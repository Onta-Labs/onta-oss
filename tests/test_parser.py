from omnix.graph.parser import parse_sparql_results


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
