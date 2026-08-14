"""NL regexes and ADR 0013 template-name aliases for hermetic Cypher fixtures.

Production ``/ask`` is always LLM — these fixtures are unit-test / helper
only. Money-leaf hard-bind is unique-resolve only.
"""

from __future__ import annotations

import re

from infona_client.graph.rdfs_helpers import (
    ENTITIES_OF_TYPE_COUNT_CYPHER,
    ENTITIES_OF_TYPE_CYPHER,
    LITERAL_AGGREGATE_CYPHER,
    LITERAL_COMPARE_CYPHER,
    LITERAL_VALUES_CYPHER,
    RELATED_ENTITIES_CYPHER,
    RELATED_ENTITY_NAME_FILTER_CYPHER,
    RELATED_ENTITY_NAME_FILTER_INVERSE_CYPHER,
    TEMPLATE_ENTITIES_OF_TYPE,
    TEMPLATE_ENTITIES_OF_TYPE_COUNT,
    TEMPLATE_LITERAL_AGGREGATE,
    TEMPLATE_LITERAL_COMPARE,
    TEMPLATE_LITERAL_VALUES,
    TEMPLATE_RELATED_ENTITIES,
    TEMPLATE_RELATED_ENTITY_NAME_FILTER,
    type_names_with_subclasses,
)
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
# / "books cheaper than 15" / "parts with unit cost under 10"
# Multi-word prop branch precedes cost-verb so "unit cost under" is not stolen.
_NUMERIC_FILTER_RE = re.compile(
    r"(?ix)^"
    r"(?:(?:list|show(?:\s+me)?|find|get|which|what)\s+)?"
    r"(?P<label>.+?)\s+"
    r"(?:"
    r"(?:with|having|where)\s+"
    r"(?P<prop>[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\s+(?!is\b|less\b|under\b|below\b|more\b|over\b|above\b|at\b|"
    r"equals?\b|greater\b)[A-Za-z_][A-Za-z0-9_]*){0,3})\s+"
    r"(?:is\s+)?"
    r"(?P<cmp><=|>=|<|>|=|==|less\s+than|under|below|more\s+than|over|above|at\s+least|at\s+most|equals?)\s+"
    r"(?:\$|USD\s*)?(?P<num>\d+(?:\.\d+)?)\s*(?:dollars?|usd|\$)?"
    r"|"
    r"(?:cost|priced?|costs?|cheaper|more\s+expensive)\s+"
    r"(?P<cost_op>less\s+than|under|below|more\s+than|over|above|at\s+least|at\s+most|exactly|than)\s+"
    r"(?:\$|USD\s*)?(?P<cost_num>\d+(?:\.\d+)?)\s*(?:dollars?|usd|\$)?"
    r")"
    r"(?:\s+.*)?$"
)

# "list books with genre Classic Fiction" / "books that have genre Romance"
# / "which books are in the genre Classic Fiction" / "books of genre Fantasy"
# / "widgets at site East" / "items in warehouse East" (promoted dim cols, ONTA-538)
_REL_NAME_FILTER_RE = re.compile(
    r"(?ix)^"
    r"(?:(?:list|show(?:\s+me)?|find|get|which|what)\s+)?"
    r"(?P<label>.+?)\s+"
    r"(?:with|having|that\s+have|have|in|at|of|from)\s+"
    r"(?:the\s+)?"
    r"(?P<rel>[A-Za-z_][A-Za-z0-9_]*)\s+"
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
# Kept for back-compat / tests; production resolve uses type-scoped
# :func:`infona_client.nlp.numeric_attr_resolve.resolve_cost_prop`.
_COST_PROP_CANDIDATES = (
    "price",
    "has_price",
    "cost",
    "has_cost",
    "unit_cost",
    "list_price",
    "assay_cost",
    "tuition_usd",
    "tuition",
    "amount",
    "value_usd",
    "msrp",
    "fee",
    "charge",
    "rate",
)

# "products made by Acme" / "books written by Orwell" / "books by Herbert"
_MADE_BY_FILTER_RE = re.compile(
    r"(?ix)^"
    r"(?:(?:list|show(?:\s+me)?|find|get|which|what)\s+)?"
    r"(?P<label>.+?)\s+"
    r"(?:made\s+by|written\s+by|sold\s+by|published\s+by|authored\s+by|"
    r"supplied\s+by|from\s+vendor|from\s+supplier|by\s+vendor|by\s+supplier|by)\s+"
    r"[\"']?(?P<value>.+?)[\"']?"
    r"$"
)

# "total amount of grants" / "sum of amount for widgets" / "average mileage of vehicles"
_AGG_RE = re.compile(
    r"(?ix)^"
    r"(?:what(?:'s|\s+is)\s+)?"
    r"(?:the\s+)?"
    r"(?P<agg>total|sum|average|avg|mean|minimum|min|maximum|max)"
    r"(?:\s+(?:of|for))?"
    r"(?:\s+(?:the\s+)?)?"
    r"(?P<prop>[A-Za-z_][A-Za-z0-9_]*)?"
    r"(?:\s+(?:of|for|across|over|on))?"
    r"(?:\s+(?:all\s+)?)?"
    r"(?P<label>.+?)"
    r"$"
)

_AGG_OP_MAP = {
    "total": "sum",
    "sum": "sum",
    "average": "avg",
    "avg": "avg",
    "mean": "avg",
    "minimum": "min",
    "min": "min",
    "maximum": "max",
    "max": "max",
}

# Prefer these leaves when the NL omits an explicit prop ("total of grants").
_NUMERIC_AGG_PROP_CANDIDATES = (
    "amount",
    "price",
    "cost",
    "unit_cost",
    "value",
    "value_usd",
    "mileage",
    "qty",
    "quantity",
    "enrollment",
    "reading",
    "score",
    "rating",
    "total",
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


