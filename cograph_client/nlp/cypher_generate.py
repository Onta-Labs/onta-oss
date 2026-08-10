"""Minimal NL→Cypher generators (E6 foundation).

Full LLM quality is later E6 work. This module provides:

1. A **deterministic stub** for "count entities of type X" that proves the
   ask → confine → GraphStore.execute_read plumbing without a live LLM.
2. Helpers to turn GraphStore records into the same binding shape the SPARQL
   answer formatter already understands.

When ``COGRAPH_GRAPH_BACKEND=neo4j``, the pipeline tries the stub first for
count questions, then falls back to the LLM Cypher prompt path if keys exist.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from cograph_client.graph.store import GraphRecord

# Count-style NL patterns. Captures a free-text type phrase when present.
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


# Canonical count-by-type Cypher (matches entity_count_by_type template shape).
COUNT_BY_TYPE_CYPHER = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE e.primary_type = $primary_type "
    "RETURN count(*) AS n"
)

COUNT_TOTAL_CYPHER = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg}) "
    "RETURN count(*) AS n"
)


def try_stub_count_query(
    question: str,
    ontology_summary: str = "",
    *,
    type_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """If ``question`` is a simple count, return a scoped Cypher payload.

    Returns ``None`` when the question is not a recognized count pattern so
    the pipeline can fall through to the LLM (or fail closed).

    Payload keys: ``cypher``, ``params``, ``explanation``, ``stub``=True.
    """
    q = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not q:
        return None
    m = _COUNT_RE.match(q)
    if not m:
        return None
    label = (m.group("label") or m.group("label2") or "").strip()
    label = _TRAILING_PUNCT_RE.sub("", label)
    names = list(type_names) if type_names is not None else extract_type_names_from_ontology(
        ontology_summary
    )

    # "how many entities" / bare "count" with no type → total
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
        return {
            "cypher": COUNT_TOTAL_CYPHER,
            "params": {},
            "explanation": "Count all entities in the knowledge graph.",
            "functions_needed": [],
            "stub": True,
        }

    matched = match_type_name(label, names) if names else None
    if matched is None:
        # Still emit a count-by-type using a PascalCase guess so plumbing works
        # even when ontology summary is empty (tests / early bootstrap).
        guess = _normalize_type_token(label)
        if not guess:
            return None
        # Title-case multi-word-ish: books → Book when all lower
        if guess.islower():
            guess = guess[0].upper() + guess[1:]
            if guess.endswith("s") and len(guess) > 1:
                guess = guess[:-1]
        matched = guess

    return {
        "cypher": COUNT_BY_TYPE_CYPHER,
        "params": {"primary_type": matched},
        "explanation": f"Count entities whose primary_type is {matched}.",
        "functions_needed": [],
        "stub": True,
    }


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
    "extract_type_names_from_ontology",
    "match_type_name",
    "neo4j_ask_enabled",
    "records_to_bindings",
    "try_stub_count_query",
]
