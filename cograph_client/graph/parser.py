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
