"""Token-usage instrumentation for the NL→SPARQL /ask path (whitepaper v3).

Covers:
  * helpers in ``infona_client.nlp.token_usage`` (parse / attach / ledger)
  * SPARQL generators attaching provider usage without changing the public
    ``sparql`` / ``explanation`` / ``functions_needed`` shape
  * ``ask()`` collecting per-attempt events onto ``NLResult.token_usage``

Mechanism-only: invented model/key/token counts — never a real provider call.

**LOST CAPABILITY (ONTA-527) — the third bullet.** The ledger lives in
``ask()``'s SPARQL branch (``token_ledger = TokenUsageLedger()``, one
``record()`` per generation + rephrase, then ``totals_for_timing()`` onto the
result). ``_ask_cypher`` constructs no ledger and its Cypher generators
(``_generate_cypher_via_openrouter`` / ``_cerebras`` / ``_anthropic``) do not
call ``attach_usage``, so on the shipped ``/ask`` path
``NLResult.token_usage`` is always ``[]`` and ``timing`` carries no
``prompt_tokens`` / ``completion_tokens`` / ``llm_calls``. The whitepaper-v3
tokens-to-complete-task metric is therefore unmeasured in production.

The helper and SPARQL-generator cases are untouched and still green.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from infona_client.nlp import pipeline as pipeline_mod
from infona_client.nlp.pipeline import NLQueryPipeline
from infona_client.nlp.token_usage import (
    STAGE_REPHRASE,
    STAGE_RETRY,
    STAGE_SPARQL_GEN,
    USAGE_DICT_KEY,
    TokenUsageLedger,
    attach_usage,
    estimate_cost_usd,
    events_to_json,
    parse_provider_usage,
    pop_attached_usage,
    stage_for_attempt,
    summarize_events,
)

_NO_TOKEN_LEDGER_ON_CYPHER = (
    "LOST CAPABILITY (ONTA-527): the TokenUsageLedger is built and drained in "
    "nlp/pipeline.py::ask's SPARQL branch, and the Cypher generators never call "
    "nlp/token_usage.attach_usage. /ask takes _ask_cypher, so NLResult."
    "token_usage is always empty and no prompt/completion/llm_calls aggregate "
    "reaches timing. Needs the ledger threaded through _ask_cypher."
)

_RealAsyncClient = httpx.AsyncClient


def _make_pipeline(provider: str = "openrouter") -> NLQueryPipeline:
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
            captured["url"] = str(request.url)
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        return _RealAsyncClient(transport=transport)

    return factory


def _sparql_json() -> str:
    return json.dumps(
        {
            "sparql": "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1",
            "explanation": "invented",
            "functions_needed": [],
        }
    )


def _resp(content, *, usage=None, model=None) -> dict:
    body: dict = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    if model is not None:
        body["model"] = model
    return body


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def test_parse_provider_usage_openai_shape():
    out = parse_provider_usage(
        {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
    )
    assert out == {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
    }


def test_parse_provider_usage_anthropic_shape():
    out = parse_provider_usage({"input_tokens": 11, "output_tokens": 3})
    assert out["prompt_tokens"] == 11
    assert out["completion_tokens"] == 3
    assert out["total_tokens"] == 14


def test_parse_provider_usage_missing_stays_empty():
    assert parse_provider_usage(None) == {}
    assert parse_provider_usage({}) == {}


def test_attach_and_pop_usage():
    result = {"sparql": "SELECT 1", "explanation": "e", "functions_needed": []}
    attach_usage(
        result,
        usage={"prompt_tokens": 5, "completion_tokens": 2},
        model="m",
        provider="openrouter",
    )
    assert USAGE_DICT_KEY in result
    blob = pop_attached_usage(result)
    assert blob["prompt_tokens"] == 5
    assert blob["completion_tokens"] == 2
    assert blob["model"] == "m"
    assert blob["provider"] == "openrouter"
    assert USAGE_DICT_KEY not in result
    # public shape untouched
    assert set(result) == {"sparql", "explanation", "functions_needed"}


def test_stage_for_attempt():
    assert stage_for_attempt(0) == STAGE_SPARQL_GEN
    assert stage_for_attempt(1) == STAGE_RETRY
    assert stage_for_attempt(2) == STAGE_RETRY


def test_ledger_sums_partial_and_totals_for_timing():
    ledger = TokenUsageLedger()
    ledger.record(
        stage=STAGE_SPARQL_GEN,
        attempt=0,
        model="m",
        prompt_tokens=100,
        completion_tokens=20,
        provider="openrouter",
    )
    ledger.record(
        stage=STAGE_REPHRASE,
        attempt=0,
        model="m2",
        # unknown counts must not zero out the known sparql_gen totals
        prompt_tokens=None,
        completion_tokens=None,
        provider="openrouter",
    )
    assert ledger.prompt_tokens == 100
    assert ledger.completion_tokens == 20
    assert ledger.total_tokens == 120
    timing = ledger.totals_for_timing()
    assert timing["prompt_tokens"] == 100.0
    assert timing["completion_tokens"] == 20.0
    assert timing["llm_calls"] == 2.0


def test_ledger_empty_totals_are_empty_dict():
    assert TokenUsageLedger().totals_for_timing() == {}


def test_estimate_cost_usd():
    cost = estimate_cost_usd(1_000_000, 1_000_000, input_per_1m=0.5, output_per_1m=3.0)
    assert cost == pytest.approx(3.5)
    assert estimate_cost_usd(None, 10, input_per_1m=1.0, output_per_1m=1.0) is None


def test_summarize_events_and_events_to_json():
    ledger = TokenUsageLedger()
    ledger.record(
        stage=STAGE_SPARQL_GEN,
        attempt=0,
        model="m",
        prompt_tokens=1,
        completion_tokens=2,
    )
    as_list = events_to_json(ledger.events)
    assert as_list[0]["stage"] == STAGE_SPARQL_GEN
    summary = summarize_events(as_list)
    assert summary["prompt_tokens"] == 1
    assert summary["completion_tokens"] == 2
    assert summary["llm_calls"] == 1


# --------------------------------------------------------------------------- #
# Generators attach usage                                                      #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_openrouter_generation_attaches_usage(monkeypatch):
    p = _make_pipeline("openrouter")
    factory = _post_factory(
        _resp(
            _sparql_json(),
            usage={"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168},
            model="served/model-id",
        )
    )
    monkeypatch.setattr(pipeline_mod.httpx, "AsyncClient", factory)
    result = await p._generate_via_openrouter("give me sparql")
    assert result["sparql"].startswith("SELECT")
    assert USAGE_DICT_KEY in result
    assert result[USAGE_DICT_KEY]["prompt_tokens"] == 123
    assert result[USAGE_DICT_KEY]["completion_tokens"] == 45
    assert result[USAGE_DICT_KEY]["provider"] == "openrouter"
    assert result[USAGE_DICT_KEY]["model"] == "served/model-id"


@pytest.mark.asyncio
async def test_cerebras_generation_attaches_usage(monkeypatch):
    p = _make_pipeline("cerebras")
    factory = _post_factory(
        _resp(
            _sparql_json(),
            usage={"prompt_tokens": 9, "completion_tokens": 8},
        )
    )
    monkeypatch.setattr(pipeline_mod.httpx, "AsyncClient", factory)
    result = await p._generate_via_cerebras("give me sparql")
    assert result[USAGE_DICT_KEY]["prompt_tokens"] == 9
    assert result[USAGE_DICT_KEY]["provider"] == "cerebras"
    # model falls back to pipeline's configured model when response omits it
    assert result[USAGE_DICT_KEY]["model"] == "invented-model-xyz"


@pytest.mark.asyncio
async def test_generation_without_usage_still_records_model(monkeypatch):
    """Providers that omit usage still get a stamped event (counts None)."""
    p = _make_pipeline("openrouter")
    factory = _post_factory(_resp(_sparql_json()))  # no usage key
    monkeypatch.setattr(pipeline_mod.httpx, "AsyncClient", factory)
    result = await p._generate_via_openrouter("give me sparql")
    blob = result[USAGE_DICT_KEY]
    assert blob["prompt_tokens"] is None
    assert blob["completion_tokens"] is None
    assert blob["model"] == "invented-model-xyz"
    assert blob["provider"] == "openrouter"


# --------------------------------------------------------------------------- #
# ask() collects events onto NLResult                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ask_collects_sparql_gen_token_usage(monkeypatch):
    """End-to-end: mocked Cypher generator usage lands on NLResult.token_usage."""
    from infona_client.graph.memory_store import MemoryGraphStore
    from unittest.mock import MagicMock

    p = _make_pipeline("openrouter")
    p._graph_store = MemoryGraphStore()

    async def fake_fetch_ontology(*_a, **_k):
        return "Type InventedThing"

    async def fake_cypher(*_a, **_k):
        return attach_usage(
            {
                "cypher": (
                    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                    "RETURN count(*) AS n"
                ),
                "params": {},
                "explanation": "e",
                "functions_needed": [],
            },
            usage={"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
            model="invented-model-xyz",
            provider="openrouter",
        )

    async def fake_exec(session, gen, cypher, forced_params):
        rec = MagicMock()
        rec.keys.return_value = ["n"]
        rec.get.side_effect = lambda k, d=None: 1 if k == "n" else d
        return [rec], "execute_read"

    async def fake_rephrase(*_a, **_k):
        p._last_rephrase_usage = {
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
            "model": "meta-llama/llama-3.1-8b-instruct",
            "provider": "openrouter",
        }
        return "one row"

    monkeypatch.setattr(p, "_fetch_ontology", fake_fetch_ontology)
    monkeypatch.setattr(p, "_try_llm_cypher", fake_cypher)
    monkeypatch.setattr(
        pipeline_mod, "try_deterministic_cypher", lambda *a, **k: None
    )
    monkeypatch.setattr(p, "_execute_confined_cypher", fake_exec)
    monkeypatch.setattr(p, "_rephrase_via_openrouter", fake_rephrase)
    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
    p._spatial_routing_enabled = False

    result = await p.ask(
        "how many invented things?",
        "https://graph.infona.ai/graphs/demo-tenant",
        "https://graph.infona.ai/graphs/demo-tenant/kg/invented",
    )

    assert result.token_usage, "expected at least sparql_gen event"
    stages = [e["stage"] for e in result.token_usage]
    assert STAGE_SPARQL_GEN in stages
    assert STAGE_REPHRASE in stages
    sparql_ev = next(e for e in result.token_usage if e["stage"] == STAGE_SPARQL_GEN)
    assert sparql_ev["prompt_tokens"] == 50
    assert sparql_ev["completion_tokens"] == 10
    assert sparql_ev["attempt"] == 0
    assert sparql_ev["model"] == "invented-model-xyz"
    assert result.timing.get("prompt_tokens") == 57.0  # 50 + 7
    assert result.timing.get("completion_tokens") == 13.0  # 10 + 3
    assert result.timing.get("llm_calls") == 2.0


@pytest.mark.asyncio
async def test_ask_retry_stage_on_second_attempt(monkeypatch):
    from infona_client.graph.memory_store import MemoryGraphStore
    from unittest.mock import MagicMock

    p = _make_pipeline("openrouter")
    p._graph_store = MemoryGraphStore()
    calls = {"n": 0}

    async def fake_fetch_ontology(*_a, **_k):
        return "Type InventedThing"

    async def fake_cypher(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return attach_usage(
                {
                    "cypher": "",  # empty → retry
                    "params": {},
                    "explanation": "e",
                    "functions_needed": [],
                },
                usage={"prompt_tokens": 1, "completion_tokens": 1},
                model="m",
                provider="openrouter",
            )
        return attach_usage(
            {
                "cypher": (
                    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
                    "RETURN count(*) AS n"
                ),
                "params": {},
                "explanation": "e",
                "functions_needed": [],
            },
            usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            model="m",
            provider="openrouter",
        )

    async def fake_exec(session, gen, cypher, forced_params):
        rec = MagicMock()
        rec.keys.return_value = ["n"]
        rec.get.side_effect = lambda k, d=None: 1 if k == "n" else d
        return [rec], "execute_read"

    monkeypatch.setattr(p, "_fetch_ontology", fake_fetch_ontology)
    monkeypatch.setattr(p, "_try_llm_cypher", fake_cypher)
    monkeypatch.setattr(
        pipeline_mod, "try_deterministic_cypher", lambda *a, **k: None
    )
    monkeypatch.setattr(p, "_execute_confined_cypher", fake_exec)
    monkeypatch.setattr(p, "_rephrase_via_openrouter", AsyncMock(return_value=""))
    monkeypatch.setattr(pipeline_mod, "get_embedding_service", lambda: None)
    p._spatial_routing_enabled = False

    result = await p.ask(
        "how many invented things?",
        "https://graph.infona.ai/graphs/demo-tenant",
        "https://graph.infona.ai/graphs/demo-tenant/kg/invented",
    )

    stages = [e["stage"] for e in result.token_usage]
    assert STAGE_SPARQL_GEN in stages
    assert STAGE_RETRY in stages
    assert calls["n"] >= 2
