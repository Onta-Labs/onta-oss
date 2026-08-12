"""NL→Cypher generators (E6 quality beyond the count stub).

Provides:

1. **Deterministic fixtures** (hermetic, no LLM) for common NL shapes.
   Fixtures compose **ADR 0013 semantic helper templates**
   (``entities_of_type``, ``literal_values``, ``related_entities``, …) — they
   do **not** translate SPARQL strings to Cypher.
2. Helpers to turn GraphStore records into the binding shape the answer
   formatter already understands (answer-set quality; not SPARQL match).
3. Ontology text formatting from :func:`ontology_catalog.schema_types_for_kg`
   summaries (when a store is present).

When ``INFONA_GRAPH_BACKEND=neo4j``, the pipeline tries fixtures first, then
falls back to the LLM Cypher prompt path if API keys exist. Fixtures prefer
allowlisted semantic templates so Memory and Neo4j both execute without
free-form Cypher. Neptune SPARQL remains the default when backend != neo4j.

Answer quality is measured by the golden-query suite (expected answer sets),
not by query-text equivalence with SPARQL — see
``docs/plans/neo4j-golden-queries.md``.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from infona_client.graph.rdfs_helpers import (
    ENTITIES_OF_TYPE_COUNT_CYPHER,
    ENTITIES_OF_TYPE_CYPHER,
    LITERAL_COMPARE_CYPHER,
    LITERAL_VALUES_CYPHER,
    RELATED_ENTITIES_CYPHER,
    RELATED_ENTITY_NAME_FILTER_CYPHER,
    RELATED_ENTITY_NAME_FILTER_INVERSE_CYPHER,
    TEMPLATE_ENTITIES_OF_TYPE,
    TEMPLATE_ENTITIES_OF_TYPE_COUNT,
    TEMPLATE_LITERAL_COMPARE,
    TEMPLATE_LITERAL_VALUES,
    TEMPLATE_RELATED_ENTITIES,
    TEMPLATE_RELATED_ENTITY_NAME_FILTER,
    type_names_with_subclasses,
)
from infona_client.graph.store import GraphRecord

# ---------------------------------------------------------------------------
# Type matching
# ---------------------------------------------------------------------------

_TRAILING_PUNCT_RE = re.compile(r"[?!.\s]+$")

# Strip trailing plural / noise words for type matching.
_NOISE_RE = re.compile(
    r"(?i)\b(?:entities|records|rows|items|entries|instances|of|the|a|an)\b"
)
_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9_]+")

# Types listed in ontology summary lines like "Type: Person" / "- Person ["
_TYPE_LINE_RE = re.compile(
    r"(?im)^\s*(?:Type:\s*|[-*]\s+|•\s+)([A-Za-z][A-Za-z0-9_]*)\b"
)

# Safe property / attr keys only (never interpolate free text into Cypher).
_SAFE_PROP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 200


def extract_type_names_from_ontology(ontology_summary: str) -> list[str]:
    """Best-effort type leaves from the SPARQL-era ontology summary text."""
    names: list[str] = []
    seen: set[str] = set()
    for m in _TYPE_LINE_RE.finditer(ontology_summary or ""):
        name = m.group(1)
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        names.append(name)
    return names


def _normalize_type_token(text: str) -> str:
    t = _NOISE_RE.sub(" ", text or "")
    t = _NON_ALNUM_RE.sub("", t.strip())
    return t


# Common irregular plurals → singular for type matching / guessing.
_IRREGULAR_SINGULAR = {
    "people": "person",
    "men": "man",
    "women": "woman",
    "children": "child",
    "mice": "mouse",
}


def match_type_name(label: str, type_names: list[str]) -> str | None:
    """Match a free-text label to an ontology type leaf (case-insensitive).

    Tries exact, singular/plural strip, and substring containment.
    """
    if not label or not type_names:
        return None
    needle = _normalize_type_token(label).lower()
    if not needle:
        return None
    by_lower = {t.lower(): t for t in type_names}
    if needle in by_lower:
        return by_lower[needle]
    # irregular plurals
    if needle in _IRREGULAR_SINGULAR and _IRREGULAR_SINGULAR[needle] in by_lower:
        return by_lower[_IRREGULAR_SINGULAR[needle]]
    # singular: books → book
    if needle.endswith("s") and needle[:-1] in by_lower:
        return by_lower[needle[:-1]]
    if needle.endswith("ies") and (needle[:-3] + "y") in by_lower:
        return by_lower[needle[:-3] + "y"]
    # Containment either way (longest first)
    ordered = sorted(type_names, key=lambda t: len(t), reverse=True)
    for t in ordered:
        tl = t.lower()
        if tl in needle or needle in tl:
            return t
    return None


def guess_type_name(label: str) -> str | None:
    """PascalCase type guess when ontology is empty (tests / bootstrap)."""
    raw = _normalize_type_token(label)
    if not raw:
        return None
    lower = raw.lower()
    if lower in _IRREGULAR_SINGULAR:
        stem = _IRREGULAR_SINGULAR[lower]
        return stem[0].upper() + stem[1:]
    guess = raw
    if guess.islower():
        guess = guess[0].upper() + guess[1:]
        if guess.endswith("s") and len(guess) > 1:
            guess = guess[:-1]
    elif guess.endswith("s") and len(guess) > 1 and guess[:-1].istitle():
        # Books → Book when already title-ish
        if guess[-2].islower():
            guess = guess[:-1]
    return guess or None

def resolve_type_name(
    label: str, type_names: list[str] | None, ontology_summary: str = ""
) -> str | None:
    names = (
        list(type_names)
        if type_names is not None
        else extract_type_names_from_ontology(ontology_summary)
    )
    matched = match_type_name(label, names) if names else None
    if matched is not None:
        return matched
    return guess_type_name(label)


# ---------------------------------------------------------------------------
# Canonical Cypher — ADR 0013 semantic helpers (rdfs_helpers / schema_bootstrap)
# ---------------------------------------------------------------------------

# Back-compat aliases (string shape for older imports / free-form fallbacks).
COUNT_BY_TYPE_CYPHER = ENTITIES_OF_TYPE_COUNT_CYPHER
COUNT_TOTAL_CYPHER = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
    "RETURN count(*) AS n"
)
LIST_BY_TYPE_CYPHER = ENTITIES_OF_TYPE_CYPHER
FILTER_PROP_EQ_CYPHER = LITERAL_VALUES_CYPHER
HOP_OUT_CYPHER = RELATED_ENTITIES_CYPHER

# Template names preferred by the pipeline when the fixture matches.
# Primary names are ADR 0013 semantic helpers; legacy entity_* names remain
# registered in schema_bootstrap for explore_store.
TEMPLATE_COUNT_BY_TYPE = TEMPLATE_ENTITIES_OF_TYPE_COUNT
TEMPLATE_COUNT_TOTAL = "entity_count_total"
TEMPLATE_LIST_BY_TYPE = TEMPLATE_ENTITIES_OF_TYPE
TEMPLATE_FILTER_PROP_EQ = TEMPLATE_LITERAL_VALUES
TEMPLATE_HOP_OUT = TEMPLATE_RELATED_ENTITIES


# ---------------------------------------------------------------------------
# NL patterns
# ---------------------------------------------------------------------------

_COUNT_RE = re.compile(
    r"(?ix)"
    r"^(?:"
    r"(?:how\s+many|count(?:\s+the|\s+of)?|number\s+of|total(?:\s+number\s+of)?)"
    r"\s+"
    r"(?P<label>.+?)"
    r"(?:\s+(?:are\s+there|do\s+we\s+have|exist|in\s+(?:the\s+)?\w+))?"
    r"|"
    r"count\s+(?P<label2>.+)"
    r")$"
)

_LIST_RE = re.compile(
    r"(?ix)^"
    r"(?:list|show(?:\s+me)?|get|find|what\s+are)"
    r"\s+"
    r"(?:all\s+)?"
    r"(?P<label>.+?)"
    r"(?:\s+(?:with\s+)?limit\s+(?P<limit>\d+))?"
    r"$"
)

# "top 5 books" / "first 10 authors" / "show me the top 3 books"
_TOP_N_LIST_RE = re.compile(
    r"(?ix)^"
    r"(?:(?:list|show(?:\s+me)?|get|find)\s+)?"
    r"(?:the\s+)?"
    r"(?:top|first|last)\s+"
    r"(?P<limit>\d+)\s+"
    r"(?P<label>.+?)"
    r"$"
)

# "list N books" / "show 10 books"
_N_PREFIX_LIST_RE = re.compile(
    r"(?ix)^"
    r"(?:list|show(?:\s+me)?|get|find)\s+"
    r"(?P<limit>\d+)\s+"
    r"(?P<label>.+?)"
    r"$"
)

# Strip trailing ORDER BY / sorted-by so list/filter still match hermetic templates
# (allowlisted entities_of_type already ORDER BY e.id; property ORDER BY is LLM path).
_ORDER_BY_SUFFIX_RE = re.compile(
    r"(?ix)\s+"
    r"(?:ordered|sorted)\s+by\s+"
    r"(?P<order_prop>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s+(?P<order_dir>asc|desc|ascending|descending))?"
    r"$"
)

# Optional trailing LIMIT on filter / hop (and list after order-by strip).
_LIMIT_SUFFIX_RE = re.compile(
    r"(?ix)\s+(?:with\s+)?limit\s+(?P<limit>\d+)$"
)

# Safe Entity properties for ORDER BY annotations (params only; template still
# orders by e.id — free-form property ORDER BY is not in the Memory allowlist).
_SAFE_ORDER_PROPS = frozenset(
    {"id", "name", "title", "primary_type", "source", "label"}
)

# "books where title is Dune" / "list books with status equals published"
_FILTER_RE = re.compile(
    r"(?ix)^"
    r"(?:(?:list|show(?:\s+me)?|find|get)\s+)?"
    r"(?P<label>.+?)\s+"
    r"(?:where|with|having)\s+"
    r"(?P<prop>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(?:is|=|equals?|==)\s+"
    r"[\"']?(?P<value>.+?)[\"']?"
    r"$"
)

# "which books cost less than 15" / "books with price under 15 dollars"
# / "books cheaper than 15" / "price under 15"
_NUMERIC_FILTER_RE = re.compile(
    r"(?ix)^"
    r"(?:(?:list|show(?:\s+me)?|find|get|which|what)\s+)?"
    r"(?P<label>.+?)\s+"
    r"(?:"
    r"(?:cost|priced?|costs?|cheaper|more\s+expensive)\s+"
    r"(?P<cost_op>less\s+than|under|below|more\s+than|over|above|at\s+least|at\s+most|exactly|than)\s+"
    r"(?:\$|USD\s*)?(?P<cost_num>\d+(?:\.\d+)?)\s*(?:dollars?|usd|\$)?"
    r"|"
    r"(?:with|having|where)\s+(?P<prop>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(?P<cmp><=|>=|<|>|=|==|less\s+than|under|below|more\s+than|over|above|at\s+least|at\s+most|equals?)\s+"
    r"(?:\$|USD\s*)?(?P<num>\d+(?:\.\d+)?)\s*(?:dollars?|usd|\$)?"
    r")"
    r"(?:\s+.*)?$"
)

# "list books with genre Classic Fiction" / "books that have genre Romance"
# / "which books are in the genre Classic Fiction" / "books of genre Fantasy"
_REL_NAME_FILTER_RE = re.compile(
    r"(?ix)^"
    r"(?:(?:list|show(?:\s+me)?|find|get|which|what)\s+)?"
    r"(?P<label>.+?)\s+"
    r"(?:with|having|that\s+have|have|in|of|from)\s+"
    r"(?:the\s+)?"
    r"(?P<rel>genre|author|publisher|category)\s+"
    r"[\"']?(?P<value>.+?)[\"']?"
    r"$"
)

_CMP_OP_MAP = {
    "<": "lt",
    "less than": "lt",
    "under": "lt",
    "below": "lt",
    ">": "gt",
    "more than": "gt",
    "over": "gt",
    "above": "gt",
    "<=": "le",
    "at most": "le",
    ">=": "ge",
    "at least": "ge",
    "=": "eq",
    "==": "eq",
    "equals": "eq",
    "equal": "eq",
    "exactly": "eq",
}

# Natural-language cost/price props for "cost less than N" phrases.
# Prefer short leaves first (export dual-writes both `price` and `has_price`).
_COST_PROP_CANDIDATES = ("price", "has_price", "cost", "has_cost", "amount")

# "products made by Acme" / "books written by Orwell" / "books by Herbert"
_MADE_BY_FILTER_RE = re.compile(
    r"(?ix)^"
    r"(?:(?:list|show(?:\s+me)?|find|get|which|what)\s+)?"
    r"(?P<label>.+?)\s+"
    r"(?:made\s+by|written\s+by|sold\s+by|published\s+by|authored\s+by|by)\s+"
    r"[\"']?(?P<value>.+?)[\"']?"
    r"$"
)

# "authors of books" / "list organizations related to people"
_HOP_OF_RE = re.compile(
    r"(?ix)^"
    r"(?:(?:list|show(?:\s+me)?|find|get|what|which)\s+)?"
    r"(?P<target>.+?)\s+"
    r"(?:of|for|related\s+to|connected\s+to|linked\s+to)\s+"
    r"(?:the\s+)?"
    r"(?P<source>.+?)"
    r"$"
)

# "books and their authors"
_HOP_THEIR_RE = re.compile(
    r"(?ix)^"
    r"(?:(?:list|show(?:\s+me)?|find|get)\s+)?"
    r"(?P<source>.+?)\s+and\s+their\s+(?P<target>.+?)"
    r"$"
)

# Optional "via works_at" suffix for hop patterns (stripped before type match).
_VIA_REL_RE = re.compile(
    r"(?ix)\s+via\s+(?P<rel>[A-Za-z_][A-Za-z0-9_]*)$"
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


def _resolve_cost_prop(ontology_summary: str) -> str:
    """Pick price/cost prop key present in the ontology text, default price."""
    text = ontology_summary or ""
    for cand in _COST_PROP_CANDIDATES:
        if re.search(rf"(?i)\b{re.escape(cand)}\b", text):
            return cand
    return "price"


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
        prop_key = _resolve_cost_prop(ontology_summary)
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
        prop = (m.group("prop") or "").strip()
        if not _SAFE_PROP_RE.match(prop):
            return None
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
    """Filter subjects by a related entity's display name (genre/author/…)."""
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None
    q, _order_prop, _order_dir = _strip_order_by_suffix(q)
    q, limit = _strip_limit_suffix(q)
    m = _REL_NAME_FILTER_RE.match(q)
    if not m:
        return None
    label = (m.group("label") or "").strip()
    rel = (m.group("rel") or "").strip().lower()
    value = _TRAILING_PUNCT_RE.sub("", (m.group("value") or "").strip())
    value, lim_from_value = _strip_limit_suffix(value)
    if lim_from_value is not None:
        limit = lim_from_value
    if not value or not _SAFE_PROP_RE.match(rel):
        return None
    # Defer "with author is Herbert" / "with genre equals Romance" to equality
    # filter — those are prop=value shapes, not related-entity names.
    if re.match(r"(?i)^(is|equals?|=|==)\b", value):
        return None
    matched = resolve_type_name(label, type_names, ontology_summary)
    if matched is None:
        return None

    # Map natural words onto common relationship leaves used by inferred ingest.
    rel_map = {
        "genre": "has_genre",
        "author": "has_author",
        "publisher": "has_publisher",
        "category": "has_genre",
    }
    rel_attr = rel_map.get(rel, rel)
    # Prefer schema keys when present (has_genre vs genre).
    if ontology_summary:
        if re.search(rf"(?i)\bhas_{re.escape(rel)}\b", ontology_summary):
            rel_attr = f"has_{rel}"
        elif re.search(rf"(?i)\b{re.escape(rel)}\b.*relationship", ontology_summary):
            # keep has_* default when both literal+rel exist
            pass

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


def try_made_by_filter_query(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """Filter subjects by a related party name (made by / written by / by X)."""
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None
    q, _order_prop, _order_dir = _strip_order_by_suffix(q)
    q, limit = _strip_limit_suffix(q)
    m = _MADE_BY_FILTER_RE.match(q)
    if not m:
        return None
    label = (m.group("label") or "").strip()
    value = _TRAILING_PUNCT_RE.sub("", (m.group("value") or "").strip())
    value, lim_from_value = _strip_limit_suffix(value)
    if lim_from_value is not None:
        limit = lim_from_value
    if not value:
        return None
    matched = resolve_type_name(label, type_names, ontology_summary)
    if matched is None:
        return None
    # Phrase → preferred leaves (makers / creators only — never has_genre).
    phrase = (m.group(0) or "").lower()
    if "written" in phrase or "authored" in phrase:
        candidates = ("has_author", "written_by", "authored_by")
    elif "published" in phrase:
        candidates = ("has_publisher", "published_by", "publisher")
    elif "sold" in phrase:
        candidates = ("sold_by", "has_seller", "seller")
    elif "made" in phrase:
        candidates = ("made_by", "manufacturer", "has_manufacturer")
    else:
        # bare "by X" — prefer maker/author leaves present on this type
        candidates = (
            "made_by",
            "has_author",
            "written_by",
            "sold_by",
            "published_by",
            "has_publisher",
        )
    text = ontology_summary or ""
    section = text
    if matched:
        m_sec = re.search(
            rf"(?ims)Type:\s*{re.escape(matched)}\b.*?(?=^Type:|\Z)",
            text,
        )
        if m_sec:
            section = m_sec.group(0)
    rel_attr: str | None = None
    for cand in candidates:
        if re.search(rf"(?i)\b{re.escape(cand)}\b", section):
            rel_attr = cand
            break
    if rel_attr is None:
        # Inverse: Organization.makes -> Product ("products made by Acme").
        inv_candidates = ("makes", "sells", "manufactures", "produces")
        inv_rel = None
        for cand in inv_candidates:
            if re.search(rf"(?i)\b{re.escape(cand)}\b.*relationship", text):
                inv_rel = cand
                break
        if inv_rel is None:
            return None
        expanded = type_names_with_subclasses(
            matched, ontology_summary=ontology_summary, include_subclasses=True
        )
        return _fixture(
            cypher=RELATED_ENTITY_NAME_FILTER_INVERSE_CYPHER,
            params={
                "type_names": expanded,
                "rel_attr": inv_rel,
                "target_name": value,
                "limit": limit if limit is not None else DEFAULT_LIST_LIMIT,
            },
            explanation=(
                f"Find {matched} entities that {inv_rel} from maker named "
                f"{value!r} (inverse related_entity_name_filter)."
            ),
            template="related_entity_name_filter_inverse",
        )
    expanded = type_names_with_subclasses(
        matched, ontology_summary=ontology_summary, include_subclasses=True
    )
    # Literal attribute (common for free-text ingest): use equality filter.
    # Relationship edge: related_entity_name_filter.
    is_literal = bool(
        re.search(
            rf"(?i)-\s*{re.escape(rel_attr)}\s*:\s*\w+\s*\(literal",
            section,
        )
    )
    if is_literal:
        # CONTAINS so "Acme" matches "Acme Corp" free-text literals.
        lit_cypher = """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names OR c.id IN $type_names
OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $prop_key
WITH e, coalesce(a.literal_value, e[$prop_key]) AS raw
WHERE raw IS NOT NULL AND toLower(toString(raw)) CONTAINS toLower($needle)
RETURN e.id AS id, e.name AS name, e.primary_type AS primary_type,
       coalesce(e.title, e.name) AS title, raw AS value
ORDER BY e.id
LIMIT $limit
""".strip()
        return _fixture(
            cypher=lit_cypher,
            params={
                "type_names": expanded,
                "prop_key": rel_attr,
                "needle": value,
                "limit": limit if limit is not None else DEFAULT_LIST_LIMIT,
            },
            explanation=(
                f"Find {matched} entities where {rel_attr} contains {value!r}."
            ),
            template=None,
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


def try_hop_query(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """Simple 1-hop outbound traversal between two types (optional rel attr / LIMIT)."""
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None

    q, _order_prop, _order_dir = _strip_order_by_suffix(q)
    q, limit = _strip_limit_suffix(q)

    rel_attr: str | None = None
    via = _VIA_REL_RE.search(q)
    if via:
        rel_attr = via.group("rel")
        q = q[: via.start()].strip()

    source_label: str | None = None
    target_label: str | None = None
    m = _HOP_THEIR_RE.match(q)
    if m:
        source_label = (m.group("source") or "").strip()
        target_label = (m.group("target") or "").strip()
    else:
        m = _HOP_OF_RE.match(q)
        if m:
            # "authors of books" → from Book (source) to Author (target)
            target_label = (m.group("target") or "").strip()
            source_label = (m.group("source") or "").strip()

    if not source_label or not target_label:
        return None

    # Source/target may still carry limit/order fragments from sloppy NL.
    source_label, lim_s = _strip_limit_suffix(source_label)
    target_label, lim_t = _strip_limit_suffix(target_label)
    if lim_s is not None:
        limit = lim_s
    if lim_t is not None:
        limit = lim_t
    source_label, _, _ = _strip_order_by_suffix(source_label)
    target_label, _, _ = _strip_order_by_suffix(target_label)

    from_type = resolve_type_name(source_label, type_names, ontology_summary)
    to_type = resolve_type_name(target_label, type_names, ontology_summary)
    if from_type is None or to_type is None:
        return None
    if from_type == to_type and rel_attr is None:
        # Ambiguous self-hop without a rel name — skip fixture.
        return None

    from_types = type_names_with_subclasses(
        from_type, ontology_summary=ontology_summary, include_subclasses=True
    )
    to_types = type_names_with_subclasses(
        to_type, ontology_summary=ontology_summary, include_subclasses=True
    )
    params: dict[str, Any] = {
        "from_types": from_types,
        "to_types": to_types,
        "rel_attr": rel_attr,
        "limit": limit if limit is not None else DEFAULT_LIST_LIMIT,
    }
    expl = f"1-hop relationships from {from_type} to {to_type} via related_entities"
    if rel_attr:
        expl += f" (attr={rel_attr})"
    return _fixture(
        cypher=HOP_OUT_CYPHER,
        params=params,
        explanation=expl + ".",
        template=TEMPLATE_HOP_OUT,
    )


def try_deterministic_cypher(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """Try hermetic fixtures in priority order; return first match or None."""
    for fn in (
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


# ---------------------------------------------------------------------------
# Ontology formatting (GraphStore catalog → prompt text)
# ---------------------------------------------------------------------------


def format_schema_types_for_cypher(types: Sequence[Any]) -> str:
    """Render :class:`SchemaTypeSummary` rows as Cypher-oriented ontology text.

    Accepts any objects with ``name``, optional ``entity_count``,
    ``description``, ``parent_type``, and ``attributes`` (each with
    ``name`` / ``kind`` / ``datatype`` / ``range_type`` / ``prop_key``).
    """
    lines: list[str] = []
    for t in types or ():
        name = getattr(t, "name", None) or ""
        if not name:
            continue
        count = int(getattr(t, "entity_count", 0) or 0)
        empty_suffix = " [no instances]" if count == 0 else f" ({count} entities)"
        lines.append(f"Type: {name}{empty_suffix}")
        desc = getattr(t, "description", None) or ""
        if desc:
            lines.append(f"  description: {desc}")
        parent = getattr(t, "parent_type", None)
        if parent:
            lines.append(f"  parent: {parent}")
        for a in getattr(t, "attributes", ()) or ():
            aname = getattr(a, "name", None) or ""
            if not aname:
                continue
            kind = getattr(a, "kind", None) or "literal"
            prop_key = getattr(a, "prop_key", None) or aname
            range_type = getattr(a, "range_type", None)
            datatype = getattr(a, "datatype", None) or "string"
            if kind == "relationship" or range_type:
                lines.append(
                    f"  - {aname} -> {range_type or '?'} "
                    f"(relationship, key={prop_key})"
                )
            else:
                lines.append(
                    f"  - {aname}: {datatype} (literal, key={prop_key})"
                )
    return "\n".join(lines)


async def ontology_from_graph_store(
    store: Any,
    *,
    tenant_id: str,
    kg: str,
) -> tuple[str, list[str]]:
    """Load ontology text + type names from GraphStore catalog when possible.

    Returns ``("", [])`` on any failure so the pipeline can fall back to the
    SPARQL ontology summary (or empty).
    """
    if store is None or not tenant_id or not kg:
        return "", []
    try:
        from infona_client.graph.ontology_catalog import schema_types_for_kg

        rows = await schema_types_for_kg(
            store, tenant_id=tenant_id, kg=kg, include_attrs=True
        )
        if not rows:
            return "", []
        text = format_schema_types_for_cypher(rows)
        names = [r.name for r in rows if getattr(r, "name", None)]
        return text, names
    except Exception:
        return "", []


def records_to_bindings(records: list[GraphRecord]) -> tuple[list[str], list[dict[str, str]]]:
    """Convert GraphStore records to (variables, bindings) like SPARQL results.

    Values are stringified so :meth:`NLQueryPipeline._format_answer` can render
    them without a second code path.
    """
    if not records:
        return [], []
    # Union of keys in order of first appearance
    variables: list[str] = []
    seen: set[str] = set()
    for rec in records:
        for k in rec.keys():
            if k not in seen:
                seen.add(k)
                variables.append(str(k))
    bindings: list[dict[str, str]] = []
    for rec in records:
        row: dict[str, str] = {}
        for k in variables:
            v = rec.get(k)
            if v is None:
                row[k] = ""
            else:
                row[k] = str(v)
        bindings.append(row)
    return variables, bindings


def neo4j_ask_enabled(*, explicit: bool | None = None) -> bool:
    """True when the NL path should generate Cypher instead of SPARQL.

    ``explicit`` overrides the env switch when the caller passes a flag.
    Default follows ``INFONA_GRAPH_BACKEND=neo4j``.
    """
    if explicit is not None:
        return bool(explicit)
    try:
        from infona_client.graph.kg_writer import graph_backend

        return graph_backend() == "neo4j"
    except Exception:
        import os

        return (os.environ.get("INFONA_GRAPH_BACKEND") or "neo4j").strip().lower() == "neo4j"


__all__ = [
    "COUNT_BY_TYPE_CYPHER",
    "COUNT_TOTAL_CYPHER",
    "DEFAULT_LIST_LIMIT",
    "FILTER_PROP_EQ_CYPHER",
    "HOP_OUT_CYPHER",
    "LIST_BY_TYPE_CYPHER",
    "LITERAL_COMPARE_CYPHER",
    "MAX_LIST_LIMIT",
    "RELATED_ENTITY_NAME_FILTER_CYPHER",
    "TEMPLATE_LITERAL_COMPARE",
    "TEMPLATE_RELATED_ENTITY_NAME_FILTER",
    "TEMPLATE_COUNT_BY_TYPE",
    "TEMPLATE_COUNT_TOTAL",
    "TEMPLATE_FILTER_PROP_EQ",
    "TEMPLATE_HOP_OUT",
    "TEMPLATE_LIST_BY_TYPE",
    "extract_type_names_from_ontology",
    "format_schema_types_for_cypher",
    "guess_type_name",
    "match_type_name",
    "neo4j_ask_enabled",
    "ontology_from_graph_store",
    "records_to_bindings",
    "resolve_type_name",
    "try_deterministic_cypher",
    "try_filter_query",
    "try_hop_query",
    "try_list_query",
    "try_made_by_filter_query",
    "try_numeric_filter_query",
    "try_related_name_filter_query",
    "try_stub_count_query",
]
