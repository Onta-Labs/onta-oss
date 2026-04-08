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
