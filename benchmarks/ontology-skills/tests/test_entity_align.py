"""Minted-URI alignment and restated-add drop. No gold URI in the prompt."""

from __future__ import annotations

from ontology_skills.dataset import load_tasks
from ontology_skills.graph_delta import GraphDelta, LiteralSet, Triple, TypeAssertion
from ontology_skills.prompts import SCHEMA_HINT, TEMPLATE_ID
from ontology_skills.scoring import graph_delta_prf, score_task


GOLD_ENT = "https://graph.infona.ai/bench/ent/acme-components"
MINTED = "https://graph.infona.ai/bench/ent/61234567890"


def _et001():
    return next(t for t in load_tasks() if t.task_id == "et-001")


def test_different_minted_uri_gets_recall_after_alignment() -> None:
    task = _et001()
    predicted = GraphDelta(
        type_assertions=(TypeAssertion(MINTED, "Supplier"),),
        literals=(
            LiteralSet(MINTED, "legalName", "Acme Components Ltd"),
            LiteralSet(MINTED, "registrationId", "GB-000111222"),
        ),
    )
    raw = graph_delta_prf(predicted, task.gold)
    assert raw["recall"] == 0.0
    metrics = score_task(predicted, task)
    assert metrics["graph_delta_recall"] == 1.0
    assert metrics["graph_delta_precision"] == 1.0
    assert metrics["graph_delta_f1"] == 1.0
    assert metrics["success"] is True


def test_restated_add_does_not_zero_f1() -> None:
    task = _et001()
    predicted = GraphDelta(
        type_assertions=(TypeAssertion(MINTED, "Supplier"),),
        literals=(
            LiteralSet(MINTED, "legalName", "Acme Components Ltd"),
            LiteralSet(MINTED, "registrationId", "GB-000111222"),
        ),
        adds=(
            Triple(MINTED, "hasName", "Acme Components Ltd"),
            Triple(MINTED, "hasVAT", "GB-000111222"),
        ),
    )
    metrics = score_task(predicted, task)
    assert metrics["graph_delta_f1"] == 1.0
    assert metrics["success"] is True


def test_ancestor_type_dump_does_not_zero_precision() -> None:
    task = _et001()
    predicted = GraphDelta(
        type_assertions=(
            TypeAssertion(MINTED, "Supplier"),
            TypeAssertion(MINTED, "Company"),
            TypeAssertion(MINTED, "Organization"),
            TypeAssertion(MINTED, "Entity"),
        ),
        literals=(LiteralSet(MINTED, "legalName", "Acme Components Ltd"),),
    )
    raw = graph_delta_prf(predicted, task.gold)
    assert raw["precision"] is not None
    assert raw["precision"] < 0.5
    metrics = score_task(predicted, task)
    assert metrics["graph_delta_precision"] == 1.0
    assert metrics["graph_delta_recall"] == 2 / 3
    assert metrics["success"] is False


def test_sibling_types_are_not_collapsed() -> None:
    task = _et001()
    predicted = GraphDelta(
        type_assertions=(
            TypeAssertion(MINTED, "Supplier"),
            TypeAssertion(MINTED, "Person"),
        ),
        literals=(
            LiteralSet(MINTED, "legalName", "Acme Components Ltd"),
            LiteralSet(MINTED, "registrationId", "GB-000111222"),
        ),
    )
    metrics = score_task(predicted, task)
    assert metrics["success"] is False
    assert metrics["graph_delta_precision"] == 0.75
    assert metrics["graph_delta_recall"] == 1.0


def test_wrong_type_id_still_misses_after_alignment() -> None:
    task = _et001()
    predicted = GraphDelta(
        type_assertions=(TypeAssertion(MINTED, "org"),),
        literals=(LiteralSet(MINTED, "legalName", "Acme Components Ltd"),),
    )
    metrics = score_task(predicted, task)
    assert metrics["success"] is False
    assert metrics["graph_delta_f1"] != 1.0


def test_fixtures_still_have_no_entity_uri_key() -> None:
    for task in load_tasks():
        assert "entity_uri" not in task.input, task.task_id
        assert "mint_as" not in task.input, task.task_id


def test_schema_hint_still_omits_supplier_and_gold_uri() -> None:
    assert TEMPLATE_ID == "ontology_skills.prompt.v5"
    assert "Supplier" not in SCHEMA_HINT
    assert GOLD_ENT not in SCHEMA_HINT
    assert "acme-components" not in SCHEMA_HINT
    assert "do not repeat those facts as adds" in SCHEMA_HINT
