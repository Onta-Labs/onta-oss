"""GraphDelta parser: fail closed, no second-model repair."""

from __future__ import annotations

from ontology_skills.graph_delta import GraphDelta
from ontology_skills.parse import parse_graph_delta
from ontology_skills.scoring import score_prediction


GOLD = {
    "type_assertions": [
        {
            "entity": "https://graph.infona.ai/bench/ent/acme-components",
            "type_id": "Supplier",
        }
    ]
}


def test_parse_raw_object() -> None:
    parsed = parse_graph_delta(
        '{"type_assertions": [{"entity": "e:a", "type_id": "Supplier"}]}'
    )
    assert parsed.ok is True
    assert parsed.predicted.type_assertions[0].type_id == "Supplier"


def test_parse_fenced_json() -> None:
    text = "```json\n" + _dumps(GOLD) + "\n```"
    parsed = parse_graph_delta(text)
    assert parsed.ok is True
    assert parsed.predicted.type_assertions[0].entity.endswith("acme-components")


def test_parse_prose_fails_empty_delta() -> None:
    parsed = parse_graph_delta("I think this is a Supplier.")
    assert parsed.ok is False
    assert parsed.predicted == GraphDelta()
    gold = GraphDelta.from_dict(GOLD)
    metrics = score_prediction(parsed.predicted, gold, family="entity_typing")
    assert metrics["success"] is False
    assert metrics["graph_delta_precision"] is None
    assert metrics["graph_delta_recall"] == 0.0
    assert metrics["graph_delta_f1"] == 0.0


def test_parse_array_root_fails() -> None:
    parsed = parse_graph_delta("[1, 2, 3]")
    assert parsed.ok is False
    assert parsed.predicted == GraphDelta()


def test_parse_malformed_graphdelta_fails() -> None:
    parsed = parse_graph_delta('{"adds": "not-a-list"}')
    assert parsed.ok is False
    assert parsed.predicted == GraphDelta()


def test_parse_unbalanced_braces_fails() -> None:
    parsed = parse_graph_delta('{"type_assertions": [')
    assert parsed.ok is False
    assert parsed.predicted == GraphDelta()


def _dumps(obj: dict) -> str:
    import json

    return json.dumps(obj)
