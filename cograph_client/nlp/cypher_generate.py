"""NL→Cypher generators (E6 quality beyond the count stub).

Provides:

1. **Deterministic fixtures** (hermetic, no LLM) for common NL shapes:
   - count by type / total
   - list entities of a type with LIMIT
   - filter by property equality
   - simple 1-hop relationship traversal
2. Helpers to turn GraphStore records into the SPARQL-style binding shape
   the answer formatter already understands.
3. Ontology text formatting from :func:`ontology_catalog.schema_types_for_kg`
   summaries (when a store is present).

When ``COGRAPH_GRAPH_BACKEND=neo4j``, the pipeline tries fixtures first, then
falls back to the LLM Cypher prompt path if API keys exist. Fixtures prefer
allowlisted templates (``template`` key on the payload) so Memory and Neo4j
both execute without free-form Cypher.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from cograph_client.graph.store import GraphRecord

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
# Canonical Cypher (matches allowlisted templates in schema_bootstrap)
# ---------------------------------------------------------------------------

COUNT_BY_TYPE_CYPHER = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE e.primary_type = $primary_type "
    "RETURN count(*) AS n"
)

COUNT_TOTAL_CYPHER = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
    "RETURN count(*) AS n"
)

LIST_BY_TYPE_CYPHER = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE e.primary_type = $primary_type "
    "AND ($after_id IS NULL OR e.id > $after_id) "
    "RETURN e.id AS id, e.tenant_id AS tenant_id, e.kg AS kg, "
    "e.primary_type AS primary_type, e.name AS name, e.source AS source "
    "ORDER BY e.id "
    "LIMIT $limit"
)

FILTER_PROP_EQ_CYPHER = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE e.primary_type = $primary_type "
    "AND e[$prop_key] = $prop_value "
    "RETURN e.id AS id, e.name AS name, e.primary_type AS primary_type "
    "ORDER BY e.id "
    "LIMIT $limit"
)

HOP_OUT_CYPHER = (
    "MATCH (a:Entity {tenant_id: $tenant_id, kg: $kg})"
    "-[r]->(b:Entity {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE a.primary_type = $from_type "
    "AND r.tenant_id = $tenant_id AND r.kg = $kg "
    "AND ($to_type IS NULL OR b.primary_type = $to_type) "
    "AND ($rel_attr IS NULL OR r.attr = $rel_attr OR type(r) = $rel_attr) "
    "RETURN a.id AS from_id, a.name AS from_name, a.primary_type AS from_type, "
    "b.id AS to_id, b.name AS to_name, b.primary_type AS to_type, "
    "type(r) AS rel_type, coalesce(r.attr, type(r)) AS attr "
    "ORDER BY a.id, b.id "
    "LIMIT $limit"
)

# Template names preferred by the pipeline when the fixture matches.
TEMPLATE_COUNT_BY_TYPE = "entity_count_by_type"
TEMPLATE_COUNT_TOTAL = "entity_count_total"
TEMPLATE_LIST_BY_TYPE = "entity_list_by_type_page"
TEMPLATE_FILTER_PROP_EQ = "entity_filter_prop_eq"
TEMPLATE_HOP_OUT = "entity_1hop_out"


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

    return _fixture(
        cypher=COUNT_BY_TYPE_CYPHER,
        params={"primary_type": matched},
        explanation=f"Count entities whose primary_type is {matched}.",
        template=TEMPLATE_COUNT_BY_TYPE,
    )


def try_list_query(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """List entities of a type with LIMIT (allowlisted page template)."""
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None
    # Do not steal count / filter questions.
    if _COUNT_RE.match(q) or _FILTER_RE.match(q):
        return None
    m = _LIST_RE.match(q)
    if not m:
        return None
    label = (m.group("label") or "").strip()
    # "list authors of books" is a hop — leave for hop fixture.
    if re.search(r"(?i)\b(?:of|for|related\s+to|and\s+their)\b", label):
        return None
    limit = _clamp_limit(m.group("limit"))
    matched = resolve_type_name(label, type_names, ontology_summary)
    if matched is None:
        return None
    return _fixture(
        cypher=LIST_BY_TYPE_CYPHER,
        params={
            "primary_type": matched,
            "after_id": None,
            "limit": limit,
        },
        explanation=f"List up to {limit} entities of type {matched}.",
        template=TEMPLATE_LIST_BY_TYPE,
    )


def try_filter_query(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """Filter entities of a type by property equality."""
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None
    m = _FILTER_RE.match(q)
    if not m:
        return None
    label = (m.group("label") or "").strip()
    prop = (m.group("prop") or "").strip()
    value = (m.group("value") or "").strip()
    value = _TRAILING_PUNCT_RE.sub("", value)
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

    return _fixture(
        cypher=FILTER_PROP_EQ_CYPHER,
        params={
            "primary_type": matched,
            "prop_key": prop_key,
            "prop_value": value,
            "limit": DEFAULT_LIST_LIMIT,
        },
        explanation=(
            f"Find {matched} entities where {prop_key} equals {value!r}."
        ),
        template=TEMPLATE_FILTER_PROP_EQ,
    )


def try_hop_query(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """Simple 1-hop outbound traversal between two types (optional rel attr)."""
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None

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

    from_type = resolve_type_name(source_label, type_names, ontology_summary)
    to_type = resolve_type_name(target_label, type_names, ontology_summary)
    if from_type is None or to_type is None:
        return None
    if from_type == to_type and rel_attr is None:
        # Ambiguous self-hop without a rel name — skip fixture.
        return None

    params: dict[str, Any] = {
        "from_type": from_type,
        "to_type": to_type,
        "rel_attr": rel_attr,
        "limit": DEFAULT_LIST_LIMIT,
    }
    expl = f"1-hop relationships from {from_type} to {to_type}"
    if rel_attr:
        expl += f" via {rel_attr}"
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
        from cograph_client.graph.ontology_catalog import schema_types_for_kg

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
    Default follows ``COGRAPH_GRAPH_BACKEND=neo4j``.
    """
    if explicit is not None:
        return bool(explicit)
    try:
        from cograph_client.graph.kg_writer import graph_backend

        return graph_backend() == "neo4j"
    except Exception:
        import os

        return (os.environ.get("COGRAPH_GRAPH_BACKEND") or "neptune").strip().lower() == "neo4j"


__all__ = [
    "COUNT_BY_TYPE_CYPHER",
    "COUNT_TOTAL_CYPHER",
    "DEFAULT_LIST_LIMIT",
    "FILTER_PROP_EQ_CYPHER",
    "HOP_OUT_CYPHER",
    "LIST_BY_TYPE_CYPHER",
    "MAX_LIST_LIMIT",
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
    "try_stub_count_query",
]
