"""Query-LLM robustness guards for the Cerebras query-provider flip.

Two real defects surfaced when switching the NL->SPARQL query model to a
Cerebras-served reasoning model (`OMNIX_QUERY_PROVIDER=cerebras`):

  1. `_generate_via_cerebras` capped `max_completion_tokens` at 512. A reasoning
     model spends output tokens on reasoning BEFORE emitting the answer, so 512
     truncated the JSON mid-string and `json.loads` raised (empirically 0/3 at
     512, 3/3 at 2048). The cap was raised to 2048.

  2. The query-generation methods crashed on an empty/`None` LLM response. A
     model returning `content: null` (GLM-5.2 did this ~40x in production) made
     `None.strip()` raise an opaque `AttributeError` (or `json.loads(None)` a
     `TypeError`). Each site now routes the content through
     `_require_message_content`, which raises a clear, provider-named
     `ValueError` the caller's existing retry/fallback path can handle.

These tests assert the MECHANISM (typed error + provider name; token budget
>= 2048; happy path preserved) with invented model/key values only — never a
model literal — so they can't be satisfied by overfitting to a specific model.
"""
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from cograph_client.nlp import pipeline as pipeline_mod
from cograph_client.nlp.pipeline import NLQueryPipeline, _require_message_content

# Capture the REAL AsyncClient before any patch: patching
# `pipeline_mod.httpx.AsyncClient` mutates the shared httpx module attribute, so
# the factory must build clients from the saved class to avoid self-recursion.
_RealAsyncClient = httpx.AsyncClient


def _make_pipeline(provider: str = "cerebras") -> NLQueryPipeline:
    """A pipeline with invented keys/model so nothing depends on a real provider
    or a specific model literal."""
    p = NLQueryPipeline(AsyncMock(), "invented-anthropic-key")
    p._query_provider = provider
    p._cerebras_key = "invented-cerebras-key"
    p._openrouter_key = "invented-openrouter-key"
    p._query_model = "invented-model-xyz"
    return p


def _post_factory(payload: dict, captured: dict | None = None):
    """Build an httpx.AsyncClient factory whose POSTs all return a 200 with JSON
    body `payload`, optionally recording the outgoing request body into
    `captured['body']`. Install it with monkeypatch on
    `pipeline_mod.httpx.AsyncClient`."""
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["body"] = json.loads(request.content)
            captured["url"] = str(request.url)
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):  # ignores timeout=; mock transport handles all
        return _RealAsyncClient(transport=transport)

    return factory


def _resp(content) -> dict:
    """An OpenAI-compatible chat-completion response carrying `content`."""
    return {"choices": [{"message": {"content": content}}]}


def _sparql_json() -> str:
    return json.dumps(
        {"sparql": "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1",
         "explanation": "invented", "functions_needed": []}
    )


# --------------------------------------------------------------------------- #
# The helper itself                                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [None, ""])
def test_require_message_content_rejects_empty(bad):
    """None/"" content -> a clear, provider-named ValueError (never the opaque
    AttributeError/TypeError the raw `.strip()`/`json.loads()` used to raise)."""
    with pytest.raises(ValueError) as ei:
        _require_message_content(_resp(bad), "someprovider")
    assert "someprovider" in str(ei.value).lower()
    assert not isinstance(ei.value, (AttributeError, TypeError))


def test_require_message_content_passes_through_present_content():
    """A normal string is returned verbatim — happy path byte-identical."""
    assert _require_message_content(_resp('{"x": 1}'), "p") == '{"x": 1}'


# --------------------------------------------------------------------------- #
# Guard 1: each query-gen method raises a typed, provider-named error on empty  #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, ""])
async def test_cerebras_generation_raises_typed_error_on_empty(monkeypatch, content):
    p = _make_pipeline("cerebras")
    factory = _post_factory(_resp(content))
    monkeypatch.setattr(pipeline_mod.httpx, "AsyncClient", factory)
    with pytest.raises(ValueError) as ei:
        await p._generate_via_cerebras("give me sparql")
    assert "cerebras" in str(ei.value).lower()
    assert not isinstance(ei.value, (AttributeError, TypeError))


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, ""])
async def test_openrouter_generation_raises_typed_error_on_empty(monkeypatch, content):
    p = _make_pipeline("openrouter")
    factory = _post_factory(_resp(content))
    monkeypatch.setattr(pipeline_mod.httpx, "AsyncClient", factory)
    with pytest.raises(ValueError) as ei:
        await p._generate_via_openrouter("give me sparql")
    assert "openrouter" in str(ei.value).lower()
    assert not isinstance(ei.value, (AttributeError, TypeError))


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["cerebras", "openrouter"])
@pytest.mark.parametrize("content", [None, ""])
async def test_structured_llm_raises_typed_error_on_empty(monkeypatch, provider, content):
    """The provider-agnostic structured-JSON classifier guards the same way and
    names whichever provider it routed to."""
    p = _make_pipeline(provider)
    factory = _post_factory(_resp(content))
    monkeypatch.setattr(pipeline_mod.httpx, "AsyncClient", factory)
    with pytest.raises(ValueError) as ei:
        await p._structured_llm("sys", "usr", "sname", {"type": "object"})
    assert provider in str(ei.value).lower()
    assert not isinstance(ei.value, (AttributeError, TypeError))


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, ""])
async def test_rephrase_fails_open_on_empty(monkeypatch, content):
    """The narrative rephraser is fail-open by design: an empty response must
    not crash — the guard's ValueError is swallowed and it returns ""."""
    p = _make_pipeline("openrouter")
    factory = _post_factory(_resp(content))
    monkeypatch.setattr(pipeline_mod.httpx, "AsyncClient", factory)
    out = await p._rephrase_via_openrouter("q?", [{"name": "row"}])
    assert out == ""


# --------------------------------------------------------------------------- #
# Guard 2: Cerebras token budget large enough for reasoning + full response     #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cerebras_request_uses_large_token_budget(monkeypatch):
    """Regression guard against silently reintroducing a too-small cap. A
    reasoning model needs headroom BEFORE the answer; 512 truncated the JSON.
    Assert the budget floor (>= 2048), not a model-specific literal."""
    p = _make_pipeline("cerebras")
    captured: dict = {}
    factory = _post_factory(_resp(_sparql_json()), captured=captured)
    monkeypatch.setattr(pipeline_mod.httpx, "AsyncClient", factory)
    await p._generate_via_cerebras("give me sparql")
    assert captured["body"]["max_completion_tokens"] >= 2048
    assert captured["body"]["max_completion_tokens"] != 512  # the old truncating cap


# --------------------------------------------------------------------------- #
# Guard 3: happy path unchanged — present content parses to the same dict       #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cerebras_happy_path_returns_parsed_dict(monkeypatch):
    p = _make_pipeline("cerebras")
    factory = _post_factory(_resp(_sparql_json()))
    monkeypatch.setattr(pipeline_mod.httpx, "AsyncClient", factory)
    result = await p._generate_via_cerebras("give me sparql")
    assert result["sparql"].startswith("SELECT")
    assert result["functions_needed"] == []


@pytest.mark.asyncio
async def test_openrouter_happy_path_returns_parsed_dict(monkeypatch):
    p = _make_pipeline("openrouter")
    factory = _post_factory(_resp(_sparql_json()))
    monkeypatch.setattr(pipeline_mod.httpx, "AsyncClient", factory)
    result = await p._generate_via_openrouter("give me sparql")
    assert result["sparql"].startswith("SELECT")
    assert result["explanation"] == "invented"


@pytest.mark.asyncio
async def test_structured_llm_happy_path_returns_parsed_dict(monkeypatch):
    p = _make_pipeline("cerebras")
    factory = _post_factory(_resp(json.dumps({"ok": True, "kind": "invented"})))
    monkeypatch.setattr(pipeline_mod.httpx, "AsyncClient", factory)
    result = await p._structured_llm("sys", "usr", "sname", {"type": "object"})
    assert result == {"ok": True, "kind": "invented"}
