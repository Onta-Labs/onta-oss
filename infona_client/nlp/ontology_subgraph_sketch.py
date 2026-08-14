"""Extract an :class:`NlSketch` from free-text NL (no ontology bind).

Looked up on :mod:`infona_client.nlp.ontology_subgraph_match` at call time via
``_host()`` when a sibling needs a patchable name.
"""

from __future__ import annotations

import re

from infona_client.nlp.cypher_generate import _SAFE_PROP_RE
from infona_client.nlp.ontology_subgraph_types import NlSketch, _STOPWORDS

_TRAILING_PUNCT_RE = re.compile(r"[?!.\s]+$")
_COUNT_PREFIX_RE = re.compile(
    r"(?ix)^"
    r"(?:how\s+many|count(?:\s+the|\s+of)?|number\s+of|total(?:\s+number\s+of)?)"
    r"\s+"
)
_LIST_PREFIX_RE = re.compile(
    r"(?ix)^"
    r"(?:list|show(?:\s+me)?|get|find|what\s+are|which)"
    r"\s+"
)
# "<type(s)> <cue> [the] [<dim>] <value...>"
_SKETCH_PATH_RE = re.compile(
    r"(?ix)^"
    r"(?P<label>.+?)\s+"
    r"(?P<cue>in|at|from|near|inside|within|into|on|with|having|of|via|by|for)\s+"
    r"(?:the\s+)?"
    r"(?:(?P<dim>[A-Za-z_][A-Za-z0-9_]*)\s+)?"
    r"[\"']?(?P<value>.+?)[\"']?"
    r"$"
)
# Bare count / list without path: "how many widgets" / "list widgets"
_BARE_TYPE_RE = re.compile(
    r"(?ix)^"
    r"(?:(?:are\s+there|do\s+we\s+have|exist)\s+)?"
    r"(?P<label>.+?)"
    r"(?:\s+(?:are\s+there|do\s+we\s+have|exist|in\s+the\s+\w+))?"
    r"$"
)


def extract_nl_sketch(question: str) -> NlSketch:
    """Extract type / value / rel-cue mentions and intent from free-text NL.

    Pure string heuristics — no ontology lookup. Messy casing / plurals kept
    as raw mentions for later resolve.
    """
    raw = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not raw:
        return NlSketch(question=question or "", intent="unknown")

    intent = "unknown"
    body = raw
    if _COUNT_PREFIX_RE.match(body):
        intent = "count"
        body = _COUNT_PREFIX_RE.sub("", body, count=1).strip()
        body = re.sub(
            r"(?i)\s+(?:are\s+there|do\s+we\s+have|exist)\s*$",
            "",
            body,
        ).strip()
    elif _LIST_PREFIX_RE.match(body):
        intent = "list"
        body = _LIST_PREFIX_RE.sub("", body, count=1).strip()
        body = re.sub(r"(?i)^(all|the)\s+", "", body).strip()
    else:
        # Bare "widgets in east" → list-ish without explicit verb
        if re.search(
            r"(?i)\b(?:in|at|from|with|having|of|near|inside|within)\b",
            body,
        ):
            intent = "list"

    type_mentions: list[str] = []
    value_mentions: list[str] = []
    rel_cues: list[str] = []
    dim_mentions: list[str] = []

    m = _SKETCH_PATH_RE.match(body)
    if m:
        label = (m.group("label") or "").strip()
        cue = (m.group("cue") or "").strip().lower()
        dim = (m.group("dim") or "").strip()
        value = _TRAILING_PUNCT_RE.sub("", (m.group("value") or "").strip())
        # If dim captured but value is empty / cue-like, don't invent.
        if label:
            type_mentions.append(label)
        if cue:
            rel_cues.append(cue)
        if dim and _SAFE_PROP_RE.match(dim) and dim.lower() not in _STOPWORDS:
            # Dim might actually be the whole value for single-token "in East"
            # when pattern greedily took dim — if value is multi-word keep both;
            # if value looks empty we already require value group.
            dim_mentions.append(dim)
        if value:
            # When dim was captured, value is the rest; when not, value may be
            # "site East" (dim word still in value) — peel first token if it
            # looks like a dim and remainder remains.
            value_mentions.append(value)
            # "site East" → dim=site, value=East when no dim group (optional
            # dim only matches when a second token exists).
            parts = value.split()
            if (
                not dim
                and len(parts) >= 2
                and _SAFE_PROP_RE.match(parts[0])
                and parts[0].lower() not in _STOPWORDS
            ):
                dim_mentions.append(parts[0])
                value_mentions[-1] = " ".join(parts[1:]).strip()
    else:
        m2 = _BARE_TYPE_RE.match(body)
        if m2:
            label = (m2.group("label") or "").strip()
            # Drop trailing noise
            label = re.sub(
                r"(?i)\s+(?:are\s+there|do\s+we\s+have|exist)$",
                "",
                label,
            ).strip()
            if label:
                type_mentions.append(label)

    # De-dupe while preserving order; drop stopword-only labels.
    type_mentions = _dedupe_keep(
        t for t in type_mentions if t and t.lower() not in _STOPWORDS
    )
    value_mentions = _dedupe_keep(v for v in value_mentions if v)
    dim_mentions = _dedupe_keep(
        d for d in dim_mentions if d and d.lower() not in _STOPWORDS
    )
    rel_cues = _dedupe_keep(rel_cues)

    return NlSketch(
        question=raw,
        intent=intent,
        type_mentions=tuple(type_mentions),
        value_mentions=tuple(value_mentions),
        rel_cues=tuple(rel_cues),
        dim_mentions=tuple(dim_mentions),
    )


def _dedupe_keep(items) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        key = x.lower() if isinstance(x, str) else str(x)
        if key in seen:
            continue
        seen.add(key)
        out.append(x)
    return out


