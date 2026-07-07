"""Query-layer robustness tests (persona-eval RCA, ONTA-240).

Covers the four fixes:
  1. `ask` never escapes as a bare 500 — generation failures retry inside the
     per-attempt loop and degrade to a graceful NLResult.
  2. Freshly-populated KG with no schema falls back to instance-graph types.
  3. Zero-bind projection columns are reported honestly, not silently dropped.
  4. Truncation is deterministic (ORDER BY) and stated prominently.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cograph_client.nlp.pipeline import NLQueryPipeline


@pytest.fixture
def mock_neptune():
    client = AsyncMock()
    client.query.return_value = {
        "head": {"vars": ["name"]},
        "results": {"bindings": [{"name": {"type": "literal", "value": "Central Park"}}]},
    }
    return client


@pytest.fixture
def pipeline(mock_neptune):
    return NLQueryPipeline(mock_neptune, "fake-key")


# --------------------------------------------------------- Fix 1: error boundary
@pytest.mark.asyncio
async def test_generation_failure_retries_then_degrades_gracefully(pipeline):
    """A provider failure inside _generate_sparql must not escape ask() as a 500.

    Before the fix, _generate_sparql ran OUTSIDE the per-attempt try/except, so a
    provider exception flew straight out of ask(). Now it's caught, retried, and
    after max attempts ask() returns a graceful NLResult.
    """
    with patch.object(
        pipeline, "_generate_sparql", new_callable=AsyncMock
    ) as mock_gen:
        mock_gen.side_effect = RuntimeError("provider 503")
        result = await pipeline.ask("List all attributes", "https://cograph.tech/graphs/t1")

    # No exception escaped; we got a well-formed NLResult explaining the failure.
    assert "Could not answer" in result.answer
    assert result.timing.get("attempts") == 3
    assert mock_gen.await_count == 3  # retried all attempts, none escaped


# --------------------------------------------------- Fix 3: zero-bind columns
@pytest.mark.asyncio
async def test_unbound_projection_column_reported(pipeline, mock_neptune):
    """A projected var that binds in zero rows is reported, not silently omitted."""
    mock_neptune.query.return_value = {
        "head": {"vars": ["name", "latency"]},
        "results": {
            "bindings": [
                {"name": {"type": "literal", "value": "svc-a"}},
                {"name": {"type": "literal", "value": "svc-b"}},
            ]
        },
    }
    llm_response = json.dumps({
        "sparql": "SELECT ?name ?latency WHERE { ?s <https://schema.org/name> ?name OPTIONAL { ?s <https://cograph.tech/types/Service/attrs/latency> ?latency } }",
        "explanation": "svc",
        "functions_needed": [],
    })
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=llm_response)]
    with patch.object(pipeline.anthropic.messages, "create", new_callable=AsyncMock) as mc:
        mc.return_value = mock_message
        result = await pipeline.ask("services and latency", "https://cograph.tech/graphs/t1")

    assert "latency" in result.answer
    assert "not present" in result.answer
    assert result.timing.get("unbound_projection_vars") == "latency"


# --------------------------------------------------- Fix 4: ORDER BY + truncation
def test_ensure_order_by_adds_deterministic_order():
    q = "SELECT ?type ?label WHERE { ?s a ?type . ?s <l> ?label }"
    out = NLQueryPipeline._ensure_order_by(q)
    assert "ORDER BY ?type ?label" in out


def test_ensure_order_by_respects_existing_order():
    q = "SELECT ?x WHERE { ?s <p> ?x } ORDER BY ?x"
    assert NLQueryPipeline._ensure_order_by(q) == q


def test_ensure_order_by_skips_aggregates():
    q = "SELECT ?type (COUNT(?s) AS ?n) WHERE { ?s a ?type } GROUP BY ?type"
    assert NLQueryPipeline._ensure_order_by(q) == q


def test_ensure_order_by_skips_select_star():
    q = "SELECT * WHERE { ?s ?p ?o }"
    assert NLQueryPipeline._ensure_order_by(q) == q


def test_ensure_order_by_skips_existing_limit():
    q = "SELECT ?x WHERE { ?s <p> ?x } LIMIT 5"
    assert NLQueryPipeline._ensure_order_by(q) == q


@pytest.mark.asyncio
async def test_format_answer_truncation_stated_prominently(pipeline):
    bindings = [{"n": f"item{i}"} for i in range(250)]
    with patch.object(pipeline, "_resolve_uri_labels", new_callable=AsyncMock) as mr:
        mr.return_value = {}
        out = await pipeline._format_answer(bindings, "expl")
    # Truncation announced up front (not buried) and count is exact.
    first_line = out.splitlines()[0]
    assert "truncated" in first_line.lower()
    assert "of 250" in first_line


@pytest.mark.asyncio
async def test_format_answer_missing_vars_note_on_empty(pipeline):
    out = await pipeline._format_answer([], "expl", missing_vars=["latency"])
    assert "No results found." in out
    assert "latency" in out


# --------------------------------------------- Fix 2: empty-schema fallback
@pytest.mark.asyncio
async def test_instance_graph_fallback_when_schema_missing(pipeline, mock_neptune):
    """Schema query empty but instances present → fall back to instance types."""
    # active_types precomputed upstream; fallback issues one predicate probe.
    mock_neptune.query.return_value = {
        "head": {"vars": ["p"]},
        "results": {
            "bindings": [
                {"p": {"type": "uri", "value": "https://cograph.tech/types/Service/attrs/name"}},
            ]
        },
    }
    summary, has_instances = await pipeline._instance_graph_ontology_fallback(
        "https://cograph.tech/graphs/t1",
        "https://cograph.tech/graphs/t1/kg/svc",
        {"Service"},
    )
    assert has_instances is True
    assert "schema" in summary.lower()
    assert "Service" in summary
    assert "view_ontology" in summary
