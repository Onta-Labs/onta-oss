"""Product-analytics seam (ONTA-323) — the OSS half of backend analytics.

The emit/registration seam lives in :mod:`infona_client.analytics.sink`. OSS
defines only the protocol + a no-op default; the real hosted-analytics sink is
proprietary and registers over this seam via ``INFONA_ANALYTICS_PLUGIN`` at app
boot (mirroring ``INFONA_AUTH_PLUGIN`` / ``INFONA_ENRICHMENT_PLUGIN`` / …).

Boundary note: per-tenant usage *metering* is OSS ("table-stakes observability",
:mod:`infona_client.usage`); *analytics that phones home to a SaaS with our
project token is proprietary* — so no third-party analytics dependency ever
appears under ``infona_client/``. See docs/oss_proprietary_boundary.md
(ONTA-323) and the analytics-hub design spec (§4–5) in the proprietary repo.
"""

from infona_client.analytics.sink import (
    AnalyticsSink,
    ExcInfo,
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
    "ExcInfo",
    "NoOpSink",
    "distinct_id_for",
    "emit",
    "flush_analytics",
    "get_analytics_sink",
    "register_analytics_sink",
    "reset_analytics_sink",
]
