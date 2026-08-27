"""NL->Cypher outage regression: the Cerebras request shape, and provider failover.

Production incident (every ``/ask`` and ``/agent`` NL question returned
``"Could not answer after 3 attempts. Last error: no generator produced Cypher"``).
Two independent faults, one per section below.

**Fault 1 — the Cerebras structured-output schema became un-postable.**
``_generate_cypher_via_cerebras`` sent ``response_format`` as a ``json_schema``
with ``strict: true``, whose ``params`` property was declared
``{"type": "object", "additionalProperties": true}`` — a free-form bag, because
the model chooses the placeholder names (``$type_names``, ``$prop_key``, …).
Cerebras' validator now rejects that outright::

    400 {"message": "Object fields require at least one of: 'properties' or
         'anyOf' with a list of possible properties.",
         "code": "wrong_api_format", "param": "response_format"}

Every escape was probed against the live API: ``properties: {}`` +
``additionalProperties: true`` also 400s (``additionalProperties`` must be
``false``); ``additionalProperties: false`` yields a permanently EMPTY
``params``; and dropping ``params`` lets the root ``additionalProperties: false``
strip it from the response. ``params`` is load-bearing — ``pipeline_ask``,
``pipeline.py`` and ``_execute_confined_cypher`` all read ``gen["params"]``, and
``CYPHER_GENERATION_SYSTEM`` documents it — so the schema went, not the field.
The Cypher path now posts ``{"type": "json_object"}``, which is what
``_generate_cypher_via_openrouter`` already sent: ONE response_format for the
Cypher path, contract stated in the system prompt where both providers see it.

**Fault 2 — one provider's failure took the whole feature down.**
``_try_llm_cypher`` wrapped the ENTIRE provider chain in a single
``try/except Exception: return None``, so the first ``return await …`` that
raised made every later branch unreachable. With Cerebras 400'ing, OpenRouter and
Anthropic were configured and healthy and never got called — on all three retry
attempts. A vendor tightening a validator must degrade to the next provider, not
black out Ask.

Both sections assert MECHANISM with invented tokens — no real model literal,
ontology, or tenant.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from infona_client.nlp.pipeline import EmptyLLMResponse, NLQueryPipeline

# invented tokens — never a real model / tenant / ontology
MODEL = "invented-model-xyz"
TENANT = "invented-tenant"
KG = "invented-kg"
ONTOLOGY = "INVENTED_ONTOLOGY_TOKEN"
QUESTION = "invented question?"

_RealAsyncClient = httpx.AsyncClient

# The verbatim rejection Cerebras returns for the shipped schema.
CEREBRAS_400_BODY = {
    "message": (
        "Object fields require at least one of: 'properties' or 'anyOf' with a "
        "list of possible properties."
    ),
    "type": "invalid_request_error",
    "param": "response_format",
    "code": "wrong_api_format",
}


def _make_pipeline(provider: str = "cerebras", **overrides: Any) -> NLQueryPipeline:
    p = NLQueryPipeline(AsyncMock(), "invented-anthropic-key")
    p._query_provider = provider
    p._cerebras_key = "invented-cerebras-key"
    p._openrouter_key = "invented-openrouter-key"
    p._query_model = MODEL
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


def _gen_payload(**extra: Any) -> dict:
    body = {
        "cypher": "MATCH (e:Entity {tenant_id:$tenant_id, kg:$kg}) RETURN count(*) AS n",
        "params": {"type_names": ["InventedType"], "prop_value": "invented"},
        "template": "invented_template",
        "explanation": "invented",
        "functions_needed": [],
    }
    body.update(extra)
    return body


def _chat_response_raw(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _chat_response(payload: dict) -> dict:
    return _chat_response_raw(json.dumps(payload))


def _route(routes: dict[str, Any], captured: dict | None = None):
    """AsyncClient factory whose MockTransport dispatches on the request host.

    ``routes`` maps a host substring ("cerebras" / "openrouter") to either an
    ``httpx.Response`` factory or a ``(status, json)`` tuple.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        for needle, spec in routes.items():
            if needle not in host:
                continue
            if captured is not None:
                captured.setdefault(needle, []).append(json.loads(request.content))
            status, body = spec
            return httpx.Response(status, json=body)
        raise AssertionError(f"unrouted host {host!r}")

    transport = httpx.MockTransport(handler)

    def factory(*a: Any, **k: Any) -> httpx.AsyncClient:
        return _RealAsyncClient(transport=transport)

    return factory


# --------------------------------------------------------------------------- #
# Fault 1 — the Cerebras request body must be one Cerebras actually accepts     #
# --------------------------------------------------------------------------- #
def _cerebras_structured_output_violations(response_format: Any) -> list[str]:
    """The two rules Cerebras' strict structured-output validator enforces.

    Derived from probing the live API, not from a doc page:

    1. Every ``{"type": "object"}`` node needs a NON-EMPTY ``properties`` (or an
       ``anyOf``). ``additionalProperties: true`` alone is the exact shape that
       400'd with ``wrong_api_format``.
    2. Under ``strict: true`` every object node must pin
       ``additionalProperties: false``; ``true`` is rejected even alongside a
       ``properties`` key.

    Returns a list of human-readable violations — empty means postable.
    """
    if not isinstance(response_format, dict):
        return ["response_format is not an object"]
    if response_format.get("type") != "json_schema":
        return []  # json_object / text impose no schema rules
    wrapper = response_format.get("json_schema") or {}
    strict = bool(wrapper.get("strict"))
    problems: list[str] = []

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            props = node.get("properties")
            if not props and "anyOf" not in node:
                problems.append(
                    f"{path}: object with neither non-empty 'properties' nor "
                    "'anyOf' (Cerebras: wrong_api_format)"
                )
            if strict and node.get("additionalProperties") is not False:
                problems.append(
                    f"{path}: strict object must set additionalProperties=false"
                )
        for key, child in (node.get("properties") or {}).items():
            walk(child, f"{path}.{key}")
        for key in ("items", "additionalProperties"):
            walk(node.get(key), f"{path}.{key}")
        for i, child in enumerate(node.get("anyOf") or []):
            walk(child, f"{path}.anyOf[{i}]")

    walk(wrapper.get("schema") or {}, "schema")
    return problems


def test_validator_catches_the_shape_that_broke_production():
    """Self-test: the guard below is not vacuous.

    Feed it the EXACT ``response_format`` we shipped and it must flag the
    free-form ``params`` bag — otherwise the pin in the next test would pass on a
    body Cerebras rejects.
    """
    shipped = {
        "type": "json_schema",
        "json_schema": {
            "name": "cypher_response",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "cypher": {"type": "string"},
                    "params": {"type": "object", "additionalProperties": True},
                    "explanation": {"type": "string"},
                    "functions_needed": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["cypher", "explanation", "functions_needed"],
                "additionalProperties": False,
            },
        },
    }
    violations = _cerebras_structured_output_violations(shipped)
    assert violations, "guard would not have caught the outage shape"
    assert any("params" in v for v in violations)


@pytest.mark.asyncio
async def test_cerebras_cypher_body_is_postable_and_keeps_params(monkeypatch):
    """Pin the outgoing Cerebras body: ``json_object``, no un-postable schema.

    Asserts three things at once, because the fix is only correct if all three
    hold: the request carries no schema Cerebras would 400 on, it asks for
    ``json_object`` (the same mode the OpenRouter Cypher generator uses), and a
    response carrying ``params`` / ``template`` reaches the caller INTACT — the
    strict schema used to strip ``template`` and could only ever return an empty
    ``params``.
    """
    p = _make_pipeline("cerebras")
    captured: dict = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _route({"cerebras": (200, _chat_response(_gen_payload()))}, captured),
    )

    out = await p._generate_cypher_via_cerebras("invented prompt")

    body = captured["cerebras"][0]
    assert _cerebras_structured_output_violations(body["response_format"]) == []
    assert body["response_format"] == {"type": "json_object"}
    assert "json_schema" not in json.dumps(body)
    # params must survive — pipeline_ask / _execute_confined_cypher read it.
    assert out["params"] == {"type_names": ["InventedType"], "prop_value": "invented"}
    assert out["template"] == "invented_template"
    assert out["cypher"].startswith("MATCH")


@pytest.mark.asyncio
async def test_cerebras_cypher_tolerates_fenced_json(monkeypatch):
    """No constrained decode any more, so the Cerebras path must tolerate the
    fences / think-prose the OpenRouter Cypher path already tolerated."""
    p = _make_pipeline("cerebras")
    fenced = "thinking out loud...\n```json\n" + json.dumps(_gen_payload()) + "\n```"
    monkeypatch.setattr(
        httpx, "AsyncClient", _route({"cerebras": (200, _chat_response_raw(fenced))})
    )
    out = await p._generate_cypher_via_cerebras("invented prompt")
    assert out["params"]["prop_value"] == "invented"


@pytest.mark.asyncio
async def test_openrouter_cypher_body_is_also_postable(monkeypatch):
    """The sibling generator must not carry the same free-form-object shape — a
    provider tightening its validator the way Cerebras did would break it too."""
    p = _make_pipeline("openrouter")
    captured: dict = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _route({"openrouter": (200, _chat_response(_gen_payload()))}, captured),
    )
    out = await p._generate_cypher_via_openrouter("invented prompt")
    body = captured["openrouter"][0]
    assert _cerebras_structured_output_violations(body["response_format"]) == []
    assert body["response_format"] == {"type": "json_object"}
    assert out["params"]["prop_value"] == "invented"


# --------------------------------------------------------------------------- #
# Fault 2 — a failing provider falls THROUGH to the next configured one         #
# --------------------------------------------------------------------------- #
async def _try(p: NLQueryPipeline, **kw: Any) -> dict | None:
    return await p._try_llm_cypher(
        QUESTION, ONTOLOGY, tenant_id=TENANT, kg_name=KG, **kw
    )


@pytest.mark.asyncio
async def test_cerebras_400_falls_over_to_openrouter_end_to_end(monkeypatch):
    """THE OUTAGE, reproduced at the wire: Cerebras returns the real 400 body and
    the answer still comes back — from OpenRouter."""
    p = _make_pipeline("cerebras")
    captured: dict = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _route(
            {
                "cerebras": (400, CEREBRAS_400_BODY),
                "openrouter": (200, _chat_response(_gen_payload(explanation="from-or"))),
            },
            captured,
        ),
    )
    out = await _try(p)
    assert out is not None, "a single provider's 400 must not blank out /ask"
    assert out["explanation"] == "from-or"
    assert captured["cerebras"], "preference order lost: Cerebras was never tried"
    assert captured["openrouter"], "failover lost: OpenRouter was never reached"


@pytest.mark.asyncio
async def test_first_provider_raising_still_reaches_a_later_one(monkeypatch):
    """Provider-level: whatever the first rung raises, the next configured rung
    is still attempted and ITS result is what the caller gets."""
    p = _make_pipeline("cerebras")
    calls: list[str] = []

    async def boom(prompt: str, **kw: Any) -> dict:
        calls.append("cerebras")
        raise httpx.HTTPStatusError(
            "400", request=httpx.Request("POST", "https://invented/"),
            response=httpx.Response(400),
        )

    async def ok(prompt: str, **kw: Any) -> dict:
        calls.append("openrouter")
        return _gen_payload(explanation="from-or")

    monkeypatch.setattr(p, "_generate_cypher_via_cerebras", boom)
    monkeypatch.setattr(p, "_generate_cypher_via_openrouter", ok)

    out = await _try(p)
    assert out is not None and out["explanation"] == "from-or"
    assert calls == ["cerebras", "openrouter"]


@pytest.mark.asyncio
async def test_cerebras_fail_tries_non_reasoning_openrouter_before_gpt_oss(
    monkeypatch,
):
    """After Cerebras JSON/HTTP failure, the next rung is Gemini Flash, not a
    second gpt-oss think-loop on OpenRouter (~76s in production)."""
    p = _make_pipeline("cerebras")
    flags: list[bool] = []

    async def boom(prompt: str, **kw: Any) -> dict:
        raise RuntimeError("cerebras json fail")

    async def openrouter(prompt: str, *, prefer_non_reasoning: bool = False) -> dict:
        flags.append(prefer_non_reasoning)
        if prefer_non_reasoning:
            return _gen_payload(explanation="from-flash")
        raise RuntimeError("should not reach gpt-oss openrouter")

    monkeypatch.setattr(p, "_generate_cypher_via_cerebras", boom)
    monkeypatch.setattr(p, "_generate_cypher_via_openrouter", openrouter)

    out = await _try(p)
    assert out is not None and out["explanation"] == "from-flash"
    assert flags == [True]


@pytest.mark.asyncio
async def test_falls_through_two_providers_to_anthropic(monkeypatch):
    """Failover is a LADDER, not a single retry: both LLM-API providers failing
    still lands on the configured Anthropic client."""
    p = _make_pipeline("cerebras")
    calls: list[str] = []

    def _raiser(name: str):
        async def _f(prompt: str, **kw: Any) -> dict:
            calls.append(name)
            raise RuntimeError(f"{name} down")

        return _f

    async def anthropic_ok(prompt: str, **kw: Any) -> dict:
        calls.append("anthropic")
        return _gen_payload(explanation="from-anthropic")

    monkeypatch.setattr(p, "_generate_cypher_via_cerebras", _raiser("cerebras"))
    monkeypatch.setattr(p, "_generate_cypher_via_openrouter", _raiser("openrouter"))
    monkeypatch.setattr(p, "_generate_cypher_via_anthropic", anthropic_ok)

    out = await _try(p)
    assert out is not None and out["explanation"] == "from-anthropic"
    assert calls[0] == "cerebras"
    assert calls.count("openrouter") >= 1
    assert calls[-1] == "anthropic"


@pytest.mark.asyncio
async def test_returns_none_only_when_every_provider_failed(monkeypatch):
    """``None`` (→ "no generator produced Cypher") stays reachable, but ONLY
    after every configured provider has actually been tried."""
    p = _make_pipeline("cerebras")
    calls: list[str] = []

    def _raiser(name: str):
        async def _f(prompt: str, **kw: Any) -> dict:
            calls.append(name)
            raise RuntimeError(f"{name} down")

        return _f

    for attr, name in (
        ("_generate_cypher_via_cerebras", "cerebras"),
        ("_generate_cypher_via_openrouter", "openrouter"),
        ("_generate_cypher_via_anthropic", "anthropic"),
    ):
        monkeypatch.setattr(p, attr, _raiser(name))

    assert await _try(p) is None
    assert calls[0] == "cerebras"
    assert calls.count("openrouter") >= 1
    assert calls[-1] == "anthropic"


@pytest.mark.asyncio
async def test_empty_llm_response_still_propagates_without_burning_a_provider(
    monkeypatch,
):
    """``EmptyLLMResponse`` is RECOVERABLE by the caller's retry ladder (bigger
    budget, then ``prefer_fallback``), so it must keep short-circuiting out
    instead of being spent on a silent failover here."""
    p = _make_pipeline("cerebras")
    calls: list[str] = []

    async def truncated(prompt: str, **kw: Any) -> dict:
        calls.append("cerebras")
        raise EmptyLLMResponse("cerebras", finish_reason="length")

    async def other(prompt: str, **kw: Any) -> dict:  # pragma: no cover - must not run
        calls.append("openrouter")
        return _gen_payload()

    monkeypatch.setattr(p, "_generate_cypher_via_cerebras", truncated)
    monkeypatch.setattr(p, "_generate_cypher_via_openrouter", other)

    with pytest.raises(EmptyLLMResponse) as ei:
        await _try(p)
    assert ei.value.finish_reason == "length"
    assert calls == ["cerebras"]


@pytest.mark.asyncio
async def test_happy_path_passes_no_max_completion_tokens(monkeypatch):
    """Unchanged pin: with no length recovery in play the Cerebras call is
    byte-identical (no ``max_completion_tokens`` kwarg threaded)."""
    p = _make_pipeline("cerebras")
    seen: list[dict] = []

    async def cerebras(prompt: str, **kw: Any) -> dict:
        seen.append(kw)
        return _gen_payload()

    monkeypatch.setattr(p, "_generate_cypher_via_cerebras", cerebras)
    await _try(p)
    assert seen == [{}]

    seen.clear()
    await _try(p, max_completion_tokens=9999)
    assert seen == [{"max_completion_tokens": 9999}]


@pytest.mark.asyncio
async def test_prefer_fallback_still_leads_with_the_non_reasoning_rung(monkeypatch):
    """``prefer_fallback`` keeps its meaning — the non-reasoning OpenRouter model
    goes FIRST — but is no longer a dead end when that rung itself fails."""
    p = _make_pipeline("cerebras")
    calls: list[tuple[str, bool]] = []

    async def openrouter(prompt: str, *, prefer_non_reasoning: bool = False) -> dict:
        calls.append(("openrouter", prefer_non_reasoning))
        raise RuntimeError("openrouter down")

    async def cerebras(prompt: str, **kw: Any) -> dict:
        calls.append(("cerebras", False))
        return _gen_payload(explanation="from-cerebras")

    monkeypatch.setattr(p, "_generate_cypher_via_openrouter", openrouter)
    monkeypatch.setattr(p, "_generate_cypher_via_cerebras", cerebras)
    monkeypatch.setattr(p, "_generate_cypher_via_anthropic", openrouter)

    out = await _try(p, prefer_fallback=True)
    assert calls[0] == ("openrouter", True)
    assert out is not None and out["explanation"] == "from-cerebras"


@pytest.mark.asyncio
async def test_no_provider_is_attempted_twice(monkeypatch):
    """The configured-provider rung and the generic rung are the SAME provider
    when both point at Cerebras — it gets one attempt, not two."""
    p = _make_pipeline("cerebras", _openrouter_key="")
    calls: list[str] = []

    async def cerebras(prompt: str, **kw: Any) -> dict:
        calls.append("cerebras")
        raise RuntimeError("down")

    async def anthropic(prompt: str, **kw: Any) -> dict:
        calls.append("anthropic")
        raise RuntimeError("down")

    monkeypatch.setattr(p, "_generate_cypher_via_cerebras", cerebras)
    monkeypatch.setattr(p, "_generate_cypher_via_anthropic", anthropic)

    assert await _try(p) is None
    assert calls == ["cerebras", "anthropic"]


@pytest.mark.asyncio
async def test_no_keys_at_all_still_returns_none_without_calling_out():
    """Unchanged: with nothing configured the pipeline degrades silently rather
    than attempting a call."""
    p = NLQueryPipeline(None, "")
    p._query_provider = "cerebras"
    p._cerebras_key = ""
    p._openrouter_key = ""
    p.anthropic = None
    assert await _try(p) is None
