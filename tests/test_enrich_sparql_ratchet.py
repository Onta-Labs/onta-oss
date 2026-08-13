"""Ratchet: the enrichment rail must not call SPARQL (ONTA-527).

Production is Neo4j-only. Dual-arm ``type is NeptuneClient`` / fail-open
``except: return {}`` / residual ``client.query(list_types_query)`` is how
prod jobs finished 50/50 no_match in <1s (oss #369, #372, then
``load_strategy``). This test fails CI if those call sites come back.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

OSS = Path(__file__).resolve().parents[1]
TARGETS = [
    OSS / "infona_client" / "enrichment",
    OSS / "infona_client" / "agent" / "capabilities" / "enrich_cap.py",
]

FORBIDDEN_NAMES = frozenset(
    {
        "parse_sparql_results",
        "list_types_query",
        "get_attribute_range_query",
        "_build_strategy_query",
        "_build_select_query",
        "_resolve_scope_predicate_query",
    }
)


def _py_files() -> list[Path]:
    out: list[Path] = []
    for target in TARGETS:
        if target.is_dir():
            out.extend(p for p in target.rglob("*.py") if "__pycache__" not in p.parts)
        elif target.is_file():
            out.append(target)
    return sorted(out)


def _rel(path: Path) -> str:
    return path.relative_to(OSS).as_posix()


def test_enrich_rail_does_not_call_sparql():
    hits: list[str] = []
    for path in _py_files():
        src = path.read_text()
        tree = ast.parse(src, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "query":
                # ``session.execute_template`` / ``logger`` are fine; we want
                # ``client.query`` / ``neptune.query`` / ``self._neptune.query``.
                if isinstance(node.value, ast.Name) and node.value.id in {
                    "client",
                    "neptune",
                }:
                    hits.append(f"{_rel(path)}:{node.lineno} .{node.value.id}.query")
                if isinstance(node.value, ast.Attribute) and node.value.attr in {
                    "neptune",
                    "_neptune",
                }:
                    hits.append(
                        f"{_rel(path)}:{node.lineno} .{node.value.attr}.query"
                    )
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                hits.append(f"{_rel(path)}:{node.lineno} {node.id}")
            if isinstance(node, ast.Compare):
                # ``type(x) is NeptuneClient`` dual-arm
                if any(
                    isinstance(op, ast.Is) for op in node.ops
                ) and any(
                    isinstance(c, ast.Name) and c.id == "NeptuneClient"
                    for c in node.comparators
                ):
                    hits.append(f"{_rel(path)}:{node.lineno} type(...) is NeptuneClient")
    assert hits == [], (
        "Enrichment rail still has SPARQL / NeptuneClient dual-arm call sites "
        "(ONTA-527). Delete them; do not fail-open into SparqlClientRetired:\n  "
        + "\n  ".join(hits)
    )


def test_ratchet_targets_exist():
    missing = [str(t) for t in TARGETS if not t.exists()]
    assert missing == [], missing


@pytest.mark.parametrize("name", sorted(FORBIDDEN_NAMES))
def test_forbidden_names_are_real_tokens(name):
    """Keep the set honest — these identifiers must stay banned, not typos."""
    assert name.isidentifier()
