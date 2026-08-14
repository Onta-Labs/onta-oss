"""Equality / numeric / related-name filter fixtures.

Money-leaf hard-bind is unique-resolve only — ambiguous multi-leaf
ties return None (fail closed).
"""

from __future__ import annotations

import re
from typing import Any

from infona_client.graph.rdfs_helpers import type_names_with_subclasses

from infona_client.graph.rdfs_helpers import (
    LITERAL_COMPARE_CYPHER,
    RELATED_ENTITY_NAME_FILTER_CYPHER,
)
from infona_client.nlp.cypher_patterns import (
    FILTER_PROP_EQ_CYPHER,
    TEMPLATE_FILTER_PROP_EQ,
    TEMPLATE_LITERAL_COMPARE,
    TEMPLATE_RELATED_ENTITY_NAME_FILTER,
)
from infona_client.nlp.cypher_rel_resolve import (
    _attr_is_relationship,
    _resolve_relationship_attr,
)
from infona_client.nlp.cypher_patterns import (  # noqa: F401
    _CMP_OP_MAP,
    _COST_PROP_CANDIDATES,
    _FILTER_RE,
    _NUMERIC_FILTER_RE,
    _REL_NAME_FILTER_RE,
    TEMPLATE_LITERAL_COMPARE,
)
from infona_client.nlp.cypher_stub_basic import (
    _clamp_limit,
    _fixture,
    _strip_limit_suffix,
    _strip_order_by_suffix,
)
from infona_client.nlp.cypher_types import (
    DEFAULT_LIST_LIMIT,
    _SAFE_PROP_RE,
    _TRAILING_PUNCT_RE,
    resolve_type_name,
)

def try_filter_query(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """Filter entities of a type by property equality (optional LIMIT suffix)."""
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None
    q, _order_prop, _order_dir = _strip_order_by_suffix(q)
    q, limit = _strip_limit_suffix(q)
    m = _FILTER_RE.match(q)
    if not m:
        return None
    label = (m.group("label") or "").strip()
    prop = (m.group("prop") or "").strip()
    value = (m.group("value") or "").strip()
    value = _TRAILING_PUNCT_RE.sub("", value)
    # Value group may have swallowed "limit N" before strip; re-strip value.
    value, lim_from_value = _strip_limit_suffix(value)
    if lim_from_value is not None:
        limit = lim_from_value
    if not _SAFE_PROP_RE.match(prop):
        return None
    if not value:
        return None
    # Do not treat "less than 500" as an equality value — numeric fixture owns it.
    if re.match(
        r"(?i)^(less\s+than|more\s+than|under|over|below|above|at\s+least|at\s+most)\s+\d",
        value,
    ):
        return None
    matched = resolve_type_name(label, type_names, ontology_summary)
    if matched is None:
        return None
    # Normalize common display names to Entity property keys.
    prop_key = prop
    if prop_key.lower() in {"label", "title"}:
        # Prefer name (Explorer primary display); title stays as prop if set.
        if prop_key.lower() == "label":
            prop_key = "name"
    elif prop_key.lower() == "name":
        prop_key = "name"
    else:
        prop_key = prop  # keep original case for custom attrs (status, isbn, …)

    expanded = type_names_with_subclasses(
        matched, ontology_summary=ontology_summary, include_subclasses=True
    )
    # Relationship-valued attrs: use related-entity name filter, not literal eq.
    rel_attr = None
    if _attr_is_relationship(prop_key, matched, ontology_summary):
        rel_attr = prop_key
    else:
        rel_attr = _resolve_relationship_attr(
            prop_key, type_name=matched, ontology_summary=ontology_summary
        )
    if rel_attr is not None:
        return _fixture(
            cypher=RELATED_ENTITY_NAME_FILTER_CYPHER,
            params={
                "type_names": expanded,
                "rel_attr": rel_attr,
                "target_name": value,
                "limit": limit if limit is not None else DEFAULT_LIST_LIMIT,
            },
            explanation=(
                f"Find {matched} entities related via {rel_attr} to "
                f"{value!r} via related_entity_name_filter."
            ),
            template=TEMPLATE_RELATED_ENTITY_NAME_FILTER,
        )
    return _fixture(
        cypher=FILTER_PROP_EQ_CYPHER,
        params={
            "type_names": expanded,
            "prop_key": prop_key,
            "prop_value": value,
            "limit": limit if limit is not None else DEFAULT_LIST_LIMIT,
        },
        explanation=(
            f"Find {matched} entities where {prop_key} equals {value!r} "
            f"via literal_values."
        ),
        template=TEMPLATE_FILTER_PROP_EQ,
    )


def _resolve_cost_prop(
    ontology_summary: str,
    type_name: str | None = None,
    *,
    mention: str = "price",
) -> str | None:
    """Type-scoped money/cost leaf resolve (semantic + family heuristics).

    Returns the unique leaf on ``type_name`` or ``None`` when ambiguous /
    missing (fail closed). Falls back to a whole-ontology candidate scan only
    when ``type_name`` is unset, still without inventing a missing default.
    """
    from infona_client.nlp.numeric_attr_resolve import resolve_cost_prop as _sem

    got = _sem(
        ontology_summary,
        type_name=type_name,
        mention=mention or "price",
    )
    if got:
        return got
    # Legacy whole-ontology first-hit when type_name missing (tests without type).
    if not type_name:
        text = ontology_summary or ""
        for cand in _COST_PROP_CANDIDATES:
            if re.search(rf"(?i)\b{re.escape(cand)}\b", text):
                return cand
    return None


def try_numeric_filter_query(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """Filter entities of a type by numeric inequality (price/cost/rating/…)."""
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None
    q, _order_prop, _order_dir = _strip_order_by_suffix(q)
    q, limit = _strip_limit_suffix(q)
    m = _NUMERIC_FILTER_RE.match(q)
    if not m:
        return None
    label = (m.group("label") or "").strip()
    # Drop trailing "list titles and prices" noise after the threshold phrase.
    label = re.sub(
        r"(?i)\s+(?:list|show|return|with)\s+(?:their\s+)?titles?.*$",
        "",
        label,
    ).strip()
    matched = resolve_type_name(label, type_names, ontology_summary)
    if matched is None:
        return None

    if m.group("cost_num") is not None:
        prop_key = _resolve_cost_prop(ontology_summary, matched, mention="price")
        if not prop_key:
            # Fail closed on ambiguous / missing money leaf — do not invent.
            return None
        op_raw = (m.group("cost_op") or "less than").strip().lower()
        g0 = (m.group(0) or "").lower()
        # Map "cheaper than" / "more expensive than" using the verb, not bare "than".
        if "cheaper" in g0 and op_raw in ("than", "less than", "under", "below"):
            op_raw = "less than"
        elif "more expensive" in g0 and op_raw in (
            "than",
            "more than",
            "over",
            "above",
        ):
            op_raw = "more than"
        threshold = float(m.group("cost_num"))
    else:
        prop_raw = (m.group("prop") or "").strip()
        # Multi-word NL props ("unit cost") → underscore for leaf resolve.
        prop = re.sub(r"\s+", "_", prop_raw)
        if not prop or not _SAFE_PROP_RE.match(prop):
            return None
        # Type-scoped semantic resolve when NL prop is a money synonym.
        from infona_client.nlp.numeric_attr_resolve import (
            is_money_nl_cue,
            resolve_numeric_attr,
        )

        if is_money_nl_cue(prop) or is_money_nl_cue(prop_raw):
            resolved = resolve_numeric_attr(
                prop,
                type_name=matched,
                ontology_summary=ontology_summary,
                money_family=True,
            )
            if resolved.confidence == "unique" and resolved.prop_key:
                prop_key = resolved.prop_key
            elif resolved.confidence == "ambiguous":
                return None
            else:
                prop_key = prop
        else:
            prop_key = prop
        op_raw = (m.group("cmp") or "<").strip().lower()
        threshold = float(m.group("num"))

    op = _CMP_OP_MAP.get(op_raw)
    if op is None:
        return None

    expanded = type_names_with_subclasses(
        matched, ontology_summary=ontology_summary, include_subclasses=True
    )
    return _fixture(
        cypher=LITERAL_COMPARE_CYPHER,
        params={
            "type_names": expanded,
            "prop_key": prop_key,
            "op": op,
            "threshold": threshold,
            "limit": limit if limit is not None else DEFAULT_LIST_LIMIT,
        },
        explanation=(
            f"Find {matched} entities where {prop_key} {op_raw} {threshold} "
            f"via literal_compare."
        ),
        template=TEMPLATE_LITERAL_COMPARE,
    )


def try_related_name_filter_query(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """Filter subjects by a related entity's display name (ontology edges only).

    Matches ``<types> with|having|in <rel> <value>`` only when ``<rel>`` resolves
    to a **relationship** leaf on the type. Literal dimensions fall through to
    :func:`try_filter_query` / the LLM — never invent a related-entity template
    for ``title`` / ``status`` literals.
    """
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None
    q, _order_prop, _order_dir = _strip_order_by_suffix(q)
    q, limit = _strip_limit_suffix(q)
    m = _REL_NAME_FILTER_RE.match(q)
    if not m:
        return None
    label = (m.group("label") or "").strip()
    rel = (m.group("rel") or "").strip()
    value = _TRAILING_PUNCT_RE.sub("", (m.group("value") or "").strip())
    value, lim_from_value = _strip_limit_suffix(value)
    if lim_from_value is not None:
        limit = lim_from_value
    if not value or not _SAFE_PROP_RE.match(rel):
        return None
    # Defer equality / numeric shapes to dedicated fixtures.
    if re.match(r"(?i)^(is|equals?|=|==|less|more|under|over|below|above|at)\b", value):
        return None
    if re.match(r"(?i)^(less|more|under|over|below|above|at\s+least|at\s+most)\b", rel):
        return None
    matched = resolve_type_name(label, type_names, ontology_summary)
    if matched is None:
        return None

    rel_attr = _resolve_relationship_attr(
        rel, type_name=matched, ontology_summary=ontology_summary
    )
    if rel_attr is None:
        return None

    expanded = type_names_with_subclasses(
        matched, ontology_summary=ontology_summary, include_subclasses=True
    )
    return _fixture(
        cypher=RELATED_ENTITY_NAME_FILTER_CYPHER,
        params={
            "type_names": expanded,
            "rel_attr": rel_attr,
            "target_name": value,
            "limit": limit if limit is not None else DEFAULT_LIST_LIMIT,
        },
        explanation=(
            f"Find {matched} entities related via {rel_attr} to "
            f"{value!r} via related_entity_name_filter."
        ),
        template=TEMPLATE_RELATED_ENTITY_NAME_FILTER,
    )


