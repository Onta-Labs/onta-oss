"""NL->SPARQL robustness against a reasoning model that exhausts its output
budget (Cerebras gpt-oss-120b).

Real persona-eval failure mode (11 occurrences across two runs, the single worst
blocker for one persona): the query model is a reasoning model that spends output
tokens on reasoning BEFORE emitting the answer. When it burns its whole
``max_completion_tokens`` and hits ``finish_reason == "length"``, the API response
message can be MISSING the ``content`` key ENTIRELY (not merely ``content: null``).
The old ``_require_message_content`` did a raw subscript
``data["choices"][0]["message"]["content"]`` and raised a hard
``KeyError('content')`` — which is NOT the diagnosable "empty LLM response"
``ValueError`` the retry path tolerates, so ``ask()`` hard-failed with
``"Could not answer … Last error: 'content'"``.

The fix (asserted here, MECHANISM only, invented tokens — never a real model
literal or ontology):
  1. ``_require_message_content`` degrades EVERY shape defect — absent/empty
     ``choices``/``message``/``content``, ``null``, or ``""`` — to ONE typed
     :class:`EmptyLLMResponse` (a ``ValueError`` subclass) that names the provider
     and carries ``finish_reason``. No raw ``KeyError``/``IndexError``/``TypeError``
     escapes.
  2. ``ask()`` RECOVERS a ``finish_reason == "length"`` truncation instead of just
     not-crashing: tier 1 retries the Cerebras path with a BIGGER token budget;
     a second consecutive truncation (tier 2) falls back to the non-reasoning
     OpenRouter/Anthropic JSON path. A hard reasoning-model question returns an
     answer rather than "Could not answer".
  3. The happy path (content present) is byte-identical — no recovery kwargs, the
     default 2048-token budget, same Cerebras call.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cograph_client.nlp import pipeline as pipeline_mod
from cograph_client.nlp.pipeline import (
    CEREBRAS_LENGTH_RECOVERY_TOKENS,
    EmptyLLMResponse,
    NLQueryPipeline,
    _require_message_content,
)

# invented ontology token — never a real type/attribute
FULL_ONTOLOGY = "FULL_ONTOLOGY_TOKEN_XYZ"

_RealAsyncClient = httpx.AsyncClient


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _resp(message: dict, finish_reason=None) -> dict:
    """An OpenAI-compatible chat-completion response with the given `message`
    dict (which may or may not carry a `content` key) and optional
    `finish_reason` on the choice."""
    choice: dict = {"message": message}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"choices": [choice]}


def _content_resp(content) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _sparql_json() -> str:
    return json.dumps(
        {"sparql": "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1",
         "explanation": "invented", "functions_needed": []}
    )


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


def _make_pipeline(provider: str = "cerebras") -> NLQueryPipeline:
    p = NLQueryPipeline(AsyncMock(), "invented-anthropic-key")
    p._query_provider = provider
    p._cerebras_key = "invented-cerebras-key"
    p._openrouter_key = "invented-openrouter-key"
    p._query_model = "invented-model-xyz"
    return p


def _post_factory(payload: dict, captured: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)

    def factory(*a, **k):
        return _RealAsyncClient(transport=transport)

    return factory


# --------------------------------------------------------------------------- #
# 1. _require_message_content: every shape defect -> typed EmptyLLMResponse     #
# --------------------------------------------------------------------------- #
def test_missing_content_key_raises_empty_not_keyerror():
    """The bug: message = {"role": "assistant"} with the `content` key ENTIRELY
    ABSENT + finish_reason="length" used to raise a hard KeyError('content'). It
    must now degrade to the diagnosable EmptyLLMResponse (a ValueError) carrying
    finish_reason='length', with NO KeyError."""
    data = _resp({"role": "assistant"}, finish_reason="length")
    with pytest.raises(ValueError) as ei:  # KeyError is NOT a ValueError
        _require_message_content(data, "invented-provider")
    assert isinstance(ei.value, EmptyLLMResponse)
    assert not isinstance(ei.value, (KeyError, IndexError, TypeError))
    assert "invented-provider" in str(ei.value).lower()
    assert ei.value.finish_reason == "length"


@pytest.mark.parametrize("bad_content", [None, ""])
def test_null_and_empty_content_still_handled(bad_content):
    """content: null / "" still degrade — and the finish_reason is threaded."""
    data = _resp({"role": "assistant", "content": bad_content}, finish_reason="length")
    with pytest.raises(EmptyLLMResponse) as ei:
        _require_message_content(data, "provx")
    assert ei.value.finish_reason == "length"
    assert "provx" in str(ei.value).lower()


@pytest.mark.parametrize("data", [
    {},                       # no "choices" key at all
    {"choices": []},          # empty choices list
    {"choices": [{}]},        # choice missing "message"
    {"choices": [{"message": None}]},   # message present but null
    {"choices": [{"message": "not-a-dict"}]},  # message wrong type
    None,                     # payload isn't even a dict
    {"choices": "garbage"},   # choices wrong type
])
def test_malformed_shapes_degrade_without_raw_keyerror(data):
    """Absent/empty/wrong-typed choices/message all degrade to EmptyLLMResponse —
    we never trade one raw KeyError/IndexError/TypeError for another."""
    with pytest.raises(EmptyLLMResponse) as ei:
        _require_message_content(data, "provx")
    assert not isinstance(ei.value, (KeyError, IndexError, TypeError))
    assert "provx" in str(ei.value).lower()


def test_finish_reason_none_when_absent():
    """A missing-content response WITHOUT a finish_reason yields finish_reason
    None (not a KeyError) — so a non-length empty isn't misread as recoverable."""
    with pytest.raises(EmptyLLMResponse) as ei:
        _require_message_content(_resp({"role": "assistant"}), "provx")
    assert ei.value.finish_reason is None


def test_present_content_returned_verbatim():
    """Happy path byte-identical: a normal string is returned unchanged even when
    finish_reason is present."""
    data = _resp({"role": "assistant", "content": "SELECT ?s WHERE {}"}, finish_reason="stop")
    assert _require_message_content(data, "p") == "SELECT ?s WHERE {}"


# --------------------------------------------------------------------------- #
# 2. _generate_via_cerebras: missing content -> EmptyLLMResponse, budget threads #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cerebras_missing_content_key_no_keyerror(monkeypatch):
    """The end-to-end generator on a length-truncated missing-content Cerebras
    response raises the typed EmptyLLMResponse(finish_reason='length'), NOT a raw
    KeyError('content')."""
    p = _make_pipeline("cerebras")
    payload = {"choices": [{"message": {"role": "assistant"}, "finish_reason": "length"}]}
    monkeypatch.setattr(pipeline_mod.httpx, "AsyncClient", _post_factory(payload))
    with pytest.raises(EmptyLLMResponse) as ei:
        await p._generate_via_cerebras("give me sparql")
    assert ei.value.finish_reason == "length"
    assert not isinstance(ei.value, (KeyError, IndexError, TypeError))


@pytest.mark.asyncio
async def test_cerebras_default_budget_unchanged(monkeypatch):
    """Happy path byte-identical: a plain call keeps the 2048 default budget."""
    p = _make_pipeline("cerebras")
    captured: dict = {}
    monkeypatch.setattr(
        pipeline_mod.httpx, "AsyncClient", _post_factory(_content_resp(_sparql_json()), captured)
    )
    out = await p._generate_via_cerebras("give me sparql")
    assert captured["body"]["max_completion_tokens"] == 2048
    assert out["sparql"].startswith("SELECT")


@pytest.mark.asyncio
async def test_cerebras_accepts_bumped_budget(monkeypatch):
    """The recovery retry can pass a BIGGER budget, which reaches the Cerebras
    payload verbatim (room for reasoning + the answer)."""
    p = _make_pipeline("cerebras")
    captured: dict = {}
    monkeypatch.setattr(
        pipeline_mod.httpx, "AsyncClient", _post_factory(_content_resp(_sparql_json()), captured)
    )
    await p._generate_via_cerebras("give me sparql", max_completion_tokens=CEREBRAS_LENGTH_RECOVERY_TOKENS)
    assert captured["body"]["max_completion_tokens"] == CEREBRAS_LENGTH_RECOVERY_TOKENS
    assert captured["body"]["max_completion_tokens"] > 2048


# --------------------------------------------------------------------------- #
# 3. _generate_sparql: prefer_fallback routes OFF the reasoning provider         #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_generate_sparql_prefer_fallback_skips_cerebras_for_openrouter():
    p = _make_pipeline("cerebras")
    called: dict = {}

    async def fake_cerebras(prompt, **k):
        called["cerebras"] = True
        return {}

    async def fake_openrouter(prompt, **k):
        called["openrouter"] = True
        return {"sparql": "SELECT ?s WHERE {}", "explanation": "", "functions_needed": []}

    with patch.object(p, "_generate_via_cerebras", new=fake_cerebras), \
         patch.object(p, "_generate_via_openrouter", new=fake_openrouter):
        out = await p._generate_sparql("q", "onto", "g", prefer_fallback=True)

    assert called.get("openrouter") is True
    assert "cerebras" not in called  # the reasoning path was skipped entirely
    assert out["sparql"].startswith("SELECT")


@pytest.mark.asyncio
async def test_generate_sparql_prefer_fallback_uses_anthropic_when_openrouter_is_provider():
    """When OpenRouter is ALREADY the (truncating) provider, prefer_fallback routes
    to Anthropic rather than re-calling the same provider."""
    p = _make_pipeline("openrouter")
    p._cerebras_key = ""
    called: dict = {}

    async def fake_openrouter(prompt, **k):
        called["openrouter"] = True
        return {}

    async def fake_anthropic(prompt, **k):
        called["anthropic"] = True
        return {"sparql": "SELECT ?s WHERE {}", "explanation": "", "functions_needed": []}

    with patch.object(p, "_generate_via_openrouter", new=fake_openrouter), \
         patch.object(p, "_generate_via_anthropic", new=fake_anthropic):
        out = await p._generate_sparql("q", "onto", "g", prefer_fallback=True)

    assert called.get("anthropic") is True
    assert "openrouter" not in called
    assert out["sparql"].startswith("SELECT")


# --------------------------------------------------------------------------- #
# 4. ask(): end-to-end recovery from a length-truncation                        #
# --------------------------------------------------------------------------- #
def _ask_ctx(p: NLQueryPipeline, gen):
    return (
        patch.object(pipeline_mod, "get_embedding_service", return_value=None),
        patch.object(p, "_fetch_ontology", new=AsyncMock(return_value=FULL_ONTOLOGY)),
        patch.object(p, "_generate_sparql", new=gen),
    )


@pytest.mark.asyncio
async def test_ask_recovers_from_length_truncation_with_bigger_budget():
    """Attempt-0 length-truncates (EmptyLLMResponse, finish_reason='length');
    attempt-1 must request a BIGGER token budget and recover the answer — not the
    "Could not answer … Last error: 'content'" degrade the raw KeyError caused."""
    neptune = AsyncMock()
    neptune.query.return_value = _rows(["name"], {"name": "widget-omega"})
    p = NLQueryPipeline(neptune, "invented-anthropic-key")
    p._openrouter_key = ""  # fail-open rephrase, no network

    gen = AsyncMock(side_effect=[
        EmptyLLMResponse("cerebras", finish_reason="length"),                     # attempt 0
        {"sparql": "SELECT ?name WHERE { ?s <p> ?name }",
         "explanation": "ok", "functions_needed": []},                            # attempt 1 recovers
    ])
    embed, fetch, generate = _ask_ctx(p, gen)
    with embed, fetch, generate:
        result = await p.ask("hard reasoning question zzqx", "https://cograph.tech/graphs/t1")

    assert "Could not answer" not in result.answer
    assert "widget-omega" in result.answer
    assert result.timing.get("attempts") == 2
    # The MECHANISM: the recovery attempt asked for a budget bigger than the 2048
    # default so the reasoning + answer both fit.
    recover_kwargs = gen.call_args_list[1].kwargs
    assert recover_kwargs.get("max_completion_tokens") == CEREBRAS_LENGTH_RECOVERY_TOKENS
    assert recover_kwargs.get("max_completion_tokens") > 2048
    # Attempt 0 (happy-path shape) carried NO recovery kwargs.
    assert "max_completion_tokens" not in gen.call_args_list[0].kwargs
    assert "prefer_fallback" not in gen.call_args_list[0].kwargs


@pytest.mark.asyncio
async def test_ask_falls_back_to_non_reasoning_provider_after_two_truncations():
    """Two consecutive length-truncations exhaust the budget-bump tier; the third
    attempt (tier 2) falls back to the non-reasoning provider and recovers."""
    neptune = AsyncMock()
    neptune.query.return_value = _rows(["name"], {"name": "row-fallback"})
    p = NLQueryPipeline(neptune, "invented-anthropic-key")
    p._openrouter_key = ""

    gen = AsyncMock(side_effect=[
        EmptyLLMResponse("cerebras", finish_reason="length"),                     # attempt 0
        EmptyLLMResponse("cerebras", finish_reason="length"),                     # attempt 1 (bumped) still truncates
        {"sparql": "SELECT ?name WHERE { ?s <p> ?name }",
         "explanation": "ok", "functions_needed": []},                            # attempt 2 recovers via fallback
    ])
    embed, fetch, generate = _ask_ctx(p, gen)
    with embed, fetch, generate:
        result = await p.ask("very hard reasoning question", "https://cograph.tech/graphs/t1")

    assert "Could not answer" not in result.answer
    assert "row-fallback" in result.answer
    assert result.timing.get("attempts") == 3
    # tier 1 (attempt 1): bumped budget; tier 2 (attempt 2): fallback provider.
    assert gen.call_args_list[1].kwargs.get("max_completion_tokens") == CEREBRAS_LENGTH_RECOVERY_TOKENS
    assert gen.call_args_list[2].kwargs.get("prefer_fallback") is True
    # tier 2 routes OFF the reasoning path — no bumped budget alongside it.
    assert "max_completion_tokens" not in gen.call_args_list[2].kwargs


@pytest.mark.asyncio
async def test_ask_empty_without_length_does_not_bump_budget():
    """Recovery is GATED to length-truncation: an empty response that is NOT a
    length-truncation (finish_reason='stop') escalates the ontology as before but
    must NOT request the reasoning-budget bump or the provider fallback."""
    neptune = AsyncMock()
    neptune.query.return_value = _rows(["name"], {"name": "widget-a"})
    p = NLQueryPipeline(neptune, "invented-anthropic-key")
    p._openrouter_key = ""

    gen = AsyncMock(side_effect=[
        EmptyLLMResponse("cerebras", finish_reason="stop"),                       # empty but NOT length
        {"sparql": "SELECT ?name WHERE { ?s <p> ?name }",
         "explanation": "ok", "functions_needed": []},
    ])
    embed, fetch, generate = _ask_ctx(p, gen)
    with embed, fetch, generate:
        result = await p.ask("q", "https://cograph.tech/graphs/t1")

    assert "Could not answer" not in result.answer
    assert "max_completion_tokens" not in gen.call_args_list[1].kwargs
    assert "prefer_fallback" not in gen.call_args_list[1].kwargs


@pytest.mark.asyncio
async def test_ask_happy_path_passes_no_recovery_kwargs():
    """A clean first attempt returns in one shot with NO recovery kwargs — the
    generation call is byte-identical to before this change."""
    neptune = AsyncMock()
    neptune.query.return_value = _rows(["name"], {"name": "happy-row"})
    p = NLQueryPipeline(neptune, "invented-anthropic-key")
    p._openrouter_key = ""

    gen = AsyncMock(return_value={
        "sparql": "SELECT ?name WHERE { ?s <p> ?name }",
        "explanation": "ok", "functions_needed": [],
    })
    embed, fetch, generate = _ask_ctx(p, gen)
    with embed, fetch, generate:
        result = await p.ask("easy question", "https://cograph.tech/graphs/t1")

    assert "Could not answer" not in result.answer
    assert result.timing.get("attempts") == 1
    kw = gen.call_args_list[0].kwargs
    assert "max_completion_tokens" not in kw
    assert "prefer_fallback" not in kw
