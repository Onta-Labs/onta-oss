"""Cypher relationship-type dialect: extract, flag, rewrite.

Dual-write shortcuts are always UPPER_SNAKE (``sanitize_rel_type``). Lowercase
``[:in_trial]`` is a silent zero-row dialect miss, not a planner.
"""
from __future__ import annotations

import re

# A Cypher relationship pattern: `-[`, an optional variable, `:`, then the type
# expression. Stops at `]`, a var-length `*`, or a property map `{` so
# `-[:SUBCLASS_OF*1..3]->` and `-[r:HAS_X {k: 1}]->` yield just the type part.
# Alternation (`-[:A|B]->`, `-[:A|:B]->`) is split by the caller.
_CYPHER_REL_PATTERN_RE = re.compile(
    r"-\[\s*(?:[A-Za-z_][A-Za-z0-9_]*)?\s*:([^\]{*]+)"
)

# A well-formed Neo4j relationship type token. `sanitize_rel_type` can only ever
# emit this shape, so anything else is a parse artefact rather than a rel type.
_REL_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _cypher_rel_types(cypher: str) -> list[str]:
    """Return every relationship TYPE token a Cypher string traverses."""
    out: list[str] = []
    for raw in _CYPHER_REL_PATTERN_RE.findall(cypher or ""):
        for part in raw.split("|"):
            token = part.strip().strip("`").lstrip(":").strip()
            if token:
                out.append(token)
    return out


def _cypher_invented_rel_types(cypher: str) -> list[str]:
    """Return traversed relationship types that CANNOT exist in this graph.

    Every relationship type in the property graph is minted by
    :func:`infona_client.graph.facts.sanitize_rel_type`, which upper-cases the
    ontology leaf (``lead_sponsor`` → ``LEAD_SPONSOR``). Neo4j relationship
    types are case-sensitive, so a token carrying a lower-case letter matches
    nothing and its MATCH can only ever return zero rows.
    """
    seen: dict[str, None] = {}
    for token in _cypher_rel_types(cypher):
        if not _REL_TOKEN_RE.match(token):
            continue
        if any(ch.islower() for ch in token):
            seen.setdefault(token, None)
    return list(seen)


def rewrite_lowercase_rel_types(cypher: str) -> tuple[str, bool]:
    """Rewrite mixed/lower-case rel type tokens to ``sanitize_rel_type`` UPPER.

    Dual-write shortcuts are always UPPER_SNAKE_CASE (``in_trial`` →
    ``IN_TRIAL``). LLMs copy ontology leaves into ``[:in_trial]``, which Neo4j
    treats as a distinct empty type and silently returns zero rows. This is
    dialect repair — not a planner and not a new writer. Idempotent.
    """
    text = cypher or ""
    if not text:
        return text, False
    from infona_client.graph.facts import sanitize_rel_type
    from infona_client.graph.scope import GraphScopeError

    pieces: list[str] = []
    last = 0
    changed = False
    for m in _CYPHER_REL_PATTERN_RE.finditer(text):
        expr = m.group(1)
        new_parts: list[str] = []
        expr_changed = False
        for part in expr.split("|"):
            lead_ws_len = len(part) - len(part.lstrip())
            trail_ws_len = len(part) - len(part.rstrip())
            lead_ws = part[:lead_ws_len]
            trail_ws = part[len(part) - trail_ws_len :] if trail_ws_len else ""
            core = (
                part[lead_ws_len : len(part) - trail_ws_len]
                if trail_ws_len
                else part[lead_ws_len:]
            )
            colon = ""
            if core.startswith(":"):
                colon = ":"
                core = core[1:]
            ticks = len(core) >= 2 and core.startswith("`") and core.endswith("`")
            token = core[1:-1] if ticks else core
            if not _REL_TOKEN_RE.match(token) or not any(ch.islower() for ch in token):
                new_parts.append(part)
                continue
            try:
                upper = sanitize_rel_type(token)
            except GraphScopeError:
                new_parts.append(part)
                continue
            if upper == token:
                new_parts.append(part)
                continue
            rebuilt = f"{lead_ws}{colon}"
            rebuilt += f"`{upper}`" if ticks else upper
            rebuilt += trail_ws
            new_parts.append(rebuilt)
            expr_changed = True
        pieces.append(text[last : m.start(1)])
        pieces.append("|".join(new_parts))
        last = m.end(1)
        changed = changed or expr_changed
    if not changed:
        return text, False
    pieces.append(text[last:])
    return "".join(pieces), True


# Kept short: GraphQueryError truncates at 600 chars via scrub_store_detail.
REL_TRAVERSAL_FEEDBACK = (
    "Rewrite as (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(from), "
    "(a)-[:OBJECT]->(to), (a)-[:PREDICATE]->(p:Property) WHERE p.name = '<leaf>' "
    "— or set the JSON `template` field to related_entities / "
    "related_entity_name_filter with matching params. "
    "Relationships are stored as :Assertion nodes, never as a typed edge named "
    "after the ontology leaf; every rel type is UPPER_SNAKE_CASE."
)

MAX_REPORTED_REL_TYPES = 3


def invented_rel_error_detail(invented: list[str]) -> str:
    """Store-error body for remaining invented rel types after dialect repair."""
    shown = invented[:MAX_REPORTED_REL_TYPES]
    more = "" if len(invented) == len(shown) else ", …"
    return (
        "generated Cypher traverses relationship type(s) "
        f"{', '.join(shown)}{more} that cannot exist. "
        + REL_TRAVERSAL_FEEDBACK
    )


def apply_cypher_dialect_rewrites(cypher: str) -> tuple[str, dict[str, float]]:
    """OPTIONAL MATCH value-filter rewrite, then lowercase rel dialect repair."""
    from infona_client.nlp.cypher_filter_integrity import rewrite_optional_value_filters

    flags: dict[str, float] = {}
    cypher, opt_rewritten = rewrite_optional_value_filters(cypher)
    if opt_rewritten:
        flags["cypher_optional_match_rewritten"] = 1.0
    cypher, rel_rewritten = rewrite_lowercase_rel_types(cypher)
    if rel_rewritten:
        flags["cypher_rel_types_rewritten"] = 1.0
    return cypher, flags
