"""Ambiguous NL asks — prefer clarify over guessing a type/count.

Product rule: if the user says “how many records/rows/items?” and this KG has
**more than one populated type**, do not pick a number. Ask which type they mean.

Anti-overfit: no shop / ListPrice / persona strings. Vague nouns are generic
English. Type hits reuse :func:`match_question_types` (name/plural only).
"""

from __future__ import annotations

import re
from typing import Sequence

from infona_client.nlp.query_build import TypePopulation, match_question_types
from infona_client.nlp.query_intent import question_has_aggregate_intent

# Underspecified count heads — not domain types.
_VAGUE_COUNT_NOUN_RE = re.compile(
    r"(?ix)\b(?:"
    r"records?|rows?|items?|things?|entities|entries|"
    r"ones?|objects?|nodes?|data|stuff"
    r")\b"
)

_HOW_MANY_RE = re.compile(r"(?ix)\b(?:how\s+many|count|number\s+of|total\s+number)\b")


def question_is_vague_count(question: str) -> bool:
    """True for underspecified cardinality asks (records/rows/items…)."""
    q = (question or "").strip()
    if not q:
        return False
    if not (_HOW_MANY_RE.search(q) or question_has_aggregate_intent(q)):
        return False
    return bool(_VAGUE_COUNT_NOUN_RE.search(q))


def ambiguous_count_needs_clarify(
    question: str,
    populated: Sequence[TypePopulation],
) -> bool:
    """Clarify when count is vague and ≥2 live types, none named in the question."""
    pops = [t for t in (populated or ()) if getattr(t, "entity_count", 0) > 0]
    if len(pops) < 2:
        return False
    if not question_is_vague_count(question):
        return False
    hits = match_question_types(question, pops)
    return len(hits) == 0


def format_type_count_clarification(
    populated: Sequence[TypePopulation],
    *,
    max_types: int = 8,
) -> str:
    """User-facing clarify: list live types + counts; do not guess."""
    pops = sorted(
        [t for t in (populated or ()) if getattr(t, "entity_count", 0) > 0],
        key=lambda t: (-t.entity_count, t.name.lower()),
    )
    if not pops:
        return (
            "Which type should I count? This knowledge graph has no populated "
            "types I can see."
        )
    lines = [
        "What do you mean by that count? This knowledge graph has more than "
        "one populated type. Say which type (or say you want the total of all "
        "nodes):"
    ]
    for t in pops[:max_types]:
        lines.append(f"- {t.name}: {t.entity_count}")
    extra = len(pops) - max_types
    if extra > 0:
        lines.append(f"- … and {extra} more")
    return "\n".join(lines)


__all__ = [
    "ambiguous_count_needs_clarify",
    "format_type_count_clarification",
    "question_is_vague_count",
]
