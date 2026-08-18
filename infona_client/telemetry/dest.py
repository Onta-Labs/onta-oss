"""Default OSS telemetry destination (ONTA-548).

The PostHog *project* token is write-only and designed to live in clients
(same class as a browser snippet key). It is not a personal key, not a
cloud-account secret, and not the Infona Webapp (Cloud) project.

Operators override with ``INFONA_TELEMETRY_URL`` / ``INFONA_TELEMETRY_KEY``.
``INFONA_TELEMETRY_URL=off`` disables HTTP even when opted in.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

URL_ENV = "INFONA_TELEMETRY_URL"
KEY_ENV = "INFONA_TELEMETRY_KEY"

DEFAULT_CAPTURE_URL = "https://us.i.posthog.com/i/v0/e/"

# Public write-only Infona-oss project token (PostHog project 563235).
# gitleaks:allow — project write key, intended for client-side capture
DEFAULT_PROJECT_KEY = "phc_B6ZuznkkXJizHmgEe9mh6gwBG4Py9A9hXN2EoLK82pid"

_OFF = frozenset({"0", "off", "none", "false"})


def configured_url() -> str:
    """HTTPS destination. Unset → Infona-oss PostHog. ``off`` → no HTTP."""
    raw = os.environ.get(URL_ENV)
    if raw is None:
        return DEFAULT_CAPTURE_URL
    value = raw.strip()
    if not value or value.lower() in _OFF:
        return ""
    return value


def project_key() -> str:
    override = os.environ.get(KEY_ENV, "").strip()
    return override or DEFAULT_PROJECT_KEY


def uses_posthog_envelope(url: str) -> bool:
    """Wrap only PostHog hosts so a custom ``/capture`` collector stays raw."""
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    return host == "posthog.com" or host.endswith(".posthog.com")


def wrap_posthog(payload: dict[str, Any]) -> dict[str, Any]:
    """Map an allowlisted job event onto PostHog's capture envelope."""
    props = {k: v for k, v in payload.items() if k != "event"}
    props["$lib"] = "infona-oss-telemetry"
    props["$process_person_profile"] = False
    return {
        "api_key": project_key(),
        "event": payload.get("event") or "job",
        "distinct_id": str(payload.get("install_id") or "anonymous"),
        "properties": props,
    }
