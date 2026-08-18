"""ONTA-544: zero-key ask of the prebuilt trials graph (cached-plan → FLAURA2).

No OpenRouter key. No LLM mock that needs a key. Production /ask stays
always-LLM when a model is configured.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.trials_snapshot import (
    DEFAULT_KG,
    DEFAULT_TENANT,
    load_trials_snapshot,
    triples_from_csv,
    write_snapshot_json,
)
from infona_client.nlp.ask_cached_plan import (
    REPLAY_LABEL,
    cached_plan_enabled,
    llm_configured,
    match_cached_plan,
    normalize_question,
)
from infona_client.nlp.pipeline import NLQueryPipeline

REPO = Path(__file__).resolve().parents[1]
PREBUILT = REPO / "examples" / "prebuilt"
HERO_QUESTION = "Which Phase 3 NSCLC trials is AstraZeneca running?"
HERO_TRIAL = "FLAURA2"
GRAPH = f"{IRI_BASE}/graphs/{DEFAULT_TENANT}/kg/{DEFAULT_KG}"


@pytest.fixture
def no_llm_keys(monkeypatch):
    for name in (
        "OPENROUTER_API_KEY",
        "INFONA_OPENROUTER_API_KEY",
        "CEREBRAS_API_KEY",
        "INFONA_CEREBRAS_API_KEY",
        "INFONA_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
        "INFONA_LLM_BASE_URL",
        "INFONA_QUERY_BASE_URL",
        "INFONA_ASK_CACHED_PLAN",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _pipe(store: MemoryGraphStore) -> NLQueryPipeline:
    neptune = MagicMock()
    neptune.query = AsyncMock(
        return_value={"head": {"vars": []}, "results": {"bindings": []}}
    )
    pipe = NLQueryPipeline(neptune, anthropic_key="", graph_store=store)
    pipe._openrouter_key = ""
    pipe._cerebras_key = ""
    return pipe


@pytest.mark.asyncio
async def test_zero_key_hero_ask_returns_flaura2(no_llm_keys):
    store = MemoryGraphStore()
    n = await load_trials_snapshot(store, tenant_id=DEFAULT_TENANT, kg=DEFAULT_KG)
    assert n >= 16
    assert store.entity_count(tenant_id=DEFAULT_TENANT, kg=DEFAULT_KG) >= 3

    pipe = _pipe(store)
    llm_calls: list[str] = []

    async def _forbid(*_a, **_k):
        llm_calls.append("called")
        raise AssertionError("zero-key ask must not call the LLM")

    pipe._try_llm_cypher = _forbid  # type: ignore[method-assign]

    result = await pipe.ask(
        HERO_QUESTION,
        graph_uri=f"{IRI_BASE}/graphs/{DEFAULT_TENANT}",
        instance_graph=GRAPH,
        use_cypher=True,
    )

    assert not llm_calls
    assert HERO_TRIAL in (result.answer or "")
    assert REPLAY_LABEL in (result.answer or "")
    assert REPLAY_LABEL.split("—")[0].strip() in (result.explanation or "").lower() or (
        "not live" in (result.explanation or "").lower()
    )
    assert result.timing.get("cached_plan_replay") == 1.0
    assert result.timing.get("query_language") == "cypher"
    assert str(result.timing.get("cypher_exec_path") or "").startswith("cached_plan")
    assert "CASPIAN" not in (result.answer or "")
    assert "DESTINY-Lung01" not in (result.answer or "")


@pytest.mark.asyncio
async def test_placeholder_key_still_replays(no_llm_keys):
    no_llm_keys.setenv("OPENROUTER_API_KEY", "sk-or-...")
    store = MemoryGraphStore()
    await load_trials_snapshot(store)
    pipe = _pipe(store)
    result = await pipe.ask(
        HERO_QUESTION,
        graph_uri=f"{IRI_BASE}/graphs/{DEFAULT_TENANT}",
        instance_graph=GRAPH,
        use_cypher=True,
    )
    assert HERO_TRIAL in (result.answer or "")
    assert result.timing.get("cached_plan_replay") == 1.0


@pytest.mark.asyncio
async def test_real_key_does_not_replay(no_llm_keys):
    no_llm_keys.setenv("OPENROUTER_API_KEY", "sk-or-live-test-key")
    store = MemoryGraphStore()
    await load_trials_snapshot(store)
    pipe = _pipe(store)
    pipe._openrouter_key = "sk-or-live-test-key"

    async def fake(question: str, ontology: str, **kw):
        return None

    pipe._try_llm_cypher = fake  # type: ignore[method-assign]
    pipe._fetch_ontology = AsyncMock(return_value="")  # type: ignore[method-assign]
    result = await pipe.ask(
        HERO_QUESTION,
        graph_uri=f"{IRI_BASE}/graphs/{DEFAULT_TENANT}",
        instance_graph=GRAPH,
        use_cypher=True,
    )
    assert result.timing.get("cached_plan_replay") in (None, 0, 0.0)
    assert HERO_TRIAL not in (result.answer or "") or "Could not answer" in (
        result.answer or ""
    )


@pytest.mark.asyncio
async def test_other_question_is_not_hardcoded(no_llm_keys):
    store = MemoryGraphStore()
    await load_trials_snapshot(store)
    pipe = _pipe(store)
    pipe._try_llm_cypher = AsyncMock(return_value=None)  # type: ignore[method-assign]
    pipe._fetch_ontology = AsyncMock(return_value="")  # type: ignore[method-assign]
    result = await pipe.ask(
        "How many books are in the warehouse?",
        graph_uri=f"{IRI_BASE}/graphs/{DEFAULT_TENANT}",
        instance_graph=GRAPH,
        use_cypher=True,
    )
    assert HERO_TRIAL not in (result.answer or "")
    assert result.timing.get("cached_plan_replay") in (None, 0, 0.0)


def test_match_is_normalized_not_fuzzy():
    assert match_cached_plan(HERO_QUESTION, kg_name="trials") is not None
    assert match_cached_plan(HERO_QUESTION.lower() + "?", kg_name="trials") is not None
    assert match_cached_plan(HERO_QUESTION, kg_name="other") is None
    assert match_cached_plan("list every trial", kg_name="trials") is None
    assert normalize_question(HERO_QUESTION) == (
        "which phase 3 nsclc trials is astrazeneca running"
    )


def test_llm_configured_ignores_placeholders(no_llm_keys):
    assert llm_configured() is False
    assert cached_plan_enabled() is True
    no_llm_keys.setenv("OPENROUTER_API_KEY", "sk-or-...")
    assert llm_configured() is False
    no_llm_keys.setenv("OPENROUTER_API_KEY", "sk-or-real")
    assert llm_configured() is True
    assert cached_plan_enabled() is False


def test_prebuilt_fixtures_exist_and_snapshot_roundtrip(tmp_path):
    plan = PREBUILT / "ask_plan_flaura2.json"
    snap = PREBUILT / "trials_snapshot.json"
    assert plan.is_file(), f"missing {plan}"
    assert snap.is_file(), f"missing {snap}"
    triples = triples_from_csv()
    assert any("FLAURA2" in t[2] or "FLAURA2" in t[0] for t in triples)
    dest = write_snapshot_json(tmp_path / "trials_snapshot.json", triples=triples)
    assert dest.is_file()
    assert dest.stat().st_size > 100


def test_replay_label_is_explicit():
    assert "cached-plan replay" in REPLAY_LABEL
    assert "not live inference" in REPLAY_LABEL
