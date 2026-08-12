"""Tests for layer-aware subclass closure + parent map (ADR 0002 §1, COG-37).

Covers:
  - parent_map_query union variant: one GRAPH-pattern UNION branch per visible
    layer graph, each BINDing its graph URI so the caller can shadow.
  - Single-graph parent_map_query stays BYTE-IDENTICAL (regression-critical).
  - _fetch_parent_map: single-graph call path unchanged; layer-aware path
    merges edges across layers with tenant edges winning on duplicate child
    keys (shadowing), and degrades to {} on error.
  - type_name_from_uri: name extraction across all three layer namespaces.
  - add_layer_from_clauses: widening a graph-scoped generated query to the
    union of visible layer graphs, idempotently.
  - rewrite_type_predicate_to_closure: verified to need NO change for layers —
    see the dedicated section below for why.

All mocked — no live Neptune, no LLM, no network. Env handled via
patch.dict / fixtures only (never module-level mutation).

**ONTA-527 note.** Everything above the "NL pipeline" section is pure SPARQL
STRING construction (``parent_map_query``, ``add_layer_from_clauses``,
``rewrite_type_predicate_to_closure``) plus ``SchemaResolver._fetch_parent_map``,
which still builds and awaits that SPARQL. Those builders still exist and are
still exercised here, so the tests are kept — but be clear-eyed about what they
prove: ``_fetch_parent_map`` runs its query against the NeptuneClient, and in
production there is no SPARQL engine behind it, so the real behaviour is its
degrade-to-``{}`` branch. The one test that drove the widening END TO END
through ``ask()`` is now a strict xfail: the pipeline generates Cypher and
discards ``layer_graph_uris`` outright.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infona_client.graph.client import NeptuneClient
from infona_client.graph.layers import (
    LayerStack,
    enhanced_graph_uri,
    public_graph_uri,
    type_name_from_uri,
)
from infona_client.graph.ontology_queries import (
    add_layer_from_clauses,
    parent_map_query,
    rewrite_type_predicate_to_closure,
)
from infona_client.resolver.schema_resolver import SchemaResolver

TENANT_GRAPH = "https://graph.infona.ai/graphs/closure-test-tenant"
RDFS_SUBCLASS = "http://www.w3.org/2000/01/rdf-schema#subClassOf"


@pytest.fixture
def mock_neptune():
    client = AsyncMock(spec=NeptuneClient)
    client.health.return_value = True
    client.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    client.update.return_value = None
    return client


@pytest.fixture
def resolver(mock_neptune):
    verdict_path = Path(tempfile.mkdtemp()) / "verdicts.json"
    from infona_client.resolver.verdict_cache import JsonVerdictCache

    with patch.dict("os.environ", {
        "ANTHROPIC_API_KEY": "test-key",
        "OPENROUTER_API_KEY": "test-or-key",
        "INFONA_ER_ENABLED": "0",
    }):
        return SchemaResolver(
            neptune=mock_neptune,
            anthropic_key="test-key",
            verdict_cache=JsonVerdictCache(verdict_path),
        )


def _bindings(rows: list[dict[str, str]]) -> dict:
    """Shape rows into a raw SPARQL-JSON result for the mock Neptune."""
    return {
        "head": {"vars": list(rows[0].keys()) if rows else []},
        "results": {
            "bindings": [
                {k: {"type": "uri", "value": v} for k, v in row.items()}
                for row in rows
            ]
        },
    }


# ---------------------------------------------------------------------------
# parent_map_query — single-graph regression + union variant
# ---------------------------------------------------------------------------


def test_single_graph_parent_map_query_byte_identical():
    """REGRESSION: the str call path must emit exactly the pre-COG-37 query."""
    expected = (
        f"SELECT ?child ?parent FROM <{TENANT_GRAPH}>\n"
        "WHERE {\n"
        f"  ?child <{RDFS_SUBCLASS}> ?parent .\n"
        "}"
    )
    assert parent_map_query(TENANT_GRAPH) == expected


def test_union_parent_map_query_contains_all_layer_graphs():
    stack = LayerStack(TENANT_GRAPH, entitled=True)
    q = parent_map_query(stack.visible_graph_uris())
    for g in (TENANT_GRAPH, enhanced_graph_uri(), public_graph_uri()):
        assert f"GRAPH <{g}>" in q, f"union query missing layer graph {g}"
        # Each branch binds its graph so the caller can apply precedence.
        assert f"BIND(<{g}> AS ?graph)" in q
    # Three branches -> two UNION keywords, and ?graph is projected.
    assert q.count("UNION") == 2
    assert q.startswith("SELECT ?child ?parent ?graph")
    assert f"<{RDFS_SUBCLASS}>" in q


def test_union_parent_map_query_non_entitled_omits_enhanced():
    stack = LayerStack(TENANT_GRAPH, entitled=False)
    q = parent_map_query(stack.visible_graph_uris())
    assert enhanced_graph_uri() not in q
    assert f"GRAPH <{TENANT_GRAPH}>" in q and f"GRAPH <{public_graph_uri()}>" in q


# ---------------------------------------------------------------------------
# type_name_from_uri — names across layer namespaces
# ---------------------------------------------------------------------------


def test_type_name_from_uri_all_namespaces():
    assert type_name_from_uri("https://graph.infona.ai/types/Hotel") == "Hotel"
    assert type_name_from_uri("https://graph.infona.ai/types/public/Person") == "Person"
    assert type_name_from_uri("https://graph.infona.ai/types/x/Guest") == "Guest"
    # Attribute URIs reduce to their type name (matches old parent-map parsing).
    assert type_name_from_uri("https://graph.infona.ai/types/Hotel/attrs/city") == "Hotel"
    # Outside every layer namespace -> None (callers skip the edge).
    assert type_name_from_uri("http://www.w3.org/2000/01/rdf-schema#Resource") is None
    assert type_name_from_uri("") is None


# ---------------------------------------------------------------------------
# _fetch_parent_map — single-graph regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_parent_map_single_graph_unchanged(resolver, mock_neptune):
    """REGRESSION: no layer_stack -> exact old query string, exact old result."""
    mock_neptune.query.return_value = _bindings([
        {"child": "https://graph.infona.ai/types/HotelGuest",
         "parent": "https://graph.infona.ai/types/Guest"},
        {"child": "https://graph.infona.ai/types/Guest",
         "parent": "https://graph.infona.ai/types/Person"},
    ])
    parent_of = await resolver._fetch_parent_map(TENANT_GRAPH)
    assert parent_of == {"HotelGuest": "Guest", "Guest": "Person"}
    # The query sent to Neptune is the byte-identical single-graph form.
    mock_neptune.query.assert_awaited_once_with(parent_map_query(TENANT_GRAPH))
    assert "UNION" not in mock_neptune.query.call_args.args[0]


@pytest.mark.asyncio
async def test_fetch_parent_map_single_graph_error_degrades(resolver, mock_neptune):
    mock_neptune.query.side_effect = RuntimeError("neptune down")
    assert await resolver._fetch_parent_map(TENANT_GRAPH) == {}


# ---------------------------------------------------------------------------
# _fetch_parent_map — layer-aware union + shadowing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_parent_map_merges_edges_across_layers(resolver, mock_neptune):
    """Edges from every visible layer land in one map; a tenant leaf under a
    Public-namespace parent (the ADR 0002 §1 motivating case) keys correctly.
    """
    stack = LayerStack(TENANT_GRAPH, entitled=True)
    mock_neptune.query.return_value = _bindings([
        # Tenant graph: tenant leaf under a PUBLIC-layer parent (cross-layer edge).
        {"child": "https://graph.infona.ai/types/HotelGuest",
         "parent": "https://graph.infona.ai/types/public/Guest",
         "graph": TENANT_GRAPH},
        # Enhanced graph: premium delta edge.
        {"child": "https://graph.infona.ai/types/x/VipGuest",
         "parent": "https://graph.infona.ai/types/public/Guest",
         "graph": enhanced_graph_uri()},
        # Public graph: universal edge.
        {"child": "https://graph.infona.ai/types/public/Guest",
         "parent": "https://graph.infona.ai/types/public/Person",
         "graph": public_graph_uri()},
    ])
    parent_of = await resolver._fetch_parent_map(TENANT_GRAPH, layer_stack=stack)
    assert parent_of == {
        "HotelGuest": "Guest",
        "VipGuest": "Guest",
        "Guest": "Person",
    }
    # One round trip, over the union of the stack's visible graphs.
    mock_neptune.query.assert_awaited_once_with(
        parent_map_query(stack.visible_graph_uris())
    )


@pytest.mark.asyncio
async def test_fetch_parent_map_tenant_edge_shadows_lower_layers(resolver, mock_neptune):
    """Duplicate child keys: Tenant > Enhanced > Public wins (shadowing)."""
    stack = LayerStack(TENANT_GRAPH, entitled=True)
    mock_neptune.query.return_value = _bindings([
        {"child": "https://graph.infona.ai/types/public/Guest",
         "parent": "https://graph.infona.ai/types/public/Person",
         "graph": public_graph_uri()},
        {"child": "https://graph.infona.ai/types/x/Guest",
         "parent": "https://graph.infona.ai/types/x/Contact",
         "graph": enhanced_graph_uri()},
        # Tenant redefines Guest's parent — must win regardless of row order.
        {"child": "https://graph.infona.ai/types/Guest",
         "parent": "https://graph.infona.ai/types/Customer",
         "graph": TENANT_GRAPH},
    ])
    parent_of = await resolver._fetch_parent_map(TENANT_GRAPH, layer_stack=stack)
    assert parent_of["Guest"] == "Customer"


@pytest.mark.asyncio
async def test_fetch_parent_map_enhanced_shadows_public(resolver, mock_neptune):
    stack = LayerStack(TENANT_GRAPH, entitled=True)
    mock_neptune.query.return_value = _bindings([
        {"child": "https://graph.infona.ai/types/x/Guest",
         "parent": "https://graph.infona.ai/types/x/Contact",
         "graph": enhanced_graph_uri()},
        {"child": "https://graph.infona.ai/types/public/Guest",
         "parent": "https://graph.infona.ai/types/public/Person",
         "graph": public_graph_uri()},
    ])
    parent_of = await resolver._fetch_parent_map(TENANT_GRAPH, layer_stack=stack)
    assert parent_of["Guest"] == "Contact"


@pytest.mark.asyncio
async def test_fetch_parent_map_layer_aware_error_degrades(resolver, mock_neptune):
    stack = LayerStack(TENANT_GRAPH, entitled=True)
    mock_neptune.query.side_effect = RuntimeError("neptune down")
    assert await resolver._fetch_parent_map(TENANT_GRAPH, layer_stack=stack) == {}


# ---------------------------------------------------------------------------
# rewrite_type_predicate_to_closure — verified: needs NO layer change
# ---------------------------------------------------------------------------
#
# The rewriter is a pure predicate transform: it upgrades `a` / rdf:type to
# `<rdf:type>/<rdfs:subClassOf>*` and never reads, adds, or alters FROM /
# GRAPH clauses. WHICH graphs the closure path walks is decided entirely by
# the query's dataset (its FROM clauses) — that is add_layer_from_clauses'
# job. The tests below pin both halves of that contract.


def test_rewriter_leaves_graph_scoping_untouched():
    q = (
        f"SELECT ?h FROM <{TENANT_GRAPH}>\n"
        "WHERE { ?h a <https://graph.infona.ai/types/Hotel> . }"
    )
    out = rewrite_type_predicate_to_closure(q)
    # Predicate upgraded...
    assert "subClassOf>*" in out
    # ...but the dataset clause is byte-identical: no FROM/GRAPH added or moved.
    assert f"FROM <{TENANT_GRAPH}>" in out
    assert out.count("FROM") == 1 and "GRAPH" not in out


def test_rewriter_composes_with_layer_from_clauses():
    """Rewrite + widen: closure path AND every layer graph in one query."""
    stack = LayerStack(TENANT_GRAPH, entitled=True)
    q = (
        f"SELECT ?h FROM <{TENANT_GRAPH}>\n"
        "WHERE { ?h a <https://graph.infona.ai/types/Hotel> . }"
    )
    out = add_layer_from_clauses(rewrite_type_predicate_to_closure(q), stack.visible_graph_uris())
    assert "subClassOf>*" in out
    for g in stack.visible_graph_uris():
        assert f"FROM <{g}>" in out


# ---------------------------------------------------------------------------
# add_layer_from_clauses — widening graph-scoped generated queries
# ---------------------------------------------------------------------------


def test_add_layer_from_clauses_appends_missing_graphs():
    q = f"SELECT ?s FROM <{TENANT_GRAPH}>\nWHERE {{ ?s ?p ?o }}"
    out = add_layer_from_clauses(q, [TENANT_GRAPH, public_graph_uri()])
    # Existing FROM kept once, public layer added after it, WHERE intact.
    assert out.count(f"FROM <{TENANT_GRAPH}>") == 1
    assert f"FROM <{public_graph_uri()}>" in out
    assert out.index("FROM") < out.index("WHERE")


def test_add_layer_from_clauses_idempotent_and_noop_cases():
    q = f"SELECT ?s FROM <{TENANT_GRAPH}>\nWHERE {{ ?s ?p ?o }}"
    widened = add_layer_from_clauses(q, [TENANT_GRAPH, public_graph_uri()])
    # Idempotent: applying again changes nothing.
    assert add_layer_from_clauses(widened, [TENANT_GRAPH, public_graph_uri()]) == widened
    # REGRESSION: empty layer list leaves the query byte-identical.
    assert add_layer_from_clauses(q, []) == q


def test_add_layer_from_clauses_inserts_before_where_when_no_from():
    q = "SELECT ?s WHERE { ?s ?p ?o }"
    out = add_layer_from_clauses(q, [public_graph_uri()])
    assert f"FROM <{public_graph_uri()}>" in out
    assert out.index("FROM") < out.index("WHERE")


# ---------------------------------------------------------------------------
# NL pipeline — opt-in layer widening; default path unchanged
# ---------------------------------------------------------------------------


def _mock_llm_message(sparql: str) -> MagicMock:
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps({
        "sparql": sparql, "explanation": "test", "functions_needed": [],
    }))]
    return msg


@pytest.mark.xfail(
    reason=(
        "LOST CAPABILITY (ONTA-527): ask() no longer widens a generated query "
        "to the visible layer graphs, and no longer closure-rewrites it, "
        "because it no longer generates SPARQL. nlp/pipeline.py::ask dispatches "
        "to _ask_cypher whenever neo4j_ask_enabled() — unconditionally true "
        "post-cutover — and _ask_cypher's first statement is "
        "`del layer_graph_uris  # reserved for catalog-layer joins in later E6`. "
        "Neither ontology_queries.add_layer_from_clauses nor "
        "rewrite_type_predicate_to_closure has a caller on the ask path, so "
        "subclass closure across layers is unavailable to NL questions. The "
        "builders themselves are unchanged and still unit-tested above; what is "
        "missing is a Cypher-side equivalent (Class SUBCLASS_OF walk + a "
        "catalog-layer join) wired into _ask_cypher."
    ),
    strict=True,
)
@pytest.mark.asyncio
async def test_ask_with_layer_graph_uris_widens_generated_query(mock_neptune):
    from infona_client.nlp.pipeline import NLQueryPipeline

    mock_neptune.query.return_value = {
        "head": {"vars": ["name"]},
        "results": {"bindings": [{"name": {"type": "literal", "value": "Ritz"}}]},
    }
    pipeline = NLQueryPipeline(mock_neptune, "fake-key")
    stack = LayerStack(TENANT_GRAPH, entitled=False)
    generated = (
        f"SELECT ?name FROM <{TENANT_GRAPH}> WHERE {{ "
        f"?h a <https://graph.infona.ai/types/Hotel> . "
        f"?h <https://graph.infona.ai/types/Hotel/attrs/name> ?name }}"
    )
    with patch.object(pipeline.anthropic.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_llm_message(generated)
        result = await pipeline.ask(
            "What hotels exist?", TENANT_GRAPH,
            layer_graph_uris=stack.visible_graph_uris(),
        )
    # Executed query is widened to every visible layer and closure-rewritten.
    assert f"FROM <{public_graph_uri()}>" in result.sparql
    assert f"FROM <{TENANT_GRAPH}>" in result.sparql
    assert "subClassOf>*" in result.sparql


@pytest.mark.asyncio
async def test_ask_without_layer_graph_uris_unchanged(mock_neptune):
    """REGRESSION: default ask() never widens — no layer graphs leak in.

    Honest caveat (ONTA-527): this now passes VACUOUSLY. ``result.sparql`` is
    the empty string, because _ask_cypher refuses a graph URI that is not
    ``…/graphs/{tenant}/kg/{kg}`` before generating anything. Kept as the
    no-leak direction of the xfailed test above — an assertion that stays true
    when the widening is restored — not as evidence the path runs.
    """
    from infona_client.nlp.pipeline import NLQueryPipeline

    mock_neptune.query.return_value = {
        "head": {"vars": ["name"]},
        "results": {"bindings": [{"name": {"type": "literal", "value": "Ritz"}}]},
    }
    pipeline = NLQueryPipeline(mock_neptune, "fake-key")
    generated = (
        f"SELECT ?name FROM <{TENANT_GRAPH}-other> WHERE {{ "
        f"?s <https://schema.org/name> ?name }}"
    )
    with patch.object(pipeline.anthropic.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_llm_message(generated)
        result = await pipeline.ask("What names exist?", f"{TENANT_GRAPH}-other")
    assert public_graph_uri() not in result.sparql
    assert enhanced_graph_uri() not in result.sparql
