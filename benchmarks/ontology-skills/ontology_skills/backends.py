"""Completion backends. Live POSTs only when an API key is present."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .conditions import Condition
from .dataset import PACKAGE_ROOT
from .harness import DecodingSpec, ModelSpec

ENV_BASE_URL = "INFONA_BENCH_BASE_URL"
ENV_MODEL = "INFONA_BENCH_MODEL"
ENV_API_KEY = "INFONA_BENCH_API_KEY"
ENV_QUANT = "INFONA_BENCH_QUANTIZATION"
ALIAS_BASE_URL = "OPENAI_BASE_URL"
ALIAS_MODEL = ("OPENAI_MODEL", "MODEL")
ALIAS_API_KEY = ("OPENAI_API_KEY", "OPENROUTER_API_KEY")

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_REFERER = "https://infona.ai"
OPENROUTER_TITLE = "Infona ontology-skills bench"

# OpenRouter catalog 2026-08-29: no Qwen *4b* id (neither Qwen3-4B nor
# Qwen3.5-4B). Closest same-family SLM is Qwen3-8B. Condition ids stay
# 4b_*; param_count records 8B so a run is not labeled 4B.
DEFAULT_MODEL_BY_BUCKET: dict[str, str] = {
    "4b": "qwen/qwen3-8b",
    "9b": "qwen/qwen3.5-9b",
    "27b_or_frontier": "qwen/qwen3.5-27b",
}
PARAM_COUNT_BY_BUCKET: dict[str, str] = {
    "4b": "8B",
    "9b": "9B",
    "27b_or_frontier": "27B",
}
ERROR_BODY_LIMIT = 2048

CANNED_PATH = PACKAGE_ROOT / "fixtures" / "canned_responses.jsonl"


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """Backend output. Resource fields are None unless the server returned them."""

    text: str
    latency_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    hosted_cost_usd: float | None = None


class CompletionBackend(Protocol):
    model: ModelSpec

    def complete(
        self, prompt: str, *, decoding: DecodingSpec, task_id: str
    ) -> CompletionResult: ...


@dataclass
class CannedBackend:
    """Map task_id → raw model text. No network."""

    responses: Mapping[str, str]
    model: ModelSpec

    def complete(
        self, prompt: str, *, decoding: DecodingSpec, task_id: str
    ) -> CompletionResult:
        del prompt, decoding
        if task_id not in self.responses:
            raise KeyError(f"no canned response for task_id {task_id!r}")
        return CompletionResult(text=self.responses[task_id])


def load_canned(path: Path | None = None) -> CannedBackend:
    path = path or CANNED_PATH
    responses: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            row = json.loads(stripped)
            responses[str(row["task_id"])] = str(row["text"])
    return CannedBackend(
        responses=responses,
        model=ModelSpec(
            name="canned",
            quantization="none",
            param_count="n/a",
            backend="fixture",
        ),
    )


def default_model_for_condition(condition: Condition) -> str:
    try:
        return DEFAULT_MODEL_BY_BUCKET[condition.model_bucket]
    except KeyError as exc:
        raise KeyError(
            f"no default model for bucket {condition.model_bucket!r}"
        ) from exc


@dataclass
class LiveBackend:
    """OpenAI-compatible POST /chat/completions (OpenRouter by default)."""

    base_url: str
    model_name: str
    api_key: str
    quantization: str = "unspecified"
    param_count: str = "unspecified"
    timeout_s: float = 120.0

    @property
    def model(self) -> ModelSpec:
        return ModelSpec(
            name=self.model_name,
            quantization=self.quantization,
            param_count=self.param_count,
            backend="openai-compat",
        )

    @classmethod
    def from_env(cls, *, condition: Condition | None = None) -> "LiveBackend | None":
        """Return a backend only when a Bearer key is present. No POST here."""
        api_key = _first_env(ENV_API_KEY, *ALIAS_API_KEY)
        if not api_key:
            return None
        bucket = condition.model_bucket if condition is not None else "4b"
        model = _first_env(ENV_MODEL, *ALIAS_MODEL)
        if not model:
            model = (
                default_model_for_condition(condition)
                if condition is not None
                else DEFAULT_MODEL_BY_BUCKET["4b"]
            )
        return cls(
            base_url=_first_env(ENV_BASE_URL, ALIAS_BASE_URL) or DEFAULT_BASE_URL,
            model_name=model,
            api_key=api_key,
            quantization=os.environ.get(ENV_QUANT) or "unspecified",
            param_count=PARAM_COUNT_BY_BUCKET.get(bucket, "unspecified"),
        )

    def complete(
        self, prompt: str, *, decoding: DecodingSpec, task_id: str
    ) -> CompletionResult:
        del task_id
        if not self.api_key:
            raise RuntimeError("live backend refused: missing API key")
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model_name,
            "temperature": decoding.temperature,
            "top_p": decoding.top_p,
            "max_tokens": decoding.max_new_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "usage": {"include": True},
            # Qwen3 thinks by default; exclude / enable_thinking=false still
            # spend reasoning_tokens. This flag zeros that spend.
            "reasoning": {"enabled": False},
        }
        if decoding.seed is not None:
            payload["seed"] = decoding.seed
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": OPENROUTER_REFERER,
            "X-Title": OPENROUTER_TITLE,
        }
        req = Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        started = time.perf_counter()
        raw = _post_json(req, timeout=self.timeout_s)
        latency_ms = (time.perf_counter() - started) * 1000.0
        try:
            text = str(raw["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("live backend response missing choices") from exc
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        return CompletionResult(
            text=text,
            latency_ms=latency_ms,
            prompt_tokens=_as_int(usage.get("prompt_tokens")),
            completion_tokens=_as_int(usage.get("completion_tokens")),
            hosted_cost_usd=_as_cost(usage),
        )


def _post_json(req: Request, *, timeout: float) -> dict:
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(_http_error_message(exc)) from exc
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"live backend failed: {exc}") from exc


def _http_error_message(exc: HTTPError) -> str:
    body = _read_http_body(exc)
    if body:
        return f"live backend HTTP {exc.code}: {body}"
    return f"live backend HTTP {exc.code}"


def _read_http_body(exc: HTTPError) -> str:
    try:
        raw = exc.read()
    except OSError:
        return ""
    if not raw:
        return ""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    if len(text) > ERROR_BODY_LIMIT:
        return text[:ERROR_BODY_LIMIT] + "…"
    return text


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_cost(usage: dict) -> float | None:
    """Copy OpenRouter ``usage.cost`` when present. Do not invent a price."""
    raw = usage.get("cost")
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None
