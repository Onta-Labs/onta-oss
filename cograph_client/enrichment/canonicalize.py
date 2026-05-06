"""Minimal canonicalizers used by the strategy-driven enrichment merge.

A canonicalizer is a string -> string transform applied to attribute values
before conflict comparison. Unknown canonicalizer names return the value
unchanged so missing strategies never break enrichment.
"""
from __future__ import annotations

from typing import Callable


def _title_case(value: str) -> str:
    if not value:
        return value
    # Title-case but preserve common ampersand spacing like "K&N".
    parts = value.strip().lower().split()
    titled = []
    for p in parts:
        if "&" in p:
            # Treat each side of the ampersand independently, upper-case
            # short tokens (<=2 chars) entirely (KN, AT&T-style).
            sides = p.split("&")
            sides = [s.upper() if len(s) <= 2 and s else s.title() for s in sides]
            titled.append("&".join(sides))
        else:
            titled.append(p.title())
    return " ".join(titled)


def _trim(value: str) -> str:
    return value.strip() if value else value


_REGISTRY: dict[str, Callable[[str], str]] = {
    "title-case": _title_case,
    "trim": _trim,
}


def apply_canonicalizer(name: str | None, value: str) -> str:
    """Apply the named canonicalizer to value. Unknown name -> value unchanged."""
    if not name or value is None:
        return value
    fn = _REGISTRY.get(name)
    if fn is None:
        return value
    try:
        return fn(value)
    except Exception:  # noqa: BLE001
        return value
