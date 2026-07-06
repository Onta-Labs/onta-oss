"""In-process usage recorder fed by the request-logging middleware.

Every tenant-scoped API request (``/graphs/{tenant}/…``) is classified into a
daily :class:`~cograph_client.usage.store.UsageBucket` — tenant, UTC day, the
KG when it appears in the path, a non-secret last-4 hint of the API key, and a
coarse route class — and buffered in process. The buffer flushes to the
configured :class:`UsageStore` opportunistically (every few seconds of
traffic, or when it grows large) plus once on app shutdown, so the hot path
never awaits the database.

Recording is strictly best-effort: a metering failure must never fail or slow
a request, so every entry point swallows and logs its own errors.

What counts: every authenticated tenant-scoped request, including the
Explorer's own reads — the route-class breakdown is what keeps that
inspectable. 401/403 responses are skipped so unauthenticated probes can't
inflate (or attribute traffic to) a tenant they never authenticated for.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Optional

import structlog

from cograph_client.usage.store import UsageBucket, UsageStore, get_usage_store

logger = structlog.stdlib.get_logger("cograph.usage")

# Seconds of traffic between opportunistic flushes, and the buffer size that
# forces one regardless. Tuned for "cheap", not "real-time": the usage panel
# reads daily buckets, so a few seconds of lag is invisible.
FLUSH_INTERVAL_S = 5.0
FLUSH_MAX_BUCKETS = 500

_GRAPHS_RE = re.compile(r"^/graphs/([^/]+)(/.*)?$")
_KG_RE = re.compile(r"^/(?:explore/)?kgs/([^/]+)")

# First path segment after /graphs/{tenant} → route class. Fixed, small
# cardinality by construction: anything unlisted lands in "other".
_ROUTE_CLASSES = {
    "ask": "ask",
    "agent": "agent",
    "agent-chat": "agent",
    "query": "query",
    "search": "search",
    "explore": "explore",
    "ingest": "ingest",
    "enrich": "enrich",
    "normalize": "normalize",
    "kgs": "kgs",
    "jobs": "jobs",
    "schedules": "jobs",
    "actions": "actions",
    "ontology": "ontology",
    "usage": "usage",
}

# Route classes that mean "the tenant queried the KG through the API" — the
# signal the dashboard's setup checklist reads, as opposed to the Explorer's
# own browsing traffic (explore/kgs/jobs/...).
QUERY_ROUTE_CLASSES = frozenset({"ask", "agent", "query", "search"})


def classify_request(path: str) -> Optional[tuple[str, str, str]]:
    """Map a request path to ``(tenant, kg_name, route_class)``.

    Returns ``None`` for paths that aren't tenant-scoped API traffic
    (``/health``, ``/v1/…``, docs). ``kg_name`` is ``''`` when the path isn't
    scoped to one KG (e.g. ``/ask``, ``/jobs``).
    """
    m = _GRAPHS_RE.match(path)
    if not m:
        return None
    tenant, rest = m.group(1), m.group(2) or ""
    segment = rest.lstrip("/").split("/", 1)[0]
    route_class = _ROUTE_CLASSES.get(segment, "other")
    kg = ""
    kg_m = _KG_RE.match(rest)
    if kg_m:
        kg = kg_m.group(1)
    return tenant, kg, route_class


def key_hint(api_key: Optional[str]) -> str:
    """Non-secret identifier for a key: its last 4 chars ('' if no key)."""
    if not api_key:
        return ""
    return api_key[-4:]


class UsageRecorder:
    """Buffers per-request observations and flushes them in batches."""

    def __init__(self, store: Optional[UsageStore] = None) -> None:
        # Resolved lazily so tests can reset the store singleton after
        # constructing the recorder.
        self._store = store
        self._pending: dict[tuple[str, str, str, str, str], list[float]] = {}
        self._last_flush = time.monotonic()
        self._flushing = False

    def observe(
        self,
        path: str,
        method: str,
        status: int,
        duration_ms: float,
        api_key: Optional[str],
    ) -> None:
        """Record one finished request. Sync + non-blocking; never raises."""
        try:
            if method == "OPTIONS" or status in (401, 403):
                return
            classified = classify_request(path)
            if classified is None:
                return
            tenant, kg, route_class = classified
            day = datetime.now(timezone.utc).date().isoformat()
            key = (tenant, day, kg, key_hint(api_key), route_class)
            counters = self._pending.setdefault(key, [0, 0, 0.0])
            counters[0] += 1
            if status >= 400:
                counters[1] += 1
            counters[2] += duration_ms
            self._maybe_schedule_flush()
        except Exception:  # noqa: BLE001 - metering must never break a request
            logger.exception("usage_observe_failed")

    def _maybe_schedule_flush(self) -> None:
        due = (
            time.monotonic() - self._last_flush >= FLUSH_INTERVAL_S
            or len(self._pending) >= FLUSH_MAX_BUCKETS
        )
        if not due or self._flushing or not self._pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.flush())

    async def flush(self) -> None:
        """Write the buffered increments to the store. Never raises."""
        if self._flushing or not self._pending:
            return
        self._flushing = True
        pending, self._pending = self._pending, {}
        try:
            buckets = [
                UsageBucket(
                    tenant_id=t,
                    day=day,
                    kg_name=kg,
                    api_key_hint=hint,
                    route_class=cls,
                    requests=int(c[0]),
                    errors=int(c[1]),
                    duration_ms_sum=c[2],
                )
                for (t, day, kg, hint, cls), c in pending.items()
            ]
            store = self._store if self._store is not None else get_usage_store()
            await store.add(buckets)
        except Exception:  # noqa: BLE001 - re-buffer so a store hiccup drops nothing
            logger.exception("usage_flush_failed", buckets=len(pending))
            for key, c in pending.items():
                counters = self._pending.setdefault(key, [0, 0, 0.0])
                counters[0] += c[0]
                counters[1] += c[1]
                counters[2] += c[2]
        finally:
            self._last_flush = time.monotonic()
            self._flushing = False


_recorder: Optional[UsageRecorder] = None


def get_usage_recorder() -> UsageRecorder:
    """Process-wide recorder singleton (shared by middleware + lifespan)."""
    global _recorder
    if _recorder is None:
        _recorder = UsageRecorder()
    return _recorder


def reset_usage_recorder() -> None:
    """Test helper — clear the singleton."""
    global _recorder
    _recorder = None
