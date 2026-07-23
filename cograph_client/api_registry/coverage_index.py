"""Precomputed capability-card embedding index for registry routing (ONTA-390).

The discovery router's prefilter used to embed ``query + EVERY entry's coverage
text`` on **every** query (``ranking.rank_top_k`` / the old ``_embedding_rank``).
Correct at tens of APIs, wasteful at hundreds: latency and cost grow with catalog
size on each discovery. This index precomputes and STORES the per-entry vectors so
a query embeds **once** and is cosine-ranked against the stored matrix.

Design:

* **Content-addressed cache.** Each entry's vector is keyed by a hash of
  ``(embedding model, coverage_text(spec))`` — the SINGLE embeddable-text contract
  in :func:`ranking.coverage_text` (now including ``keywords``). So:
    - two entries with identical cards share one vector;
    - changing an entry's description/keywords changes its text → new key → a
      clean re-embed on the next rank/rebuild (invalidation is automatic);
    - a model upgrade changes every key → the whole catalog re-embeds cleanly.
  Keying by content (not slug) also means a tenant-shadowed slug never thrashes a
  global slug's cached vector.
* **Lazy + eager.** :meth:`rank` embeds any not-yet-cached entries it is asked to
  rank (first-use warm), then embeds the query alone. :func:`rebuild` warms the
  whole catalog up front (wired to catalog load / tenant invalidation).
* **Fail-open.** No key / no ``embed_fn`` / any embedding error ⇒ :meth:`rank`
  returns ``None`` so the router falls back to the deterministic lexical rank —
  discovery must never break because embeddings are unavailable.

Process-local for v1 (an in-memory dict is plenty at hundreds of entries); a
disk/pgvector store can follow under ONTA-341 without changing this interface.

Pure ``cograph_client.*`` + stdlib + numpy — no ``from cograph.*``.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

from ..nlp.embed_client import EMBEDDING_MODEL, cosine_similarity
from .ranking import EmbedFn, coverage_text, lexical_rank
from .spec import ApiSourceSpec

logger = logging.getLogger(__name__)


def _resolve_embed_fn(embed_fn: Optional[EmbedFn], openrouter_key: str) -> Optional[EmbedFn]:
    """The injected ``embed_fn`` if given, else a real OpenRouter-backed one when a
    key is present, else ``None`` (caller falls back to lexical)."""
    if embed_fn is not None:
        return embed_fn
    if not openrouter_key:
        return None
    from ..nlp.embed_client import embed_texts

    async def fn(items: list[str]) -> list[list[float]]:
        return await embed_texts(items, api_key=openrouter_key)

    return fn


def _key(text: str, model: str) -> str:
    """Content address for a coverage-text vector: hash of (model, text)."""
    return hashlib.sha256(f"{model}\n{text}".encode("utf-8")).hexdigest()


class CoverageIndex:
    """A process-local store of coverage-text embeddings for registry entries."""

    def __init__(self, model: str = EMBEDDING_MODEL) -> None:
        self._model = model
        # content-hash -> embedding vector
        self._vectors: dict[str, list[float]] = {}

    def __len__(self) -> int:
        return len(self._vectors)

    def clear(self) -> None:
        self._vectors.clear()

    def invalidate(self, spec: ApiSourceSpec) -> None:
        """Drop ``spec``'s current coverage-text vector (if cached). Rarely needed
        — a changed card simply hashes to a new key — but exposed for explicit
        eviction (e.g. a tenant source deletion)."""
        self._vectors.pop(_key(coverage_text(spec), self._model), None)

    async def _ensure(self, specs: list[ApiSourceSpec], embed_fn: EmbedFn) -> None:
        """Embed and cache the vectors for any of ``specs`` not already cached.
        Uncached cards are embedded in ONE batch call; already-cached cards cost
        nothing (this is what stops a per-query re-embed of the whole catalog)."""
        pending: dict[str, str] = {}  # key -> text (dedup identical cards)
        for spec in specs:
            text = coverage_text(spec)
            k = _key(text, self._model)
            if k not in self._vectors and k not in pending:
                pending[k] = text
        if not pending:
            return
        keys = list(pending.keys())
        vectors = await embed_fn([pending[k] for k in keys])
        if not vectors or len(vectors) != len(keys):
            raise ValueError(
                f"embed_fn returned {len(vectors) if vectors else 0} vectors "
                f"for {len(keys)} texts"
            )
        for k, v in zip(keys, vectors):
            self._vectors[k] = v

    async def rebuild(
        self,
        specs: list[ApiSourceSpec],
        *,
        openrouter_key: str = "",
        embed_fn: Optional[EmbedFn] = None,
    ) -> int:
        """Warm the cache for ``specs`` (typically ``catalog.enabled()``). Returns
        the number of entries embedded this call (0 when all were cached or no
        embedder is available). Best-effort: never raises — a failure just leaves
        the cache as-is and rank() falls back to lexical."""
        fn = _resolve_embed_fn(embed_fn, openrouter_key)
        if fn is None:
            return 0
        before = len(self._vectors)
        try:
            await self._ensure(list(specs), fn)
        except Exception as exc:  # noqa: BLE001 - rebuild must never break startup
            logger.debug("coverage_index rebuild failed: %s", exc)
            return 0
        return len(self._vectors) - before

    async def rank(
        self,
        query: str,
        specs: list[ApiSourceSpec],
        *,
        top_k: int,
        openrouter_key: str = "",
        embed_fn: Optional[EmbedFn] = None,
    ) -> Optional[list[ApiSourceSpec]]:
        """Return the ``top_k`` specs most semantically similar to ``query`` using
        STORED entry vectors + ONE query embed, or ``None`` when embeddings are
        unavailable / fail (so the caller uses the lexical fallback).

        Never re-embeds an already-cached entry, so ranking a second query only
        pays for the query's own vector — the whole point of the index."""
        if top_k <= 0:
            return []
        if not specs:
            return []
        fn = _resolve_embed_fn(embed_fn, openrouter_key)
        if fn is None:
            return None
        try:
            import numpy as np

            await self._ensure(specs, fn)
            q_vectors = await fn([query])
            if not q_vectors:
                return None
            q_vec = np.array(q_vectors[0], dtype=np.float32)
            matrix = np.array(
                [self._vectors[_key(coverage_text(s), self._model)] for s in specs],
                dtype=np.float32,
            )
            scores = cosine_similarity(q_vec, matrix)
            order = sorted(range(len(specs)), key=lambda i: scores[i], reverse=True)
            return [specs[i] for i in order[:top_k]]
        except Exception as exc:  # noqa: BLE001 - ranking must never break discovery
            logger.debug("coverage_index rank failed: %s", exc)
            return None


def rank_lexical(query: str, specs: list[ApiSourceSpec], *, top_k: int) -> list[ApiSourceSpec]:
    """Deterministic, network-free top-k over the same coverage text — the fallback
    the router uses when :meth:`CoverageIndex.rank` returns ``None``."""
    if top_k <= 0 or not specs:
        return []
    scores = lexical_rank(query, [coverage_text(s) for s in specs])
    order = sorted(range(len(specs)), key=lambda i: scores[i], reverse=True)
    return [specs[i] for i in order[:top_k]]


# --------------------------------------------------------------------------- #
# Process-wide singleton (rebuilt on catalog load / tenant invalidation)
# --------------------------------------------------------------------------- #
_index_singleton: Optional[CoverageIndex] = None


def get_coverage_index() -> CoverageIndex:
    global _index_singleton
    if _index_singleton is None:
        _index_singleton = CoverageIndex()
    return _index_singleton


def reset_coverage_index() -> None:
    """Drop the process-wide index (tests / a full catalog rebuild)."""
    global _index_singleton
    _index_singleton = None


__all__ = [
    "CoverageIndex",
    "rank_lexical",
    "get_coverage_index",
    "reset_coverage_index",
]
