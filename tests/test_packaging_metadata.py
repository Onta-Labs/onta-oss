"""Packaging metadata for the public ``infona-client`` PyPI package.

Reads ``pyproject.toml`` only — no network, no wheel build. Guards the
``pip install infona-client`` story: distribution name, requires-python,
repository URL, hatch wheel packages, and a tests-free sdist. The TS CLI
owns the ``infona`` bin; this package must not claim it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"
EXAMPLE_BANK = REPO / "infona_client" / "nlp" / "data" / "example_bank.jsonl"


def _pyproject() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_project_name_and_requires_python() -> None:
    project = _pyproject()["project"]
    assert project["name"] == "infona-client"
    assert project["requires-python"] == ">=3.12"


def test_repository_url() -> None:
    urls = _pyproject()["project"]["urls"]
    assert urls["Repository"] == "https://github.com/infona-ai/infona-oss"


def test_readme_and_classifiers() -> None:
    project = _pyproject()["project"]
    assert project["readme"] == "README.md"
    classifiers = project["classifiers"]
    assert "Programming Language :: Python :: 3.12" in classifiers
    assert "License :: OSI Approved :: Apache Software License" in classifiers
    assert "Framework :: FastAPI" in classifiers


def test_hatch_wheel_packages_include_infona_client() -> None:
    packages = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert "infona_client" in packages


def test_sdist_excludes_tests_and_eval() -> None:
    include = _pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    joined = " ".join(include)
    assert "infona_client" in joined
    assert "tests" not in joined
    assert "eval_holdout" not in joined


def test_example_bank_lives_in_package_tree() -> None:
    """Hatch wheels the ``infona_client`` tree; the few-shot bank must be in it."""
    assert EXAMPLE_BANK.is_file()


def test_no_infona_console_script() -> None:
    scripts = _pyproject()["project"].get("scripts") or {}
    assert "infona" not in scripts
