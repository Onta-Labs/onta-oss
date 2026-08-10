"""Confinement for LLM-GENERATED Cypher (E6 foundation / ONTA-424 successor).

SPARQL confinement lives in :mod:`infona_client.graph.sparql_scope` and is
dataset-clause based (``FROM``). Property-graph isolation is structural:
every instance pattern must carry ``tenant_id`` / ``kg`` as **parameters** that
the :class:`~infona_client.graph.scope.GraphScope` session overwrites.

This module is the NL→Cypher choke point:

* **Reject** unscoped free Cypher that omits required scope patterns.
* **Never trust** model-supplied ``tenant_id`` / ``kg`` parameter *values* —
  callers pass session scope and we return a params map that forces them.
* **Scrub** store / generator error text before it is fed back into a retry
  prompt or surfaced in an answer (hosts, passwords, bolt URIs).
* **Read-only** — reject mutation clauses so a jailbroken generator cannot write.

It reuses the same heuristic gates as
:func:`infona_client.graph.store.assert_cypher_is_scoped` so free-form NL
queries and admin ``execute_read`` share one definition of "scoped". Prefer
:meth:`GraphSession.execute_template` for allowlisted app paths; this module
is for generated free-form reads that still must be parameterized and scoped.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from infona_client.graph.scope import GraphScope, GraphScopeError
from infona_client.graph.store import (
    assert_cypher_is_scoped,
    cypher_has_scope_param,
    merge_scope_params,
    scrub_store_detail,
)

# Re-export scrub so NL callers have one import surface.
scrub_cypher_error = scrub_store_detail

# Write / admin clauses forbidden on the NL read path.
_WRITE_CLAUSE_RE = re.compile(
    r"\b(?:"
    r"CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|"
    r"LOAD\s+CSV|CALL\s+dbms\.|CALL\s+db\.|apoc\.|PERIODIC\s+COMMIT"
    r")\b",
    re.IGNORECASE,
)

# SPARQL leftovers that must never reach Neo4j as "Cypher".
_SPARQL_LEFTOVER_RE = re.compile(
    r"(?:"
    r"\bFROM\s+(?:NAMED\s+)?<"
    r"|\bGRAPH\s*<"
    r"|\bPREFIX\s+\w+:"
    r"|\bINSERT\s+DATA\b"
    r"|\bSERVICE\s*<"
    r"|<[a-zA-Z][\w+.-]*://[^<>\s]*/graphs/"
    r")",
    re.IGNORECASE,
)

# Collapse whitespace for stable storage / comparison; keep single spaces.
_WS_RE = re.compile(r"\s+")


class CypherScopeError(ValueError):
    """Generated Cypher is not safe to run under the request scope.

    ``status_code`` mirrors :class:`~infona_client.graph.sparql_scope.TenantScopeError`
    so the ask route can map 400 vs 403 without a special case.
    """

    def __init__(self, detail: str, status_code: int = 400):
        self.detail = scrub_cypher_error(detail)
        self.status_code = status_code
        super().__init__(self.detail)


class CrossTenantCypherError(CypherScopeError):
    """Generated Cypher tried to name a foreign workspace (never repaired)."""

    def __init__(self, detail: str):
        super().__init__(detail, status_code=403)


def normalize_cypher(cypher: str) -> str:
    """Light cleanup of LLM Cypher (strip fences, collapse whitespace)."""
    text = (cypher or "").strip()
    if not text:
        return ""
    # Drop markdown fences if the model wrapped the query.
    if text.startswith("```"):
        lines = text.split("\n")
        # drop first fence line and trailing fence
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def is_read_only_cypher(cypher: str) -> bool:
    """True when the query has no write / admin clause tokens."""
    return not bool(_WRITE_CLAUSE_RE.search(cypher or ""))


def has_sparql_leftovers(cypher: str) -> bool:
    """True when the generator emitted SPARQL constructs (wrong backend)."""
    return bool(_SPARQL_LEFTOVER_RE.search(cypher or ""))


def force_session_params(
    scope: GraphScope,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the params map for execution: session **overwrites** tenant/kg.

    Model- or client-supplied ``tenant_id`` / ``kg`` values are discarded
    (model §3.3 T2). Extra filter params (e.g. ``primary_type``) are kept.
    """
    return merge_scope_params(scope, params, for_write=False)


def confine_generated_cypher(
    cypher: str,
    *,
    tenant_id: str,
    kg: str,
    params: Mapping[str, Any] | None = None,
    privileged: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Validate + scope-bind LLM-generated Cypher for a single request.

    Returns ``(normalized_cypher, forced_params)``. Raises
    :class:`CypherScopeError` (400) for unscoped / write / SPARQL leftovers,
    :class:`CrossTenantCypherError` (403) if a future check finds a foreign
    scope (currently scope values are never taken from the model).

    Does **not** concatenate tenant/kg into the query text — only parameters.
    """
    normalized = normalize_cypher(cypher)
    if not normalized:
        raise CypherScopeError("Generated Cypher is empty.", 400)

    if has_sparql_leftovers(normalized):
        raise CypherScopeError(
            "Generated query looks like SPARQL (FROM/GRAPH/PREFIX/…). "
            "Cypher is required when the graph backend is neo4j.",
            400,
        )

    if not is_read_only_cypher(normalized):
        raise CypherScopeError(
            "Only read-only Cypher is allowed (MATCH/RETURN). "
            "CREATE/MERGE/DELETE/SET and similar clauses are rejected.",
            400,
        )

    try:
        scope = GraphScope.for_instance(tenant_id, kg)
    except (GraphScopeError, ValueError) as exc:
        raise CypherScopeError(
            f"Invalid request scope for Cypher execution: {exc}", 400
        ) from exc

    # Optional light repair: if the model used bare Entity MATCH without
    # $tenant_id/$kg, inject the map-form scope on the first :Entity pattern.
    # Only when BOTH params are missing — never rewrite a partially scoped query.
    if not (
        cypher_has_scope_param(normalized, "tenant_id")
        and cypher_has_scope_param(normalized, "kg")
    ):
        repaired = _try_inject_entity_scope(normalized)
        if repaired is not None:
            normalized = repaired

    try:
        assert_cypher_is_scoped(normalized, privileged=privileged)
    except GraphScopeError as exc:
        raise CypherScopeError(str(exc), 400) from exc

    forced = force_session_params(scope, params)
    # Defense in depth: if the model smuggled alternate scope keys, drop them
    # (merge_scope_params already overwrites; assert identity here).
    if forced.get("tenant_id") != scope.tenant_id or forced.get("kg") != scope.kg:
        raise CrossTenantCypherError(
            "Session scope was not forced onto Cypher parameters."
        )
    return normalized, forced


# Bare Entity node patterns only — never rewrite an existing property map
# (could duplicate tenant_id keys and leave a literal foreign scope).
_BARE_ENTITY_MATCH_RE = re.compile(
    r"\b(MATCH|OPTIONAL\s+MATCH)\s*(\(\s*\w+\s*:\s*Entity\s*)\)",
    re.IGNORECASE,
)


def _try_inject_entity_scope(cypher: str) -> str | None:
    """Scope bare ``(e:Entity)`` patterns with ``{tenant_id: $tenant_id, kg: $kg}``.

    Only rewrites nodes that have **no** property map. Patterns that already
    open a map are left alone (caller rejects if still unscoped). Returns
    ``None`` when nothing was rewritten.
    """
    if "Entity" not in cypher:
        return None

    def _repl(m: re.Match[str]) -> str:
        return (
            f"{m.group(1)} {m.group(2)}"
            f"{{tenant_id: $tenant_id, kg: $kg}})"
        )

    out, n = _BARE_ENTITY_MATCH_RE.subn(_repl, cypher)
    if n == 0:
        return None
    if not (
        cypher_has_scope_param(out, "tenant_id") and cypher_has_scope_param(out, "kg")
    ):
        return None
    return out


def compact_cypher_for_prompt(cypher: str) -> str:
    """Single-line Cypher for few-shot example formatting."""
    return _WS_RE.sub(" ", (cypher or "").strip())


__all__ = [
    "CypherScopeError",
    "CrossTenantCypherError",
    "compact_cypher_for_prompt",
    "confine_generated_cypher",
    "force_session_params",
    "has_sparql_leftovers",
    "is_read_only_cypher",
    "normalize_cypher",
    "scrub_cypher_error",
]
