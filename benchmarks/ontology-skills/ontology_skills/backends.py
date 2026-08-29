"""Completion backends. Live HTTP is implemented but not invoked by dry-run."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen

from .dataset import PACKAGE_ROOT
from .harness import DecodingSpec, ModelSpec

# Primary names (this package). OpenAI-compatible aliases are accepted so a
# later vLLM / llama.cpp / proxy run can reuse the usual env without code
# changes. None of these are read unless LiveBackend.from_env() is called.
ENV_BASE_URL = "INFONA_BENCH_BASE_URL"
ENV_MODEL = "INFONA_BENCH_MODEL"
ENV_API_KEY = "INFONA_BENCH_API_KEY"
ENV_QUANT = "INFONA_BENCH_QUANTIZATION"
ALIAS_BASE_URL = "OPENAI_BASE_URL"
ALIAS_MODEL = ("OPENAI_MODEL", "MODEL")
ALIAS_API_KEY = "OPENAI_API_KEY"

CANNED_PATH = PACKAGE_ROOT / "fixtures" / "canned_responses.jsonl"


class CompletionBackend(Protocol):
    model: ModelSpec

    def complete(
        self, prompt: str, *, decoding: DecodingSpec, task_id: str
    ) -> str: ...


@dataclass
class CannedBackend:
    """Map task_id → raw model text. No network."""

    responses: Mapping[str, str]
    model: ModelSpec

    def complete(
        self, prompt: str, *, decoding: DecodingSpec, task_id: str
    ) -> str:
        del prompt, decoding
        if task_id not in self.responses:
            raise KeyError(f"no canned response for task_id {task_id!r}")
        return self.responses[task_id]


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


@dataclass
class LiveBackend:
    """OpenAI-compatible POST /chat/completions. Not used by dry-run."""

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
    def from_env(cls) -> "LiveBackend | None":
        base = _first_env(ENV_BASE_URL, ALIAS_BASE_URL)
        model = _first_env(ENV_MODEL, *ALIAS_MODEL)
        if not base or not model:
            return None
        return cls(
            base_url=base,
            model_name=model,
            api_key=_first_env(ENV_API_KEY, ALIAS_API_KEY) or "",
            quantization=os.environ.get(ENV_QUANT) or "unspecified",
        )

    def complete(
        self, prompt: str, *, decoding: DecodingSpec, task_id: str
    ) -> str:
        del task_id
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model_name,
            "temperature": decoding.temperature,
            "top_p": decoding.top_p,
            "max_tokens": decoding.max_new_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if decoding.seed is not None:
            payload["seed"] = decoding.seed
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = json.dumps(payload).encode("utf-8")
        req = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=self.timeout_s) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"live backend failed: {exc}") from exc
        try:
            return str(raw["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("live backend response missing choices") from exc


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None
