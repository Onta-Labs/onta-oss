"""NL query intent sketch for constraint coverage (filter-miss class).

Pure, deterministic helpers used after Cypher generation to decide whether the
plan covers the question's constraints. No ontology, no LLM, no persona gold.

Token extract/collapse live in :mod:`infona_client.nlp.query_intent_tokens`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from infona_client.nlp.cypher_filter_integrity import question_has_filter_intent
from infona_client.nlp.query_intent_tokens import (
    collapse_filter_tokens,
    extract_filter_tokens,
    extract_measure_prop_candidates,
)

_AGGREGATE_RE = re.compile(
    r"(?ix)\b(?:"
    r"sum|total|totals|average|avg|mean|count|how\s+many|"
    r"min(?:imum)?|max(?:imum)?|aggregate|rollup"
    r")\b"
)

_UNIQUE_COUNT_RE = re.compile(r"(?ix)\b(?:unique|distinct)\b")

# Group-by SUM then top-1: "which X has the highest total Y". Not entity
# max ("highest price") and not "most common" / top-k.
_ARGMAX_RE = re.compile(
    r"(?ix)\b(?:which|what)\b[\s\S]{0,80}\b(?:highest|greatest|largest)\b"
    r"[\s\S]{0,40}\b(?:total|sum)\b"
)

# Topology questions (exists / shortest-path / highest-degree). These are not
# analytic COUNT-with-filters: instruction prose ("Answer:", "knowledge graph")
# must not fail-close as unbound dim tokens. "How many relations of type X
# does Y have" is still a filtered count — it does not match these.
_GRAPH_EXISTS_RE = re.compile(
    r"(?ix)(?:is\s+the\s+following\s+(?:triplet|triple|fact)\s+present"
    r"|triplet\s+fact\s+present\s+in\s+the\s+(?:knowledge\s+)?graph)"
)
_GRAPH_PATH_RE = re.compile(r"(?ix)\bshortest\s+path\b")
_GRAPH_DEGREE_RE = re.compile(
    r"(?ix)(?:which|what)\s+(?:entity|node)\b[\s\S]{0,100}\b"
    r"(?:highest|most|largest)\b[\s\S]{0,60}\b"
    r"(?:incoming|outgoing|total)?\s*(?:number\s+of\s+)?"
    r"(?:edges|degree|relations)\b"
    r"|\bhighest\s+(?:number\s+of\s+)?"
    r"(?:incoming|outgoing|total)\s+(?:edges|degree)"
    r"|\bhighest\s+degree\b"
)


@dataclass(frozen=True)
class QueryIntentSketch:
    """Lightweight structural read of the NL question (no ontology)."""

    question: str
    has_aggregate_intent: bool = False
    has_filter_intent: bool = False
    has_unique_count_intent: bool = False
    has_argmax_intent: bool = False
    has_graph_structure_intent: bool = False
    aggregate_ops: tuple[str, ...] = ()
    filter_tokens: tuple[str, ...] = ()
    measure_prop_candidates: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def question_has_aggregate_intent(question: str) -> bool:
    """True when the question looks like a sum/avg/count/total style ask."""
    return bool(_AGGREGATE_RE.search(question or ""))


def question_has_unique_count_intent(question: str) -> bool:
    """True when the ask is a unique/distinct count, not a type-scan total."""
    q = question or ""
    return question_has_aggregate_intent(q) and bool(_UNIQUE_COUNT_RE.search(q))


def question_has_argmax_intent(question: str) -> bool:
    """True for which/what + highest/greatest/largest + total/sum."""
    return bool(_ARGMAX_RE.search(question or ""))


def question_has_graph_structure_intent(question: str) -> bool:
    """True for exists-triple / shortest-path / highest-degree topology asks."""
    q = question or ""
    return bool(
        _GRAPH_EXISTS_RE.search(q)
        or _GRAPH_PATH_RE.search(q)
        or _GRAPH_DEGREE_RE.search(q)
    )


def _aggregate_ops(question: str) -> list[str]:
    q = (question or "").lower()
    ops: list[str] = []
    for op, pat in (
        ("sum", r"\b(?:sum|total|totals)\b"),
        ("avg", r"\b(?:average|avg|mean)\b"),
        ("count", r"\b(?:count|how\s+many)\b"),
        ("min", r"\bmin(?:imum)?\b"),
        ("max", r"\bmax(?:imum)?\b"),
    ):
        if re.search(pat, q, re.I):
            ops.append(op)
    return ops


def sketch_query_intent(question: str) -> QueryIntentSketch:
    """Build a pure intent sketch for coverage / confidence gates."""
    q = (question or "").strip()
    has_agg = question_has_aggregate_intent(q)
    has_unique = question_has_unique_count_intent(q)
    has_argmax = question_has_argmax_intent(q)
    has_graph = question_has_graph_structure_intent(q)
    has_filt = question_has_filter_intent(q)
    tokens = collapse_filter_tokens(extract_filter_tokens(q))
    measures = extract_measure_prop_candidates(q)
    if tokens and not has_filt:
        has_filt = True
    if has_unique:
        has_filt = True
    return QueryIntentSketch(
        question=q,
        has_aggregate_intent=has_agg,
        has_filter_intent=has_filt,
        has_unique_count_intent=has_unique,
        has_argmax_intent=has_argmax,
        has_graph_structure_intent=has_graph,
        aggregate_ops=tuple(_aggregate_ops(q)),
        filter_tokens=tuple(tokens),
        measure_prop_candidates=tuple(measures),
    )


__all__ = [
    "QueryIntentSketch",
    "collapse_filter_tokens",
    "extract_filter_tokens",
    "extract_measure_prop_candidates",
    "question_has_aggregate_intent",
    "question_has_argmax_intent",
    "question_has_graph_structure_intent",
    "sketch_query_intent",
]
