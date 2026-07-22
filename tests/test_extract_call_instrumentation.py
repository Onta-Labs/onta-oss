"""Per-extraction-LLM-call instrumentation (ONTA-200).

`SchemaResolver._extract` emits exactly ONE structured ``extract_call`` log per
extraction LLM call carrying ``completion_tokens`` / ``prompt_tokens`` (threaded
back from the OpenRouter ``usage`` object, previously discarded), ``finish_reason``,
``records_in_chunk`` (records in the JSON chunk being extracted), and
``duration_ms``. Pure observability — no behavior change — so a slow discovery
run reveals output-token bloat directly instead of being reconstructed from
CloudWatch request gaps.

Assertions record against a MagicMock swapped in for the module logger rather
than ``structlog.testing.capture_logs()``: under the full suite the
``cograph.resolver`` module logger is cached by earlier tests, so ``capture_logs``
would silently intercept nothing. The mock-logger pattern (mirroring
tests/test_stage_timing.py) is order-independent.

The router half also asserts ``openrouter_chat``'s new ``return_usage`` flag is a
NON-breaking addition: every existing return shape (bare content, and
``return_finish_reason``-only) is untouched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import cograph_client.resolver.schema_resolver as sr
from cograph_client.resolver import llm_router
from cograph_client.resolver.schema_resolver import SchemaResolver
from cograph_client.resolver.verdict_cache import JsonVerdictCache


# --------------------------------------------------------------------------- #
# Harness                                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture
def mock_neptune():
    client = AsyncMock()
    client.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    client.update.return_value = None
    client.batch_exists.return_value = set()
    return client


def _resolver(mock_neptune, tmp_path):
    return SchemaResolver(
        mock_neptune, "fake-anthropic-key", JsonVerdictCache(tmp_path / "cache.json")
    )


def _extract_calls(mock_logger):
    """Every ``extract_call`` info() call's kwargs on a mock module logger."""
    return [
        c.kwargs
        for c in mock_logger.info.call_args_list
        if c.args and c.args[0] == "extract_call"
    ]


# --------------------------------------------------------------------------- #
# _extract — OpenRouter path                                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_extract_call_logged_with_usage_and_records(
    mock_neptune, tmp_path, monkeypatch
):
    """One ``extract_call`` per LLM call, with usage tokens threaded back from the
    OpenRouter response, the chunk's record count, finish_reason and duration."""
    resolver = _resolver(mock_neptune, tmp_path)
    resolver._openrouter_key = "test-key"  # force the OpenRouter branch

    rec = MagicMock()
    monkeypatch.setattr(sr, "logger", rec)

    records = [{"id": i, "name": f"m{i}"} for i in range(5)]
    content = json.dumps(records)

    async def fake_via_openrouter(user_content, system_prompt=None, **kwargs):
        # (content, finish_reason, usage) — the new 3-tuple shape.
        # ``**kwargs`` absorbs ONTA-381's adaptive ``max_tokens``.
        return (
            json.dumps({"entities": [], "relationships": []}),
            "stop",
            {"prompt_tokens": 123, "completion_tokens": 456, "total_tokens": 579},
        )

    monkeypatch.setattr(resolver, "_extract_via_openrouter", fake_via_openrouter)

    await resolver._extract(content, "json", existing_types={})

    calls = _extract_calls(rec)
    assert len(calls) == 1, "exactly one extract_call log per LLM call"
    kw = calls[0]
    assert kw["provider"] == "openrouter"
    assert kw["prompt_tokens"] == 123
    assert kw["completion_tokens"] == 456
    assert kw["finish_reason"] == "stop"
    assert kw["records_in_chunk"] == 5
    assert isinstance(kw["duration_ms"], (int, float))
    assert kw["duration_ms"] >= 0
    # ONTA-381: the adaptive budget actually requested is logged for diagnosis.
    assert kw["max_tokens"] >= SchemaResolver.EXTRACT_MAX_TOKENS


@pytest.mark.asyncio
async def test_extract_call_logs_none_tokens_when_usage_absent(
    mock_neptune, tmp_path, monkeypatch
):
    """A provider that omits ``usage`` → None token fields (not a KeyError), and
    the call is still logged (observability must not depend on usage presence)."""
    resolver = _resolver(mock_neptune, tmp_path)
    resolver._openrouter_key = "test-key"

    rec = MagicMock()
    monkeypatch.setattr(sr, "logger", rec)

    async def fake_via_openrouter(user_content, system_prompt=None, **kwargs):
        return (json.dumps({"entities": [], "relationships": []}), "length", None)

    monkeypatch.setattr(resolver, "_extract_via_openrouter", fake_via_openrouter)

    await resolver._extract(json.dumps([{"id": 1}]), "json", existing_types={})

    calls = _extract_calls(rec)
    assert len(calls) == 1
    kw = calls[0]
    assert kw["prompt_tokens"] is None
    assert kw["completion_tokens"] is None
    assert kw["finish_reason"] == "length"  # truncation signal still surfaced
    assert kw["records_in_chunk"] == 1
    assert "max_tokens" in kw


@pytest.mark.asyncio
async def test_extract_call_records_none_for_non_json(
    mock_neptune, tmp_path, monkeypatch
):
    """Free-text content has no records array → ``records_in_chunk`` is None
    (not a bogus 0), keeping the token-vs-records read honest."""
    resolver = _resolver(mock_neptune, tmp_path)
    resolver._openrouter_key = "test-key"

    rec = MagicMock()
    monkeypatch.setattr(sr, "logger", rec)

    async def fake_via_openrouter(user_content, system_prompt=None, **kwargs):
        return (json.dumps({"entities": [], "relationships": []}), "stop", {})

    monkeypatch.setattr(resolver, "_extract_via_openrouter", fake_via_openrouter)

    await resolver._extract("some free prose", "text", existing_types={})

    calls = _extract_calls(rec)
    assert len(calls) == 1
    assert calls[0]["records_in_chunk"] is None


# --------------------------------------------------------------------------- #
# _extract — Anthropic path                                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_extract_call_logged_on_anthropic_path(
    mock_neptune, tmp_path, monkeypatch
):
    """No OpenRouter key → the Anthropic branch, which pulls token counts from the
    SDK ``usage`` object and stop_reason for finish_reason."""
    resolver = _resolver(mock_neptune, tmp_path)
    resolver._openrouter_key = ""  # force the Anthropic branch

    rec = MagicMock()
    monkeypatch.setattr(sr, "logger", rec)

    fake_msg = SimpleNamespace(
        content=[SimpleNamespace(text=json.dumps({"entities": [], "relationships": []}))],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=11, output_tokens=22),
    )
    resolver._anthropic = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=fake_msg))
    )

    await resolver._extract(json.dumps([{"id": 1}, {"id": 2}]), "json", existing_types={})

    calls = _extract_calls(rec)
    assert len(calls) == 1
    kw = calls[0]
    assert kw["provider"] == "anthropic"
    assert kw["prompt_tokens"] == 11
    assert kw["completion_tokens"] == 22
    assert kw["finish_reason"] == "end_turn"
    assert kw["records_in_chunk"] == 2


# --------------------------------------------------------------------------- #
# openrouter_chat — return_usage is a NON-breaking addition                    #
# --------------------------------------------------------------------------- #
def _stub_client(monkeypatch, payload: dict):
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
async def test_openrouter_chat_returns_usage_with_finish_reason(monkeypatch):
    """return_finish_reason + return_usage → (content, finish_reason, usage)."""
    _stub_client(
        monkeypatch,
        {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 9},
        },
    )
    content, reason, usage = await llm_router.openrouter_chat(
        "k", "s", "u", return_finish_reason=True, return_usage=True
    )
    assert content == "hi"
    assert reason == "stop"
    assert usage == {"prompt_tokens": 7, "completion_tokens": 9}


@pytest.mark.asyncio
async def test_openrouter_chat_usage_only(monkeypatch):
    """return_usage alone → (content, usage), no finish_reason element."""
    _stub_client(
        monkeypatch,
        {
            "choices": [{"message": {"content": "yo"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        },
    )
    content, usage = await llm_router.openrouter_chat("k", "s", "u", return_usage=True)
    assert content == "yo"
    assert usage == {"prompt_tokens": 1, "completion_tokens": 2}


@pytest.mark.asyncio
async def test_openrouter_chat_usage_none_when_absent(monkeypatch):
    """A provider that omits ``usage`` yields None (not a KeyError)."""
    _stub_client(monkeypatch, {"choices": [{"message": {"content": "done"}}]})
    content, usage = await llm_router.openrouter_chat("k", "s", "u", return_usage=True)
    assert content == "done"
    assert usage is None


@pytest.mark.asyncio
async def test_openrouter_chat_existing_shapes_unchanged(monkeypatch):
    """The pre-ONTA-200 contracts are byte-identical: bare content by default,
    and (content, finish_reason) with return_finish_reason only."""
    _stub_client(
        monkeypatch,
        {
            "choices": [{"message": {"content": "x"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 6},
        },
    )
    # Default: bare string, no tuple (usage present in payload but not surfaced).
    assert await llm_router.openrouter_chat("k", "s", "u") == "x"
    # finish_reason only: 2-tuple, still no usage element.
    out = await llm_router.openrouter_chat("k", "s", "u", return_finish_reason=True)
    assert out == ("x", "length")
