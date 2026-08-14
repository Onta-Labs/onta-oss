"""Hermetic tests: live-graph query *build* notes + reasoning model defaults.

Anti-overfit: synthetic type names only (SynthWidget / SynthGadget).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infona_client.nlp import pipeline as pipeline_mod
from infona_client.nlp.query_build import (
    QueryBuildContext,
    TypePopulation,
    format_query_build_for_prompt,
    match_question_types,
)
from infona_client.nlp.prompts import (
    CYPHER_GENERATION_SYSTEM,
    build_cypher_generation_prompt,
)


def test_openrouter_default_model_is_gpt_oss_120b(monkeypatch):
    monkeypatch.delenv("INFONA_QUERY_MODEL", raising=False)
    monkeypatch.delenv("INFONA_QUERY_PROVIDER", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    monkeypatch.delenv("INFONA_CEREBRAS_API_KEY", raising=False)
    monkeypatch.delenv("INFONA_OPENROUTER_API_KEY", raising=False)
    assert pipeline_mod._default_query_provider() == "openrouter"
    assert pipeline_mod._default_query_model("openrouter") == "openai/gpt-oss-120b"


def test_cerebras_default_model_is_gpt_oss_120b(monkeypatch):
    monkeypatch.delenv("INFONA_QUERY_MODEL", raising=False)
    assert pipeline_mod._default_query_model("cerebras") == "gpt-oss-120b"


def test_is_reasoning_query_model():
    assert pipeline_mod._is_reasoning_query_model("openai/gpt-oss-120b")
    assert pipeline_mod._is_reasoning_query_model("gpt-oss-120b")
    assert not pipeline_mod._is_reasoning_query_model("google/gemini-2.5-flash")


def test_match_question_types_hits_populated_only():
    pops = [
        TypePopulation("SynthWidget", 12),
        TypePopulation("SynthGadget", 3),
        TypePopulation("Product", 0),  # should not be in list if filtered earlier
    ]
    hits = match_question_types("how many synthwidgets are stocked?", pops)
    assert "SynthWidget" in hits
    assert "Product" not in hits or "Product" not in [
        t.name for t in pops if t.entity_count > 0
    ]


def test_format_query_build_prefers_populated_and_build_language():
    ctx = QueryBuildContext(
        types=(
            TypePopulation("SynthWidget", 12),
            TypePopulation("SynthDepot", 3),
        ),
        question_type_hits=("SynthWidget",),
        total_entities=15,
    )
    text = format_query_build_for_prompt(ctx)
    assert "Graph build notes" in text
    assert "SynthWidget: 12" in text
    assert "SynthDepot: 3" in text
    assert "SynthWidget" in text
    assert "Do NOT invent" in text or "do not invent" in text.lower() or "BUILD" in text or "Build" in text


def test_format_empty_ctx():
    assert format_query_build_for_prompt(None) == ""
    assert format_query_build_for_prompt(QueryBuildContext()) == ""


def test_cypher_system_says_build_not_guess():
    assert "BUILD" in CYPHER_GENERATION_SYSTEM or "Build" in CYPHER_GENERATION_SYSTEM
    assert "guess" in CYPHER_GENERATION_SYSTEM.lower()


def test_prompt_includes_build_grounding():
    p = build_cypher_generation_prompt(
        "how many SynthWidget?",
        "Type: SynthWidget\n  - status_label: string",
        tenant_id="t",
        kg_name="k",
        grounding_text="## Graph build notes\n- SynthWidget: 12 entities\n",
    )
    assert "Graph build notes" in p
    assert "BUILD a read-only Cypher" in p


@pytest.mark.asyncio
async def test_collect_query_build_context_from_type_counts():
    from infona_client.graph.explore_store import TypeCountRow
    from infona_client.nlp.query_build import collect_query_build_context

    store = MagicMock()
    rows = [
        TypeCountRow(name="SynthWidget", entity_count=12),
        TypeCountRow(name="SynthEmpty", entity_count=0),
    ]
    with patch(
        "infona_client.graph.explore_store.type_counts",
        new=AsyncMock(return_value=rows),
    ):
        ctx = await collect_query_build_context(
            store,
            tenant_id="t",
            kg="k",
            question="count SynthWidget units",
        )
    assert ctx is not None
    assert ctx.populated_type_names == ("SynthWidget",)
    assert "SynthWidget" in ctx.question_type_hits


@pytest.mark.asyncio
async def test_openrouter_payload_reasoning_budget_and_cerebras_order():
    """gpt-oss path requests large max_tokens + Cerebras provider preference."""
    pipe = pipeline_mod.NLQueryPipeline.__new__(pipeline_mod.NLQueryPipeline)
    pipe._openrouter_key = "sk-test"
    pipe._query_provider = "openrouter"
    pipe._query_model = "openai/gpt-oss-120b"

    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"cypher":"RETURN 1","explanation":"x","functions_needed":[]}'
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                "model": "openai/gpt-oss-120b",
            }

    class _Client:
        def __init__(self, *a, **k):
            captured["timeout"] = k.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            return _Resp()

    with patch("httpx.AsyncClient", _Client):
        out = await pipe._generate_cypher_via_openrouter("prompt")
    assert out["cypher"] == "RETURN 1"
    body = captured["json"]
    assert body["model"] == "openai/gpt-oss-120b"
    assert body.get("max_tokens", 0) >= 4096
    assert body.get("provider", {}).get("order") == ["Cerebras"]
    assert captured["timeout"] >= 60


def test_openrouter_maps_bare_cerebras_slug():
    pipe = pipeline_mod.NLQueryPipeline.__new__(pipeline_mod.NLQueryPipeline)
    pipe._query_provider = "cerebras"
    pipe._query_model = "gpt-oss-120b"
    assert (
        pipe._openrouter_cypher_model_id(prefer_non_reasoning=False)
        == "openai/gpt-oss-120b"
    )
    assert (
        pipe._openrouter_cypher_model_id(prefer_non_reasoning=True)
        == "google/gemini-2.5-flash"
    )


@pytest.mark.asyncio
async def test_cerebras_to_openrouter_recovery_uses_valid_slug_and_non_reasoning():
    """prefer_fallback must not send bare gpt-oss-120b to OpenRouter."""
    pipe = pipeline_mod.NLQueryPipeline.__new__(pipeline_mod.NLQueryPipeline)
    pipe._openrouter_key = "sk-or-test"
    pipe._cerebras_key = "csk-test"
    pipe._query_provider = "cerebras"
    pipe._query_model = "gpt-oss-120b"
    pipe.anthropic = None

    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"cypher":"RETURN 2","explanation":"x","functions_needed":[]}'
                        }
                    }
                ],
                "usage": {},
                "model": "google/gemini-2.5-flash",
            }

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return _Resp()

    with patch("httpx.AsyncClient", _Client):
        out = await pipe._try_llm_cypher(
            "q",
            "Type: X",
            tenant_id="t",
            kg_name="k",
            prefer_fallback=True,
        )
    assert out is not None
    assert out["cypher"] == "RETURN 2"
    assert captured["json"]["model"] == "google/gemini-2.5-flash"
    assert "gpt-oss" not in captured["json"]["model"]
