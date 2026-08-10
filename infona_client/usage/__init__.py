"""Per-tenant API usage metering (requests / errors / latency).

The recording half lives in :mod:`infona_client.usage.recorder` (fed by the
request-logging middleware); the storage half in
:mod:`infona_client.usage.store` (in-memory or Postgres, mirroring the
jobs / kg-stats store pattern). The read side is the canonical
``GET /graphs/{tenant}/usage`` route in ``api/routes/usage.py``.

Boundary note: usage *metering* (counting what a tenant's keys did) is OSS —
table-stakes observability for any self-hosted deployment. Quota / plan /
billing *enforcement* on top of these numbers stays a proprietary concern.
"""

from infona_client.usage.recorder import UsageRecorder, get_usage_recorder
from infona_client.usage.store import (
    InMemoryUsageStore,
    PostgresUsageStore,
    UsageBucket,
    UsageStore,
    get_usage_store,
    reset_usage_store,
)

__all__ = [
    "InMemoryUsageStore",
    "PostgresUsageStore",
    "UsageBucket",
    "UsageRecorder",
    "UsageStore",
    "get_usage_recorder",
    "get_usage_store",
    "reset_usage_store",
]
