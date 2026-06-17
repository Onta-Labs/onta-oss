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
