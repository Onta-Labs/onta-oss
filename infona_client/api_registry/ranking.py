"""Shared semantic-retrieval primitives over the API catalog (ONTA-341).

The discovery router (``router.py``) and the enrichment selector
(``registry_selection.py``) both need the SAME thing: rank a set of catalog
entries by how well each entry's capability card matches a need, using a
real embedding when a key is available and a deterministic lexical fallback
otherwise. Keeping ONE copy of that ranking here — rather than one per rail — is
the read-path mirror of write-path convergence: the moment two copies drift on
what "the capability card" is or how similarity is scored, "relevant source"
means different things on the two rails.

* :func:`coverage_text` — the entry's capability card: title + publisher +
  description + coverage (entity kinds, attributes, geo, temporal) + the
  author-written ``example_asks``. This is the text a vector rank embeds.
* :func:`embedding_rank` — cosine similarity of the need against each card via
  the shared OSS embeddings client; ``None`` when no key / any embedding error.
* :func:`lexical_rank` — token-overlap fallback, deterministic and network-free.
* :func:`rank_top_k` — the composed prefilter: embed-rank (fallback lexical),
  keep the top-k specs in descending relevance.

Pure data + stdlib + numpy — no ``cograph.*`` import; the embeddings call is the
one shared ``nlp.embed_client`` seam every embedding consumer already uses.
"""

from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable, Optional

from .spec import ApiSourceSpec

logger = logging.getLogger(__name__)

# Injectable embedding seam (tests pass a fake; prod wires the real helper).
EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


def coverage_text(spec: ApiSourceSpec) -> str:
    """The entry's capability card as one blob of prose for semantic ranking.

    This is the SINGLE source of truth for what gets embedded/searched (ONTA-390):
    the human-readable identity (title / publisher / description), the structured
    coverage prose (entity kinds, attributes, geo, temporal), the author-written
    ``example_asks`` few-shots, and the curated ``keywords`` retrieval terms.
    Empty parts are dropped so a sparse entry doesn't dilute the card with blank
    lines. Both the precomputed coverage index and the lexical fallback derive
    their text from here, so "what an entry matches on" never forks."""
    c = spec.coverage
    parts = [
        spec.title, spec.publisher, spec.description,
        " ".join(c.entity_kinds), " ".join(c.attributes), c.geo, c.temporal,
        " ".join(c.example_asks), " ".join(c.keywords),
    ]
    return " \n".join(p for p in parts if p)


async def embedding_rank(
    query: str,
    texts: list[str],
    *,
    openrouter_key: str = "",
    embed_fn: Optional[EmbedFn] = None,
) -> Optional[list[float]]:
    """Cosine similarity of ``query`` against each of ``texts``, or ``None``.

    Returns ``None`` (so the caller falls back to :func:`lexical_rank`) when no
    ``embed_fn`` is injected AND no ``openrouter_key`` is set, or on any embedding
    error / shape mismatch. Never raises."""
    fn = embed_fn
    if fn is None:
        if not openrouter_key:
            return None
        from ..nlp.embed_client import embed_texts

        async def fn(items: list[str]) -> list[list[float]]:  # type: ignore[misc]
            return await embed_texts(items, api_key=openrouter_key)

    try:
        import numpy as np

        from ..nlp.embed_client import cosine_similarity

        vectors = await fn([query, *texts])
        if not vectors or len(vectors) != len(texts) + 1:
            return None
        q_vec = np.array(vectors[0], dtype=np.float32)
        matrix = np.array(vectors[1:], dtype=np.float32)
        return [float(s) for s in cosine_similarity(q_vec, matrix)]
    except Exception as exc:  # noqa: BLE001 - ranking must never raise
        logger.debug("api_registry embedding rank failed: %s", exc)
        return None


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def lexical_rank(query: str, texts: list[str]) -> list[float]:
    """Deterministic token-overlap score of ``query`` against each of ``texts``.

    Fraction of the query's tokens present in the candidate text — the same
    network-free fallback the discovery router used before the ranking helpers
    were shared."""
    q_tokens = set(_TOKEN_RE.findall(query.lower()))
    scores: list[float] = []
    for t in texts:
        t_tokens = set(_TOKEN_RE.findall(t.lower()))
        overlap = len(q_tokens & t_tokens)
        scores.append(overlap / (len(q_tokens) + 1e-9))
    return scores


async def rank_top_k(
    query: str,
    specs: list[ApiSourceSpec],
    *,
    top_k: int,
    openrouter_key: str = "",
    embed_fn: Optional[EmbedFn] = None,
) -> list[ApiSourceSpec]:
    """Return the ``top_k`` specs most relevant to ``query`` (descending).

    Semantic (embedding) rank when a key/embed_fn is available, else the
    deterministic lexical fallback. A stable sort is used so ties preserve the
    input order (the caller's arbitration then applies its own total order).
    When ``len(specs) <= top_k`` the ranking round-trip is skipped and the input
    is returned unchanged — there is nothing to narrow."""
    if top_k <= 0:
        return []
    if len(specs) <= top_k:
        return list(specs)
    texts = [coverage_text(s) for s in specs]
    scores = await embedding_rank(
        query, texts, openrouter_key=openrouter_key, embed_fn=embed_fn
    )
    if scores is None:
        scores = lexical_rank(query, texts)
    order = sorted(range(len(specs)), key=lambda i: scores[i], reverse=True)
    return [specs[i] for i in order[:top_k]]


__all__ = [
    "EmbedFn",
    "coverage_text",
    "embedding_rank",
    "lexical_rank",
    "rank_top_k",
]
