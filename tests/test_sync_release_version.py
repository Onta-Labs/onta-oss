"""Hermetic tests for scripts/sync_release_version.py."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "sync_release_version.py"


def _run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    root = cwd or REPO
    script = root / "scripts" / "sync_release_version.py"
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def test_script_reports_three_packages() -> None:
    result = _run([])
    out = result.stdout
    assert result.returncode == 0, result.stderr + out
    assert "@infona-ai/cli=" in out
    assert "@infona-ai/mcp=" in out
    assert "infona-client=" in out
    assert "DRIFT" not in result.stderr


def test_set_writes_the_same_version(tmp_path: Path) -> None:
    dest = tmp_path / "repo"
    dest.mkdir()
    (dest / "scripts").mkdir()
    shutil.copy(SCRIPT, dest / "scripts" / "sync_release_version.py")
    (dest / "packages" / "cli").mkdir(parents=True)
    (dest / "packages" / "mcp").mkdir(parents=True)
    (dest / "packages" / "cli" / "package.json").write_text(
        json.dumps({"name": "@infona-ai/cli", "version": "0.1.0"}) + "\n"
    )
    (dest / "packages" / "mcp" / "package.json").write_text(
        json.dumps(
            {
                "name": "@infona-ai/mcp",
                "version": "0.1.1",
                "dependencies": {"@infona-ai/cli": "^0.1.0"},
            }
        )
        + "\n"
    )
    (dest / "pyproject.toml").write_text('[project]\nname = "infona-client"\nversion = "0.0.9"\n')

    result = _run(["--set", "1.2.3"], cwd=dest)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1.2.3"
    cli = json.loads((dest / "packages" / "cli" / "package.json").read_text())
    mcp = json.loads((dest / "packages" / "mcp" / "package.json").read_text())
    py = (dest / "pyproject.toml").read_text()
    assert cli["version"] == "1.2.3"
    assert mcp["version"] == "1.2.3"
    assert mcp["dependencies"]["@infona-ai/cli"] == "1.2.3"
    assert 'version = "1.2.3"' in py


def test_bump_patch_uses_the_highest_current(tmp_path: Path) -> None:
    dest = tmp_path / "repo"
    dest.mkdir()
    (dest / "scripts").mkdir()
    shutil.copy(SCRIPT, dest / "scripts" / "sync_release_version.py")
    (dest / "packages" / "cli").mkdir(parents=True)
    (dest / "packages" / "mcp").mkdir(parents=True)
    (dest / "packages" / "cli" / "package.json").write_text(
        '{"name":"@infona-ai/cli","version":"0.1.18"}\n'
    )
    (dest / "packages" / "mcp" / "package.json").write_text(
        '{"name":"@infona-ai/mcp","version":"0.1.17","dependencies":{"@infona-ai/cli":"^0.1.0"}}\n'
    )
    (dest / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    result = _run(["--bump", "patch"], cwd=dest)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.1.19"


@pytest.mark.parametrize("bad", ["1.2", "v1.2.3", "1.2.3.4", ""])
def test_rejects_non_patch_semver(tmp_path: Path, bad: str) -> None:
    dest = tmp_path / "repo"
    dest.mkdir()
    (dest / "scripts").mkdir()
    shutil.copy(SCRIPT, dest / "scripts" / "sync_release_version.py")
    (dest / "packages" / "cli").mkdir(parents=True)
    (dest / "packages" / "mcp").mkdir(parents=True)
    (dest / "packages" / "cli" / "package.json").write_text(
        '{"name":"@infona-ai/cli","version":"0.1.0"}\n'
    )
    (dest / "packages" / "mcp" / "package.json").write_text(
        '{"name":"@infona-ai/mcp","version":"0.1.0"}\n'
    )
    (dest / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    result = _run(["--set", bad], cwd=dest)
    assert result.returncode != 0
