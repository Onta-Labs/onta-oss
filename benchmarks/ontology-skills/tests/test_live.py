"""Live OpenRouter client: mock HTTP only. Never hit the network in CI."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from ontology_skills.backends import (
    DEFAULT_BASE_URL,
    LiveBackend,
    default_model_for_condition,
    load_canned,
)
from ontology_skills.conditions import condition_by_id
from ontology_skills.executor import execute_main
from ontology_skills.harness import LIVE_MAX_NEW_TOKENS, DecodingSpec

FOUR_B_IDS = (
    "4b_vanilla",
    "4b_ontology_context",
    "4b_flat_skills",
    "4b_ontology_routed",
    "teacher_skills_4b",
    "4b_rag_skills",
)


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class CapturingUrlopen:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[Request] = []

    def __call__(self, request: Request, timeout: float | None = None) -> FakeResponse:
        del timeout
        self.calls.append(request)
        return FakeResponse(self.payload)


def _headers(req: Request) -> dict[str, str]:
    return {key.lower(): value for key, value in req.header_items()}


def test_default_models_by_condition() -> None:
    for cid in FOUR_B_IDS:
        assert default_model_for_condition(condition_by_id(cid)) == "qwen/qwen3-8b"
    assert (
        default_model_for_condition(condition_by_id("9b_vanilla")) == "qwen/qwen3.5-9b"
    )
    assert (
        default_model_for_condition(condition_by_id("27b_or_frontier_vanilla"))
        == "qwen/qwen3.5-27b"
    )


def test_from_env_maps_bucket_and_openrouter_defaults(
    clear_live_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del clear_live_env
    monkeypatch.setenv("INFONA_BENCH_API_KEY", "sk-test")
    four = LiveBackend.from_env(condition=condition_by_id("4b_ontology_routed"))
    nine = LiveBackend.from_env(condition=condition_by_id("9b_vanilla"))
    frontier = LiveBackend.from_env(
        condition=condition_by_id("27b_or_frontier_vanilla")
    )
    assert four is not None and nine is not None and frontier is not None
    assert four.base_url == DEFAULT_BASE_URL
    assert four.model_name == "qwen/qwen3-8b"
    assert four.param_count == "8B"
    assert nine.model_name == "qwen/qwen3.5-9b"
    assert nine.param_count == "9B"
    assert frontier.model_name == "qwen/qwen3.5-27b"
    assert frontier.param_count == "27B"


def test_from_env_model_override_keeps_bucket_param_count(
    clear_live_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del clear_live_env
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setenv("INFONA_BENCH_MODEL", "qwen/custom-override")
    live = LiveBackend.from_env(condition=condition_by_id("9b_vanilla"))
    assert live is not None
    assert live.model_name == "qwen/custom-override"
    assert live.param_count == "9B"
    assert live.api_key == "sk-or"


def test_live_cli_missing_key_does_not_post(
    clear_live_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del clear_live_env
    capturing = CapturingUrlopen({"choices": [{"message": {"content": "{}"}}]})
    monkeypatch.setattr("ontology_skills.backends.urlopen", capturing)
    assert execute_main(["--backend", "live", "--task-id", "et-001"]) == 2
    assert capturing.calls == []


def test_live_cli_without_task_id_does_not_post_even_with_key(
    clear_live_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del clear_live_env
    monkeypatch.setenv("INFONA_BENCH_API_KEY", "sk-test")
    capturing = CapturingUrlopen({"choices": [{"message": {"content": "{}"}}]})
    monkeypatch.setattr("ontology_skills.backends.urlopen", capturing)
    assert execute_main(["--backend", "live"]) == 2
    assert capturing.calls == []


def test_live_cli_condition_5_blocked_does_not_post(
    clear_live_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del clear_live_env
    monkeypatch.setenv("INFONA_BENCH_API_KEY", "sk-test")
    capturing = CapturingUrlopen({"choices": [{"message": {"content": "{}"}}]})
    monkeypatch.setattr("ontology_skills.backends.urlopen", capturing)
    assert (
        execute_main(
            [
                "--backend",
                "live",
                "--condition",
                "4b_ft_ontology_routed",
                "--task-id",
                "et-001",
            ]
        )
        == 2
    )
    assert capturing.calls == []


def test_live_cli_mocked_200_parses_into_graph_delta_scoring(
    clear_live_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del clear_live_env
    monkeypatch.setenv("INFONA_BENCH_API_KEY", "sk-test")
    text = load_canned().responses["et-001"]
    capturing = CapturingUrlopen(
        {
            "id": "gen-mock",
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "total_tokens": 140,
                "cost": 0.0025,
            },
        }
    )
    monkeypatch.setattr("ontology_skills.backends.urlopen", capturing)
    out = tmp_path / "live.jsonl"
    code = execute_main(
        [
            "--backend",
            "live",
            "--condition",
            "4b_ontology_routed",
            "--task-id",
            "et-001",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert len(capturing.calls) == 1
    req = capturing.calls[0]
    assert req.get_method() == "POST"
    assert req.full_url == "https://openrouter.ai/api/v1/chat/completions"
    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == "qwen/qwen3-8b"
    assert body["usage"] == {"include": True}
    assert body["reasoning"] == {"enabled": False}
    assert body["max_tokens"] == LIVE_MAX_NEW_TOKENS
    assert body["max_tokens"] > 512
    assert body["response_format"] == {"type": "json_object"}
    headers = _headers(req)
    assert headers["authorization"] == "Bearer sk-test"
    referer = headers.get("http-referer") or headers.get("referer")
    assert referer == "https://infona.ai"
    assert headers["x-title"] == "Infona ontology-skills bench"

    row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert row["status"] == "ok"
    assert row["metrics"]["success"] is True
    assert row["metrics"]["graph_delta_f1"] == 1.0
    assert row["model"]["name"] == "qwen/qwen3-8b"
    assert row["model"]["param_count"] == "8B"
    assert row["model"]["backend"] == "openai-compat"
    assert row["parse"]["ok"] is True
    assert row["parse"]["error"] is None
    assert row["predicted"]["type_assertions"]
    assert row["resources"]["prompt_tokens"] == 100
    assert row["resources"]["completion_tokens"] == 40
    assert row["resources"]["hosted_cost_usd"] == 0.0025
    assert row["resources"]["latency_ms"] is not None
    assert row["resources"]["latency_ms"] >= 0.0
    assert "live executor" in row["notes"]


def test_live_complete_does_not_invent_cost(
    clear_live_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del clear_live_env
    monkeypatch.setenv("INFONA_BENCH_API_KEY", "sk-test")
    live = LiveBackend.from_env(condition=condition_by_id("4b_vanilla"))
    assert live is not None
    capturing = CapturingUrlopen(
        {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2},
        }
    )
    monkeypatch.setattr("ontology_skills.backends.urlopen", capturing)
    result = live.complete("prompt", decoding=DecodingSpec(), task_id="et-001")
    assert result.prompt_tokens == 8
    assert result.completion_tokens == 2
    assert result.hosted_cost_usd is None
    body = json.loads(capturing.calls[0].data.decode("utf-8"))
    assert body["reasoning"] == {"enabled": False}
    assert body["response_format"] == {"type": "json_object"}


def test_live_max_new_tokens_can_finish_a_small_delta() -> None:
    spec = DecodingSpec()
    assert spec.max_new_tokens == LIVE_MAX_NEW_TOKENS
    assert spec.max_new_tokens > 512
    assert spec.max_new_tokens >= 1024


def test_live_http_error_includes_status_and_body(
    clear_live_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INFONA_BENCH_API_KEY", "sk-test")
    live = LiveBackend.from_env(condition=condition_by_id("4b_vanilla"))
    assert live is not None
    body = b'{"error":{"message":"No endpoints found for qwen/qwen3-4b"}}'

    def boom(request: Request, timeout: float | None = None) -> FakeResponse:
        del timeout
        raise HTTPError(
            request.full_url, 404, "Not Found", hdrs={}, fp=BytesIO(body)
        )

    monkeypatch.setattr("ontology_skills.backends.urlopen", boom)
    with pytest.raises(RuntimeError, match="HTTP 404") as caught:
        live.complete("prompt", decoding=DecodingSpec(), task_id="et-001")
    assert "No endpoints found for qwen/qwen3-4b" in str(caught.value)
