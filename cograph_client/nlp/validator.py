import re

MUTATION_KEYWORDS = {"INSERT", "DELETE", "DROP", "CREATE", "CLEAR", "LOAD", "COPY", "MOVE", "ADD"}

# The live URI namespace. `omnix.dev` is the retired one (renamed 2026-04-27;
# deployed graph stores were migrated by a one-shot script) — it no longer
# resolves to anything in any graph, so any occurrence is normalized away.
ONTO_BASE = "https://cograph.tech"
LEGACY_ONTO_HOSTS = ("omnix.dev",)

_BARE_URI_RE = re.compile(
    r"<https://(?:"
    + "|".join(re.escape(h) for h in (ONTO_BASE.removeprefix("https://"), *LEGACY_ONTO_HOSTS))
    + r")/([\w/.\-]+)>"  # hyphens matter: tenants and _safe_id entity ids carry them
)


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
    # <https://cograph.tech/Property> → <https://cograph.tech/types/Property>
    # <https://cograph.tech/bedrooms> → <https://cograph.tech/types/Property/attrs/bedrooms>
    # Also rewrite the retired omnix.dev namespace (renamed 2026-04-27) onto the
    # live one — the LLM can still echo it back from a stale prompt or example.
    # Correct path shapes (/types/, /onto/, /entities/, /graphs/, …) keep their
    # shape; only the host is normalized.
    def _fix_bare_uri(m: re.Match) -> str:
        path = m.group(1)
        if path.startswith(("types/", "onto/", "entities/", "graphs/", "functions/", "kgs/", "attr_meta/")):
            return f"<{ONTO_BASE}/{path}>"  # shape already correct
        # PascalCase = bare type name → add /types/
        if path[0].isupper():
            return f"<{ONTO_BASE}/types/{path}>"
        # lowercase = likely a bare attribute name (bedrooms, price, etc.). Can't
        # place it without knowing the owning type, so leave the path alone and
        # normalize only the host — pipeline.py's _fix_attribute_uris gets a
        # second, ontology-grounded pass at it right after this.
        return f"<{ONTO_BASE}/{path}>"

    result = _BARE_URI_RE.sub(_fix_bare_uri, result)

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
