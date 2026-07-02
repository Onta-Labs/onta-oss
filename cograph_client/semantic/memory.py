"""Pure-Python in-memory :class:`SemanticIndex` — the OSS default (ONTA-175).

Zero-config, non-durable, per-process. Fully functional so OSS deployments
without Postgres (and the whole test suite) work with no external service:

* **Lexical scoring** is naive term-frequency with coverage weighting — no
  stemming, no idf, no external deps. The pgvector backend (ONTA-176) uses a
  real ``tsvector``/``ts_rank`` pipeline; parity between the two is *loose by
  design* (a later smoke-parity suite only asserts overlap, not ordering).
* **Vector scoring** is exact cosine via numpy over whatever chunks have a
  filled embedding.
* **Fusion** is Reciprocal Rank Fusion with the same ``k=60`` the SQL backend
  uses (two top-:data:`_CANDIDATES_PER_LEG` legs → ``1/(k+rank)`` summed), so
  the two backends rank in the same spirit even though the leg scorers differ.

The queue semantics mirror the durable store exactly: a chunk with
``embedding=None`` is "pending", :meth:`fetch_pending` drains deterministically,
and :meth:`fill_embeddings` / :meth:`mark_embed_failed` honor the
``content_hash`` optimistic-concurrency guard (see the protocol docstrings).
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from typing import Optional, Sequence

import numpy as np

from cograph_client.semantic.protocol import (
    ChunkKey,
    SemanticChunk,
    SemanticHit,
    SemanticSearchResult,
)

#: RRF constant — matches the ONTA-176 SQL (`1/(60+rank)`), the standard value
#: from the original RRF paper. Not worth configuring.
_RRF_K = 60

#: Per-leg candidate depth before fusion — matches the SQL backend's two
#: top-50 CTEs (FTS + ANN).
_CANDIDATES_PER_LEG = 50

#: Snippet budget for hits. Big enough to be a readable citation, small enough
#: that a 20-hit response stays light (the full chunk is never shipped).
_SNIPPET_MAX_CHARS = 240

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _lexical_score(query_tokens: list[str], chunk_tokens: list[str]) -> float:
    """Naive lexical relevance: length-normalized term frequency, weighted by
    query-term coverage (a chunk matching both of two query terms beats one
    repeating a single term). Zero when nothing matches. Deliberately simple —
    see the module docstring's loose-parity note."""
    if not query_tokens or not chunk_tokens:
        return 0.0
    counts = Counter(chunk_tokens)
    distinct_query = set(query_tokens)
    matched_terms = [t for t in distinct_query if counts[t] > 0]
    if not matched_terms:
        return 0.0
    tf = sum(counts[t] for t in matched_terms)
    coverage = len(matched_terms) / len(distinct_query)
    # +10 damps the normalization for very short chunks so a 3-token chunk
    # doesn't automatically dominate every ranking.
    return coverage * tf / (len(chunk_tokens) + 10.0)


def _cosine(query: np.ndarray, vec: list[float]) -> float:
    """Cosine similarity, guarding zero vectors and dimension mismatches
    (a chunk embedded under an older model with a different dim scores 0
    rather than crashing the query)."""
    v = np.asarray(vec, dtype=float)
    if v.shape != query.shape:
        return 0.0
    denom = float(np.linalg.norm(query) * np.linalg.norm(v))
    if denom == 0.0:
        return 0.0
    return float(np.dot(query, v) / denom)


def _snippet(text: str, limit: int = _SNIPPET_MAX_CHARS) -> str:
    """First ``limit`` chars of a chunk, cut back to a word boundary."""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip() + "…"


class InMemorySemanticIndex:
    """Non-durable, per-process :class:`SemanticIndex` — the registered default."""

    def __init__(self) -> None:
        # PK -> chunk; the dict IS the entity_semantic_chunk table.
        self._chunks: dict[ChunkKey, SemanticChunk] = {}
        self._lock = asyncio.Lock()

    # -- writes ---------------------------------------------------------------

    async def upsert_chunks(self, chunks: Sequence[SemanticChunk]) -> None:
        """Replace-per-doc upsert (see the Protocol's complete-document
        contract): unchanged-hash rows are kept (preserving embeddings),
        changed rows replaced, and each doc's stale tail deleted."""
        async with self._lock:
            docs: dict[tuple[str, str, str, str], list[SemanticChunk]] = {}
            for c in chunks:
                docs.setdefault(c.doc_key(), []).append(c)
            for (tenant_id, kg_name, entity_uri, attr), group in docs.items():
                for c in group:
                    existing = self._chunks.get(c.key())
                    if (
                        existing is not None
                        and existing.content_hash == c.content_hash
                    ):
                        # Same content -> keep the stored row as-is. This is
                        # what makes replaying an unchanged doc free: a filled
                        # embedding survives instead of being re-queued.
                        continue
                    self._chunks[c.key()] = c.model_copy(deep=True)
                # Stale tail: the doc shrank -> rows past its highest incoming
                # index are leftovers of the previous, longer version.
                doc_len = max(c.chunk_ix for c in group) + 1
                stale = [
                    k
                    for k, v in self._chunks.items()
                    if v.tenant_id == tenant_id
                    and v.kg_name == kg_name
                    and v.entity_uri == entity_uri
                    and v.attr == attr
                    and v.chunk_ix >= doc_len
                ]
                for k in stale:
                    del self._chunks[k]

    async def delete(
        self,
        entity_uri: str,
        tenant_id: str,
        *,
        kg_name: Optional[str] = None,
        attr: Optional[str] = None,
    ) -> None:
        async with self._lock:
            self._chunks = {
                k: v
                for k, v in self._chunks.items()
                if not (
                    v.tenant_id == tenant_id
                    and v.entity_uri == entity_uri
                    and (kg_name is None or v.kg_name == kg_name)
                    and (attr is None or v.attr == attr)
                )
            }

    async def clear(self, tenant_id: str, *, kg_name: Optional[str] = None) -> None:
        async with self._lock:
            self._chunks = {
                k: v
                for k, v in self._chunks.items()
                if not (
                    v.tenant_id == tenant_id
                    and (kg_name is None or v.kg_name == kg_name)
                )
            }

    # -- search ---------------------------------------------------------------

    def _candidates(
        self,
        tenant_id: str,
        kg_name: Optional[str],
        type_filter: Optional[str],
    ) -> list[SemanticChunk]:
        return [
            c
            for c in self._chunks.values()
            if c.tenant_id == tenant_id  # tenant isolation: never cross tenants
            and (kg_name is None or c.kg_name == kg_name)
            and (type_filter is None or c.attrs.get("type") == type_filter)
        ]

    async def search(
        self,
        tenant_id: str,
        query_text: str,
        *,
        query_embedding: Optional[Sequence[float]] = None,
        kg_name: Optional[str] = None,
        type_filter: Optional[str] = None,
        top_k: int = 10,
    ) -> SemanticSearchResult:
        async with self._lock:
            candidates = self._candidates(tenant_id, kg_name, type_filter)

            # Leg 1 — lexical (always available; a just-written chunk with a
            # NULL embedding is findable here immediately, mirroring the
            # generated-tsvector freshness property of the durable store).
            query_tokens = _tokens(query_text)
            lexical = [
                (score, c)
                for c in candidates
                if (score := _lexical_score(query_tokens, _tokens(c.chunk_text))) > 0.0
            ]
            lexical.sort(key=lambda sc: (-sc[0], sc[1].key()))
            lexical = lexical[:_CANDIDATES_PER_LEG]

            # Leg 2 — vector, only when the caller could embed the query.
            degraded = query_embedding is None
            vector: list[tuple[float, SemanticChunk]] = []
            if query_embedding is not None:
                q = np.asarray(list(query_embedding), dtype=float)
                vector = [
                    (score, c)
                    for c in candidates
                    if c.embedding is not None
                    and (score := _cosine(q, c.embedding)) > 0.0
                ]
                vector.sort(key=lambda sc: (-sc[0], sc[1].key()))
                vector = vector[:_CANDIDATES_PER_LEG]

            # RRF fusion (k=60) over the two ranked legs, then group chunks
            # into entities: an entity scores as its best fused chunk.
            fused: dict[ChunkKey, float] = {}
            by_key: dict[ChunkKey, SemanticChunk] = {}
            for leg in (lexical, vector):
                for rank, (_score, c) in enumerate(leg, start=1):
                    fused[c.key()] = fused.get(c.key(), 0.0) + 1.0 / (_RRF_K + rank)
                    by_key[c.key()] = c
            best_per_entity: dict[str, tuple[float, SemanticChunk]] = {}
            for key, score in fused.items():
                c = by_key[key]
                prev = best_per_entity.get(c.entity_uri)
                if prev is None or score > prev[0]:
                    best_per_entity[c.entity_uri] = (score, c)

            ranked = sorted(
                best_per_entity.items(), key=lambda kv: (-kv[1][0], kv[0])
            )[: max(top_k, 0)]
            hits = [
                SemanticHit(
                    entity_uri=uri,
                    attrs=dict(chunk.attrs),
                    snippet=_snippet(chunk.chunk_text),
                    attr=chunk.attr,
                    score=score,
                )
                for uri, (score, chunk) in ranked
            ]
            return SemanticSearchResult(hits=hits, degraded=degraded)

    # -- embed-fill sweep seam --------------------------------------------------

    async def fetch_pending(
        self,
        *,
        limit: int = 100,
        max_attempts: Optional[int] = None,
        tenant_id: Optional[str] = None,
        kg_name: Optional[str] = None,
    ) -> list[SemanticChunk]:
        async with self._lock:
            pending = [
                c
                for c in self._chunks.values()
                if c.embedding is None
                and (max_attempts is None or c.attempt_count < max_attempts)
                and (tenant_id is None or c.tenant_id == tenant_id)
                and (kg_name is None or c.kg_name == kg_name)
            ]
            # Deterministic drain order (the durable store orders by PK too).
            pending.sort(key=lambda c: c.key())
            return [c.model_copy(deep=True) for c in pending[: max(limit, 0)]]

    async def fill_embeddings(
        self,
        chunks: Sequence[SemanticChunk],
        embeddings: Sequence[Sequence[float]],
        *,
        embed_model: str,
    ) -> int:
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"chunks ({len(chunks)}) and embeddings ({len(embeddings)}) "
                "must be parallel sequences"
            )
        filled = 0
        async with self._lock:
            for c, vec in zip(chunks, embeddings):
                row = self._chunks.get(c.key())
                # content_hash is the optimistic-concurrency token: if the doc
                # was replaced between fetch and fill, the stale vector must
                # not land on the new text (see the Protocol docstring).
                if (
                    row is None
                    or row.content_hash != c.content_hash
                    or row.embedding is not None
                ):
                    continue
                row.embedding = [float(x) for x in vec]
                row.embed_model = embed_model
                row.last_error = None
                filled += 1
        return filled

    async def mark_embed_failed(
        self, chunks: Sequence[SemanticChunk], *, error: str
    ) -> int:
        marked = 0
        async with self._lock:
            for c in chunks:
                row = self._chunks.get(c.key())
                if (
                    row is None
                    or row.content_hash != c.content_hash
                    or row.embedding is not None
                ):
                    continue
                row.attempt_count += 1
                row.last_error = error
                marked += 1
        return marked
