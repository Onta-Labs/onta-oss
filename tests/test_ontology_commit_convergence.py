"""Drift guard: every ontology SCHEMA write must funnel through commit_ontology.

ONTA-403 — the schema-write analogue of ``test_write_path_convergence.py``
(ADR 0007). Builders in ``graph/ontology_queries.py`` remain the SPARQL
construction layer; only ``graph/ontology_commit.py`` may *apply* them in
production (call a builder and hand the SPARQL to ``neptune.update`` / call
the builder in a write path).

Two layers:
- **Structural** — deny-by-default source scan of ``infona_client/`` for raw
  builder CALLS outside the allowlist, with a planted-violation self-test.
- **Positive** — production write modules import/call ``commit_ontology``.
"""

from __future__ import annotations

import io
import pathlib
import re
import tokenize

import infona_client

# Schema-mutation builders that production code must NOT call directly.
_BUILDERS = (
    "insert_type",
    "insert_attribute",
    "insert_subtype",
    "upsert_type",
    "upsert_type_comment",
    "upsert_attribute",
    "set_object_property_range",
    "retract_object_property",
    "upsert_attribute_text_kind",
    "mark_core_slot",
    "delete_attribute_declaration",
)

# Modules permitted to *call* a builder. ontology_queries defines them;
# ontology_commit is the single application path.
_ALLOWLIST: dict[str, str] = {
    "graph/ontology_queries.py": "defines the SPARQL builders",
    "graph/ontology_commit.py": "the ONE commit path that applies builders (ONTA-403)",
    # Dual-backend catalog apply path (SPARQL builders + Neo4j pg upserts).
    "graph/ontology_catalog.py": "catalog dual-backend apply; wraps builders for SPARQL stores",
}

_PKG_ROOT = pathlib.Path(infona_client.__file__).parent

_CALL_RES = {
    name: re.compile(rf"(?<![\w.]){re.escape(name)}\(") for name in _BUILDERS
}


def _strip_comments(src: str) -> str:
    """Blank out ``#`` comment token spans (preserve strings/docstrings)."""
    lines = src.splitlines(keepends=True)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src
    for tok in toks:
        if tok.type != tokenize.COMMENT:
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        if srow == erow:
            line = lines[srow - 1]
            lines[srow - 1] = line[:scol] + " " * (ecol - scol) + line[ecol:]
    return "".join(lines)


def _builder_calls(code: str) -> list[str]:
    hits = []
    for name, cre in _CALL_RES.items():
        if cre.search(code):
            hits.append(f"{name}(")
    return hits


def test_no_raw_ontology_builder_call_outside_commit_path():
    """Scan all of ``infona_client/`` for raw schema-builder calls; fail on any
    hit outside the allowlist (deny-by-default)."""
    violations: list[str] = []
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        rel = path.relative_to(_PKG_ROOT).as_posix()
        if rel in _ALLOWLIST:
            continue
        code = _strip_comments(path.read_text())
        hits = _builder_calls(code)
        if hits:
            violations.append(f"{rel}: {', '.join(hits)}")
    assert not violations, (
        "Raw ontology-schema builder call(s) found OUTSIDE the commit path. "
        "Route these through infona_client.graph.ontology_commit.commit_ontology "
        "(ONTA-403), or — if the module is a justified exception — add it to "
        "_ALLOWLIST with a one-line reason. Offenders:\n  "
        + "\n  ".join(violations)
    )


def test_allowlist_entries_are_live():
    """Allowlist entries must still call (or define) a builder — no dead weight."""
    stale = []
    for rel in _ALLOWLIST:
        path = _PKG_ROOT / rel
        if not path.exists():
            stale.append(f"{rel} (missing)")
            continue
        if not _builder_calls(_strip_comments(path.read_text())):
            stale.append(f"{rel} (no builder calls — remove from allowlist)")
    assert not stale, "Stale ontology-commit allowlist entries:\n  " + "\n  ".join(stale)


def test_production_writers_call_commit_ontology():
    """The known schema-write modules must route through commit_ontology."""
    must_call = [
        "resolver/schema_resolver.py",
        "api/routes/ontology.py",
        "api/routes/ingest.py",
        "api/routes/lambda_functions.py",
        "agent/capabilities/ontology_cap.py",
        "enrichment/executor.py",
        "normalization/execute.py",
        "semantic/reconciler.py",
        "graph/attr_meta_migration.py",
    ]
    missing = []
    for rel in must_call:
        src = (_PKG_ROOT / rel).read_text()
        if "commit_ontology" not in src:
            missing.append(rel)
    assert not missing, (
        "production schema writers must call commit_ontology (ONTA-403):\n  "
        + "\n  ".join(missing)
    )


# --- Guard self-tests ----------------------------------------------------------


def test_guard_flags_planted_insert_type():
    planted = "async def f(n, g):\n    await n.update(insert_type(g, 'Person'))\n"
    assert "insert_type(" in _builder_calls(_strip_comments(planted))


def test_guard_flags_planted_upsert_attribute():
    planted = "await neptune.update(upsert_attribute(g, 'T', 'a', '', 'string'))\n"
    assert "upsert_attribute(" in _builder_calls(_strip_comments(planted))


def test_guard_ignores_comment_mentions():
    planted = "x = 1  # would have called insert_type() and upsert_attribute()\n"
    assert _builder_calls(_strip_comments(planted)) == []


def test_guard_would_fail_for_unallowlisted_writer():
    fake_rel = "api/routes/rogue_schema.py"
    fake_src = "await client.update(insert_type(g, 'X'))\n"
    hits = _builder_calls(_strip_comments(fake_src))
    is_violation = bool(hits) and fake_rel not in _ALLOWLIST
    assert is_violation
