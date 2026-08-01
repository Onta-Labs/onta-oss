from __future__ import annotations

from cograph_client.graph.iri import IRI_BASE
"""Attribute alias mechanism — alias-first amendments, lazy backfill (ADR 0002 §7).

Renaming an attribute or moving it up the hierarchy (``phone_num → phone``)
touches instance data. Instead of an eager rewrite, the ontology records an
alias triple::

    <old-attr-IRI> <https://graph.onta.sh/onto/aliasOf> <new-attr-IRI>

and the query path resolves through aliases immediately — nothing breaks on
day one. Old instance triples are rewritten lazily (backfill_aliases), after
which the alias is retired (retire_alias). Aliases are a migration vehicle,
not a permanent translation layer.

Chains are allowed (a renamed attribute can itself be renamed): fetch_alias_map
flattens ``a → b → c`` to ``a → c`` so every rewrite is one hop. Cyclic alias
data (``a → b → a``) is nonsensical — entries whose chain hits a cycle are
dropped with a warning rather than rewritten unpredictably.

**Authoring (ONTA-407a + ONTA-407b).** Production callers MUST go through
:func:`cograph_client.graph.ontology_commit.commit_ontology`:

- ``OntologyOpKind.RENAME_ATTRIBUTE`` — full rename lifecycle; **always**
  creates the alias (cannot record a rename without one), ensures the new
  attribute declaration, and drops the old schema declaration.
- ``OntologyOpKind.REGISTER_ALIAS`` — alias edge only (both attributes already
  exist; hierarchy moves).
- ``OntologyOpKind.RETIRE_ALIAS`` — drop the alias **only when** the instance
  graph has zero remaining triples on the old predicate (real reference check).

Thin REST routes under ``POST/DELETE …/ontology/aliases*`` use those ops.
Direct ``register_alias`` / ``retire_alias`` remain the SPARQL writers that
commit applies — do not hand-roll a second INSERT path. Instance backfill is
:func:`backfill_aliases` (REST ``POST …/aliases/backfill``).

**Write-path allowlist (ONTA-407b).** This module still issues raw
``INSERT DATA`` / ``DELETE WHERE`` for ``aliasOf`` schema triples and batched
instance-predicate rewrites. It stays on the write-path allowlist with an
honest justification: sole production callers are ``ontology_commit``
(register / rename / retire) and the alias backfill entrypoint. Not a general
instance writer — domain facts continue to flow through ``kg_writer``.

**Type renames are a remaining gap.** Attribute renames use this mechanism;
renaming a *type* would also need entity-URI re-keying (``entities/<Type>/…``
embeds the type leaf) and is intentionally out of scope for 407b.

**alignedTo is NOT this mechanism.** Governance shape alignment
(``cograph/governance/writer.write_alignment``) used to write tenant type URIs
into the global layer as ``onto/alignedTo``; ONTA-402a stopped that. ONTA-407a
decides **stop writing** (already done) rather than adding a reader — alignment
audit lives in the shared provenance graph + changelog, not as a query-path
alias. Do not conflate ``aliasOf`` (tenant attribute rename vehicle) with
``alignedTo`` (global shape promotion audit).
"""


import math

import structlog

from cograph_client.graph.ontology_queries import OMNIX_ONTO
from cograph_client.graph.parser import parse_sparql_results

logger = structlog.stdlib.get_logger("cograph.graph.aliases")

ALIAS_OF = f"{OMNIX_ONTO}/aliasOf"


class AliasStillReferencedError(Exception):
    """Raised when retiring an alias while old-predicate instance triples remain.

    Retirement is only safe after :func:`backfill_aliases` has rewritten every
    remaining reference (count == 0). Carries the count so callers can surface
    a useful error without a second probe.
    """

    def __init__(self, old_attr_uri: str, remaining: int, data_graph_uri: str):
        self.old_attr_uri = old_attr_uri
        self.remaining = remaining
        self.data_graph_uri = data_graph_uri
        super().__init__(
            f"cannot retire alias for {old_attr_uri!r}: {remaining} instance "
            f"triple(s) still reference it in {data_graph_uri!r}; "
            f"run backfill first"
        )


async def register_alias(neptune, graph_uri: str, old_attr_uri: str, new_attr_uri: str) -> None:
    """Record `old_attr_uri aliasOf new_attr_uri` in the (tenant) ontology graph.

    Production authoring goes through :func:`commit_ontology`
    (``REGISTER_ALIAS`` / ``RENAME_ATTRIBUTE``). This is the SPARQL writer that
    commit applies — do not call from a second production path.
    """
    if old_attr_uri == new_attr_uri:
        raise ValueError(f"alias must point to a different attribute, got {old_attr_uri} -> itself")
    await neptune.update(
        f"INSERT DATA {{\n"
        f"  GRAPH <{graph_uri}> {{\n"
        f"    <{old_attr_uri}> <{ALIAS_OF}> <{new_attr_uri}> .\n"
        f"  }}\n"
        f"}}"
    )


async def retire_alias(
    neptune,
    graph_uri: str,
    old_attr_uri: str,
    *,
    data_graph_uri: str | None = None,
) -> None:
    """Remove the alias triple for `old_attr_uri` — call after backfill completes.

    When ``data_graph_uri`` is provided, refuses retirement while any instance
    triples still use the old predicate (real reference check, ONTA-407b).
    Production retirement goes through :func:`commit_ontology`
    (``RETIRE_ALIAS``), which always supplies the data graph.
    """
    if data_graph_uri:
        remaining = await count_attr_references(neptune, data_graph_uri, old_attr_uri)
        if remaining > 0:
            raise AliasStillReferencedError(old_attr_uri, remaining, data_graph_uri)
    await neptune.update(
        f"DELETE WHERE {{ GRAPH <{graph_uri}> {{ <{old_attr_uri}> <{ALIAS_OF}> ?new }} }}"
    )


def alias_map_query(graph_uri: str) -> str:
    """SELECT every alias edge so a caller can build the old->new map."""
    return (
        f"SELECT ?old ?new FROM <{graph_uri}>\n"
        f"WHERE {{\n"
        f"  ?old <{ALIAS_OF}> ?new .\n"
        f"}}"
    )


async def fetch_alias_map(neptune, graph_uri: str) -> dict[str, str]:
    """Fetch the alias map for an ontology graph, chains flattened.

    ``a → b`` and ``b → c`` resolve to ``{a: c, b: c}`` so every rewrite is a
    single hop. Entries whose chain hits a cycle (including self-aliases) are
    dropped — see module docstring.
    """
    raw = await neptune.query(alias_map_query(graph_uri))
    _, bindings = parse_sparql_results(raw)
    edges = {row["old"]: row["new"] for row in bindings if row.get("old") and row.get("new")}

    resolved: dict[str, str] = {}
    for old in edges:
        target = edges[old]
        seen = {old}
        while target in edges:
            if target in seen:
                logger.warning("alias_cycle_dropped", graph_uri=graph_uri, attr_uri=old)
                target = ""
                break
            seen.add(target)
            target = edges[target]
        if target and target != old:
            resolved[old] = target
    return resolved


def rewrite_query_attrs(sparql: str, alias_map: dict[str, str]) -> str:
    """Rewrite aliased (old) attribute IRIs to their new IRIs in generated SPARQL.

    String-level and conservative, same style as rewrite_type_predicate_to_closure:
    only full ``<IRI>`` tokens are matched (the angle brackets are part of the
    match, so `<.../attrs/phone>` never fires inside `<.../attrs/phone_num>`).
    Single-pass with one alternation so a replacement is never itself re-matched.
    Empty map => the query is returned untouched.
    """
    if not alias_map:
        return sparql
    import re

    pattern = "|".join(re.escape(f"<{old}>") for old in alias_map)
    return re.sub(pattern, lambda m: f"<{alias_map[m.group(0)[1:-1]]}>", sparql)


def _count_attr_query(graph_uri: str, attr_uri: str) -> str:
    return f"SELECT (COUNT(*) AS ?n) FROM <{graph_uri}> WHERE {{ ?s <{attr_uri}> ?o . }}"


async def count_attr_references(neptune, data_graph_uri: str, attr_uri: str) -> int:
    """Count instance triples in ``data_graph_uri`` that use ``attr_uri`` as predicate.

    The real reference check behind :func:`retire_alias` / ``RETIRE_ALIAS`` —
    retirement is refused while this returns > 0.
    """
    raw = await neptune.query(_count_attr_query(data_graph_uri, attr_uri))
    _, bindings = parse_sparql_results(raw)
    try:
        return int(bindings[0].get("n", "0")) if bindings else 0
    except (ValueError, TypeError):
        return 0


def _backfill_batch_update(graph_uri: str, old_attr_uri: str, new_attr_uri: str, limit: int) -> str:
    """One batch of the lazy rewrite: DELETE/INSERT WHERE over a LIMITed subselect."""
    return (
        f"DELETE {{ GRAPH <{graph_uri}> {{ ?s <{old_attr_uri}> ?o }} }}\n"
        f"INSERT {{ GRAPH <{graph_uri}> {{ ?s <{new_attr_uri}> ?o }} }}\n"
        f"WHERE {{\n"
        f"  {{ SELECT ?s ?o WHERE {{ GRAPH <{graph_uri}> {{ ?s <{old_attr_uri}> ?o }} }} LIMIT {limit} }}\n"
        f"}}"
    )


async def backfill_aliases(
    neptune, data_graph_uri: str, alias_map: dict[str, str], batch_size: int = 1000
) -> int:
    """Lazily rewrite old-predicate instance triples to the new predicate.

    For each alias, counts the remaining old-predicate triples in the DATA
    graph, then issues batched DELETE/INSERT WHERE updates (batch_size triples
    per update). Returns the total number of triples rewritten. After a clean
    backfill the caller retires the alias via :func:`retire_alias` /
    ``OntologyOpKind.RETIRE_ALIAS`` (which re-checks the count).
    """
    total = 0
    for old_uri, new_uri in alias_map.items():
        count = await count_attr_references(neptune, data_graph_uri, old_uri)
        if count <= 0:
            continue
        for _ in range(math.ceil(count / batch_size)):
            await neptune.update(_backfill_batch_update(data_graph_uri, old_uri, new_uri, batch_size))
        total += count
    return total
