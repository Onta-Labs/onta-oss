"""Mandatory short descriptions for ontology types / attributes / relationships.

When the catalog expands, every OntoType and OntoAttr must carry a non-empty
short description and a UTC date of when that description was last set
(``description_updated_at``, ISO ``YYYY-MM-DD``).

Callers may supply a description; empty input is filled with a deterministic
humanized default derived from the leaf name (and datatype / range for attrs).
No LLM required — works offline and stays anti-overfit (no domain hardcodes).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

Kind = Literal["type", "literal", "relationship"]

_SNAKE_SPLIT = re.compile(r"[_\s]+")
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def utc_description_date(now: datetime | None = None) -> str:
    """UTC calendar date ``YYYY-MM-DD`` for ``description_updated_at``."""
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date().isoformat()


def humanize_leaf(name: str) -> str:
    """``unit_cost`` / ``UnitCost`` / ``SynthWidget`` → readable phrase."""
    raw = (name or "").strip()
    if not raw:
        return "unnamed"
    # snake / kebab first, then camel
    parts = _SNAKE_SPLIT.split(raw.replace("-", "_"))
    words: list[str] = []
    for p in parts:
        if not p:
            continue
        words.extend(_CAMEL_SPLIT.split(p))
    cleaned = [w for w in words if w]
    if not cleaned:
        return raw
    return " ".join(cleaned).lower()


def default_short_description(
    name: str,
    *,
    kind: Kind,
    domain: str | None = None,
    datatype: str | None = None,
    range_type: str | None = None,
) -> str:
    """Deterministic one-line description for a catalog leaf."""
    label = humanize_leaf(name)
    if kind == "type":
        # Capitalize first letter for entity types
        pretty = label[:1].upper() + label[1:] if label else name
        return f"Entity type for {pretty}."
    if kind == "relationship":
        dom = humanize_leaf(domain or "entity")
        rng = humanize_leaf(range_type or "related entity")
        return f"Links {dom} to {rng} ({name})."
    # literal attribute
    dom = humanize_leaf(domain or "entity")
    dt = (datatype or "string").strip() or "string"
    return f"{label[:1].upper() + label[1:] if label else name} ({dt}) on {dom}."


def ensure_description(
    name: str,
    description: str | None = "",
    *,
    kind: Kind,
    domain: str | None = None,
    datatype: str | None = None,
    range_type: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Return ``(description, description_updated_at)`` never empty for description.

    * Non-empty ``description`` is stripped and kept; date stamped **now**.
    * Empty / whitespace → :func:`default_short_description`; date stamped **now**.
    """
    text = (description or "").strip()
    if not text:
        text = default_short_description(
            name,
            kind=kind,
            domain=domain,
            datatype=datatype,
            range_type=range_type,
        )
    return text, utc_description_date(now)


__all__ = [
    "default_short_description",
    "ensure_description",
    "humanize_leaf",
    "utc_description_date",
]
