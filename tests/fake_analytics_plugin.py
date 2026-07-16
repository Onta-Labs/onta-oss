"""Test fixture — an analytics plugin registered by test_analytics.

`register()` receives no arguments and registers a sink via the OSS seam,
mirroring how the proprietary PostHog sink registers itself at app boot through
the ``OMNIX_ANALYTICS_PLUGIN`` hook.
"""

from cograph_client.analytics import register_analytics_sink

LOADED = False
CAPTURED: list[dict] = []


class _FixtureSink:
    name = "fake-analytics"

    def capture(self, *, event, distinct_id, properties, exc_info=None):
        CAPTURED.append(
            {
                "event": event,
                "distinct_id": distinct_id,
                "properties": dict(properties),
                "exc_info": exc_info,
            }
        )

    def flush(self):  # pragma: no cover - not exercised by the loader test
        pass


def register():
    global LOADED
    LOADED = True
    register_analytics_sink(_FixtureSink())
