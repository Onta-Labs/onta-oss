"""Tests for the central LLM router (primary + OpenRouter fallback)."""

import pytest

from cograph_client.resolver import llm_router


def test_model_chain_appends_fallback(monkeypatch):
    monkeypatch.setattr(llm_router, "FALLBACK_MODEL", "openai/gpt-5.5")
    assert llm_router.model_chain("anthropic/claude-opus-4.8") == [
        "anthropic/claude-opus-4.8",
        "openai/gpt-5.5",
    ]


def test_model_chain_dedups_and_drops_empty(monkeypatch):
    # Fallback equal to primary → single entry (no duplicate routing).
    monkeypatch.setattr(llm_router, "FALLBACK_MODEL", "x")
    assert llm_router.model_chain("x") == ["x"]
    # Empty fallback → primary only.
    monkeypatch.setattr(llm_router, "FALLBACK_MODEL", "")
    assert llm_router.model_chain("y") == ["y"]


def test_model_chain_defaults_to_primary(monkeypatch):
    monkeypatch.setattr(llm_router, "PRIMARY_MODEL", "anthropic/claude-opus-4.8")
    monkeypatch.setattr(llm_router, "FALLBACK_MODEL", "openai/gpt-5.5")
    assert llm_router.model_chain() == ["anthropic/claude-opus-4.8", "openai/gpt-5.5"]


@pytest.mark.asyncio
async def test_openrouter_chat_sends_fallback_array_and_returns_content(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "hello"}}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr(llm_router.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(llm_router, "FALLBACK_MODEL", "openai/gpt-5.5")

    out = await llm_router.openrouter_chat(
        "secret-key", "sys", "usr", model="anthropic/claude-opus-4.8", max_tokens=7
    )

    assert out == "hello"
    assert captured["json"]["model"] == "anthropic/claude-opus-4.8"
    assert captured["json"]["models"] == ["anthropic/claude-opus-4.8", "openai/gpt-5.5"]
    assert captured["json"]["max_tokens"] == 7
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert captured["url"].endswith("/chat/completions")


def _stub_client(monkeypatch, payload: dict):
    """Patch httpx.AsyncClient to return a canned OpenRouter response payload."""

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            return _Resp()

    monkeypatch.setattr(llm_router.httpx, "AsyncClient", _Client)


@pytest.mark.asyncio
async def test_openrouter_chat_default_returns_bare_content(monkeypatch):
    """ONTA-196: the default contract is UNCHANGED — a bare content string — so
    every existing caller keeps working without unpacking a tuple."""
    _stub_client(
        monkeypatch,
        {"choices": [{"message": {"content": "hi"}, "finish_reason": "length"}]},
    )
    out = await llm_router.openrouter_chat("k", "s", "u")
    assert out == "hi"  # a plain str, not a tuple


@pytest.mark.asyncio
async def test_openrouter_chat_returns_finish_reason_when_requested(monkeypatch):
    """With return_finish_reason=True the call returns (content, finish_reason) so
    a caller can detect a length-truncated reply and split-retry instead of
    dropping the batch."""
    _stub_client(
        monkeypatch,
        {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]},
    )
    content, reason = await llm_router.openrouter_chat(
        "k", "s", "u", return_finish_reason=True
    )
    assert content == "partial"
    assert reason == "length"


@pytest.mark.asyncio
async def test_openrouter_chat_finish_reason_none_when_absent(monkeypatch):
    """A provider that omits finish_reason yields None (not a KeyError) — a clean
    finish is indistinguishable from an unknown one, which is the safe default
    (no spurious truncation signal)."""
    _stub_client(monkeypatch, {"choices": [{"message": {"content": "done"}}]})
    content, reason = await llm_router.openrouter_chat(
        "k", "s", "u", return_finish_reason=True
    )
    assert content == "done"
    assert reason is None
