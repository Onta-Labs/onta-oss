"""Fail-open delivery (ONTA-548).

Sinks (first match):

* a test sink registered via :func:`set_test_sink`
* ``INFONA_TELEMETRY_SINK=stderr`` — one JSON object on stderr
* ``INFONA_TELEMETRY_SINK=file`` — append JSONL to ``INFONA_TELEMETRY_FILE``
  (default ``~/.infona/telemetry.jsonl``)
* otherwise HTTPS POST to ``INFONA_TELEMETRY_URL`` (stdlib ``urllib``, 2s
  timeout, background thread). No URL → no network.

Telemetry errors never propagate. No page-fetcher registration.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

URL_ENV = "INFONA_TELEMETRY_URL"
SINK_ENV = "INFONA_TELEMETRY_SINK"
FILE_ENV = "INFONA_TELEMETRY_FILE"
SYNC_ENV = "INFONA_TELEMETRY_SYNC"
TIMEOUT_SEC = 2.0

Sink = Callable[[dict[str, Any]], None]

_test_sink: Optional[Sink] = None
_lock = threading.Lock()
_inflight: list[threading.Thread] = []


def set_test_sink(sink: Optional[Sink]) -> None:
    """Tests only — capture payloads in-process. ``None`` clears."""
    global _test_sink
    _test_sink = sink


def reset_send() -> None:
    global _test_sink
    _test_sink = None
    with _lock:
        _inflight.clear()


def configured_url() -> str:
    return os.environ.get(URL_ENV, "").strip()


def _file_path() -> Path:
    override = os.environ.get(FILE_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".infona" / "telemetry.jsonl"


def _write_stderr(payload: dict[str, Any]) -> None:
    sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")


def _write_file(payload: dict[str, Any]) -> None:
    path = _file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, separators=(",", ":")) + "\n")


def _post_http(payload: dict[str, Any]) -> None:
    url = configured_url()
    if not url:
        return
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "infona-oss-telemetry/1",
        },
    )
    with urlopen(req, timeout=TIMEOUT_SEC) as resp:  # noqa: S310 — operator URL
        resp.read()


def _deliver(payload: dict[str, Any]) -> None:
    try:
        if _test_sink is not None:
            _test_sink(payload)
            return
        sink = os.environ.get(SINK_ENV, "").strip().lower()
        if sink == "stderr":
            _write_stderr(payload)
            return
        if sink == "file":
            _write_file(payload)
            return
        if configured_url():
            _post_http(payload)
    except (OSError, URLError, TimeoutError, ValueError):
        return
    except Exception:  # noqa: BLE001 — fail-open
        return


def dispatch(payload: dict[str, Any]) -> None:
    """Hand ``payload`` to the active sink. Never raises."""
    if _test_sink is not None or os.environ.get(SYNC_ENV, "").strip() == "1":
        _deliver(payload)
        return
    sink = os.environ.get(SINK_ENV, "").strip().lower()
    if sink in {"stderr", "file"}:
        _deliver(payload)
        return
    if not configured_url():
        return
    thread = threading.Thread(
        target=_deliver, args=(payload,), name="infona-telemetry", daemon=True
    )
    with _lock:
        _inflight.append(thread)
    thread.start()


def flush_telemetry(timeout: float = 2.0) -> None:
    """Best-effort join of in-flight HTTP posts. Never raises."""
    with _lock:
        pending = list(_inflight)
        _inflight.clear()
    for thread in pending:
        try:
            thread.join(timeout=timeout)
        except Exception:  # noqa: BLE001
            return
