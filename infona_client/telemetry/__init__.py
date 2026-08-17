"""Opt-in anonymous job telemetry (ONTA-548).

Disabled by default. ``INFONA_TELEMETRY=1`` turns it on; ``INFONA_TELEMETRY=0``
wins over a CLI consent file. Never phones home unless opted in. Fail-open.

This is *install signal* (which job, source-type mix) — not
:mod:`infona_client.analytics` (product/app events) and not
:mod:`infona_client.usage` (per-tenant metering).
"""

from infona_client.telemetry.client import record_job, reset_telemetry
from infona_client.telemetry.consent import is_enabled
from infona_client.telemetry.sanitize import ALLOWED_PAYLOAD_KEYS
from infona_client.telemetry.send import flush_telemetry, set_test_sink

__all__ = [
    "ALLOWED_PAYLOAD_KEYS",
    "flush_telemetry",
    "is_enabled",
    "record_job",
    "reset_telemetry",
    "set_test_sink",
]
