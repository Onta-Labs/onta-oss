"""Embedding interface. Live POSTs; tests use a mock. Hashing is not the baseline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Mapping, Protocol
from urllib.request import Request

ENV_EMBED_MODEL = "INFONA_BENCH_EMBED_MODEL"
ALIAS_EMBED_MODEL = ("OPENAI_EMBED_MODEL",)
# Hosted on OpenRouter; used only when a Bearer key is present.
DEFAULT_EMBED_MODEL = "openai/text-embedding-3-small"
MOCK_EMBEDDER_ID = "mock.deterministic.v1"
TABLE_EMBEDDER_ID = "fixture.table.v1"
MOCK_DIM = 16


class Embedder(Protocol):
    embedder_id: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class MockEmbedder:
    """Deterministic fake vectors for CI. Not the published RAG baseline."""

    embedder_id: str = MOCK_EMBEDDER_ID
    dim: int = MOCK_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_hash_vector(text, self.dim) for text in texts]


@dataclass(frozen=True, slots=True)
class TableEmbedder:
    """Pinned text → vector. Missing texts are the zero vector of ``dim``.

    Used so hermetic RAG tests can force a distractor into top-k without a
    live embedding model. Not the published RAG baseline.
    """

    table: Mapping[str, tuple[float, ...]]
    dim: int = 4
    embedder_id: str = TABLE_EMBEDDER_ID

    def embed(self, texts: list[str]) -> list[list[float]]:
        zero = (0.0,) * self.dim
        out: list[list[float]] = []
        for text in texts:
            vec = self.table.get(text, zero)
            if len(vec) != self.dim:
                raise RuntimeError("fixture embedding dim mismatch")
            out.append(list(vec))
        return out


@dataclass
class OpenAICompatEmbedder:
    """POST {base}/embeddings. Fail closed if constructed without a key."""

    base_url: str
    model_name: str
    api_key: str
    timeout_s: float = 120.0

    @property
    def embedder_id(self) -> str:
        return self.model_name

    @classmethod
    def from_env(cls) -> "OpenAICompatEmbedder | None":
        from .backends import (
            ALIAS_API_KEY,
            ALIAS_BASE_URL,
            DEFAULT_BASE_URL,
            ENV_API_KEY,
            ENV_BASE_URL,
            _first_env,
        )

        api_key = _first_env(ENV_API_KEY, *ALIAS_API_KEY)
        if not api_key:
            return None
        return cls(
            base_url=_first_env(ENV_BASE_URL, ALIAS_BASE_URL) or DEFAULT_BASE_URL,
            model_name=_first_env(ENV_EMBED_MODEL, *ALIAS_EMBED_MODEL)
            or DEFAULT_EMBED_MODEL,
            api_key=api_key,
        )

    @classmethod
    def from_chat_credentials(
        cls, *, base_url: str, api_key: str
    ) -> "OpenAICompatEmbedder | None":
        from .backends import _first_env

        if not api_key:
            return None
        return cls(
            base_url=base_url,
            model_name=_first_env(ENV_EMBED_MODEL, *ALIAS_EMBED_MODEL)
            or DEFAULT_EMBED_MODEL,
            api_key=api_key,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        from .backends import OPENROUTER_REFERER, OPENROUTER_TITLE, _post_json

        if not self.api_key:
            raise RuntimeError("live embedder refused: missing API key")
        if not texts:
            return []
        url = self.base_url.rstrip("/") + "/embeddings"
        payload = {"model": self.model_name, "input": texts}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": OPENROUTER_REFERER,
            "X-Title": OPENROUTER_TITLE,
        }
        req = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        raw = _post_json(req, timeout=self.timeout_s)
        return _parse_embeddings(raw, expected=len(texts))


def _hash_vector(text: str, dim: int) -> list[float]:
    digest = sha256(text.encode("utf-8")).digest()
    out: list[float] = []
    i = 0
    while len(out) < dim:
        chunk = digest[i % len(digest)]
        out.append((chunk / 127.5) - 1.0)
        i += 1
        if i % len(digest) == 0:
            digest = sha256(digest).digest()
    return out


def _parse_embeddings(raw: object, *, expected: int) -> list[list[float]]:
    if not isinstance(raw, dict):
        raise RuntimeError("live embedder response is not an object")
    rows = raw.get("data")
    if not isinstance(rows, list) or len(rows) != expected:
        raise RuntimeError("live embedder response missing embedding data")
    ordered = sorted(rows, key=lambda row: int(row.get("index", 0)))
    vectors: list[list[float]] = []
    for row in ordered:
        if not isinstance(row, dict):
            raise RuntimeError("live embedder data row is not an object")
        vec = row.get("embedding")
        if not isinstance(vec, list) or not vec:
            raise RuntimeError("live embedder row missing embedding")
        vectors.append([float(x) for x in vec])
    return vectors
