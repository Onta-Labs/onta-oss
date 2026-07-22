"""Transport-retry hardening for NeptuneClient (nightly-QC audit robustness).

Neptune closes idle keep-alive connections; httpx then reuses a dead one and
raises ``RemoteProtocolError`` ("Server disconnected without sending a response")
on the very next request. A single such drop was crashing the entire ~10-min
nightly QC audit sweep uncaught. ``NeptuneClient`` now retries transient
TRANSPORT errors on the read path (bounded), while HTTP *status* errors
(deterministic — a malformed query returns the same 400 every time) and writes
are left untouched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from cograph_client.graph import client as client_mod
from cograph_client.graph.client import (
    _MAX_TRANSPORT_ATTEMPTS,
    NeptuneClient,
    SparqlQueryError,
)

_OK_BODY = {"head": {"vars": ["x"]}, "results": {"bindings": []}}


def _client_with(handler) -> NeptuneClient:
    c = NeptuneClient("http://neptune.local")  # http -> no TLS verify
    c._client = httpx.AsyncClient(
        base_url="http://neptune.local", transport=httpx.MockTransport(handler)
    )
    return c


@pytest.fixture(autouse=True)
def _no_backoff():
    """Zero the retry backoff so the bounded-retry tests don't actually sleep."""
    with patch.object(client_mod, "_RETRY_BACKOFF_S", 0):
        yield


@pytest.mark.asyncio
async def test_query_retries_transient_drop_then_succeeds():
    """A dropped keep-alive (RemoteProtocolError) is retried, not fatal — this is
    the exact failure that crashed the nightly QC audit."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        return httpx.Response(200, json=_OK_BODY)

    c = _client_with(handler)
    try:
        assert await c.query("SELECT ?x WHERE { ?x ?p ?o }") == _OK_BODY
    finally:
        await c.close()
    assert calls["n"] == 2  # failed once, retried, then succeeded


@pytest.mark.asyncio
async def test_query_gives_up_after_max_attempts():
    """A persistent transport failure propagates after the bounded retries — the
    retry is a safety net, not an infinite loop."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    c = _client_with(handler)
    try:
        with pytest.raises(httpx.ConnectError):
            await c.query("SELECT ?x WHERE { ?x ?p ?o }")
    finally:
        await c.close()
    assert calls["n"] == _MAX_TRANSPORT_ATTEMPTS  # tried, then gave up


@pytest.mark.asyncio
async def test_query_does_not_retry_http_status_error():
    """A deterministic 4xx (malformed LLM query) must NOT be retried: one shot,
    surfaced as SparqlQueryError so the NL loop can self-correct."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            400, json={"code": "MalformedQueryException", "detailedMessage": "bad token"}
        )

    c = _client_with(handler)
    try:
        with pytest.raises(SparqlQueryError):
            await c.query("SELECT ?x WHERE { ?x FILTeR }")
    finally:
        await c.close()
    assert calls["n"] == 1  # HTTP status errors are deterministic, never retried


@pytest.mark.asyncio
async def test_read_timeout_is_not_retried():
    """ReadTimeout (a genuinely slow query) is deliberately excluded from the
    retryable set — re-issuing it would only amplify endpoint load."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out")

    c = _client_with(handler)
    try:
        with pytest.raises(httpx.ReadTimeout):
            await c.query("SELECT ?x WHERE { ?x ?p ?o }")
    finally:
        await c.close()
    assert calls["n"] == 1  # not in _RETRYABLE_TRANSPORT_ERRORS


@pytest.mark.asyncio
async def test_connect_timeout_is_not_retried():
    """Timeout-class errors are excluded so retries can't stack multiple 120s
    stalls onto one live query — only FAST-FAILING transport errors are retried.
    ConnectTimeout must therefore surface on the first attempt."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("connect timed out")

    c = _client_with(handler)
    try:
        with pytest.raises(httpx.ConnectTimeout):
            await c.query("SELECT ?x WHERE { ?x ?p ?o }")
    finally:
        await c.close()
    assert calls["n"] == 1  # timeout-class errors are not retried


@pytest.mark.asyncio
async def test_ask_read_path_also_retries():
    """The hardening covers every read method, not just query() — ask() too."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        return httpx.Response(200, json={"boolean": True})

    c = _client_with(handler)
    try:
        assert await c.ask("ASK { ?s ?p ?o }") is True
    finally:
        await c.close()
    assert calls["n"] == 2
