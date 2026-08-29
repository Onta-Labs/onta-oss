"""Exact-set scoring against gold GraphDeltas. No judge model."""

from __future__ import annotations

import json
from pathlib import Path

from ontology_skills.dataset import load_fixture_bundle, load_tasks
from ontology_skills.graph_delta import (
    GraphDelta,
    LiteralSet,
    Merge,
    Triple,
    TypeAssertion,
)
from ontology_skills.scoring import (
    graph_delta_prf,
    pairwise_er_prf,
    score_main,
    score_prediction,
    score_task,
    task_success,
)


def test_exact_match_is_success() -> None:
    gold = GraphDelta(
        type_assertions=(
            TypeAssertion(
                "https://graph.infona.ai/bench/ent/acme-components", "Supplier"
            ),
        )
    )
    assert task_success(gold, gold) is True
    prf = graph_delta_prf(gold, gold)
    assert prf == {"precision": 1.0, "recall": 1.0, "f1": 1.0}


def test_extra_op_hurts_precision() -> None:
    gold = GraphDelta(
        adds=(
            Triple("s", "https://graph.infona.ai/bench/onto/SUPPLIES_TO", "o"),
        )
    )
    predicted = GraphDelta(
        adds=gold.adds
        + (Triple("s", "https://graph.infona.ai/bench/onto/EMPLOYS", "x"),)
    )
    prf = graph_delta_prf(predicted, gold)
    assert prf["precision"] == 0.5
    assert prf["recall"] == 1.0
    assert task_success(predicted, gold) is False


def test_empty_predicted_vs_nonempty_gold() -> None:
    gold = GraphDelta(
        type_assertions=(
            TypeAssertion(
                "https://graph.infona.ai/bench/ent/acme-components", "Supplier"
            ),
        )
    )
    prf = graph_delta_prf(GraphDelta(), gold)
    assert prf == {"precision": None, "recall": 0.0, "f1": 0.0}
    metrics = score_prediction(GraphDelta(), gold, family="entity_typing")
    assert metrics["success"] is False
    assert metrics["graph_delta_precision"] is None
    assert metrics["graph_delta_recall"] == 0.0
    assert metrics["graph_delta_f1"] == 0.0
    assert metrics["constraint_valid"] is None
    assert metrics["er_f1"] is None


def test_er_pairs_are_undirected() -> None:
    gold = (Merge("a", "b"),)
    predicted = (Merge("b", "a"),)
    prf = pairwise_er_prf(predicted, gold)
    assert prf == {"precision": 1.0, "recall": 1.0, "f1": 1.0}
    metrics = score_prediction(
        GraphDelta(merges=(Merge("b", "a"),)),
        GraphDelta(merges=(Merge("a", "b"),)),
        family="entity_resolution",
    )
    # Exact success uses directed canonical MERGE strings; P/R/F1 is undirected.
    assert metrics["success"] is False
    assert metrics["er_f1"] == 1.0


def test_gold_versus_gold_on_every_fixture() -> None:
    bundle = load_fixture_bundle()
    for task in bundle.tasks:
        metrics = score_task(task.gold, task)
        assert metrics["success"] is True, task.task_id
        assert metrics["graph_delta_f1"] == 1.0, task.task_id
        if task.family == "constraint_violation_repair":
            assert metrics["constraint_valid"] is True, task.task_id
        else:
            assert metrics["constraint_valid"] is None, task.task_id
        if task.family == "entity_resolution":
            assert metrics["er_f1"] == 1.0, task.task_id
        else:
            assert metrics["er_f1"] is None, task.task_id


def test_empty_pred_on_fixture_matches_spec() -> None:
    task = next(t for t in load_tasks() if t.task_id == "et-001")
    metrics = score_task(GraphDelta(), task)
    assert metrics["success"] is False
    assert metrics["graph_delta_precision"] is None
    assert metrics["graph_delta_recall"] == 0.0
    assert metrics["graph_delta_f1"] == 0.0


def test_cvr_empty_pred_is_constraint_invalid() -> None:
    task = next(t for t in load_tasks() if t.task_id == "cvr-001")
    metrics = score_task(GraphDelta(), task)
    assert metrics["constraint_valid"] is False
    assert metrics["success"] is False


def test_cvr_does_not_launder_person_into_supplier() -> None:
    task = next(t for t in load_tasks() if t.task_id == "cvr-001")
    jamie = "https://graph.infona.ai/bench/ent/jamie-lee"
    predicted = GraphDelta(
        type_assertions=(TypeAssertion(jamie, "Supplier"),)
    )
    metrics = score_task(predicted, task)
    assert metrics["success"] is False
    assert metrics["constraint_valid"] is False


def test_full_type_iri_and_wrong_slug_scores_zero_on_et001() -> None:
    """Live v1 smoke: IRI type_ids + invented slug is a miss, not a score."""
    task = next(t for t in load_tasks() if t.task_id == "et-001")
    bogus = "https://graph.infona.ai/bench/ent/registration-id"
    onto = "https://graph.infona.ai/bench/onto/"
    predicted = GraphDelta(
        type_assertions=(
            TypeAssertion(bogus, onto + "Supplier"),
            TypeAssertion(bogus, onto + "Company"),
            TypeAssertion(bogus, onto + "Organization"),
        ),
        literals=(LiteralSet(bogus, onto + "has_vat", "GB-000111222"),),
    )
    metrics = score_task(predicted, task)
    assert metrics["success"] is False
    assert metrics["graph_delta_f1"] == 0.0


def test_score_cli_task_id(tmp_path: Path) -> None:
    task = next(t for t in load_tasks() if t.task_id == "et-001")
    pred = tmp_path / "pred.json"
    pred.write_text(json.dumps(task.gold.to_dict()), encoding="utf-8")
    assert score_main(["--task-id", "et-001", "--predicted", str(pred)]) == 0
