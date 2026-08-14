"""Count / list hermetic Cypher fixtures (not used on production /ask)."""

from __future__ import annotations

import re
from typing import Any

from infona_client.graph.rdfs_helpers import type_names_with_subclasses

from infona_client.nlp.cypher_patterns import (
    COUNT_BY_TYPE_CYPHER,
    COUNT_TOTAL_CYPHER,
    LIST_BY_TYPE_CYPHER,
    TEMPLATE_COUNT_BY_TYPE,
    TEMPLATE_COUNT_TOTAL,
    TEMPLATE_LIST_BY_TYPE,
)
from infona_client.nlp.cypher_patterns import (  # noqa: F401
    _COUNT_RE,
    _LIMIT_SUFFIX_RE,
    _LIST_RE,
    _N_PREFIX_LIST_RE,
    _ORDER_BY_SUFFIX_RE,
    _TOP_N_LIST_RE,
)
from infona_client.nlp.cypher_patterns import _FILTER_RE, _SAFE_ORDER_PROPS
from infona_client.nlp.cypher_types import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    _SAFE_PROP_RE,
    _TRAILING_PUNCT_RE,
    _normalize_type_token,
    resolve_type_name,
)

def _strip_order_by_suffix(text: str) -> tuple[str, str | None, str | None]:
    """Return (text_without_order_by, order_prop|None, order_dir|None)."""
    m = _ORDER_BY_SUFFIX_RE.search(text or "")
    if not m:
        return text, None, None
    prop = (m.group("order_prop") or "").strip()
    direction = (m.group("order_dir") or "").strip().lower() or None
    if direction in ("ascending",):
        direction = "asc"
    elif direction in ("descending",):
        direction = "desc"
    if prop and not _SAFE_PROP_RE.match(prop):
        prop = None
        direction = None
    elif prop and prop.lower() not in _SAFE_ORDER_PROPS:
        # Unknown prop — still strip the suffix so the list fixture can match;
        # do not annotate an unsafe order key.
        prop = None
        direction = None
    else:
        prop = prop.lower() if prop else None
        if prop == "label":
            prop = "name"
    return text[: m.start()].strip(), prop, direction


def _strip_limit_suffix(text: str) -> tuple[str, int | None]:
    """Return (text_without_limit, limit|None)."""
    m = _LIMIT_SUFFIX_RE.search(text or "")
    if not m:
        return text, None
    return text[: m.start()].strip(), _clamp_limit(m.group("limit"))


def _clamp_limit(raw: str | int | None) -> int:
    if raw is None or raw == "":
        return DEFAULT_LIST_LIMIT
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIST_LIMIT
    if n < 1:
        return 1
    return min(n, MAX_LIST_LIMIT)


def _fixture(
    *,
    cypher: str,
    params: dict[str, Any],
    explanation: str,
    template: str | None,
) -> dict[str, Any]:
    return {
        "cypher": cypher,
        "params": params,
        "explanation": explanation,
        "functions_needed": [],
        "stub": True,
        "fixture": True,
        "template": template,
    }


def try_stub_count_query(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """If ``question`` is a simple count, return a scoped Cypher payload.

    Kept as a public alias for the count arm of :func:`try_deterministic_cypher`.
    """
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None
    m = _COUNT_RE.match(q)
    if not m:
        return None
    label = (m.group("label") or m.group("label2") or "").strip()
    label = _TRAILING_PUNCT_RE.sub("", label)

    bare = _normalize_type_token(label).lower()
    if not bare or bare in {
        "entities",
        "entity",
        "records",
        "items",
        "things",
        "nodes",
        "rows",
    }:
        return _fixture(
            cypher=COUNT_TOTAL_CYPHER,
            params={},
            explanation="Count all entities in the knowledge graph.",
            template=TEMPLATE_COUNT_TOTAL,
        )

    # "total amount of grants" is an aggregation, not a type count — fall through.
    if re.search(
        r"(?i)\b(?:amount|sum|total|average|avg|mean|price|cost|mileage|qty|quantity)\b"
        r".*\bof\b",
        label,
    ) or re.match(
        r"(?i)^(?:amount|price|cost|mileage|qty|quantity|value)\b",
        label,
    ):
        return None

    # Refuse silent wrong counts: any filtered / scoped "how many X …" is NOT a
    # bare type count (e.g. "how many sensors are at Plant-A"). Fall through so
    # LLM / filter fixtures handle it — never answer unfiltered total.
    if re.search(
        r"(?i)\b(?:have|has|with|where|having|that\s+have|under|over|above|below|"
        r"less\s+than|more\s+than|at\s+least|at\s+most|equals?|is\s+not|"
        r"are\s+at|is\s+at|located|belong|belonging|matching|filtered|"
        r"\bat\b|\bfrom\b|\bfor\b|\bin\b|\bon\b|\bby\b|"
        r"whose|which\s+are|that\s+are)\b",
        label,
    ):
        return None

    matched = resolve_type_name(label, type_names, ontology_summary)
    if matched is None:
        return None

    expanded = type_names_with_subclasses(
        matched, ontology_summary=ontology_summary, include_subclasses=True
    )
    return _fixture(
        cypher=COUNT_BY_TYPE_CYPHER,
        params={"type_names": expanded},
        explanation=(
            f"Count entities of type {matched}"
            + (" (incl. subclasses)" if len(expanded) > 1 else "")
            + " via entities_of_type_count."
        ),
        template=TEMPLATE_COUNT_BY_TYPE,
    )


def try_list_query(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """List entities of a type with LIMIT (allowlisted page template).

    Also matches ORDER BY / sorted-by suffixes and top-N / first-N variants.
    Templates still ``ORDER BY e.id`` (Memory allowlist / ADR 0013 helpers);
    recognized order props are noted in the explanation only.
    """
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None
    if _COUNT_RE.match(q):
        return None

    order_prop: str | None = None
    order_dir: str | None = None
    limit: int | None = None
    label: str | None = None

    # top/first/last N <type>
    m = _TOP_N_LIST_RE.match(q)
    if m:
        label = (m.group("label") or "").strip()
        limit = _clamp_limit(m.group("limit"))
    else:
        # list/show N <type>
        m = _N_PREFIX_LIST_RE.match(q)
        if m:
            label = (m.group("label") or "").strip()
            limit = _clamp_limit(m.group("limit"))
        else:
            # Strip ORDER BY / LIMIT then match core list pattern.
            q_core, order_prop, order_dir = _strip_order_by_suffix(q)
            q_core, lim_suffix = _strip_limit_suffix(q_core)
            # Do not steal filter questions after cleanup.
            if _FILTER_RE.match(q_core) or _FILTER_RE.match(q):
                return None
            m = _LIST_RE.match(q_core) or _LIST_RE.match(q)
            if not m:
                return None
            label = (m.group("label") or "").strip()
            label, op2, od2 = _strip_order_by_suffix(label)
            if op2:
                order_prop, order_dir = op2, od2
            limit = _clamp_limit(m.group("limit") or lim_suffix)

    if not label:
        return None
    # Label may still carry order/limit fragments (top-N branch).
    label, op3, od3 = _strip_order_by_suffix(label)
    if op3:
        order_prop, order_dir = op3, od3
    label, lim_label = _strip_limit_suffix(label)
    if lim_label is not None:
        limit = lim_label

    # "list authors of books" is a hop — leave for hop fixture.
    if re.search(r"(?i)\b(?:of|for|related\s+to|and\s+their)\b", label):
        return None
    if limit is None:
        limit = DEFAULT_LIST_LIMIT
    matched = resolve_type_name(label, type_names, ontology_summary)
    if matched is None:
        return None
    expanded = type_names_with_subclasses(
        matched, ontology_summary=ontology_summary, include_subclasses=True
    )
    expl = f"List up to {limit} entities of type {matched} via entities_of_type"
    if order_prop:
        expl += f" (requested order by {order_prop}"
        if order_dir:
            expl += f" {order_dir}"
        expl += "; template orders by id)"
    return _fixture(
        cypher=LIST_BY_TYPE_CYPHER,
        params={
            "type_names": expanded,
            "after_id": None,
            "limit": limit,
        },
        explanation=expl + ".",
        template=TEMPLATE_LIST_BY_TYPE,
    )



