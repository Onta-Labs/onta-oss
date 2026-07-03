"""The ONE cost seam every retrieval rail reads (ONTA-193 P2).

Discovery, enrichment, and research each grew their own byte-for-byte identical
cost reducer — ``provider_cost`` / ``adapter_cost`` / ``fetcher_cost`` — so a plan
card pricing a paid call had to know which rail produced the source. They now all
delegate to :func:`source_cost` here; the three named functions remain as thin
back-compat aliases (imported in many places) and will deprecate.

A "source" is anything with the optional cost attributes ``is_paid`` /
``cost_per_call`` — a ``WebSourceProvider``, a ``SourceAdapter``, a
``PageFetcher``, or a future unified ``RetrievalSource``. Cost is read
defensively (``getattr`` with free defaults) so a source that declares nothing is
free and a malformed ``cost_per_call`` never raises.

Boundary: OSS. Imports only stdlib.
"""

from __future__ import annotations

from typing import Any


def source_cost(source: Any) -> tuple[bool, float]:
    """Read a retrieval source's cost signal generically → ``(is_paid, cost_per_call)``.

    Defensive ``getattr`` with free defaults; never raises on a malformed
    ``cost_per_call``. A positive cost implies paid even when ``is_paid`` is unset;
    a negative cost is clamped to 0.
    """
    try:
        cost = float(getattr(source, "cost_per_call", 0.0) or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost < 0.0:
        cost = 0.0
    is_paid = bool(getattr(source, "is_paid", False)) or cost > 0.0
    return is_paid, cost


def rows_per_call(source: Any) -> int:
    """Records returned per ONE paid request (discovery pagination pricing).

    ``0`` (the default) means "one billed call per run" — the caller prices a
    single call rather than ``ceil(rows / rows_per_call)``. Defensive: a malformed
    value degrades to 0.
    """
    try:
        n = int(getattr(source, "rows_per_call", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


__all__ = ["rows_per_call", "source_cost"]
