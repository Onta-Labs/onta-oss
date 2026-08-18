"""Public ``record_job`` entry (ONTA-548). Fire-and-forget, fail-open."""

from __future__ import annotations

from typing import Any, Optional

from infona_client.telemetry.consent import (
    declared_use_case,
    install_id,
    is_enabled,
    reset_consent_cache,
)
from infona_client.telemetry.sanitize import build_payload
from infona_client.telemetry.send import dispatch, reset_send


def record_job(
    job_type: str,
    *,
    row_count: Optional[int] = None,
    source_type: Any = None,
    error: Any = None,
) -> None:
    """Record one opted-in job event. No-op when telemetry is off. Never raises.

    ``source_type`` may be a connector token (``csv`` / ``json`` / …) or a job
    object with a ``platforms`` list (``file:csv`` → ``csv``). Filenames are
    stripped. ``error`` may be an exception, HTTP status, or ``True`` (use
    ``sys.exc_info()``); message text is never sent.
    """
    try:
        if not is_enabled():
            return
        payload = build_payload(
            install_id=install_id(),
            job_type=job_type,
            row_count=row_count,
            source_type=source_type,
            error=error,
            use_case=declared_use_case(),
        )
        if payload is None:
            return
        dispatch(payload)
    except Exception:  # noqa: BLE001 — telemetry must never fail a user job
        return


def reset_telemetry() -> None:
    """Test helper — drop cached consent + the in-process sink."""
    reset_consent_cache()
    reset_send()
