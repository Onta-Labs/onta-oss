"""Embed-fill sweep — drain ``embedding IS NULL`` via the shared embed client."""

from __future__ import annotations

from typing import Optional

from infona_client.semantic.protocol import SemanticIndex
from infona_client.semantic.reconciler_common import _host
from infona_client.semantic.reconciler_const import _MAX_SWEEP_ITERATIONS
from infona_client.semantic.reconciler_env import embed_max_attempts, semantic_index_enabled
from infona_client.semantic.registry import get_semantic_index


async def run_embed_fill_sweep(
    *,
    index: Optional[SemanticIndex] = None,
    api_key: Optional[str] = None,
    limit: int = 100,
    max_attempts: Optional[int] = None,
) -> dict[str, int]:
    """Drain ``embedding IS NULL`` rows through the shared embed client.

    The NULL embedding column IS the durable queue (protocol docstring), which
    is what makes this sweep crash-safe: a deploy kill mid-fill loses nothing —
    rows already filled stay filled (``fill_embeddings`` is guarded by the
    ``content_hash`` optimistic-concurrency token), the rest are still NULL and
    drain on the next sweep.

    Poison handling: a failed batch is ``mark_embed_failed`` (attempt_count++)
    and the sweep MOVES ON — an in-sweep ``seen`` set prevents re-fetching the
    same rows in this run, and ``fetch_pending(max_attempts=…)`` dead-letters
    rows past the cutoff on later runs (they stay inspectable via a higher
    ``max_attempts``, never silently vanish). Backoff = one sweep interval per
    attempt (the sweep cadence is the spacing; no per-row timer state).

    Counters (structlog, emitted every run — no silent zeros):
    ``embeds_pending`` (rows found queued), ``embeds_filled``,
    ``embed_failures``.
    """
    from infona_client.config import settings
    from infona_client.nlp.embed_client import EMBEDDING_MODEL, embed_texts

    h = _host()
    counters = {"embeds_pending": 0, "embeds_filled": 0, "embed_failures": 0}
    if not semantic_index_enabled():
        h.logger.info("semantic_embed_fill_skipped_disabled")
        return counters

    idx = index if index is not None else get_semantic_index()
    key = api_key if api_key is not None else settings.openrouter_api_key
    cutoff = max_attempts if max_attempts is not None else embed_max_attempts()

    # Keys of rows that FAILED this sweep. Only failures stay pending (a
    # successful fill drains the row from the queue), so only failures need
    # the fetch window widened to slide past them — adding every processed row
    # here would make the window (and the backend's scan) grow linearly with
    # progress, i.e. a quadratic sweep over a large healthy queue.
    seen: set[tuple] = set()
    for _ in range(_MAX_SWEEP_ITERATIONS):
        # fetch_pending drains in deterministic (PK) order, so a row that just
        # FAILED is still at the head of the queue. Widening the window by the
        # rows already failed this sweep lets the fetch slide past them —
        # otherwise a poison row at the head would wedge the whole sweep at
        # ``limit=len(failed)`` (exactly the failure mode ONTA-181 forbids).
        batch = await idx.fetch_pending(limit=limit + len(seen), max_attempts=cutoff)
        fresh = [c for c in batch if c.key() not in seen][:limit]
        if not fresh:
            break
        counters["embeds_pending"] += len(fresh)
        if not key:
            # Lexical-only deployment (no OpenRouter key): rows stay queued —
            # search still works degraded (generated tsvector), and the queue
            # drains the moment a key is configured. Loud, not silent.
            h.logger.warning(
                "semantic_embed_fill_no_api_key", pending=counters["embeds_pending"]
            )
            break
        try:
            vectors = await embed_texts([c.chunk_text for c in fresh], api_key=key)
            counters["embeds_filled"] += await idx.fill_embeddings(
                fresh, vectors, embed_model=EMBEDDING_MODEL
            )
        except Exception as exc:  # noqa: BLE001 — a bad batch must not wedge the sweep
            seen.update(c.key() for c in fresh)  # failed rows stay pending: slide past
            counters["embed_failures"] += len(fresh)
            await idx.mark_embed_failed(fresh, error=str(exc)[:500])
            h.logger.warning(
                "semantic_embed_fill_batch_failed",
                batch_size=len(fresh),
                error=str(exc)[:200],
            )
    h.logger.info("semantic_embed_fill_sweep", **counters)
    return counters
