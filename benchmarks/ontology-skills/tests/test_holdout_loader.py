"""Holdout loader points execute_task at fixtures/holdout. No live POST."""

from __future__ import annotations

from pathlib import Path

from ontology_skills.backends import CannedBackend
from ontology_skills.harness import ModelSpec
from ontology_skills.holdout import (
    HOLDOUT_DIR,
    execute_holdout_task,
    holdout_main,
    load_holdout_bundle,
)


def _dummy_backend(task_id: str) -> CannedBackend:
    return CannedBackend(
        responses={task_id: "{}"},
        model=ModelSpec(
            name="canned",
            quantization="none",
            param_count="n/a",
            backend="fixture",
        ),
    )


def test_load_holdout_bundle_is_the_holdout_tree() -> None:
    bundle = load_holdout_bundle()
    assert HOLDOUT_DIR.name == "holdout"
    assert "Carrier" in bundle.ontology.types
    assert "Supplier" not in bundle.ontology.types
    assert len(bundle.tasks) == 24
    assert all(t.task_id.startswith("ho-") for t in bundle.tasks)


def test_execute_holdout_task_compiles_against_holdout_ontology() -> None:
    bundle = load_holdout_bundle()
    task = next(t for t in bundle.tasks if t.task_id == "ho-et-01")
    result = execute_holdout_task(
        task,
        condition_id="4b_ontology_routed",
        backend=_dummy_backend(task.task_id),
    )
    assert "Carrier" in result.compiled.type_lineage
    assert "Supplier" not in result.compiled.type_lineage
    assert result.prompt_template_id == "ontology_skills.prompt.v5"
    assert result.metrics["success"] is False
    from ontology_skills.dataset import load_ontology
    from ontology_skills.executor import load_fixture_bundle as live_loader

    assert "Supplier" in load_ontology().types
    assert live_loader().ontology.types["Supplier"].type_id == "Supplier"


def test_holdout_gold_runs_through_prepare_for_score() -> None:
    """Minted slug + matching legalName is aligned. Gold ops stay as authored."""
    from ontology_skills.graph_delta import GraphDelta, LiteralSet, TypeAssertion
    from ontology_skills.scoring import graph_delta_prf, score_task

    task = next(t for t in load_holdout_bundle().tasks if t.task_id == "ho-et-01")
    minted = "https://graph.infona.ai/bench/ent/not-the-gold-slug"
    predicted = GraphDelta(
        type_assertions=(TypeAssertion(minted, "Carrier"),),
        literals=(
            LiteralSet(minted, "legalName", "Kestrel Haul Ltd"),
            LiteralSet(minted, "scac", "KSTL"),
        ),
    )
    raw = graph_delta_prf(predicted, task.gold)
    assert raw["recall"] == 0.0
    metrics = score_task(predicted, task)
    assert metrics["graph_delta_recall"] == 1.0
    assert metrics["success"] is True
    gold_ent = task.gold.type_assertions[0].entity
    assert minted != gold_ent
    assert gold_ent not in task.input.values()


def test_holdout_cli_requires_task_id() -> None:
    try:
        holdout_main([])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected argparse to require --task-id")


def test_holdout_cli_canned_without_text_is_not_a_score() -> None:
    assert holdout_main(["--task-id", "ho-et-01"]) == 2


def test_holdout_cli_live_without_key_does_not_post(clear_live_env: None) -> None:
    del clear_live_env
    assert holdout_main(["--backend", "live", "--task-id", "ho-et-01"]) == 2


def test_holdout_cli_canned_path_runs_one_task(tmp_path: Path) -> None:
    canned = tmp_path / "canned.jsonl"
    canned.write_text('{"task_id": "ho-et-01", "text": "{}"}\n', encoding="utf-8")
    assert (
        holdout_main(
            ["--task-id", "ho-et-01", "--canned", str(canned)]
        )
        == 0
    )
