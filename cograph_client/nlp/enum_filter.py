"""Detect over-constrained CONTAINS FILTERs against closed enum attributes.

When the ontology schema annotates a string attribute with ``[values: "a", "b"]``
(low-cardinality enum discovery in :meth:`NLQueryPipeline._fetch_ontology`), the
NL→SPARQL generator sometimes still places a free-text needle from the question
on that attribute — e.g. ``FILTER(CONTAINS(LCASE(?setting), "bladder"))`` when
listed values are only ``"adjuvant"``, ``"metastatic"``, etc.

That produces a *valid* SPARQL query that returns zero rows for structural
reasons: the needle cannot match any listed value. This module detects that
mismatch so ``ask()`` can regenerate with targeted feedback instead of answering
"No matches found."

High-cardinality attributes annotated ``[N unique values]`` are intentionally
not treated as closed enums and never trigger recovery.
"""
from __future__ import annotations

import re
from typing import NamedTuple

# Ontology line fragment, e.g.:
#   setting (string) — URI: <https://cograph.tech/types/Indication/attrs/setting>
#     [values: "adjuvant", "metastatic", ...]
_ENUM_ATTR_RE = re.compile(
    r"URI:\s*<(https://cograph\.tech/types/[^>\s]+/attrs/[^>\s]+)>\s*"
    r"\[values:\s*([^\]]+)\]",
    re.IGNORECASE,
)
_ENUM_VAL_RE = re.compile(r'"([^"]*)"')
# Ontology summary truncates long enum lists: "a", "b", … (25 total). A partial
# sample must NOT be treated as exhaustive (false "impossible" FILTERs).
_TRUNCATED_ENUM_RE = re.compile(r"\(?\s*\d+\s+total\s*\)?", re.IGNORECASE)

# FILTER(CONTAINS(LCASE(?var), "needle")) or FILTER(CONTAINS(LCASE(?var), LCASE("needle")))
_CONTAINS_FILTER_RE = re.compile(
    r"FILTER\s*\(\s*CONTAINS\s*\(\s*LCASE\s*\(\s*\?(\w+)\s*\)\s*,\s*"
    r"(?:LCASE\s*\(\s*)?\"([^\"]+)\"\s*\)?\s*\)\s*\)",
    re.IGNORECASE,
)

# ?s <attr_uri> ?o  — object var holds the attribute value being filtered
_ATTR_BIND_RE = re.compile(
    r"\?(\w+)\s+<(https://cograph\.tech/types/[^>\s]+/attrs/[^>\s]+)>\s+\?(\w+)",
)


class EnumContainsMismatch(NamedTuple):
    attr_uri: str
    var: str
    needle: str
    listed_values: tuple[str, ...]

    @property
    def attr_leaf(self) -> str:
        return self.attr_uri.rsplit("/", 1)[-1]


def parse_enum_attr_values(ontology: str) -> dict[str, list[str]]:
    """Map attribute URI → listed sample values from ``[values: ...]`` annotations.

    Ignores:
    - ``[N unique values]`` high-cardinality annotations (no closed list)
    - truncated samples that end with ``… (N total)`` — incomplete, not exhaustive
    """
    out: dict[str, list[str]] = {}
    for m in _ENUM_ATTR_RE.finditer(ontology or ""):
        uri, blob = m.group(1), m.group(2)
        if _TRUNCATED_ENUM_RE.search(blob):
            continue
        vals = _ENUM_VAL_RE.findall(blob)
        if vals:
            out[uri] = vals
    return out


def impossible_enum_contains(
    sparql: str, ontology: str
) -> list[EnumContainsMismatch]:
    """CONTAINS FILTERs whose needle cannot match any listed enum value for that attr.

    Returns mismatches only when:
    - the filtered variable is bound as the *object* of a types/.../attrs/... triple, and
    - that attribute has a closed ``[values: ...]`` list in the ontology summary, and
    - the needle is not a case-insensitive substring of any listed value.
    """
    enums = parse_enum_attr_values(ontology)
    if not enums or not sparql:
        return []

    var_attrs: dict[str, set[str]] = {}
    for m in _ATTR_BIND_RE.finditer(sparql):
        _subj, pred, obj = m.group(1), m.group(2), m.group(3)
        var_attrs.setdefault(obj, set()).add(pred)

    out: list[EnumContainsMismatch] = []
    seen: set[tuple[str, str]] = set()
    for m in _CONTAINS_FILTER_RE.finditer(sparql):
        var, needle = m.group(1), m.group(2)
        needle_l = needle.lower()
        for attr in var_attrs.get(var, ()):
            if attr not in enums:
                continue
            vals = enums[attr]
            if any(needle_l in v.lower() for v in vals):
                continue
            key = (attr, needle_l)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                EnumContainsMismatch(
                    attr_uri=attr,
                    var=var,
                    needle=needle,
                    listed_values=tuple(vals),
                )
            )
    return out


def enum_mismatch_feedback(
    mismatches: list[EnumContainsMismatch], previous_sparql: str = ""
) -> str:
    """Error feedback for a SPARQL regeneration attempt after zero rows."""
    if not mismatches:
        return ""
    lines: list[str] = []
    for m in mismatches:
        sample = ", ".join(f'"{v}"' for v in m.listed_values[:12])
        more = f" … ({len(m.listed_values)} total)" if len(m.listed_values) > 12 else ""
        lines.append(
            f'- FILTER(CONTAINS(LCASE(?{m.var}), "{m.needle}")) on attribute '
            f"`{m.attr_leaf}` (<{m.attr_uri}>) cannot match any listed value "
            f"(listed: {sample}{more}). Do NOT filter `{m.attr_leaf}` with that needle."
        )
    tail = f"\nPrevious query was:\n{previous_sparql}" if previous_sparql else ""
    return (
        "The previous query returned ZERO rows because it over-constrained "
        "string FILTERs against attributes whose known values cannot contain "
        "the filter string:\n"
        + "\n".join(lines)
        + "\n\nRewrite the SPARQL:\n"
        "- Put free-text / clinical / colloquial phrases (e.g. \"bladder surgery\", "
        "\"after surgery\") on free-text attributes such as disease, "
        "indication_summary, name, or label — not on short enum-like fields "
        "(setting, status, line_of_therapy) when their [values: ...] list is closed.\n"
        "- You may OR the same phrase across multiple free-text attributes.\n"
        "- Only FILTER an enum-listed attribute when the needle is a substring of "
        "a listed value (e.g. \"adjuvant\", \"metastatic\", \"withdrawn\")."
        + tail
    )
