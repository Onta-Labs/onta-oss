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
from infona_client.nlp.query_intent import extract_filter_tokens

# Underspecified count heads — not domain types. Keep tight: do not fire on
# "data"/"stuff"/"ones" inside other questions (SUM/AVG/filtered counts).
_VAGUE_COUNT_NOUN_RE = re.compile(
    r"(?ix)\b(?:records?|rows?|items?|things?|entities|entries)\b"
)

_HOW_MANY_RE = re.compile(r"(?ix)\b(?:how\s+many|count|number\s+of)\b")

# Filter / compare cues: these are not bare "which type?" asks.
_CONSTRAINT_RE = re.compile(
    r"(?ix)\b(?:"
    r"under|over|above|below|less|greater|at\s+least|at\s+most|"
    r"where|with|having|status|named|equals?|ready"
    r")\b|[<>=]"
)

# Follow-up asks whose referent is ONLY a prior turn. Bare ``we`` / ``their``
# is not enough: "list companies and their revenue" / "how many do we have"
# bind in the same sentence.
_FOLLOWUP_RE = re.compile(
    r"(?ix)(?:"
    r"\bwho\s+else\b|"
    r"\bwhat\s+about\s+(?:them|him|her|that|it)\b|"
    r"\b(?:this|that|those|these)\s+(?:one|entity|record|item|event)\b|"
    r"\bthe\s+last\s+one\b|"
    r"\bwhat\s+did\s+we\s+(?:talk|discuss|cover|say)\b|"
    r"\bwhat\s+were\s+their\b|"
    r"\b(?:when|where)\s+was\s+(?:that|this)\b|"
    r"\bthey\s+were\s+(?:met|seen|held)\b"
    r")"
)
_INTRA_SENTENTIAL_RE = re.compile(
    r"(?ix)(?:"
    r"\b[A-Za-z][A-Za-z0-9_]*\s+and\s+their\b|"
    r"\b(?:do|did|can|have|are)\s+we\s+(?:have|got|need)\b|"
    r"\bhow\s+many\b[\s\S]{0,40}\bwe\b"
    r")"
)


def question_is_vague_count(question: str) -> bool:
    """True for *bare* underspecified cardinality asks (records/rows/items)."""
    q = (question or "").strip()
    if not q:
        return False
    if not _HOW_MANY_RE.search(q):
        return False
    if not _VAGUE_COUNT_NOUN_RE.search(q):
        return False
    if _CONSTRAINT_RE.search(q):
        return False
    return True


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
    if extract_filter_tokens(question):
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


def question_is_anaphoric(question: str) -> bool:
    """True when the ask is a follow-up whose referent is a prior turn."""
    q = (question or "").strip()
    if not q:
        return False
    if _INTRA_SENTENTIAL_RE.search(q):
        return False
    return bool(_FOLLOWUP_RE.search(q))


def conversation_has_prior_turn(conversation: object | None) -> bool:
    """True when the caller supplied at least one prior user or assistant turn."""
    if not conversation:
        return False
    try:
        return len(list(conversation)) > 0
    except TypeError:
        return False


def ambiguous_anaphora_needs_clarify(
    question: str,
    conversation: object | None = None,
) -> bool:
    """Clarify pronoun follow-ups that have no prior turn to bind against.

    With conversation history the Cypher planner resolves the referent.
    Without it, executing the ask as a new unfiltered type-scan dumps the
    whole graph and the 8B rephraser covers it with a fluent wrong story.
    """
    if conversation_has_prior_turn(conversation):
        return False
    if not question_is_anaphoric(question):
        return False
    if extract_filter_tokens(question):
        return False
    return True


def format_anaphora_clarification() -> str:
    """User-facing clarify for an unbound follow-up."""
    return (
        "Which entity do you mean? Name it in this question, or ask as a "
        "follow-up in the same thread so I can use the previous turn."
    )


def format_conversation_for_prompt(conversation: object | None) -> str:
    """Render prior turns for the Cypher planner. Empty when there is no history.

    The model may use this block ONLY to resolve pronouns / 'that meeting'.
    A fully specified current question must not inherit prior filters.
    """
    turns = list(conversation or [])
    if not turns:
        return ""
    lines = [
        "This question is a FOLLOW-UP to the prior turn. Resolve we / they / "
        "that / the last to the entities named below. Do not switch to a "
        "different person or meeting."
    ]
    for t in turns[-8:]:
        if isinstance(t, dict):
            role = str(t.get("role") or "user").strip().lower() or "user"
            text = str(t.get("text") or "").strip()
        else:
            role = str(getattr(t, "role", "user") or "user").strip().lower() or "user"
            text = str(getattr(t, "text", "") or "").strip()
        if not text:
            continue
        if role not in ("user", "assistant"):
            role = "user"
        # Bound each turn so a long prior answer cannot drown the live question.
        if len(text) > 600:
            text = text[:600].rstrip() + "…"
        lines.append(f"{role}: {text}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


__all__ = [
    "ambiguous_anaphora_needs_clarify",
    "ambiguous_count_needs_clarify",
    "conversation_has_prior_turn",
    "format_anaphora_clarification",
    "format_conversation_for_prompt",
    "format_type_count_clarification",
    "question_is_anaphoric",
    "question_is_vague_count",
]
