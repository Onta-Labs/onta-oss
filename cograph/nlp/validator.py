import re

MUTATION_KEYWORDS = {"INSERT", "DELETE", "DROP", "CREATE", "CLEAR", "LOAD", "COPY", "MOVE", "ADD"}


def normalize_sparql(sparql: str) -> str:
    """Fix common SPARQL syntax issues from LLM generation.

    - Expands PREFIX declarations inline (LLMs invent wrong prefixes)
    - Moves FROM clauses to after SELECT
    """
    lines = sparql.strip().split("\n")

    # Step 1: Extract and expand PREFIX declarations
    prefixes: dict[str, str] = {}
    non_prefix_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("PREFIX "):
            # Parse: PREFIX name: <uri>
            match = re.match(r"PREFIX\s+(\S+:)\s*<([^>]+)>", stripped, re.IGNORECASE)
            if match:
                prefixes[match.group(1)] = match.group(2)
            # Don't include PREFIX lines in output — we expand inline
        else:
            non_prefix_lines.append(line)

    # Expand all prefixed names in the query
    result = "\n".join(non_prefix_lines)
    for prefix, uri in prefixes.items():
        pattern = re.escape(prefix) + r"([\w/]+)"
        result = re.sub(pattern, lambda m: f"<{uri}{m.group(1)}>", result)

    # Fix common URI mistakes from LLMs that use wrong prefix expansion:
    # <https://omnix.dev/Property> → <https://omnix.dev/types/Property>
    # <https://omnix.dev/bedrooms> → <https://omnix.dev/types/Property/attrs/bedrooms>
    # But don't touch already correct: /types/, /onto/, /entities/, /graphs/
    def _fix_omnix_uri(m: re.Match) -> str:
        path = m.group(1)
        if path.startswith(("types/", "onto/", "entities/", "graphs/", "functions/", "kgs/")):
            return m.group(0)  # already correct
        # PascalCase = bare type name → add /types/
        if path[0].isupper():
            return f"<https://omnix.dev/types/{path}>"
        # lowercase = likely a bare attribute name (bedrooms, price, etc.)
        # Can't fix without knowing the type, so try onto/ namespace
        # (relationships use onto/, attributes use types/Type/attrs/)
        return m.group(0)

    result = re.sub(r"<https://omnix\.dev/([\w/.]+)>", _fix_omnix_uri, result)

    # Step 2: Fix bare aggregates — SELECT COUNT(?x) → SELECT (COUNT(?x) AS ?count)
    # Neptune requires aggregates to be aliased
    bare_agg_pattern = re.compile(
        r'\bSELECT\s+((?:COUNT|SUM|AVG|MIN|MAX)\s*\([^)]+\))',
        re.IGNORECASE,
    )
    m = bare_agg_pattern.search(result)
    if m:
        agg_expr = m.group(1)
        # Derive alias from aggregate function name
        func_name = re.match(r'(\w+)', agg_expr).group(1).lower()
        alias = func_name if func_name != "count" else "count"
        result = result[:m.start(1)] + f"({agg_expr} AS ?{alias})" + result[m.end(1):]

    # Step 3: Extract FROM clauses from anywhere and place between SELECT and WHERE
    from_pattern = re.compile(r'\bFROM\s+<[^>]+>', re.IGNORECASE)
    from_clauses = from_pattern.findall(result)
    if from_clauses:
        result = from_pattern.sub("", result).strip()
        # Insert FROM right before WHERE
        where_match = re.search(r'\bWHERE\b', result, re.IGNORECASE)
        if where_match:
            from_str = "\n".join(from_clauses)
            result = result[:where_match.start()] + from_str + "\n" + result[where_match.start():]

    return result


def validate_sparql(sparql: str) -> tuple[bool, str]:
    """Validate a SPARQL query is safe to execute.

    Returns (is_valid, error_message). If valid, error_message is empty.
    """
    if not sparql.strip():
        return False, "Empty query"

    upper = sparql.upper()
    for keyword in MUTATION_KEYWORDS:
        pattern = rf'\b{keyword}\b'
        if re.search(pattern, upper):
            return False, f"Mutation keyword '{keyword}' is not allowed in read queries"

    open_braces = sparql.count("{")
    close_braces = sparql.count("}")
    if open_braces != close_braces:
        return False, f"Mismatched braces: {open_braces} open, {close_braces} close"

    return True, ""
