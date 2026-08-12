"""Fix A: capture the store's error body so the NL retry loop can self-correct
instead of retrying blind.

The query is LLM-generated; on a malformed query the store returns an error
whose body carries the exact parse diagnostic (naming the offending token).
`raise_for_status()` discarded that body and raised a generic
`"Client error '400 Bad Request' for url '<host>/sparql'"` — which ALSO leaked
the endpoint host. `NeptuneClient.query` raises `SparqlQueryError` carrying the
host-scrubbed parse diagnostic, and the pipeline threads `str(e)` into the next
attempt's generation feedback.

**ONTA-527.** The `NeptuneClient` cases below are unchanged (that client is
still in the residual surface the ratchet is counting down, and its
error-capture contract is what the property-graph one was modelled on). The
end-to-end case moved: `/ask` generates Cypher, so the diagnostic that has to
reach attempt 2 is a `GraphQueryError` from the GraphStore, scrubbed by
`nlp/cypher_scope.py::scrub_cypher_error` (= `graph/store.py::scrub_store_detail`)
rather than a `SparqlQueryError`. The property is identical and is asserted the
same way: the retry sees the ACTUAL diagnostic, and no host survives into it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from infona_client.graph.client import NeptuneClient, SparqlQueryError
from infona_client.nlp.pipeline import NLQueryPipeline

# A realistic Neptune malformed-query body (with a URL to prove scrubbing).
_MALFORMED_BODY = {
    "detailedMessage": "Bad character range: offending token 'FILTeR' at line 3, "
    "column 5. See https://neptune.internal-host/docs for syntax.",
    "code": "MalformedQueryException",
    "requestId": "abc-123",
}


def _client_with(handler) -> NeptuneClient:
    # allow_http: residual SPARQL HTTP unit tests under hermetic GraphStore
    # (ONTA-534 fail-closed gate would otherwise raise SparqlClientRetired).
    client = NeptuneClient("http://neptune.local", allow_http=True)
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
async def test_query_error_scrubs_scheme_less_host_port():
    """A bare `host:port` with NO scheme (e.g. an Envoy/ALB upstream error) must
    also be scrubbed — the Neptune host must never leak, and a scheme-only scrub
    would miss this shape. The useful parse text around it survives."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            text=(
                "upstream connect error to "
                "infona-cluster.abc123.us-east-1.neptune.amazonaws.com:8182 "  # boundary-ok: synthetic hostname in a simulated upstream error
                "MalformedQueryException at line 5"
            ),
        )

    client = _client_with(handler)
    try:
        with pytest.raises(SparqlQueryError) as ei:
            await client.query("SELECT ?x WHERE { ?x ?p ?o }")
    finally:
        await client.close()
    msg = str(ei.value)
    assert "neptune.amazonaws.com" not in msg and "8182" not in msg
    assert "[endpoint]" in msg
    assert "MalformedQueryException" in msg  # useful diagnostic preserved


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


@pytest.mark.asyncio
async def test_store_diagnostic_reaches_retry_feedback():
    """The pipeline threads the store's parse error into attempt-2's generation
    feedback — the retry is no longer blind.

    Ported by ONTA-527 from the SPARQL loop to ``_ask_cypher``'s
    ``GraphQueryError`` handler. ``_execute_confined_cypher`` is the seam that
    ``_ask_cypher``'s try/except wraps, so raising there is the property-graph
    equivalent of the old ``neptune.query`` 400.
    """
    from infona_client.graph.store import GraphQueryError

    calls = {"n": 0}
    records = [MagicMock(data={"name": "widget-a"})]

    async def execute(session, gen_payload, cypher, params):
        calls["n"] += 1
        if calls["n"] == 1:
            # A realistic Neo4j failure, complete with the bolt host that must
            # not survive into the prompt.
            raise GraphQueryError(
                "Invalid input 'RETRUN': expected an expression "
                "(line 3, column 5) @ bolt://neo4j.internal-host:7687"
            )
        return records, "execute_read"

    p = NLQueryPipeline(AsyncMock(), "invented-anthropic-key", graph_store=MagicMock())
    p._openrouter_key = ""              # narrative rephraser fail-open (no network)
    p._spatial_routing_enabled = False

    feedbacks: list[str] = []

    async def gen(question, ontology, **kw):
        feedbacks.append(kw.get("error_feedback") or "")
        return {
            "cypher": (
                "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                "RETURN e.name AS name"
            ),
            "params": {},
            "explanation": "ok",
            "functions_needed": [],
        }

    with patch.object(p, "_fetch_ontology", new=AsyncMock(return_value="ONT")), \
         patch.object(p, "_try_llm_cypher", new=gen), \
         patch.object(p, "_execute_confined_cypher", new=execute), \
         patch.object(p, "_format_answer", new=AsyncMock(return_value="widget-a")):
        result = await p.ask(
            "reconcile the zzqx widgets",
            "https://graph.infona.ai/graphs/t1",
            "https://graph.infona.ai/graphs/t1/kg/widgets",
        )

    assert result.timing.get("attempts") == 2
    assert result.timing.get("cypher_retry") == 1.0
    assert feedbacks[0] == ""
    feedback = feedbacks[1]
    # Attempt 2 sees the store's ACTUAL parse error — the offending token — not
    # a generic "query failed" that the old blind retry received.
    assert "RETRUN" in feedback
    assert "line 3, column 5" in feedback
    # ...and the host is scrubbed out of anything user/prompt-facing.
    assert "neo4j.internal-host" not in feedback
    assert "7687" not in feedback
    assert "[endpoint]" in feedback
