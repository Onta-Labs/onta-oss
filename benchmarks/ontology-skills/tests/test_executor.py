"""Canned executor loop: prompt → parse → score → run log. No network."""

from __future__ import annotations

from io import StringIO

import pytest

from ontology_skills.backends import LiveBackend, load_canned
from ontology_skills.executor import execute_main, execute_task, run_dry
from ontology_skills.harness import DecodingSpec


def test_canned_et001_is_exact_success() -> None:
    rows = run_dry(condition_id="4b_ontology_routed", task_id="et-001")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok"
    assert row["metrics"]["success"] is True
    assert row["metrics"]["graph_delta_f1"] == 1.0
    assert row["prompt"]["sha256"]
    assert row["prompt"]["template_id"] == "ontology_skills.prompt.v2"
    assert row["prompt"]["skill_injection"] == "routed"
    assert row["compiler"]["mode"] == "routed"
    assert row["compiler"]["skill_ids"]
    assert row["model"]["backend"] == "fixture"
    assert row["resources"]["latency_ms"] is None
    assert row["resources"]["hosted_cost_usd"] is None
    assert "canned-fixture" in row["notes"]
    assert row["schema_version"] == "1.1.0"
    assert row["parse"]["ok"] is True
    assert row["parse"]["error"] is None
    assert row["predicted"]["type_assertions"]


def test_canned_prose_is_parse_error_not_a_score_invention() -> None:
    rows = run_dry(condition_id="4b_ontology_routed", task_id="et-002")
    row = rows[0]
    assert row["status"] == "error"
    assert row["metrics"]["success"] is False
    assert row["metrics"]["graph_delta_precision"] is None
    assert row["metrics"]["graph_delta_recall"] == 0.0
    assert "parse_failure" in row["notes"]
    assert row["parse"]["ok"] is False
    assert row["parse"]["error"]
    assert row["predicted"]["type_assertions"] == []
    assert row["predicted"]["literals"] == []


def test_condition_5_still_blocked() -> None:
    backend = load_canned()
    from ontology_skills.dataset import load_tasks

    task = next(t for t in load_tasks() if t.task_id == "et-001")
    with pytest.raises(RuntimeError, match="blocked"):
        execute_task(
            task,
            condition_id="4b_ft_ontology_routed",
            backend=backend,
            decoding=DecodingSpec(),
        )


def test_live_from_env_absent_is_none(clear_live_env: None) -> None:
    del clear_live_env
    assert LiveBackend.from_env() is None


def test_execute_cli_refuses_live_without_posting() -> None:
    assert execute_main(["--backend", "live"]) == 2


def test_execute_cli_dry_run_jsonl() -> None:
    buf = StringIO()
    rows = run_dry(
        condition_id="4b_vanilla", task_id="et-001", dest=buf
    )
    assert rows[0]["prompt"]["skill_injection"] == "none"
    dumped = buf.getvalue().strip().splitlines()
    assert len(dumped) == 1
    assert '"success": true' in dumped[0]
