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


def _sandbox(
    dest: Path,
    *,
    cli: str,
    mcp: str,
    py: str,
    pin: str = "^0.1.0",
    root: str | None = None,
) -> None:
    dest.mkdir()
    (dest / "scripts").mkdir()
    shutil.copy(SCRIPT, dest / "scripts" / "sync_release_version.py")
    (dest / "packages" / "cli").mkdir(parents=True)
    (dest / "packages" / "mcp").mkdir(parents=True)
    (dest / "package.json").write_text(
        json.dumps({"name": "infona-oss-monorepo", "private": True, "version": root or cli})
        + "\n"
    )
    (dest / "packages" / "cli" / "package.json").write_text(
        json.dumps({"name": "@infona-ai/cli", "version": cli}) + "\n"
    )
    (dest / "packages" / "mcp" / "package.json").write_text(
        json.dumps(
            {"name": "@infona-ai/mcp", "version": mcp, "dependencies": {"@infona-ai/cli": pin}}
        )
        + "\n"
    )
    (dest / "pyproject.toml").write_text(f'[project]\nname = "infona-client"\nversion = "{py}"\n')


def test_script_reports_lockstep_packages() -> None:
    result = _run([])
    out = result.stdout
    assert result.returncode == 0, result.stderr + out
    assert "infona-oss-monorepo=" in out
    assert "@infona-ai/cli=" in out
    assert "@infona-ai/mcp=" in out
    assert "infona-client=" in out
    assert "DRIFT" not in result.stderr


def test_set_writes_the_same_version(tmp_path: Path) -> None:
    dest = tmp_path / "repo"
    _sandbox(dest, cli="0.1.0", mcp="0.1.1", py="0.0.9", pin="^0.1.0", root="0.1.0")

    result = _run(["--set", "1.2.3"], cwd=dest)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1.2.3"
    root = json.loads((dest / "package.json").read_text())
    cli = json.loads((dest / "packages" / "cli" / "package.json").read_text())
    mcp = json.loads((dest / "packages" / "mcp" / "package.json").read_text())
    py = (dest / "pyproject.toml").read_text()
    assert root["version"] == "1.2.3"
    assert cli["version"] == "1.2.3"
    assert mcp["version"] == "1.2.3"
    assert mcp["dependencies"]["@infona-ai/cli"] == "1.2.3"
    assert 'version = "1.2.3"' in py


def test_root_package_json_mismatch_is_drift(tmp_path: Path) -> None:
    dest = tmp_path / "repo"
    _sandbox(dest, cli="0.1.20", mcp="0.1.20", py="0.1.20", pin="0.1.20", root="0.1.0")
    result = _run([], cwd=dest)
    assert result.returncode == 1
    assert "DRIFT" in result.stderr


def test_bump_patch_uses_the_highest_current(tmp_path: Path) -> None:
    dest = tmp_path / "repo"
    _sandbox(dest, cli="0.1.18", mcp="0.1.17", py="0.1.0", root="0.1.0")
    result = _run(["--bump", "patch"], cwd=dest)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.1.19"
    root = json.loads((dest / "package.json").read_text())
    assert root["version"] == "0.1.19"


@pytest.mark.parametrize("bad", ["1.2", "v1.2.3", "1.2.3.4", ""])
def test_rejects_non_patch_semver(tmp_path: Path, bad: str) -> None:
    dest = tmp_path / "repo"
    _sandbox(dest, cli="0.1.0", mcp="0.1.0", py="0.1.0")
    result = _run(["--set", bad], cwd=dest)
    assert result.returncode != 0
