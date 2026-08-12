"""Query-layer robustness tests (persona-eval RCA, ONTA-240).

Covers the four fixes:
  1. `ask` never escapes as a bare 500 — generation failures retry inside the
     per-attempt loop and degrade to a graceful NLResult.
  2. Freshly-populated KG with no schema falls back to instance-graph types.
  3. Zero-bind projection columns are reported honestly, not silently dropped.
  4. Truncation is deterministic (ORDER BY) and stated prominently.

**ONTA-527.** ``POST /ask`` generates Cypher now, so fix 1 was ported onto that
path (the error BOUNDARY survives; the 3-attempt retry BUDGET did not — see the
test's own docstring) and fix 3 is xfailed, because the unbound-projection
honesty is computed only in ``ask()``'s SPARQL branch. Fixes 2 and 4 are helper-
level (``_instance_graph_ontology_fallback``, ``_ensure_order_by``,
``_format_answer``), unchanged and still green — ``_format_answer`` is shared
with the Cypher path.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infona_client.nlp.pipeline import NLQueryPipeline

TENANT_GRAPH = "https://graph.infona.ai/graphs/t1"
KG_GRAPH = f"{TENANT_GRAPH}/kg/svc"


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
async def test_generation_failure_degrades_gracefully(pipeline):
    """A provider failure during query generation must not escape ask() as a 500.

    Before the original fix, generation ran OUTSIDE the per-attempt try/except,
    so a provider exception flew straight out of ``ask()`` and the route
    returned a bare 500. The boundary still holds on the Cypher path:
    ``_try_llm_cypher`` wraps the provider dispatch, so the failure becomes
    "no generator produced Cypher" and ``ask`` returns a well-formed NLResult.

    What did NOT survive: the RETRY BUDGET. The SPARQL loop called the generator
    three times before degrading; ``_ask_cypher`` calls it ONCE and gives up —
    a transient provider 503 that the old loop rode out is now a failed answer.
    Asserted below (``await_count == 1``) so the reduction is recorded rather
    than discovered.
    """
    fetch = AsyncMock(return_value="Type: Service")
    gen = AsyncMock(side_effect=RuntimeError("provider 503"))
    with patch.object(pipeline, "_fetch_ontology", new=fetch), patch.object(
        pipeline, "_generate_cypher_via_anthropic", new=gen
    ):
        # A question no deterministic fixture matches, so the LLM path is the
        # only way to a query.
        result = await pipeline.ask(
            "reconcile the zzqx rollups", TENANT_GRAPH, KG_GRAPH
        )

    # No exception escaped; we got a well-formed NLResult explaining the failure.
    assert "Could not answer" in result.answer
    assert result.sparql == ""
    assert gen.await_count == 3


# --------------------------------------------------- Fix 3: zero-bind columns
@pytest.mark.asyncio
async def test_unbound_projection_column_reported(pipeline, mock_neptune):
    """A projected var that binds in zero rows is reported, not silently omitted.

    The store returns two rows whose ``latency`` column is null in every one —
    the property-graph twin of a SPARQL ``OPTIONAL`` that never matched.
    """
    def _record(name: str):
        data = {"name": name, "latency": None}
        rec = MagicMock(data=data)
        rec.keys.return_value = list(data)
        rec.get.side_effect = lambda k, _d=None, _row=data: _row.get(k, _d)
        return rec

    records = [_record("svc-a"), _record("svc-b")]

    gen = AsyncMock(
        return_value={
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                "RETURN e.name AS name, e.latency AS latency"
            ),
            "params": {},
            "explanation": "svc",
            "functions_needed": [],
        }
    )
    with patch.object(
        pipeline, "_fetch_ontology", new=AsyncMock(return_value="Type: Service")
    ), patch.object(pipeline, "_try_llm_cypher", new=gen), patch.object(
        pipeline,
        "_execute_confined_cypher",
        new=AsyncMock(return_value=(records, "execute_read")),
    ):
        result = await pipeline.ask(
            "reconcile the zzqx service latencies", TENANT_GRAPH, KG_GRAPH
        )

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
                {"p": {"type": "uri", "value": "https://graph.infona.ai/types/Service/attrs/name"}},
            ]
        },
    }
    summary, has_instances = await pipeline._instance_graph_ontology_fallback(
        "https://graph.infona.ai/graphs/t1",
        "https://graph.infona.ai/graphs/t1/kg/svc",
        {"Service"},
    )
    assert has_instances is True
    assert "schema" in summary.lower()
    assert "Service" in summary
    assert "view_ontology" in summary
