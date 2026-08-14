"""Session resolution + write-path gates for :mod:`kg_writer`.

Look up sibling / facade names via :func:`_host` so tests that monkeypatch
``infona_client.graph.kg_writer.<name>`` keep working.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

import structlog

from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import parse_kg_graph_uri, parse_tenant_graph_uri
from infona_client.graph.scope import GraphScope, GraphScopeError, ONTOLOGY_KG

if TYPE_CHECKING:
    from infona_client.graph.store import GraphSession, GraphStore

logger = structlog.stdlib.get_logger("infona.graph.kg_writer")

# Hard cap on the synchronous spatio-temporal index upsert inside insert_facts.
# The index is a DERIVED, eventually-consistent companion store; Neptune (the
# source of truth) is already written by the time we reach it. Catching exceptions
# isn't enough — a hung/partitioned Postgres (pool exhaustion, Aurora failover)
# would otherwise block the KG-write request on this await with no exception. The
# timeout converts a hang into a caught TimeoutError → logged, index skipped, the
# write proceeds. Env-overridable for ops.
_INDEX_UPSERT_TIMEOUT_S = float(
    os.environ.get("INFONA_SPATIOTEMPORAL_UPSERT_TIMEOUT_S", "10")
)


def _host():
    from infona_client.graph import kg_writer as _mod

    return _mod

def _semantic_upsert_timeout_s() -> float:
    """Timeout for the semantic-index write hook (ONTA-181) — the same hang-to-
    TimeoutError conversion as ``_INDEX_UPSERT_TIMEOUT_S``, with its own knob
    because the semantic hook does strictly more work per write (marker-map
    read + touched-entity re-read + chunk upsert + empty-doc deletes). Read per
    call so tests/ops can tune it without re-importing the module."""
    return float(os.environ.get("INFONA_SEMANTIC_UPSERT_TIMEOUT_S", "10"))


def _semantic_hook_max_entities() -> int:
    """Cap on TOUCHED ENTITIES the semantic hook re-reads from Neptune per
    write. The touched-entity fetch is one VALUES-scoped SELECT, so its cost
    scales with the entity count of the write; a huge ingest batch must not
    turn the hook into a full-graph scan. Overflow is logged (never silent)
    and repaired by the reconciler's next full scan. Read per call so
    tests/ops can tune it without re-importing the module."""
    try:
        return int(float(os.environ.get("INFONA_SEMANTIC_HOOK_MAX_ENTITIES", "500")))
    except ValueError:
        return 500


def _provenance_enabled(*, store_path: bool = False) -> bool:
    """Whether removal/rename primitives write companion-graph provenance events.

    Gated by the same ``INFONA_PROVENANCE_ENABLED`` env var the ingest path uses
    for assertion provenance (default OFF), so tombstone/rewrite events only land
    when governance/undo is switched on.

    E8 store-path optional always-on: when ``store_path=True`` and
    ``INFONA_PROVENANCE_STORE_ALWAYS=1``, provenance events fire on the
    property-graph path even if the global flag is off (useful for hermetic
    isolation QC / Neo4j local without enabling Neptune companion graphs).
    """
    if os.environ.get("INFONA_PROVENANCE_ENABLED", "0") == "1":
        return True
    if store_path and os.environ.get("INFONA_PROVENANCE_STORE_ALWAYS", "0") == "1":
        return True
    return False


def _resolve_graph_session(
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    instance_graph: str | None = None,
    tenant_id: str | None = None,
    kg_name: str | None = None,
) -> "GraphSession":
    """Return the scoped GraphSession for this write.

    Priority:
    1. Explicit ``session``
    2. Explicit ``store`` + scope derived from graph URI or tenant/kg
    3. Process :func:`get_optional_graph_store` (Neo4j / injected test store)

    Scope derivation from ``instance_graph``:

    * per-KG URI (``…/graphs/<t>/kg/<kg>``) → :meth:`GraphScope.for_instance`
    * bare tenant ontology URI (``…/graphs/<t>``) →
      :meth:`GraphScope.for_catalog` (``layer='tenant'``, ``kg=__ontology__``) —
      the path tenant-config stores use (normalization rules, clean/verify
      policies; ONTA-529)

    Never returns ``None``: Neo4j is the only backend (ONTA-527), so there is
    no SPARQL path to fall back to. Raises :class:`GraphConfigError` when no
    store is configured and :class:`GraphScopeError` when the scope cannot be
    derived — fail closed rather than write nowhere.
    """
    if session is not None:
        # When both an explicit session and instance_graph are supplied, fail
        # closed if they disagree — silent cross-scope writes are worse than a
        # loud GraphScopeError (isolation / ADR 0012).
        if instance_graph:
            sess_scope = getattr(session, "scope", None)
            scope_pair = parse_kg_graph_uri(instance_graph)
            if (
                scope_pair is not None
                and sess_scope is not None
                and (
                    getattr(sess_scope, "tenant_id", None) != scope_pair[0]
                    or getattr(sess_scope, "kg", None) != scope_pair[1]
                )
            ):
                raise GraphScopeError(
                    f"session scope ({getattr(sess_scope, 'tenant_id', None)!r}/"
                    f"{getattr(sess_scope, 'kg', None)!r}) does not match "
                    f"instance_graph ({scope_pair[0]!r}/{scope_pair[1]!r})"
                )
            catalog_tid = parse_tenant_graph_uri(instance_graph)
            if (
                catalog_tid is not None
                and sess_scope is not None
                and (
                    getattr(sess_scope, "tenant_id", None) != catalog_tid
                    or getattr(sess_scope, "kg", None) != ONTOLOGY_KG
                )
            ):
                raise GraphScopeError(
                    f"session scope ({getattr(sess_scope, 'tenant_id', None)!r}/"
                    f"{getattr(sess_scope, 'kg', None)!r}) does not match "
                    f"tenant catalog graph ({catalog_tid!r}/{ONTOLOGY_KG!r})"
                )
        return session
    if store is None:
        from infona_client.graph.store import get_optional_graph_store

        store = get_optional_graph_store()
    tid, kg = tenant_id, kg_name
    if (not tid or not kg) and instance_graph:
        scope_pair = parse_kg_graph_uri(instance_graph)
        if scope_pair is not None:
            tid, kg = scope_pair
        else:
            # Tenant ontology graph → catalog scope (rules / policies / schema
            # metadata live here, never in a per-KG instance scope).
            catalog_tid = parse_tenant_graph_uri(instance_graph)
            if catalog_tid is not None:
                return store.session(
                    GraphScope.for_catalog(layer="tenant", tenant_id=catalog_tid)
                )
            raise GraphScopeError(
                f"Cannot derive tenant/kg scope from instance_graph={instance_graph!r}; "
                "pass tenant_id+kg_name, a per-KG graph URI, or a tenant ontology graph URI"
            )
    if not tid or not kg:
        raise GraphScopeError(
            "Neo4j write path requires tenant_id and kg (or a parseable instance_graph)"
        )
    return store.session(GraphScope.for_instance(tid, kg))


def _value_history_enabled() -> bool:
    """Whether an attribute UPDATE records a dated value-history entry (ONTA-236).

    Gated by ``INFONA_VALUE_HISTORY_ENABLED`` (default OFF) so bulk ingest stays
    byte-stable and the extra read-before-delete + companion-graph write are only
    paid where "what changed, old→new, when" matters. When ON, ``delete_facts``
    reads the prior value of each predicate-scoped clear it is given a NEW value
    for, and versions any genuine change (see :func:`_record_value_history`). The
    mechanism is GENERAL — it versions ANY attribute of ANY type, with zero
    domain knowledge.
    """
    return os.environ.get("INFONA_VALUE_HISTORY_ENABLED", "0") == "1"


def _chunk(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def _count_matching(neptune, count_sparql: str) -> int:
    """Best-effort ``SELECT (COUNT(*) AS ?n)`` → int (0 on any failure).

    Used by :func:`delete_facts` to return an accurate removed-triple count for
    the pattern-based (subject / predicate-scoped) removals, whose count can't be
    known up front the way a concrete-triple list's can. Best-effort because the
    count is informational — a hiccup here must never fail the delete."""
    try:
        _, rows = parse_sparql_results(await neptune.query(count_sparql))
        return int(rows[0].get("n", 0)) if rows else 0
    except Exception:  # noqa: BLE001 — the count is informational, never load-bearing
        return 0

def _warn_unported_companions(
    instance_graph: str,
    *,
    validity_triples: Optional[list[Triple]] = None,
    suppression_triples: Optional[list[Triple]] = None,
    reopen_facts: Optional[list[Triple]] = None,
) -> None:
    """Log once per write when a caller passes an unported companion payload.

    Valid-time and suppression companions were named-graph SPARQL writes. Their
    property-graph node ports are E7, so on Neo4j these payloads have been
    dropped on the floor since the cutover. Warning here turns a silent no-op
    into something greppable (ONTA-527).
    """
    unported = [
        name
        for name, payload in (
            ("validity_triples", validity_triples),
            ("suppression_triples", suppression_triples),
            ("reopen_facts", reopen_facts),
        )
        if payload
    ]
    if unported:
        logger.warning(
            "insert_facts_companion_payload_not_ported",
            instance_graph=instance_graph,
            payloads=unported,
            detail=(
                "valid-time / suppression companions have no property-graph "
                "port yet (E7); payload ignored"
            ),
        )
