"""Committed OpenAPI + API.md must match scripts/generate_api_docs.py.

The docs.yml path filter used to point at a package that does not exist
(``infona/``), so the generator never ran. This test is the hermetic PR
gate: generate into a temp dir and fail when the committed files drift.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "generate_api_docs.py"
WORKFLOW = REPO / ".github" / "workflows" / "docs.yml"
DOC_NAMES = ("openapi.json", "API.md")


def _generate(output_dir: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("INFONA_NEPTUNE_ENDPOINT", "http://fake:8182")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--output-dir", str(output_dir)],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_docs_workflow_points_at_infona_client() -> None:
    text = WORKFLOW.read_text()
    assert "infona_client/api/**" in text
    assert "infona_client/models/**" in text
    assert "scripts/generate_api_docs.py" in text
    assert "'infona/api/**'" not in text
    assert "'infona/models/**'" not in text
    assert "git diff --exit-code" in text


def test_committed_api_docs_match_generator(tmp_path: Path) -> None:
    result = _generate(tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    stale: list[str] = []
    for name in DOC_NAMES:
        generated = (tmp_path / name).read_text()
        committed = (REPO / "docs" / name).read_text()
        if generated != committed:
            stale.append(name)
    if stale:
        pytest.fail(
            "generated API docs drifted: "
            + ", ".join(stale)
            + ". Run: python scripts/generate_api_docs.py"
        )
