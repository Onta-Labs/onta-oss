"""Lightweight, dependency-free timing observability (ONTA-198 follow-up).

``timed(logger, stage, **fields)`` is an async context manager that emits ONE
``stage_timing`` structured log carrying the wrapped block's wall-clock
``duration_ms``. Pure observability — it never changes control flow, and a
failure inside the block still logs the elapsed time (the ``finally``) before the
exception propagates.

Why: a discovery / ingest run is a chain of LLM + SPARQL round-trips (classify →
spec-resolve → registry-route → extract → type-resolve → insert). The httpx
client logs *that* a call happened but not which stage it was or how long it
took, so profiling a slow run meant hand-reconstructing per-call latency from the
gaps between CloudWatch request lines. ``stage_timing`` makes every run
self-profiling: filter the log group for ``stage_timing`` and read the breakdown
directly.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any


@asynccontextmanager
async def timed(logger: Any, stage: str, **fields: Any):
    t0 = time.monotonic()
    try:
        yield
    finally:
        logger.info(
            "stage_timing",
            stage=stage,
            duration_ms=round((time.monotonic() - t0) * 1000, 1),
            **fields,
        )
