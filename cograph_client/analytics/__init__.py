"""Product-analytics seam (ONTA-323) — the OSS half of backend analytics.

The emit/registration seam lives in :mod:`cograph_client.analytics.sink`. OSS
defines only the protocol + a no-op default; the real hosted-analytics sink is
proprietary and registers over this seam via ``OMNIX_ANALYTICS_PLUGIN`` at app
boot (mirroring ``OMNIX_AUTH_PLUGIN`` / ``OMNIX_ENRICHMENT_PLUGIN`` / …).

Boundary note: per-tenant usage *metering* is OSS ("table-stakes observability",
:mod:`cograph_client.usage`); *analytics that phones home to a SaaS with our
project token is proprietary* — so no third-party analytics dependency ever
appears under ``cograph_client/``. See docs/oss_proprietary_boundary.md
(ONTA-323) and the analytics-hub design spec (§4–5) in the proprietary repo.
"""

from cograph_client.analytics.sink import (
    AnalyticsSink,
    NoOpSink,
    distinct_id_for,
    emit,
    flush_analytics,
    get_analytics_sink,
    register_analytics_sink,
    reset_analytics_sink,
)

__all__ = [
    "AnalyticsSink",
    "NoOpSink",
    "distinct_id_for",
    "emit",
    "flush_analytics",
    "get_analytics_sink",
    "register_analytics_sink",
    "reset_analytics_sink",
]
