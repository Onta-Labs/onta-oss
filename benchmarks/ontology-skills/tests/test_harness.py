"""Harness stub writes a locked run-log row; no fake metrics."""

from __future__ import annotations

import json
from io import StringIO

import pytest

from ontology_skills.conditions import CONDITION_MATRIX, condition_by_id
from ontology_skills.dataset import load_fixture_bundle
from ontology_skills.harness import (
    SCHEMA_VERSION,
    RunResult,
    compile_for_condition,
    run_stub,
    write_result_row,
)
from ontology_skills.scoring import empty_metrics


REQUIRED_ROW_KEYS = {
    "schema_version",
    "run_id",
    "created_at",
    "status",
    "condition",
    "task_id",
    "task_family",
    "split",
    "model",
    "prompt",
    "context_budget",
    "tools",
    "decoding",
    "resources",
    "compiler",
    "metrics",
    "parse",
    "predicted",
    "notes",
}

REQUIRED_MODEL_KEYS = {"name", "quantization", "param_count", "backend"}
REQUIRED_DECODING_KEYS = {
    "temperature",
    "top_p",
    "top_k",
    "seed",
    "max_new_tokens",
}
REQUIRED_RESOURCE_KEYS = {
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "ram_mb",
    "vram_mb",
    "hosted_cost_usd",
}
REQUIRED_METRIC_KEYS = set(empty_metrics())


def test_matrix_order_is_locked() -> None:
    ids = tuple(c.condition_id for c in CONDITION_MATRIX)
    assert ids == (
        "4b_vanilla",
        "4b_ontology_context",
        "4b_flat_skills",
        "4b_ontology_routed",
        "4b_ft_ontology_routed",
        "9b_vanilla",
        "27b_or_frontier_vanilla",
        "teacher_skills_4b",
    )
    assert CONDITION_MATRIX[3].index == 4
    assert CONDITION_MATRIX[4].runnable is False
    assert CONDITION_MATRIX[4].fine_tuned is True


def test_fine_tune_condition_is_blocked() -> None:
    bundle = load_fixture_bundle()
    cond = condition_by_id("4b_ft_ontology_routed")
    with pytest.raises(RuntimeError, match="blocked"):
        compile_for_condition(
            bundle.ontology, bundle.tasks[0].neighborhood, cond
        )


def test_stub_row_has_null_metrics_and_required_keys() -> None:
    bundle = load_fixture_bundle()
    task = bundle.tasks[0]
    cond = condition_by_id("4b_ontology_routed")
    compiled = compile_for_condition(bundle.ontology, task.neighborhood, cond)
    row = RunResult(condition=cond, task=task, compiled=compiled).to_dict()
    assert set(row) == REQUIRED_ROW_KEYS
    assert row["schema_version"] == SCHEMA_VERSION
    assert row["status"] == "stub"
    assert set(row["model"]) == REQUIRED_MODEL_KEYS
    assert set(row["decoding"]) == REQUIRED_DECODING_KEYS
    assert set(row["resources"]) == REQUIRED_RESOURCE_KEYS
    assert set(row["metrics"]) == REQUIRED_METRIC_KEYS
    assert all(v is None for v in row["metrics"].values())
    assert row["parse"] == {"ok": None, "error": None}
    assert row["predicted"]["adds"] == []
    assert row["predicted"]["type_assertions"] == []
    assert row["compiler"]["mode"] == "routed"
    assert row["compiler"]["skill_ids"]
    assert row["prompt"]["skill_injection"] == "routed"


def test_write_result_row_jsonl(tmp_path) -> None:
    bundle = load_fixture_bundle()
    task = bundle.tasks[0]
    cond = condition_by_id("4b_vanilla")
    compiled = compile_for_condition(bundle.ontology, task.neighborhood, cond)
    result = RunResult(condition=cond, task=task, compiled=compiled)
    dest = tmp_path / "rows.jsonl"
    write_result_row(result, dest)
    write_result_row(result, dest)
    lines = dest.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    parsed = json.loads(lines[0])
    assert parsed["condition"]["condition_id"] == "4b_vanilla"
    assert parsed["compiler"]["mode"] == "none"


def test_run_stub_covers_every_fixture_task() -> None:
    buf = StringIO()
    rows = run_stub(condition_id="4b_ontology_routed", dest=buf)
    bundle = load_fixture_bundle()
    assert len(rows) == len(bundle.tasks)
    dumped = buf.getvalue().strip().splitlines()
    assert len(dumped) == len(rows)
    assert json.loads(dumped[0])["metrics"]["success"] is None
