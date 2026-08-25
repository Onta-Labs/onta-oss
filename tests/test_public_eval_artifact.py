"""Hermetic smoke: the published eval claim parses and keeps its miss column.

Does not call an LLM or a live API. Guards ONTA-541: docs/EVAL.md, the
README fragment, and docs/eval/public_results.json stay schema-complete
even when status is ``partial`` (no invented scores).
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ARTIFACT = REPO / "docs" / "eval" / "public_results.json"
EVAL_MD = REPO / "docs" / "EVAL.md"
FRAGMENT = REPO / "docs" / "_fragments" / "ONTA-541.md"
WRAPPER = REPO / "scripts" / "run_public_eval.py"

REQUIRED_TOP = {
    "schema_version",
    "status",
    "dataset",
    "models",
    "tiers",
    "failures",
    "repro",
}
REQUIRED_TIER = {"tier", "name", "passed", "total", "misses"}
REQUIRED_FAIL = {"tier", "question", "expected", "got", "verdict"}
TIER_NAMES = {1: "Count/Lookup", 2: "Filter", 3: "Join", 4: "Multi-hop"}


def _load() -> dict:
    assert ARTIFACT.is_file(), f"missing {ARTIFACT.relative_to(REPO)}"
    return json.loads(ARTIFACT.read_text())


def test_artifact_schema_and_status():
    data = _load()
    missing = REQUIRED_TOP - data.keys()
    assert not missing, f"public_results.json missing keys: {sorted(missing)}"
    assert data["schema_version"] == 1
    assert data["status"] in {"live", "partial"}
    assert isinstance(data["failures"], list)
    assert isinstance(data["tiers"], list)
    assert len(data["tiers"]) == 4
    assert data["dataset"]["path"] == "examples/trials.csv"
    assert data["dataset"]["name"] == "trials.csv"
    assert "query_model" in data["models"]
    assert "eval_judge" in data["models"] or "question_gen" in data["models"]
    assert "run_public_eval.py" in data["repro"]["command"]
    assert "infona ingest" in data["repro"]["ingest"]


def test_tiers_have_names_and_miss_column():
    data = _load()
    for i, row in enumerate(data["tiers"], start=1):
        missing = REQUIRED_TIER - row.keys()
        assert not missing, f"tier {i} missing {sorted(missing)}"
        assert row["tier"] == i
        assert row["name"] == TIER_NAMES[i]
        assert isinstance(row["misses"], list)


def test_partial_has_no_invented_scores_or_live_has_ints():
    data = _load()
    if data["status"] == "partial":
        assert "Not yet run on this tree" in (data.get("note") or "")
        for row in data["tiers"]:
            assert row["passed"] is None
            assert row["total"] is None
            assert row["misses"] == []
        assert data["failures"] == []
        return
    for row in data["tiers"]:
        assert isinstance(row["passed"], int)
        assert isinstance(row["total"], int)
    for fail in data["failures"]:
        missing = REQUIRED_FAIL - fail.keys()
        assert not missing, f"failure missing {sorted(missing)}"
        assert fail["verdict"] != "correct"


def test_eval_md_has_table_and_stranger_command():
    text = EVAL_MD.read_text()
    assert EVAL_MD.is_file()
    assert "Visible misses" in text
    assert "examples/trials.csv" in text
    assert "openai/gpt-oss-120b" in text
    assert "deepseek/deepseek-v4-pro-0813" in text
    assert "python scripts/run_public_eval.py" in text
    assert "infona ingest" in text
    assert "always-LLM Cypher" in text
    # Four tier rows plus the visible-miss column header.
    assert text.count("| Count/Lookup |") >= 1
    assert text.count("| Filter |") >= 1
    assert text.count("| Join |") >= 1
    assert text.count("| Multi-hop |") >= 1


def test_fragment_is_readme_ready():
    text = FRAGMENT.read_text()
    assert FRAGMENT.is_file()
    assert "Visible misses" in text
    assert "examples/trials.csv" in text
    assert "python scripts/run_public_eval.py" in text
    assert "docs/EVAL.md" in text
    assert "| Count/Lookup |" in text
    assert "| Multi-hop |" in text


def test_wrapper_is_thin_and_not_an_infona_eval_cli():
    text = WRAPPER.read_text()
    lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    assert lines <= 200, f"run_public_eval.py is {lines} lines (cap 200)"
    assert "run_full_eval" in text
    assert "does **not**" in text and "infona eval" in text
    assert "OPENROUTER_API_KEY" in text
    assert 'DEFAULT_EVAL_MODEL = "deepseek/deepseek-v4-pro-0813"' in text
    assert "cache_questions" in text
