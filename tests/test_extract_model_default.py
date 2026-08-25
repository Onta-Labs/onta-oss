"""Hermetic: ingest extract default is DeepSeek V4 Pro reasoning."""
from __future__ import annotations

import pytest

from infona_client.eval_run import EXTRACT_MODEL_DEFAULT as EVAL_EXTRACT_DEFAULT
from infona_client.resolver.llm_router import (
    EXTRACT_MODEL_DEFAULT,
    EXTRACT_REASONING,
    is_reasoning_extract_model,
    openrouter_chat,
)
from infona_client.resolver.ontology_resolver import OntologyResolver
from infona_client.resolver.schema_resolver import SchemaResolver


def test_extract_default_is_latest_deepseek_reasoning():
    assert EXTRACT_MODEL_DEFAULT == "deepseek/deepseek-v4-pro-0813"
    assert SchemaResolver.EXTRACT_MODEL == EXTRACT_MODEL_DEFAULT
    assert OntologyResolver.EXTRACT_MODEL == EXTRACT_MODEL_DEFAULT
    assert EVAL_EXTRACT_DEFAULT == EXTRACT_MODEL_DEFAULT
    assert EXTRACT_REASONING == {"enabled": True, "effort": "high", "exclude": True}
    assert is_reasoning_extract_model(EXTRACT_MODEL_DEFAULT)
    assert not is_reasoning_extract_model("anthropic/claude-opus-4.8")


@pytest.mark.asyncio
async def test_openrouter_chat_sends_reasoning_for_extract_model(monkeypatch):
    captured: dict = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return FakeResp()

    monkeypatch.setattr("infona_client.resolver.llm_router.httpx.AsyncClient", FakeClient)
    text = await openrouter_chat("k", "sys", "user", model=EXTRACT_MODEL_DEFAULT)
    assert text == "{}"
    body = captured["json"]
    assert body["model"] == "deepseek/deepseek-v4-pro-0813"
    assert body["reasoning"] == {"enabled": True, "effort": "high", "exclude": True}


@pytest.mark.asyncio
async def test_openrouter_chat_skips_reasoning_for_claude(monkeypatch):
    captured: dict = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return FakeResp()

    monkeypatch.setattr("infona_client.resolver.llm_router.httpx.AsyncClient", FakeClient)
    await openrouter_chat("k", "sys", "user", model="anthropic/claude-opus-4.8")
    assert "reasoning" not in captured["json"]
