"""Guardrail: @infona-ai/cli, @infona-ai/mcp, and infona-client stay on one version.

The writer is scripts/sync_release_version.py. This file is the harness:
in-repo versions + exact mcp pin must match, and --check-published must
refuse registry drift. Write tests copy the script into dest/ so ROOT is
the sandbox, not this repo.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "sync_release_version.py"


def _run(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(cwd / "scripts" / "sync_release_version.py"), *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def _sandbox(tmp_path: Path, *, cli: str, mcp: str, py: str, pin: str) -> Path:
    dest = tmp_path / "repo"
    dest.mkdir()
    (dest / "scripts").mkdir()
    shutil.copy(SCRIPT, dest / "scripts" / "sync_release_version.py")
    (dest / "packages" / "cli").mkdir(parents=True)
    (dest / "packages" / "mcp").mkdir(parents=True)
    (dest / "packages" / "cli" / "package.json").write_text(
        json.dumps({"name": "@infona-ai/cli", "version": cli}) + "\n"
    )
    (dest / "packages" / "mcp" / "package.json").write_text(
        json.dumps(
            {
                "name": "@infona-ai/mcp",
                "version": mcp,
                "dependencies": {"@infona-ai/cli": pin},
            }
        )
        + "\n"
    )
    (dest / "pyproject.toml").write_text(
        f'[project]\nname = "infona-client"\nversion = "{py}"\n'
    )
    return dest


def _load_script():
    spec = importlib.util.spec_from_file_location("sync_release_version", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _JsonResp:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode()

    def __enter__(self) -> "_JsonResp":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


def test_repo_packages_share_one_version() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "DRIFT" not in result.stderr
    assert "@infona-ai/cli=" in result.stdout
    assert "infona-client=" in result.stdout


def test_version_mismatch_is_drift(tmp_path: Path) -> None:
    dest = _sandbox(tmp_path, cli="0.1.19", mcp="0.1.19", py="0.1.0", pin="0.1.19")
    result = _run([], cwd=dest)
    assert result.returncode == 1
    assert "DRIFT" in result.stderr


def test_caret_pin_is_drift(tmp_path: Path) -> None:
    dest = _sandbox(tmp_path, cli="0.1.19", mcp="0.1.19", py="0.1.19", pin="^0.1.19")
    result = _run([], cwd=dest)
    assert result.returncode == 1
    assert "DRIFT" in result.stderr


def test_lockstep_and_exact_pin_pass(tmp_path: Path) -> None:
    dest = _sandbox(tmp_path, cli="1.2.3", mcp="1.2.3", py="1.2.3", pin="1.2.3")
    result = _run([], cwd=dest)
    assert result.returncode == 0, result.stderr
    assert "DRIFT" not in result.stderr


def test_check_published_lockstep() -> None:
    mod = _load_script()

    def fake_urlopen(req: object, timeout: int = 20) -> _JsonResp:
        url = getattr(req, "full_url", str(req))
        if url.endswith("/@infona-ai/cli/latest"):
            return _JsonResp({"version": "0.1.19"})
        if url.endswith("/@infona-ai/mcp/latest"):
            return _JsonResp(
                {"version": "0.1.19", "dependencies": {"@infona-ai/cli": "0.1.19"}}
            )
        if "pypi.org" in url:
            return _JsonResp({"info": {"version": "0.1.19"}})
        raise AssertionError(url)

    with patch.object(mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        assert mod.check_published() == 0


def test_check_published_reports_registry_drift() -> None:
    mod = _load_script()

    def fake_urlopen(req: object, timeout: int = 20) -> _JsonResp:
        url = getattr(req, "full_url", str(req))
        if url.endswith("/@infona-ai/cli/latest"):
            return _JsonResp({"version": "0.1.19"})
        if url.endswith("/@infona-ai/mcp/latest"):
            return _JsonResp(
                {"version": "0.1.19", "dependencies": {"@infona-ai/cli": "0.1.19"}}
            )
        if "pypi.org" in url:
            return _JsonResp({"info": {"version": "0.1.18"}})
        raise AssertionError(url)

    with patch.object(mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        assert mod.check_published() == 1


def test_check_published_reports_caret_pin() -> None:
    mod = _load_script()

    def fake_urlopen(req: object, timeout: int = 20) -> _JsonResp:
        url = getattr(req, "full_url", str(req))
        if url.endswith("/@infona-ai/cli/latest"):
            return _JsonResp({"version": "0.1.19"})
        if url.endswith("/@infona-ai/mcp/latest"):
            return _JsonResp(
                {"version": "0.1.19", "dependencies": {"@infona-ai/cli": "^0.1.19"}}
            )
        if "pypi.org" in url:
            return _JsonResp({"info": {"version": "0.1.19"}})
        raise AssertionError(url)

    with patch.object(mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        assert mod.check_published() == 1
