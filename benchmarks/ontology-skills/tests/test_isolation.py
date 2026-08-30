"""The benchmark package must not import product runtime or old eval harnesses."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANNED_PREFIXES = ("infona_client", "infona")


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_no_infona_client_imports() -> None:
    py_files = sorted((ROOT / "ontology_skills").rglob("*.py"))
    py_files += sorted((ROOT / "tests").rglob("*.py"))
    assert py_files
    for path in py_files:
        for name in _imported_modules(path):
            assert not name.startswith(BANNED_PREFIXES), (
                f"{path} imports {name!r}; this benchmark is isolated"
            )
