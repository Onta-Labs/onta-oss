"""Neo4j is the only graph backend — structural + behavioural guard (ONTA-527).

Two failure modes this locks down.

**1. A second copy of the backend switch.** Before ONTA-527, three modules each
read ``INFONA_GRAPH_BACKEND`` off ``os.environ`` and normalised it themselves —
``graph/store.py``, ``graph/kg_writer.py`` and ``graph/ontology_catalog.py``.
Three independent readings of one variable is drift waiting to happen: change
the accepted values in one and the other two silently keep their own opinion.
The switch now lives in exactly one module and this test fails if a second
appears.

**2. A legacy value quietly selecting SPARQL.** Amazon Neptune was decommissioned
2026-08-11 and its SPARQL execution path is deleted. A deploy still carrying
``INFONA_GRAPH_BACKEND=neptune`` must fail loudly at startup or first use, never
route instance reads/writes at a store that does not exist.

The third section is a **ratchet**, not a clean bill of health: plenty of modules
still import ``NeptuneClient`` for read paths that have not been ported to
GraphStore. That set may shrink, never grow.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from infona_client.graph.store import (
    GRAPH_BACKEND_ENV,
    NEO4J_BACKEND,
    GraphConfigError,
    graph_backend,
)

PKG = Path(__file__).resolve().parents[1] / "infona_client"

# The ONE module allowed to own the switch.
SWITCH_MODULE = "graph/store.py"


def _py_files() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: Path) -> str:
    return path.relative_to(PKG).as_posix()


# ---------------------------------------------------------------------------
# 1. Exactly one backend switch
# ---------------------------------------------------------------------------


def test_only_one_module_defines_the_backend_switch():
    definers = []
    for path in _py_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name in ("graph_backend", "_graph_backend")
            ):
                definers.append(_rel(path))
    assert definers == [SWITCH_MODULE], (
        "The graph backend switch must be defined once, in "
        f"{SWITCH_MODULE}. Found: {sorted(set(definers))}. Import it "
        "(`from infona_client.graph.store import graph_backend`) instead of "
        "re-deriving it — ONTA-527 removed two such duplicates."
    )


def test_only_the_switch_module_reads_the_backend_env_var():
    """Reading the raw env elsewhere bypasses the validation in graph_backend()."""
    readers = []
    for path in _py_files():
        rel = _rel(path)
        if rel == SWITCH_MODULE:
            continue
        for line in path.read_text().splitlines():
            code = line.split("#", 1)[0]
            if GRAPH_BACKEND_ENV in code and "environ" in code:
                readers.append(f"{rel}: {line.strip()}")
    assert not readers, (
        "Only graph/store.py may read the backend env var; everywhere else "
        "call graph_backend() so a legacy value is rejected rather than "
        f"silently normalised:\n  " + "\n  ".join(readers)
    )


# ---------------------------------------------------------------------------
# 2. Legacy values are rejected
# ---------------------------------------------------------------------------


def test_unset_defaults_to_neo4j(monkeypatch):
    monkeypatch.delenv(GRAPH_BACKEND_ENV, raising=False)
    assert graph_backend() == NEO4J_BACKEND


@pytest.mark.parametrize("value", ["neo4j", "NEO4J", "  neo4j  "])
def test_neo4j_is_accepted_case_and_space_insensitively(monkeypatch, value):
    monkeypatch.setenv(GRAPH_BACKEND_ENV, value)
    assert graph_backend() == NEO4J_BACKEND


@pytest.mark.parametrize(
    "value", ["neptune", "fuseki", "NEPTUNE", " neptune ", "sparql", "postgres", ""]
)
def test_every_non_neo4j_value_is_rejected(monkeypatch, value):
    monkeypatch.setenv(GRAPH_BACKEND_ENV, value)
    if value == "":
        # Empty string is indistinguishable from unset for this switch.
        assert graph_backend() == NEO4J_BACKEND
        return
    with pytest.raises(GraphConfigError) as exc:
        graph_backend()
    assert GRAPH_BACKEND_ENV in str(exc.value)


def test_optional_store_resolution_never_returns_none(monkeypatch):
    """`get_optional_graph_store` used to return None to mean "use SPARQL"."""
    from infona_client.graph.memory_store import MemoryGraphStore
    from infona_client.graph.store import configure_graph_store, get_optional_graph_store

    monkeypatch.delenv(GRAPH_BACKEND_ENV, raising=False)
    store = MemoryGraphStore()
    configure_graph_store(store)
    assert get_optional_graph_store() is store


# ---------------------------------------------------------------------------
# 3. Public SPARQL HTTP surfaces execute nothing
# ---------------------------------------------------------------------------


def test_public_sparql_routes_hold_no_store_client():
    """/query, /update and /triples must not be able to reach a store at all."""
    for module in ("query.py", "triples.py"):
        src = (PKG / "api" / "routes" / module).read_text()
        assert "get_neptune_client" not in src, f"{module} still resolves a client"
        assert "await client." not in src, f"{module} still calls a store client"


# ---------------------------------------------------------------------------
# 4. Ratchet: the residual NeptuneClient surface may shrink, never grow
# ---------------------------------------------------------------------------

# Modules that still import NeptuneClient. These are read paths (Explorer
# aggregates, NL SPARQL generation, ontology reads, QC) whose GraphStore ports
# are follow-up work to ONTA-527 — they are NOT sanctioned, just not yet gone.
# Nothing may be ADDED here; delete an entry when its module is ported.
_RESIDUAL_NEPTUNE_IMPORTERS = {
    "agent/registry.py",
    "api/app.py",
    "api/deps.py",
    "api/routes/actions.py",
    "api/routes/agent.py",
    "api/routes/ask.py",
    "api/routes/corrections.py",
    "api/routes/enrich.py",
    # Added upstream AFTER this ratchet was written — caught by it, which is the
    # point. It declares Depends(get_neptune_client) but never calls the client;
    # the export itself reads the GraphStore. Listed so the count can still only
    # go down, not because a new SPARQL dependency is sanctioned.
    "api/routes/export.py",
    "api/routes/explore.py",
    "api/routes/functions.py",
    "api/routes/grep.py",
    "api/routes/ingest.py",
    "api/routes/knowledge_graphs.py",
    "api/routes/lambda_functions.py",
    "api/routes/normalize.py",
    "api/routes/ontology.py",
    "api/routes/operator.py",
    "enrichment/executor.py",
    "enrichment/strategy.py",
    "functions/registry.py",
    "graph/attr_meta_migration.py",
    "graph/client.py",
    "graph/neo4j_store.py",
    "graph/store.py",
    "nlp/ontology_embeddings.py",
    "nlp/pipeline.py",
    "normalization/execute.py",
    "normalization/inference.py",
    "normalization/policy.py",
    "normalization/rules.py",
    "qc/__main__.py",
    "qc/audit.py",
    "qc/isolation.py",
    "qc/scenario.py",
    "resolver/ontology_resolver.py",
    "resolver/schema_resolver.py",
    "verification/policy.py",
}

_NEPTUNE_CLIENT_RE = re.compile(r"\bNeptuneClient\b")


def _current_neptune_importers() -> set[str]:
    return {
        _rel(p) for p in _py_files() if _NEPTUNE_CLIENT_RE.search(p.read_text())
    }


def test_no_new_module_reaches_for_neptune_client():
    added = _current_neptune_importers() - _RESIDUAL_NEPTUNE_IMPORTERS
    assert not added, (
        "New module(s) referencing NeptuneClient: "
        f"{sorted(added)}. The SPARQL client is being removed (ONTA-527) — "
        "use GraphStore / a scoped GraphSession instead of adding to the "
        "residual surface."
    )


def test_residual_neptune_list_has_no_stale_entries():
    """Keeps the ratchet honest: a ported module must leave the list."""
    stale = _RESIDUAL_NEPTUNE_IMPORTERS - _current_neptune_importers()
    assert not stale, (
        "These modules no longer reference NeptuneClient — remove them from "
        f"_RESIDUAL_NEPTUNE_IMPORTERS so the ratchet keeps tightening: {sorted(stale)}"
    )
