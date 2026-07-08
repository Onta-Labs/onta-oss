"""Tests for the central LLM router (primary + OpenRouter fallback)."""

import pytest

from cograph_client.resolver import llm_router


def _capturing_client(monkeypatch, payload: dict, capture: dict):
    """Patch httpx.AsyncClient to record the request and return ``payload``."""

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
            capture["url"] = url
            capture["headers"] = headers
            capture["json"] = json
            return _Resp()

    monkeypatch.setattr(llm_router.httpx, "AsyncClient", _Client)


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


# --------------------------------------------------------------------------- #
# Provider selection (OMNIX_LLM_PROVIDER): openrouter (default) vs cerebras.
# All HTTP is mocked — no real network. Test data uses only invented tokens.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_provider_unset_defaults_to_openrouter(monkeypatch):
    """Env unset → OpenRouter base + the caller's OpenRouter key/model, EXACTLY
    as before. This guards the byte-identical-default requirement."""
    monkeypatch.delenv("OMNIX_LLM_PROVIDER", raising=False)
    monkeypatch.setattr(llm_router, "FALLBACK_MODEL", "widgetco/sprocket-fallback")
    cap: dict = {}
    _capturing_client(
        monkeypatch, {"choices": [{"message": {"content": "Widget"}}]}, cap
    )

    out = await llm_router.openrouter_chat(
        "openrouter-secret", "sys", "usr", model="widgetco/sprocket-2"
    )

    assert out == "Widget"
    assert cap["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert cap["headers"]["Authorization"] == "Bearer openrouter-secret"
    assert cap["json"]["model"] == "widgetco/sprocket-2"
    # OpenRouter path keeps the primary→fallback `models` array.
    assert cap["json"]["models"] == ["widgetco/sprocket-2", "widgetco/sprocket-fallback"]


@pytest.mark.asyncio
async def test_provider_openrouter_explicit_matches_default(monkeypatch):
    """OMNIX_LLM_PROVIDER=openrouter behaves identically to unset."""
    monkeypatch.setenv("OMNIX_LLM_PROVIDER", "openrouter")
    cap: dict = {}
    _capturing_client(
        monkeypatch, {"choices": [{"message": {"content": "Gadget"}}]}, cap
    )

    out = await llm_router.openrouter_chat("or-key", "s", "u", model="widgetco/gadget-1")

    assert out == "Gadget"
    assert cap["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert cap["headers"]["Authorization"] == "Bearer or-key"
    assert cap["json"]["model"] == "widgetco/gadget-1"


@pytest.mark.asyncio
async def test_provider_cerebras_uses_cerebras_base_key_and_bare_slug(monkeypatch):
    """OMNIX_LLM_PROVIDER=cerebras (+ CEREBRAS_API_KEY) → Cerebras base URL, the
    Cerebras key, the BARE model slug, and NO `models` fallback array. The caller's
    OpenRouter key is NOT sent."""
    monkeypatch.setenv("OMNIX_LLM_PROVIDER", "cerebras")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    cap: dict = {}
    _capturing_client(
        monkeypatch, {"choices": [{"message": {"content": "Sprocket"}}]}, cap
    )

    # Caller passes an OpenRouter key + an OpenRouter-prefixed model; both should be
    # overridden in cerebras mode. The bare slug comes from PRIMARY_MODEL when the
    # caller-supplied model is a prefixed one? No — the caller's `model` is honored
    # verbatim (it's the per-role model), so pass the bare slug the operator wants.
    monkeypatch.setattr(llm_router, "PRIMARY_MODEL", "gadget-oss-120b")
    out = await llm_router.openrouter_chat(
        "openrouter-secret-should-be-ignored", "sys", "usr", model="gadget-oss-120b"
    )

    assert out == "Sprocket"
    assert cap["url"] == "https://api.cerebras.ai/v1/chat/completions"
    assert cap["headers"]["Authorization"] == "Bearer cerebras-secret"
    assert "openrouter-secret-should-be-ignored" not in cap["headers"]["Authorization"]
    assert cap["json"]["model"] == "gadget-oss-120b"  # bare slug, as-is
    assert "models" not in cap["json"]  # no OpenRouter fallback array on Cerebras


@pytest.mark.asyncio
async def test_provider_cerebras_uses_primary_model_when_caller_omits(monkeypatch):
    """In cerebras mode with no per-role model, the bare OMNIX_LLM_MODEL
    (PRIMARY_MODEL) slug is sent."""
    monkeypatch.setenv("OMNIX_LLM_PROVIDER", "cerebras")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    monkeypatch.setattr(llm_router, "PRIMARY_MODEL", "sprocket-oss-120b")
    cap: dict = {}
    _capturing_client(
        monkeypatch, {"choices": [{"message": {"content": "Widget"}}]}, cap
    )

    out = await llm_router.openrouter_chat("ignored", "s", "u")

    assert out == "Widget"
    assert cap["json"]["model"] == "sprocket-oss-120b"


@pytest.mark.asyncio
async def test_provider_cerebras_return_contract_matches_openrouter(monkeypatch):
    """The (content, finish_reason, usage) contract is parsed IDENTICALLY for
    Cerebras — same OpenAI-shaped choices/finish_reason/usage mapping."""
    monkeypatch.setenv("OMNIX_LLM_PROVIDER", "cerebras")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    cap: dict = {}
    _capturing_client(
        monkeypatch,
        {
            "choices": [
                {"message": {"content": "Gadget"}, "finish_reason": "length"}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        },
        cap,
    )

    content, reason, usage = await llm_router.openrouter_chat(
        "ignored",
        "s",
        "u",
        model="gadget-oss-120b",
        return_finish_reason=True,
        return_usage=True,
    )
    assert content == "Gadget"
    assert reason == "length"
    assert usage == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}


@pytest.mark.asyncio
async def test_provider_cerebras_missing_key_raises_clear_error(monkeypatch):
    """OMNIX_LLM_PROVIDER=cerebras with NO CEREBRAS_API_KEY → a clear error, NOT a
    silent fall-back to OpenRouter (we want the misconfiguration to be loud)."""
    monkeypatch.setenv("OMNIX_LLM_PROVIDER", "cerebras")
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)

    # No HTTP client patched — if it tried to POST, it would blow up differently.
    with pytest.raises(RuntimeError) as excinfo:
        await llm_router.openrouter_chat("or-key", "s", "u", model="gadget-oss-120b")

    msg = str(excinfo.value)
    assert "CEREBRAS_API_KEY" in msg
    assert "cerebras" in msg.lower()
    # Never leak the caller's (or any) key in the error.
    assert "or-key" not in msg
