"""Opt-in gate + anonymous install id (ONTA-548).

Precedence:
  1. ``INFONA_TELEMETRY=0`` / ``false`` / ``off`` / ``no`` → off (always wins)
  2. ``INFONA_TELEMETRY=1`` / ``true`` / ``on`` / ``yes`` → on
  3. ``~/.infona/telemetry.json`` ``opt_in: true`` (CLI first-run consent)
  4. otherwise off

Under pytest the consent file is ignored so a developer's local opt-in cannot
leak a network call from the hermetic suite. Tests that need the on-path set
``INFONA_TELEMETRY=1``.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Optional

ENV_NAME = "INFONA_TELEMETRY"
STATE_ENV = "INFONA_TELEMETRY_STATE"
USE_CASE_ENV = "INFONA_TELEMETRY_USE_CASE"

_OFF = frozenset({"0", "false", "off", "no"})
_ON = frozenset({"1", "true", "on", "yes"})
USE_CASES = frozenset({"research", "ops", "product", "other"})

_state_cache: Optional[dict[str, Any]] = None


def state_path() -> Path:
    override = os.environ.get(STATE_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".infona" / "telemetry.json"


def env_override() -> Optional[bool]:
    """Explicit env decision, or ``None`` if unset / unrecognized."""
    raw = os.environ.get(ENV_NAME, "").strip().lower()
    if raw in _OFF:
        return False
    if raw in _ON:
        return True
    return None


def _read_state_file() -> dict[str, Any]:
    path = state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_state() -> dict[str, Any]:
    global _state_cache
    if _state_cache is None:
        _state_cache = _read_state_file()
    return _state_cache


def save_state(state: dict[str, Any]) -> None:
    global _state_cache
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        return
    _state_cache = dict(state)


def consent_file_opt_in() -> bool:
    return load_state().get("opt_in") is True


def is_enabled() -> bool:
    forced = env_override()
    if forced is False:
        return False
    if forced is True:
        return True
    # Never honor a leftover ~/.infona consent file inside pytest.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return consent_file_opt_in()


def install_id() -> str:
    """Stable anonymous id for this install. Not a tenant / user id."""
    state = load_state()
    existing = state.get("install_id")
    if isinstance(existing, str) and existing:
        return existing
    minted = str(uuid.uuid4())
    next_state = dict(state)
    next_state["install_id"] = minted
    save_state(next_state)
    return minted


def declared_use_case() -> Optional[str]:
    raw = os.environ.get(USE_CASE_ENV, "").strip().lower()
    return raw if raw in USE_CASES else None


def reset_consent_cache() -> None:
    global _state_cache
    _state_cache = None
