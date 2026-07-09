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
    # A tiny caller budget (7) is FLOORED to REASONING_MIN_TOKENS so a reasoning
    # model isn't starved of its content phase (see the reasoning-floor tests below).
    assert captured["json"]["max_tokens"] == llm_router.REASONING_MIN_TOKENS
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
async def test_provider_cerebras_slash_model_stays_on_openrouter(monkeypatch):
    """Under OMNIX_LLM_PROVIDER=cerebras, a per-role model with an OpenRouter
    ``vendor/model`` slug (which Cerebras cannot serve) keeps routing to
    OpenRouter with the caller's key and the ``models`` fallback array. This is
    the production regression: the global flip sent CSV schema inference's
    ``google/gemini-2.5-flash`` to api.cerebras.ai → 404 → every ingest 500d."""
    monkeypatch.setenv("OMNIX_LLM_PROVIDER", "cerebras")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    monkeypatch.setattr(llm_router, "PRIMARY_MODEL", "sprocket-oss-120b")
    monkeypatch.setattr(llm_router, "FALLBACK_MODEL", "acme/fallback-1")
    cap: dict = {}
    _capturing_client(
        monkeypatch, {"choices": [{"message": {"content": "Doohickey"}}]}, cap
    )

    out = await llm_router.openrouter_chat(
        "openrouter-secret", "sys", "usr", model="google/gemini-2.5-flash"
    )

    assert out == "Doohickey"
    assert cap["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert cap["headers"]["Authorization"] == "Bearer openrouter-secret"
    assert cap["json"]["model"] == "google/gemini-2.5-flash"
    assert cap["json"]["models"] == ["google/gemini-2.5-flash", "acme/fallback-1"]


@pytest.mark.asyncio
async def test_provider_cerebras_slash_primary_model_stays_on_openrouter(monkeypatch):
    """Same slug-shape guard when the caller omits ``model`` and PRIMARY_MODEL
    itself is an OpenRouter slug (OMNIX_LLM_MODEL left at a vendor/model id)."""
    monkeypatch.setenv("OMNIX_LLM_PROVIDER", "cerebras")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    monkeypatch.setattr(llm_router, "PRIMARY_MODEL", "anthropic/claude-opus-4.8")
    cap: dict = {}
    _capturing_client(
        monkeypatch, {"choices": [{"message": {"content": "Widget"}}]}, cap
    )

    out = await llm_router.openrouter_chat("or-key", "s", "u")

    assert out == "Widget"
    assert cap["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert cap["json"]["model"] == "anthropic/claude-opus-4.8"


@pytest.mark.asyncio
async def test_provider_cerebras_slash_model_needs_no_cerebras_key(monkeypatch):
    """A slash-slug call under cerebras mode routes to OpenRouter, so a missing
    CEREBRAS_API_KEY must not fail it — the fail-loud guard is only for calls
    Cerebras would actually serve (bare slugs)."""
    monkeypatch.setenv("OMNIX_LLM_PROVIDER", "cerebras")
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    cap: dict = {}
    _capturing_client(
        monkeypatch, {"choices": [{"message": {"content": "Gizmo"}}]}, cap
    )

    out = await llm_router.openrouter_chat(
        "or-key", "s", "u", model="google/gemini-2.5-flash"
    )

    assert out == "Gizmo"
    assert cap["url"] == "https://openrouter.ai/api/v1/chat/completions"


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


# --------------------------------------------------------------------------- #
# Missing / empty / malformed response-shape guard (production KeyError today).
#
# ``openrouter_chat`` did a RAW subscript ``choices[0]["message"]["content"]``:
# when a (reasoning) model returns a message that OMITS ``content`` — e.g.
# Cerebras gpt-oss-120b with ``finish_reason == "length"`` after reasoning ate
# the whole budget, or a filtered/empty reply — that raised a HARD
# ``KeyError('content')`` on the EXTRACTION path (web-ingest spec resolution,
# agent classify, enrichment, CSV schema inference). This is the same bug class
# #172 fixed for the SEPARATE query pipeline (``nlp/pipeline.py``); this guards
# the extraction router. The fix degrades every shape defect to ONE diagnosable
# ``ValueError`` naming the ACTIVE provider — NEVER a raw KeyError/IndexError/
# TypeError. All values below are invented tokens; all HTTP is mocked.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        # content key MISSING (the production traceback: reasoning-budget exhaustion)
        {"choices": [{"message": {"role": "assistant"}, "finish_reason": "length"}]},
        {"choices": [{"message": {"content": None}}]},  # content: null
        {"choices": [{"message": {"content": ""}}]},  # content: ""
        {},  # no choices key at all
        {"choices": []},  # empty choices list
        {"choices": [{"finish_reason": "stop"}]},  # choice has no message
        {"choices": [{"message": "not-a-dict"}]},  # message is not a dict
        {"choices": ["not-a-dict"]},  # choice is not a dict
        {"choices": "not-a-list"},  # choices is not a list
        [],  # whole payload is not a dict
        None,  # whole payload is None
    ],
    ids=[
        "content-key-missing",
        "content-null",
        "content-empty-string",
        "no-choices-key",
        "empty-choices-list",
        "choice-without-message",
        "message-not-dict",
        "choice-not-dict",
        "choices-not-list",
        "payload-not-dict",
        "payload-none",
    ],
)
async def test_openrouter_chat_missing_content_degrades_to_clean_error(
    monkeypatch, payload
):
    """Every malformed / empty response shape raises the SAME clean ``ValueError``
    (NOT a raw KeyError/IndexError/TypeError), and the message names the provider.
    This is the mechanism the production ``KeyError('content')`` violated."""
    monkeypatch.delenv("OMNIX_LLM_PROVIDER", raising=False)  # default = openrouter
    _stub_client(monkeypatch, payload)

    with pytest.raises(ValueError) as excinfo:
        await llm_router.openrouter_chat("k", "s", "u")

    # Assert the MECHANISM: not a raw lookup/type crash — a typed, diagnosable error.
    assert not isinstance(excinfo.value, (KeyError, IndexError, TypeError))
    msg = str(excinfo.value)
    assert "empty LLM response" in msg
    assert "openrouter" in msg  # names the ACTIVE provider


@pytest.mark.asyncio
async def test_openrouter_chat_missing_content_names_cerebras_provider(monkeypatch):
    """When the ACTIVE provider is Cerebras (bare slug), the clean empty-response
    error names Cerebras — reusing this function's post-#165 provider derivation,
    not a hardcoded backend."""
    monkeypatch.setenv("OMNIX_LLM_PROVIDER", "cerebras")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    monkeypatch.setattr(llm_router, "PRIMARY_MODEL", "gadget-oss-120b")
    # finish_reason=length + content key absent = the exact production shape.
    _stub_client(
        monkeypatch,
        {"choices": [{"message": {"role": "assistant"}, "finish_reason": "length"}]},
    )

    with pytest.raises(ValueError) as excinfo:
        await llm_router.openrouter_chat("ignored", "s", "u", model="gadget-oss-120b")

    assert not isinstance(excinfo.value, (KeyError, IndexError, TypeError))
    msg = str(excinfo.value)
    assert "empty LLM response" in msg
    assert "cerebras" in msg
    # The readily-available finish_reason is threaded in for diagnosis (like #172).
    assert "length" in msg


@pytest.mark.asyncio
async def test_openrouter_chat_missing_content_guard_holds_for_return_variants(
    monkeypatch,
):
    """The guard fires BEFORE the return-shape branch, so the
    ``return_finish_reason`` / ``return_usage`` variants also raise the clean
    error (never a KeyError) on a missing content — the guard covers ALL exit
    paths, not just the bare-string one."""
    monkeypatch.delenv("OMNIX_LLM_PROVIDER", raising=False)
    _stub_client(
        monkeypatch,
        {"choices": [{"message": {"role": "assistant"}, "finish_reason": "length"}]},
    )

    for kwargs in (
        {"return_finish_reason": True},
        {"return_usage": True},
        {"return_finish_reason": True, "return_usage": True},
    ):
        with pytest.raises(ValueError) as excinfo:
            await llm_router.openrouter_chat("k", "s", "u", **kwargs)
        assert not isinstance(excinfo.value, (KeyError, IndexError, TypeError))
        assert "empty LLM response" in str(excinfo.value)


@pytest.mark.asyncio
async def test_openrouter_chat_happy_path_returns_content_verbatim(monkeypatch):
    """Regression guard: a normal non-empty content string is returned BYTE-FOR-BYTE
    unchanged — the guard only touches the missing/empty/malformed branches."""
    monkeypatch.delenv("OMNIX_LLM_PROVIDER", raising=False)
    _stub_client(
        monkeypatch,
        {"choices": [{"message": {"content": "  Sprocket-42 verbatim  "}}]},
    )
    out = await llm_router.openrouter_chat("k", "s", "u")
    assert out == "  Sprocket-42 verbatim  "  # not stripped, not altered


# --------------------------------------------------------------------------- #
# Reasoning-adequate token floor (REASONING_MIN_TOKENS).
#
# A reasoning model (Cerebras gpt-oss-120b, the deployed PRIMARY_MODEL) spends
# part of its budget on a hidden reasoning phase BEFORE emitting content. A small
# `max_tokens` (the ~200-400 several call sites pass) is consumed entirely by
# reasoning, so the model returns `finish_reason=length` with EMPTY content and
# openrouter_chat raises the `empty LLM response ... (finish_reason=length)` guard
# above. openrouter_chat floors every completion budget to REASONING_MIN_TOKENS so
# no caller can starve reasoning. The floor is a CEILING: a value at/above it is
# forwarded verbatim; a value below it is raised to it. It applies to whichever
# completion-budget field each request branch sends. All HTTP is mocked.
# --------------------------------------------------------------------------- #


def _cerebras_env(monkeypatch):
    """Route through the Cerebras request branch (bare-slug PRIMARY_MODEL)."""
    monkeypatch.setenv("OMNIX_LLM_PROVIDER", "cerebras")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    monkeypatch.setattr(llm_router, "PRIMARY_MODEL", "gadget-oss-120b")


def _openrouter_env(monkeypatch):
    """Route through the OpenRouter request branch (vendor/model slug)."""
    monkeypatch.delenv("OMNIX_LLM_PROVIDER", raising=False)
    monkeypatch.setattr(llm_router, "PRIMARY_MODEL", "widgetco/sprocket-2")


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["openrouter", "cerebras"])
async def test_openrouter_chat_floors_small_max_tokens(monkeypatch, route):
    """A small caller budget (200) is raised to REASONING_MIN_TOKENS on BOTH the
    OpenAI-style (OpenRouter) and Cerebras request branches — the exact production
    starvation (`empty LLM response ... finish_reason=length`) this prevents."""
    (_cerebras_env if route == "cerebras" else _openrouter_env)(monkeypatch)
    cap: dict = {}
    _capturing_client(monkeypatch, {"choices": [{"message": {"content": "ok"}}]}, cap)

    out = await llm_router.openrouter_chat("k", "s", "u", max_tokens=200)

    assert out == "ok"
    assert cap["json"]["max_tokens"] == llm_router.REASONING_MIN_TOKENS
    assert cap["json"]["max_tokens"] >= 2048


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["openrouter", "cerebras"])
async def test_openrouter_chat_does_not_lower_large_max_tokens(monkeypatch, route):
    """The floor never LOWERS a budget: a value above the floor (8000) is forwarded
    verbatim on both branches — max_tokens stays a ceiling for callers above it."""
    (_cerebras_env if route == "cerebras" else _openrouter_env)(monkeypatch)
    cap: dict = {}
    _capturing_client(monkeypatch, {"choices": [{"message": {"content": "ok"}}]}, cap)

    await llm_router.openrouter_chat("k", "s", "u", max_tokens=8000)

    assert cap["json"]["max_tokens"] == 8000


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["openrouter", "cerebras"])
async def test_openrouter_chat_value_at_floor_stays(monkeypatch, route):
    """A value exactly at the floor is unchanged (boundary: max() is inclusive)."""
    (_cerebras_env if route == "cerebras" else _openrouter_env)(monkeypatch)
    cap: dict = {}
    _capturing_client(monkeypatch, {"choices": [{"message": {"content": "ok"}}]}, cap)

    await llm_router.openrouter_chat(
        "k", "s", "u", max_tokens=llm_router.REASONING_MIN_TOKENS
    )

    assert cap["json"]["max_tokens"] == llm_router.REASONING_MIN_TOKENS


@pytest.mark.asyncio
async def test_openrouter_chat_default_budget_unchanged_by_floor(monkeypatch):
    """The default max_tokens (4096) already exceeds the floor, so an unspecified
    budget is forwarded verbatim — the floor only touches small-budget callers."""
    _openrouter_env(monkeypatch)
    cap: dict = {}
    _capturing_client(monkeypatch, {"choices": [{"message": {"content": "ok"}}]}, cap)

    await llm_router.openrouter_chat("k", "s", "u")

    assert cap["json"]["max_tokens"] == 4096
