"""Unique-count grain: type-scan DISTINCT is only ok when unique-noun is the type.

``how many unique gadgets`` + ``entities_of_type_count`` is honest.
``how many unique vendor_code values among gadgets`` + type-scan is the
wrong grain — use ``literal_distinct_count``. No eval lexicon.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from infona_client.nlp.query_constraint_coverage_types import CoverageResult
from infona_client.nlp.query_intent import QueryIntentSketch

_UNIQUE_HEAD_RE = re.compile(r"(?ix)\b(?:unique|distinct)\s+([a-z][a-z0-9_]*)")
_TYPE_SCAN_UNIQUE = frozenset(
    {
        "entities_of_type",
        "entities_of_type_count",
        "entity_count_total",
        "entity_count_by_type",
    }
)


def _stem(word: str) -> str:
    s = (word or "").lower().replace("_", "")
    if s.endswith("ies") and len(s) > 3:
        return s[:-3] + "y"
    if s.endswith("s") and not s.endswith("ss") and len(s) > 3:
        return s[:-1]
    return s


def unique_noun(question: str) -> str:
    m = _UNIQUE_HEAD_RE.search(question or "")
    return (m.group(1) if m else "").lower()


def unique_noun_matches_type(noun: str, type_names: Sequence[str] | None) -> bool:
    if not noun:
        return False
    n = _stem(noun)
    for raw in type_names or ():
        t = _stem(str(raw))
        if t and (t == n or t in n or n in t):
            return True
    return False


def unique_count_wrong_grain(
    sketch: QueryIntentSketch,
    template: str | None,
    params: Mapping[str, Any] | None = None,
) -> CoverageResult | None:
    """Fail-close unique-of-a-leaf answered as unique-of-the-container-type."""
    if not getattr(sketch, "has_unique_count_intent", False):
        return None
    tmpl = (template or "").strip()
    if tmpl not in _TYPE_SCAN_UNIQUE:
        return None
    head = unique_noun(sketch.question)
    types = list((params or {}).get("type_names") or ())
    if unique_noun_matches_type(head, types):
        return None
    return CoverageResult(
        ok=False,
        confidence="low",
        reason=(
            "unique/distinct count of a value that is not the scanned type — "
            "use literal_distinct_count with $prop_key from schema (or "
            "count(DISTINCT <leaf>)), not a type-scan DISTINCT entity"
        ),
        fail_closed=True,
        sketch=sketch,
        extra={"unique_noun": head, "unique_twin": "literal_distinct_count"},
    )
