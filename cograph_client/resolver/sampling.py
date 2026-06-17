"""Representative sampling for CSV schema inference (COG-59).

Schema inference is grounded in a *sample* of the file (the profiler in
``profiler.py`` computes its statistics over whatever rows the client sends to
``/ingest/csv/schema``). When that sample is the **head** of the file —
``rows[:5000]`` then ``rows[:5]`` in the legacy client — any distinguishing
pattern that first appears later in the file is invisible to inference, so the
schema is decided from the head alone.

This module replaces head-truncation with a uniform **reservoir sample**
(Algorithm R) taken over the *entire* streamed file in a single pass. Every row
has equal probability of selection regardless of position, so a value block that
only appears after row 5000 is represented in proportion to its share of the
file — not dropped. The sample size stays bounded (token budget) while its
coverage is position-independent.

Pure stdlib + structlog; streams an iterator so the whole file is never
materialized. Deterministic for a fixed ``seed`` and input order, which is what
the COG-59 tests assert.
"""

from __future__ import annotations

import os
import random
from typing import Any, Iterable, Iterator, TypeVar

import structlog

logger = structlog.stdlib.get_logger("cograph.resolver.sampling")

T = TypeVar("T")

#: Default sample size for schema inference. The /ingest/csv/schema docstring
#: asks clients to send "the full file (capped at a few thousand rows)"; the
#: profiler's statistics converge well below this and the LLM only ever sees a
#: density-ranked handful of them (``_rank_sample_rows``).
DEFAULT_SAMPLE_SIZE = int(os.environ.get("OMNIX_CSV_SAMPLE_ROWS", "3000"))


def reservoir_sample(stream: Iterable[T], k: int, *, seed: int = 0) -> list[T]:
    """Uniform random sample of up to ``k`` items from a stream (Algorithm R).

    Single pass, O(1) extra memory beyond the reservoir, never materializes the
    stream. Each item of an N-item stream ends up in the result with probability
    ``min(1, k/N)``, independent of its position — so a sample is representative
    of the whole file, not just its head.

    Deterministic given ``seed`` and the input order. The relative order of the
    sampled items is not meaningful (downstream density-ranks them anyway).
    """
    if k <= 0:
        return []
    rng = random.Random(seed)
    reservoir: list[T] = []
    for i, item in enumerate(stream):
        if i < k:
            reservoir.append(item)
        else:
            # Inclusive 0..i; if it lands in the window, evict that slot.
            j = rng.randint(0, i)
            if j < k:
                reservoir[j] = item
    return reservoir


def sample_for_inference(
    rows: Iterable[dict[str, Any]],
    *,
    k: int = DEFAULT_SAMPLE_SIZE,
    seed: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Stream rows once → ``(sample_rows, total_rows)`` for ``/ingest/csv/schema``.

    ``sample_rows`` is a position-independent reservoir sample of size
    ``min(k, total_rows)``; ``total_rows`` is the true row count of the full
    file (so the profiler reports honest ``rows_profiled/total_rows`` coverage).
    Both come from a single streaming pass — the file is read once and never
    held in memory in full.
    """
    rng = random.Random(seed)
    reservoir: list[dict[str, Any]] = []
    total = 0
    for total, row in _enumerate1(rows):
        i = total - 1
        if i < k:
            reservoir.append(row)
        else:
            j = rng.randint(0, i)
            if j < k:
                reservoir[j] = row
    logger.info(
        "csv_reservoir_sampled",
        total_rows=total,
        sampled=len(reservoir),
        requested_k=k,
    )
    return reservoir, total


def _enumerate1(it: Iterable[T]) -> Iterator[tuple[int, T]]:
    """``enumerate`` starting at 1 — yields ``(count_so_far, item)`` so the
    final count equals the total without a trailing ``+1``."""
    n = 0
    for item in it:
        n += 1
        yield n, item
