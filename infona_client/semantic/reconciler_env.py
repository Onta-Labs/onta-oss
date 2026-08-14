"""Env knobs and clocks for the semantic reconciler.

Read per call so tests/ops can tune without re-import. ``_now_monotonic`` is
monkeypatched on the public facade — call it via ``_host()``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from infona_client.semantic.reconciler_common import _host


def semantic_index_enabled() -> bool:
    """Master gate for the semantic index write path + reconciler (ONTA-181).

    Default **false**: indexing costs embedding spend and index growth, so a
    deployment opts in explicitly. Gates the ``kg_writer`` hook, both
    reconciler duties, the schedule seeding, and the reindex route.
    """
    raw = os.environ.get("INFONA_SEMANTIC_INDEX_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(float(raw))
    except ValueError:
        return default
    return val if val >= minimum else default


def embed_fill_interval_s() -> int:
    return _int_env("INFONA_SEMANTIC_EMBED_FILL_INTERVAL_S", 300)


def reconcile_interval_s() -> int:
    return _int_env("INFONA_SEMANTIC_RECONCILE_INTERVAL_S", 3600)


def embed_max_attempts() -> int:
    return _int_env("INFONA_SEMANTIC_EMBED_MAX_ATTEMPTS", 5)


def _scan_page_size() -> int:
    return _int_env("INFONA_SEMANTIC_SCAN_PAGE_SIZE", 10000)


def _ensure_memo_ttl_s() -> int:
    return _int_env("INFONA_SEMANTIC_ENSURE_MEMO_TTL_S", 600)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_monotonic() -> float:
    """Seam for the memo clock (monkeypatched in tests — no sleeps)."""
    import time

    return time.monotonic()


def _monotonic_now() -> float:
    """Read the (possibly patched) memo clock off the facade."""
    return _host()._now_monotonic()
