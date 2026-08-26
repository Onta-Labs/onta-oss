from __future__ import annotations

import re

_RETURN_BODY_RE = re.compile(
    r"(?is)\bRETURN\b\s+(?:DISTINCT\s+)?(.+?)"
    r"(?=\bORDER\s+BY\b|\bLIMIT\b|\bUNION\b|$)"
)
_AS_ALIAS_RE = re.compile(r"(?i)\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$")
_TRAILING_IDENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*$")
_CYPHER_RETURN_SKIP = frozenset(
    {
        "distinct",
        "null",
        "true",
        "false",
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "collect",
        "coalesce",
        "tostring",
        "tofloat",
        "tointeger",
        "type",
        "labels",
        "keys",
        "size",
        "head",
        "last",
        "as",
    }
)


def parse_sparql_results(raw: dict) -> tuple[list[str], list[dict[str, str]]]:
    """Parse SPARQL JSON results into (variable_names, bindings).

    Neptune returns results in the standard SPARQL Results JSON format:
    {
        "head": {"vars": ["s", "p", "o"]},
        "results": {"bindings": [{"s": {"type": "uri", "value": "..."}, ...}]}
    }
    """
    head = raw.get("head", {})
    variables = head.get("vars", [])

    results = raw.get("results", {})
    raw_bindings = results.get("bindings", [])

    bindings = []
    for row in raw_bindings:
        parsed_row = {}
        for var in variables:
            if var in row:
                parsed_row[var] = row[var].get("value", "")
        bindings.append(parsed_row)

    return variables, bindings


def unbound_projection_vars(
    variables: list[str], bindings: list[dict[str, str]]
) -> list[str]:
    """Return projected variables that bound in ZERO rows.

    ``parse_sparql_results`` keeps a per-row key only when that row bound the
    variable, so a projected variable that never binds (e.g. an OPTIONAL for an
    attribute that isn't present on any matching entity, or an attribute whose
    URI drifted from the instance data) silently disappears from every row with
    no signal. The caller then can't tell "no such column" apart from "column
    exists but empty".

    This companion detects those vars — present in the query projection
    (``head.vars``) but absent from every parsed row — so the query layer can
    report them honestly ("latency: not present on any matching entity")
    instead of omitting them without a trace. It is intentionally a SEPARATE
    helper: ``parse_sparql_results`` keeps its ``(vars, bindings)`` contract
    that 100+ call sites depend on.

    A projection over zero rows is treated as "no signal" (returns ``[]``): with
    no rows at all we can't distinguish an unbound var from an empty result set,
    and the empty-result path is reported elsewhere.
    """
    if not bindings:
        return []
    bound: set[str] = set()
    for row in bindings:
        bound.update(row.keys())
    return [var for var in variables if var not in bound]


def _split_top_level_csv(body: str) -> list[str]:
    """Split a RETURN body on commas that are not inside ``()`` / ``[]`` / ``{}``."""
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in body or "":
        if ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            item = "".join(buf).strip()
            if item:
                items.append(item)
            buf = []
        else:
            buf.append(ch)
    item = "".join(buf).strip()
    if item:
        items.append(item)
    return items


def cypher_return_aliases(cypher: str) -> list[str]:
    """Return aliases from the last Cypher ``RETURN`` clause, first-seen order.

    Used to detect columns a template rescue silently dropped (generated
    ``RETURN person_name, date`` vs ``template:related_entities`` rows).
    """
    matches = list(_RETURN_BODY_RE.finditer(cypher or ""))
    if not matches:
        return []
    body = matches[-1].group(1).strip()
    if not body or body == "*":
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in _split_top_level_csv(body):
        as_m = _AS_ALIAS_RE.search(item)
        name = as_m.group(1) if as_m else None
        if name is None:
            ident = _TRAILING_IDENT_RE.search(item)
            name = ident.group(1) if ident else None
        if not name or name.lower() in _CYPHER_RETURN_SKIP:
            continue
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def dropped_projection_aliases(
    cypher: str,
    variables: list[str],
    bindings: list[dict[str, str]],
) -> list[str]:
    """Generated ``RETURN`` aliases that never appeared in the executed rows.

    Zero-row results are "no signal" (same as :func:`unbound_projection_vars`):
    every alias is absent, and the empty-result path is reported elsewhere.
    Computed aliases like ``path`` / ``answer`` on a miss must not be framed
    as unpopulated entity attributes.
    """
    if not bindings:
        return []
    present: set[str] = set(variables or ())
    for row in bindings or ():
        present.update(row.keys())
    return [a for a in cypher_return_aliases(cypher) if a not in present]


def apply_unbound_confidence(
    missing_vars: list[str],
    confidence: str,
    reason: str = "",
) -> tuple[str, str]:
    """Never report ``query_confidence: high`` when projected columns never bound."""
    if not missing_vars:
        return confidence, reason
    extra = "projected columns unbound: " + ", ".join(missing_vars)
    conf = (confidence or "").strip().lower()
    if conf == "high" or not conf:
        confidence = "low"
    if extra not in (reason or ""):
        reason = extra if not reason else f"{reason}; {extra}"
    return confidence, reason
