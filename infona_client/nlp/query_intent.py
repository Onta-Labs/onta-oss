"""NL query intent sketch for constraint coverage (filter-miss class).

Pure, deterministic helpers used after Cypher generation to decide whether the
plan covers the question's constraints. No ontology, no LLM, no persona gold.

Product rule: interpret constraints generally (for / in / where / with / quoted
values / label+digit) — never hardcode domain strings like term names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from infona_client.nlp.cypher_filter_integrity import question_has_filter_intent

# Aggregate / measure cues (general English + common NL→SQL phrasing).
_AGGREGATE_RE = re.compile(
    r"(?ix)\b(?:"
    r"sum|total|totals|average|avg|mean|count|how\s+many|"
    r"min(?:imum)?|max(?:imum)?|aggregate|rollup"
    r")\b"
)

# Measure-ish noun after aggregate verb: "sum unit_qty", "total price", "avg score".
_MEASURE_AFTER_AGG_RE = re.compile(
    r"(?ix)\b(?:sum|total|average|avg|mean|min(?:imum)?|max(?:imum)?)\s+"
    r"(?:of\s+|the\s+)*"
    r"(?P<measure>[A-Za-z][A-Za-z0-9_]*(?:\s+[A-Za-z][A-Za-z0-9_]*){0,2})"
)

# Free tokens after dim prepositions. Deliberately does NOT start on bare
# ``is``/``are`` (those would swallow ``status_label is active`` as one token);
# value-after-copula is handled by ``_VALUE_AFTER_COPULA_RE``.
_AFTER_PREP_RE = re.compile(
    r"(?ix)\b(?:for|in|at|where|with|having|status|named|labelled|labeled|"
    r"matching)\s+"
    r"(?:the\s+|a\s+|an\s+)*"
    r"(?P<tok>['\"][^'\"]+['\"]|[A-Za-z][A-Za-z0-9_]*(?:\s+[A-Za-z0-9_]+){0,2})"
)

# ``status_label is active`` / ``tier equals T2`` → capture the value only.
_VALUE_AFTER_COPULA_RE = re.compile(
    r"(?ix)\b(?:is|are|equals?|equal\s+to|=)\s+"
    r"(?:the\s+|a\s+|an\s+)*"
    r"(?P<tok>['\"][^'\"]+['\"]|[A-Za-z][A-Za-z0-9_]*)"
)

_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")

_LABEL_DIGIT_RE = re.compile(
    r"(?ix)\b(?P<label>[A-Za-z][A-Za-z0-9_]*)\s+(?P<num>[0-9]+(?:\.[0-9]+)?)\b"
)

# Words that are never useful as filter *values* when extracted as free tokens.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "from",
        "by",
        "on",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "that",
        "this",
        "these",
        "those",
        "which",
        "who",
        "whom",
        "what",
        "when",
        "how",
        "many",
        "much",
        "all",
        "each",
        "every",
        "any",
        "some",
        "no",
        "not",
        "only",
        "just",
        "also",
        "than",
        "then",
        "into",
        "over",
        "under",
        "above",
        "below",
        "between",
        "about",
        "per",
        "via",
        "vs",
        "versus",
        "list",
        "show",
        "give",
        "find",
        "get",
        "return",
        "please",
        "me",
        "us",
        "you",
        "their",
        "its",
        "our",
        "your",
        "sum",
        "total",
        "totals",
        "average",
        "avg",
        "mean",
        "count",
        "counts",
        "min",
        "minimum",
        "max",
        "maximum",
        "aggregate",
        "value",
        "values",
        "number",
        "numbers",
        "amount",
        "quantity",
        "qty",
        "entities",
        "entity",
        "rows",
        "row",
        "items",
        "item",
        "records",
        "record",
        "results",
        "result",
        "data",
        "type",
        "types",
        "status",  # cue word, not a value by itself
        "term",  # generic dim name without value
        "name",
        "named",
        "label",
        "labeled",
        "labelled",
        "filter",
        "filtered",
        "where",
        "with",
        "having",
        "whose",
        "for",
        "in",
        "at",
        "equal",
        "equals",
        "matching",
        "less",
        "more",
        "greater",
        "least",
        "most",
        "top",
        "first",
        "last",
        "limit",
        "page",
        "offset",
        "skip",
        "take",
        "next",
        "prev",
        "previous",
        "version",
        "v",
        "null",
        "true",
        "false",
        "yes",
        "no",
    }
)

# Re-allow common short enum-ish values that are often real filters. We removed
# them from the permanent stop list intentionally — "active" / "open" etc. stay
# extractable. (Listed here only as documentation of the choice.)
_STATUS_VALUE_ALLOW = frozenset(
    {
        "active",
        "inactive",
        "open",
        "closed",
        "pending",
        "draft",
        "published",
        "enabled",
        "disabled",
    }
)

# After "for/in/…", drop measure-ish heads when they look like the aggregate
# object rather than a dim value ("sum qty for North" → drop qty, keep North).
_MEASURE_HEAD_STOP = frozenset(
    {
        "qty",
        "quantity",
        "amount",
        "value",
        "values",
        "price",
        "cost",
        "units",
        "unit",
        "score",
        "scores",
        "count",
        "counts",
        "number",
        "numbers",
        "total",
        "totals",
        "sum",
        "revenue",
        "sales",
        "weight",
        "size",
        "length",
        "width",
        "height",
        "rate",
        "rates",
        "percent",
        "percentage",
    }
)

_LABEL_DIGIT_STOP = frozenset(
    {
        "top",
        "first",
        "last",
        "limit",
        "page",
        "offset",
        "skip",
        "take",
        "next",
        "prev",
        "previous",
        "row",
        "rows",
        "item",
        "items",
        "of",
        "and",
        "or",
        "the",
        "a",
        "an",
        "in",
        "on",
        "at",
        "by",
        "from",
        "to",
        "vs",
        "version",
        "v",
    }
)


@dataclass(frozen=True)
class QueryIntentSketch:
    """Lightweight structural read of the NL question (no ontology)."""

    question: str
    has_aggregate_intent: bool = False
    has_filter_intent: bool = False
    aggregate_ops: tuple[str, ...] = ()
    filter_tokens: tuple[str, ...] = ()
    measure_prop_candidates: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def question_has_aggregate_intent(question: str) -> bool:
    """True when the question looks like a sum/avg/count/total style ask."""
    return bool(_AGGREGATE_RE.search(question or ""))


def _normalize_token(raw: str) -> str:
    t = (raw or "").strip().strip("\"'").strip()
    # Collapse internal whitespace.
    t = re.sub(r"\s+", " ", t)
    return t


def _is_stop_token(tok: str) -> bool:
    low = tok.lower().strip()
    if not low or len(low) < 2:
        return True
    if low in _STOPWORDS and low not in _STATUS_VALUE_ALLOW:
        return True
    # Pure numeric alone is usually a limit/threshold, not a free dim label
    # (thresholds are handled by integrity / compare templates).
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", low):
        return True
    return False


def extract_filter_tokens(question: str) -> list[str]:
    """Extract candidate constraint *values* from free-form NL.

    General patterns only: quoted strings, free tokens after for/in/where/with,
    and label+digit phrases. Deduped, order-preserving, stopword-filtered.
    """
    q = (question or "").strip()
    if not q:
        return []

    seen: set[str] = set()
    out: list[str] = []

    def _add(raw: str, *, allow_measure_head: bool = False) -> None:
        tok = _normalize_token(raw)
        if not tok:
            return
        # If a prep capture still contains a copula, keep the RHS value only
        # (``where status_label is active`` → ``active``).
        low_full = tok.lower()
        for sep in (" is ", " are ", " equals ", " equal to ", " = "):
            if sep in low_full:
                tok = tok[low_full.rfind(sep) + len(sep) :].strip()
                low_full = tok.lower()
                break
        if not tok or _is_stop_token(tok):
            return
        low = tok.lower()
        if not allow_measure_head and low in _MEASURE_HEAD_STOP:
            # Single measure head alone is not a dim value.
            if " " not in low:
                return
        # Prop-key shaped single tokens (status_label) after where/with are
        # attribute names, not values — drop unless allowlisted status words.
        if " " not in low and "_" in low and low not in _STATUS_VALUE_ALLOW:
            return
        key = low
        if key in seen:
            return
        seen.add(key)
        out.append(tok)

    for m in _QUOTED_RE.finditer(q):
        _add(m.group(1), allow_measure_head=True)

    for m in _VALUE_AFTER_COPULA_RE.finditer(q):
        _add(m.group("tok"), allow_measure_head=True)

    for m in _AFTER_PREP_RE.finditer(q):
        _add(m.group("tok"))

    for m in _LABEL_DIGIT_RE.finditer(q):
        label = m.group("label")
        if label.lower() in _LABEL_DIGIT_STOP:
            continue
        _add(f"{label} {m.group('num')}", allow_measure_head=True)

    return out


def extract_measure_prop_candidates(question: str) -> list[str]:
    """Optional measure-property phrases after aggregate verbs (best-effort)."""
    q = (question or "").strip()
    if not q:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _MEASURE_AFTER_AGG_RE.finditer(q):
        raw = _normalize_token(m.group("measure"))
        if not raw:
            continue
        # Drop trailing "for …" residue if the regex over-captured (it shouldn't
        # with the limited {0,2} words, but be safe).
        parts = [p for p in raw.split() if p.lower() not in ("for", "in", "at", "where")]
        if not parts:
            continue
        # Prefer a single leaf-ish token (last content word).
        leaf = parts[-1]
        if _is_stop_token(leaf) and leaf.lower() not in _MEASURE_HEAD_STOP:
            continue
        key = leaf.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(leaf)
    return out


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
    has_filt = question_has_filter_intent(q)
    tokens = extract_filter_tokens(q)
    measures = extract_measure_prop_candidates(q)
    # If we found free filter tokens, treat as filter intent even when the
    # integrity cue regex missed (e.g. bare "North" after an unusual cue).
    if tokens and not has_filt:
        has_filt = True
    return QueryIntentSketch(
        question=q,
        has_aggregate_intent=has_agg,
        has_filter_intent=has_filt,
        aggregate_ops=tuple(_aggregate_ops(q)),
        filter_tokens=tuple(tokens),
        measure_prop_candidates=tuple(measures),
    )


__all__ = [
    "QueryIntentSketch",
    "extract_filter_tokens",
    "extract_measure_prop_candidates",
    "question_has_aggregate_intent",
    "sketch_query_intent",
]
