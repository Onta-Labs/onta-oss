"""Condition 9 RAG picker. Mocked embeddings only; no network."""

from __future__ import annotations

import json
from urllib.request import Request

import pytest

from ontology_skills.backends import LiveBackend
from ontology_skills.compiler import compile_routed
from ontology_skills.conditions import condition_by_id
from ontology_skills.dataset import load_fixture_bundle, load_tasks
from ontology_skills.embedder import (
    DEFAULT_EMBED_MODEL,
    MOCK_EMBEDDER_ID,
    MockEmbedder,
    OpenAICompatEmbedder,
)
from ontology_skills.executor import execute_task, run_dry
from ontology_skills.harness import DecodingSpec, compile_for_condition
from ontology_skills.prompts import SCHEMA_HINT, build_prompt
from ontology_skills.rag import retrieve_skills


def test_condition_9_is_runnable() -> None:
    cond = condition_by_id("4b_rag_skills")
    assert cond.index == 9
    assert cond.runnable is True
    assert cond.skill_mode == "rag"
    assert cond.fine_tuned is False


def test_mocked_rag_is_deterministic_and_matches_routed_k() -> None:
    bundle = load_fixture_bundle()
    task = next(t for t in bundle.tasks if t.task_id == "et-001")
    embedder = MockEmbedder()
    first = retrieve_skills(bundle.ontology, task, embedder)
    second = retrieve_skills(bundle.ontology, task, embedder)
    routed = compile_routed(bundle.ontology, task.neighborhood)
    assert first.compiled.skill_ids == second.compiled.skill_ids
    assert first.hits == second.hits
    assert first.k == len(routed.skills)
    assert len(first.compiled.skills) == first.k
    assert first.embedder_id == MOCK_EMBEDDER_ID
    assert first.compiled.mode == "rag"
    assert first.compiled.type_lineage == ()
    assert first.compiled.relation_ids == ()
    for skill in first.compiled.skills:
        assert skill.provenance != "rag"


def test_rag_and_routed_can_disagree_on_skill_ids() -> None:
    bundle = load_fixture_bundle()
    embedder = MockEmbedder()
    disagreed = False
    for task in bundle.tasks:
        routed = compile_routed(bundle.ontology, task.neighborhood)
        rag = retrieve_skills(bundle.ontology, task, embedder)
        assert len(rag.compiled.skills) == len(routed.skills), task.task_id
        if set(rag.compiled.skill_ids) != set(routed.skill_ids):
            disagreed = True
            break
    assert disagreed, "RAG vs routed should differ on at least one task"


def test_canned_rag_records_retrieval_and_still_scores_canned_gold() -> None:
    rows = run_dry(condition_id="4b_rag_skills", task_id="et-001")
    row = rows[0]
    assert row["prompt"]["skill_injection"] == "rag"
    assert row["compiler"]["mode"] == "rag"
    assert row["retrieval"]["embedder_id"] == MOCK_EMBEDDER_ID
    assert row["retrieval"]["k"] == len(row["compiler"]["skill_ids"])
    assert row["retrieval"]["hits"]
    assert row["metrics"]["success"] is True
    assert "canned-fixture" in row["notes"]


def test_rag_prompt_uses_v3_hint_without_supplier_in_schema() -> None:
    bundle = load_fixture_bundle()
    task = next(t for t in bundle.tasks if t.task_id == "et-001")
    cond = condition_by_id("4b_rag_skills")
    compiled = compile_for_condition(
        bundle.ontology,
        task.neighborhood,
        cond,
        task=task,
        embedder=MockEmbedder(),
    )
    prompt = build_prompt(task, bundle.ontology, compiled, cond)
    assert prompt.template_id == "ontology_skills.prompt.v3"
    assert SCHEMA_HINT in prompt.text
    assert "Supplier" not in SCHEMA_HINT
    assert "skills (rag):" in prompt.text


def test_live_rag_without_key_does_not_hash_or_post(
    clear_live_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del clear_live_env
    calls: list[Request] = []

    def boom(request: Request, timeout: float | None = None):
        del timeout
        calls.append(request)
        raise AssertionError("must not POST")

    monkeypatch.setattr("ontology_skills.backends.urlopen", boom)
    live = LiveBackend(
        base_url="https://openrouter.ai/api/v1",
        model_name="qwen/qwen3-8b",
        api_key="",
    )
    task = next(t for t in load_tasks() if t.task_id == "et-001")
    with pytest.raises(RuntimeError, match="published RAG baseline"):
        execute_task(
            task,
            condition_id="4b_rag_skills",
            backend=live,
            decoding=DecodingSpec(),
        )
    assert calls == []
    assert OpenAICompatEmbedder.from_env() is None
    assert OpenAICompatEmbedder.from_chat_credentials(
        base_url="https://openrouter.ai/api/v1", api_key=""
    ) is None
    assert DEFAULT_EMBED_MODEL == "openai/text-embedding-3-small"


def test_live_rag_posts_embeddings_using_chat_credentials(
    clear_live_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del clear_live_env
    bundle = load_fixture_bundle()
    n_texts = 1 + sum(1 for s in bundle.ontology.skills if s.enabled)
    captured: list[Request] = []

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def fake_urlopen(request: Request, timeout: float | None = None) -> FakeResponse:
        del timeout
        captured.append(request)
        url = request.get_full_url()
        if url.endswith("/embeddings"):
            return FakeResponse(
                {
                    "data": [
                        {"index": i, "embedding": [float(i + 1), 0.0]}
                        for i in range(n_texts)
                    ]
                }
            )
        return FakeResponse({"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr("ontology_skills.backends.urlopen", fake_urlopen)
    live = LiveBackend(
        base_url="https://openrouter.ai/api/v1",
        model_name="qwen/qwen3-8b",
        api_key="sk-chat",
    )
    task = next(t for t in bundle.tasks if t.task_id == "et-001")
    result = execute_task(
        task,
        condition_id="4b_rag_skills",
        backend=live,
        decoding=DecodingSpec(),
    )
    urls = [req.get_full_url() for req in captured]
    assert urls[0].endswith("/embeddings")
    assert urls[1].endswith("/chat/completions")
    embed_headers = {k.lower(): v for k, v in captured[0].header_items()}
    assert embed_headers["authorization"] == "Bearer sk-chat"
    assert result.retrieval is not None
    assert result.retrieval["embedder_id"] == DEFAULT_EMBED_MODEL
    assert result.retrieval["k"] == len(result.compiled.skill_ids)
    assert result.compiled.mode == "rag"


def test_openai_compat_embedder_parses_mocked_200(
    clear_live_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del clear_live_env
    monkeypatch.setenv("INFONA_BENCH_API_KEY", "sk-test")
    captured: list[Request] = []

    class FakeResponse:
        def __init__(self) -> None:
            payload = {
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            }
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def fake_urlopen(request: Request, timeout: float | None = None) -> FakeResponse:
        del timeout
        captured.append(request)
        return FakeResponse()

    monkeypatch.setattr("ontology_skills.backends.urlopen", fake_urlopen)
    embedder = OpenAICompatEmbedder.from_env()
    assert embedder is not None
    vectors = embedder.embed(["query", "doc"])
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert len(captured) == 1
    body = json.loads(captured[0].data.decode("utf-8"))
    assert body["model"] == DEFAULT_EMBED_MODEL
    assert captured[0].get_full_url().endswith("/embeddings")
