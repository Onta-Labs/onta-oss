"""Aggregate fixtures + the hermetic try_deterministic_cypher dispatcher.

**Not used on the production /ask path** — user-facing NL→Cypher is
always LLM. Money-leaf hard-bind is unique-resolve only.
"""

from __future__ import annotations

import re
from typing import Any

from infona_client.graph.rdfs_helpers import (
    LITERAL_AGGREGATE_CYPHER,
    type_names_with_subclasses,
)
from infona_client.nlp.cypher_patterns import (
    TEMPLATE_LITERAL_AGGREGATE,
    _AGG_OP_MAP,
    _NUMERIC_AGG_PROP_CANDIDATES,
)
from infona_client.nlp.cypher_stub_basic import try_list_query, try_stub_count_query
from infona_client.nlp.cypher_stub_filter import (
    try_filter_query,
    try_numeric_filter_query,
    try_related_name_filter_query,
)
from infona_client.nlp.cypher_stub_rel import try_hop_query, try_made_by_filter_query
from infona_client.nlp.cypher_patterns import _AGG_RE
from infona_client.nlp.cypher_rel_resolve import _ontology_section_for_type
from infona_client.nlp.cypher_stub_basic import _fixture
from infona_client.nlp.cypher_types import (
    _SAFE_PROP_RE,
    _TRAILING_PUNCT_RE,
    resolve_type_name,
)

def _resolve_numeric_prop(prop: str | None, ontology_summary: str, type_name: str) -> str | None:
    """Pick a numeric/datatype prop key from the question or ontology section.

    Type-scoped and semantic-aware: ``price`` NL can bind ``unit_cost`` when
    that is the only money leaf. Ambiguous multi-leaf ties return ``None``.
    """
    from infona_client.nlp.numeric_attr_resolve import resolve_numeric_attr

    mention = (prop or "amount").strip()
    resolved = resolve_numeric_attr(
        mention,
        type_name=type_name,
        ontology_summary=ontology_summary,
        money_family=True,
    )
    if resolved.confidence == "unique" and resolved.prop_key:
        return resolved.prop_key
    if resolved.confidence == "ambiguous":
        return None

    section = _ontology_section_for_type(type_name, ontology_summary)
    text = section or ontology_summary or ""
    if prop and _SAFE_PROP_RE.match(prop):
        # Prefer exact leaf in section; else accept the word if it appears.
        if re.search(rf"(?im)^\s*-\s*{re.escape(prop)}\b", text) or re.search(
            rf"(?i)\b{re.escape(prop)}\b", text
        ):
            return prop
    for cand in _NUMERIC_AGG_PROP_CANDIDATES:
        if re.search(rf"(?im)^\s*-\s*{re.escape(cand)}\b", text):
            return cand
        if re.search(rf"(?i)\b{re.escape(cand)}\b", text):
            return cand
    if prop and _SAFE_PROP_RE.match(prop):
        return prop
    return None


def try_aggregate_query(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """Sum / avg / min / max over a datatype property for a type (hermetic).

    Uses Assertion literal_value coalesce Entity denorm props — NEVER invents
    HAS_ASSERTION (the LLM failure mode that returned silent 0 for totals).
    """
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None
    m = _AGG_RE.match(q)
    if not m:
        return None
    agg_word = (m.group("agg") or "").strip().lower()
    op = _AGG_OP_MAP.get(agg_word)
    if not op:
        return None
    prop = (m.group("prop") or "").strip() or None
    if prop:
        from infona_client.nlp.numeric_attr_resolve import strip_leading_agg_modifier

        noun, peeled = strip_leading_agg_modifier(prop)
        if peeled and noun:
            prop = noun
    label = (m.group("label") or "").strip()
    peeled_from_number = False
    # "total number of grants" is a COUNT; "total number of seats" is SUM(seats)
    # only when ``seats`` is a declared attribute on the type (not any English noun).
    if prop and prop.lower() in {
        "number", "count", "counts", "entities", "records", "items", "rows", "things",
    }:
        first = (label.split() or [""])[0].strip(".,")
        if first and _SAFE_PROP_RE.match(first):
            prop = first
            label = " ".join(label.split()[1:]).strip()
            label = re.sub(r"(?i)^(of|for|across|over|on|all|the)\s+", "", label).strip()
            peeled_from_number = True
        else:
            return None
    label = _TRAILING_PUNCT_RE.sub("", label)
    # Strip leading noise left in label ("all grants", "the widgets")
    label = re.sub(r"(?i)^(all|the|of|for)\s+", "", label).strip()
    if not label:
        return None
    # "grant amount" style: prop may be empty and label ends with amount
    if prop is None:
        parts = label.split()
        if len(parts) >= 2 and parts[-1].lower() in {
            c.lower() for c in _NUMERIC_AGG_PROP_CANDIDATES
        }:
            prop = parts[-1]
            label = " ".join(parts[:-1]).strip()
    matched = resolve_type_name(label, type_names, ontology_summary)
    if matched is None:
        return None
    # Gate: prop must appear as a declared leaf on the type (or known candidate
    # present in the section). Never invent SUM(students) when only seats exists.
    section = _ontology_section_for_type(matched, ontology_summary)
    if prop:
        declared = bool(
            re.search(rf"(?im)^\s*-\s*{re.escape(prop)}\b", section or "")
            or re.search(
                rf"(?i)\bkey={re.escape(prop)}\b", section or ontology_summary or ""
            )
        )
        if not declared and prop.lower() not in {
            c.lower() for c in _NUMERIC_AGG_PROP_CANDIDATES
        }:
            return None
        if not declared and peeled_from_number:
            # Peeling "number of <noun>" only when <noun> is a real attr.
            if not re.search(rf"(?im)^\s*-\s*{re.escape(prop)}\b", section or ""):
                return None
    prop_key = _resolve_numeric_prop(prop, ontology_summary, matched)
    if not prop_key or not _SAFE_PROP_RE.match(prop_key):
        return None
    # Final guard: resolved prop_key must be on the type section when we peeled.
    if peeled_from_number and not re.search(
        rf"(?im)^\s*-\s*{re.escape(prop_key)}\b", section or ""
    ):
        return None
    expanded = type_names_with_subclasses(
        matched, ontology_summary=ontology_summary, include_subclasses=True
    )
    return _fixture(
        cypher=LITERAL_AGGREGATE_CYPHER,
        params={
            "type_names": expanded,
            "prop_key": prop_key,
            "agg_op": op,
        },
        explanation=(
            f"{op.upper()} of {prop_key} for {matched} entities "
            f"via literal_aggregate (no HAS_ASSERTION)."
        ),
        template=TEMPLATE_LITERAL_AGGREGATE,
    )


def try_deterministic_cypher(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """Try hermetic fixtures in priority order; return first match or None.

    **Not used on the production ``/ask`` path** — user-facing NL→Cypher is
    always LLM. Call this from unit tests of template builders, or from
    non-ask helpers (e.g. internal URI selection) that intentionally prefer
    deterministic shapes.
    """
    for fn in (
        try_aggregate_query,  # before count: "total amount of grants" ≠ bare count
        try_stub_count_query,
        try_numeric_filter_query,  # before equality so "price under 15" wins
        try_related_name_filter_query,  # before equality so "with genre X" wins
        try_made_by_filter_query,  # "products made by Acme" / "books by X"
        try_filter_query,  # before list so "list X where …" wins
        try_hop_query,  # before list so "authors of books" wins
        try_list_query,
    ):
        got = fn(question, ontology_summary, type_names=type_names)
        if got is not None:
            return got
    return None

