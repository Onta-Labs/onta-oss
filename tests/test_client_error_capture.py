"""Fix A: capture Neptune's 4xx/5xx error body so the NL→SPARQL retry loop can
self-correct instead of retrying blind.

The SPARQL is LLM-generated; on a malformed query Neptune returns a 400 whose
body carries the exact `MalformedQueryException` (naming the offending token).
`raise_for_status()` discarded that body and raised a generic
`"Client error '400 Bad Request' for url '<host>/sparql'"` — which ALSO leaked
the endpoint host. `NeptuneClient.query` now raises `SparqlQueryError` carrying
the host-scrubbed parse diagnostic, and the pipeline threads `str(e)` into the
next attempt's generation feedback.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from cograph_client.graph.client import NeptuneClient, SparqlQueryError
from cograph_client.nlp import pipeline as pipeline_mod
from cograph_client.nlp.pipeline import NLQueryPipeline

# A realistic Neptune malformed-query body (with a URL to prove scrubbing).
_MALFORMED_BODY = {
    "detailedMessage": "Bad character range: offending token 'FILTeR' at line 3, "
    "column 5. See https://neptune.internal-host/docs for syntax.",
    "code": "MalformedQueryException",
    "requestId": "abc-123",
}


def _client_with(handler) -> NeptuneClient:
    client = NeptuneClient("http://neptune.local")  # http -> no TLS verify
    client._client = httpx.AsyncClient(
        base_url="http://neptune.local", transport=httpx.MockTransport(handler)
    )
    return client


@pytest.mark.asyncio
async def test_query_400_surfaces_malformed_query_diagnostic():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=_MALFORMED_BODY)

    client = _client_with(handler)
    try:
        with pytest.raises(SparqlQueryError) as ei:
            await client.query("SELECT ?x WHERE { ?x FILTeR }")
    finally:
        await client.close()

    msg = str(ei.value)
    # The retry loop (which reads str(e)) now sees the actual parse error…
    assert ei.value.status_code == 400
    assert "MalformedQueryException" in msg
    assert "FILTeR" in msg  # the offending token, not just "400 Bad Request"
    # …and NOT the generic httpx message.
    assert "Client error" not in msg
    # Host scrubbed: no URL leaks into anything user/log-facing.
    assert "neptune.internal-host" not in msg
    assert "http" not in msg


@pytest.mark.asyncio
async def test_query_error_falls_back_to_raw_text_body():
    """Non-JSON error bodies (e.g. an HTML 502 page) still surface, host-scrubbed."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway from https://alb.internal/sparql")

    client = _client_with(handler)
    try:
        with pytest.raises(SparqlQueryError) as ei:
            await client.query("SELECT ?x WHERE { ?x ?p ?o }")
    finally:
        await client.close()
    msg = str(ei.value)
    assert "Bad Gateway" in msg
    assert "alb.internal" not in msg and "[endpoint]" in msg


@pytest.mark.asyncio
async def test_query_success_unchanged():
    """The happy path is untouched — a 200 returns the parsed JSON as before."""
    body = {"head": {"vars": ["x"]}, "results": {"bindings": []}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = _client_with(handler)
    try:
        assert await client.query("SELECT ?x WHERE { ?x ?p ?o }") == body
    finally:
        await client.close()


def _rows(vars_, *value_rows) -> dict:
    return {
        "head": {"vars": list(vars_)},
        "results": {
            "bindings": [
                {k: {"type": "literal", "value": v} for k, v in row.items()}
                for row in value_rows
            ]
        },
    }


@pytest.mark.asyncio
async def test_neptune_diagnostic_reaches_retry_feedback():
    """The pipeline threads Neptune's parse error into attempt-2's generation
    feedback — the retry is no longer blind."""
    calls = {"n": 0}

    async def query(sparql, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SparqlQueryError(
                400, "MalformedQueryException: offending token 'FILTeR' at line 3"
            )
        return _rows(["name"], {"name": "widget-a"})

    neptune = AsyncMock()
    neptune.query = AsyncMock(side_effect=query)
    p = NLQueryPipeline(neptune, "invented-anthropic-key")
    p._openrouter_key = ""              # narrative rephraser fail-open (no network)
    p._spatial_routing_enabled = False  # skip the geo fast path for this NL

    gen = AsyncMock(side_effect=[
        {"sparql": "SELECT ?name WHERE { ?s <p1> ?name }", "explanation": "", "functions_needed": []},
        {"sparql": "SELECT ?name WHERE { ?s <p2> ?name }", "explanation": "ok", "functions_needed": []},
    ])

    with patch.object(pipeline_mod, "get_embedding_service", return_value=None), \
         patch.object(p, "_fetch_ontology", new=AsyncMock(return_value="ONT")), \
         patch.object(p, "_generate_sparql", new=gen):
        result = await p.ask("list the widgets", "https://cograph.tech/graphs/t1")

    assert result.timing.get("attempts") == 2
    feedback = gen.call_args_list[1].kwargs.get("error_feedback", "")
    # Attempt 2 sees Neptune's ACTUAL parse error — the offending token — not the
    # generic "400 Bad Request" that the old blind retry received.
    assert "MalformedQueryException" in feedback
    assert "FILTeR" in feedback
    assert "400 Bad Request" not in feedback
