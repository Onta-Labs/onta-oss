"""Shared constants, models, and helpers for knowledge-graph routes.

Look up patched entry points (``_store_triple_count``,
``invalidate_triple_count``) on the public ``knowledge_graphs`` facade at
call time via ``_host()``.

Never mention the retired SPARQL client class by name in this sibling —
the residual allowlist must not grow (ONTA-534).
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from infona_client.graph.iri import IRI_BASE, ONTO_BASE
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import (
    is_valid_kg_name,
    kg_graph_uri,
    kg_meta_uri,
    tenant_graph_uri,
)

INFONA_ONTO = ONTO_BASE
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
NAME_ATTRS = ("name", "title", "label", "headline")

# Predicate carrying a KG's precomputed triple count in the tenant metadata
# graph (next to kg_name/kg_description). Counting every triple in a KG graph
# is a full scan — seconds for a large KG — so `list_kgs` must NOT compute it
# live on each request (the Explorer's load was dominated by N serial scans).
# Instead the count is stored once and served as a tiny lookup inside the
# metadata query that already lists the KGs. It is (re)materialized lazily on
# read when absent and invalidated after every successful instance write via
# the shared post-write path (`kg_writer.refresh_after_write` →
# `invalidate_triple_count`), plus again from explore.recompute_kg_stats when
# type-stats recompute finishes.
KG_TRIPLE_COUNT = f"{INFONA_ONTO}/kg_triple_count"


# Canonical in ``graph/queries.py`` so create_kg, list_kgs, the shared write
# path's ``ensure_kg_registered`` and the ONTA-413 existence probe all mint the
# SAME registration URI. Aliased (not redefined) to keep this module's callers
# unchanged.
#
# NOTE: unlike ``kg_graph_uri``, ``kg_meta_uri`` does NOT validate its name — so
# the count helpers below branch on ``is_valid_kg_name`` (THE predicate, per
# ONTA-414) before interpolating a name into an IRI. They fail soft (skip, no
# raise) because all are called from paths that must never fail their caller.
#
# The guard in ``_store_triple_count`` is LOAD-BEARING, not decorative: it is the
# only thing between a ``>``-bearing registered name and a top-level injection on
# the tenant metadata graph. The ``<kg_uri>`` IRI closes early and the rest of the
# name becomes statement-level SPARQL on a ``client.update`` — e.g.
# ``; DROP SILENT GRAPH <…/graphs/other-tenant> ;``, a cross-tenant WRITE. Do not
# "simplify" it away. ``invalidate_triple_count``'s guard is the same shape and is
# load-bearing for the shared write path: ``refresh_after_write`` passes
# ``kg_name`` through without re-validating, so the helper must refuse
# un-IRI-able names itself (``explore.recompute`` still trips
# ``_stats_graph_uri`` first as defense-in-depth).
_kg_meta_uri = kg_meta_uri


def _host():
    """Call-time lookup of the public knowledge_graphs module (monkeypatch surface)."""
    from infona_client.api.routes import knowledge_graphs as _mod

    return _mod


def _skip_invalid_kg_name(name: str, op: str) -> bool:
    """Whether ``name`` can't legally sit in an IRI — log and skip if so.

    A REGISTERED name that fails ``is_valid_kg_name`` means a corrupt row in the
    tenant metadata graph, so it must stay observable. Before this module
    degraded such rows, the corruption was loud (a 422 on every Explorer load);
    serving ``triple_count: 0`` instead would make it silent, since that is
    indistinguishable from a legitimately empty KG. Mirrors the
    ``ensure_kg_registered_invalid_name`` warning the shared write path emits on
    exactly this condition, so an operator can find the offending row.
    """
    if is_valid_kg_name(name):
        return False
    # Per-call logger rather than the module-level ``logger = ...`` most route
    # modules use, deliberately: ``cache_logger_on_first_use=True`` freezes a
    # module-level proxy at import, after which ``structlog.testing.capture_logs``
    # can no longer intercept it — the hazard that forces the import-order
    # workarounds in test_sec_user_agent.py / test_web_ingest_fastpath.py. Minting
    # the proxy per call keeps this warning assertable regardless of test order.
    # Not hot: the valid-name fast path above returns before ever getting here.
    import structlog

    structlog.get_logger("infona.kg").warning(
        "kg_name_invalid_skipped", kg_name=name, op=op
    )
    return True


async def _neo4j_live_kg_counts(
    tenant_id: str, kg_names: list[str]
) -> dict[str, dict[str, int]]:
    """Live Entity + Assertion counts per KG from GraphStore (best-effort).

    Used when registry ``triple_count`` / durable stats are still zero after
    ingest — common on OSS local Neo4j without Postgres ``kg_stats_store``.
    """
    if not kg_names:
        return {}
    try:
        from infona_client.graph.store import get_graph_store

        store = get_graph_store()
    except Exception:  # noqa: BLE001
        return {}
    run = getattr(store, "_run", None)
    if not callable(run):
        return {}
    cypher = """
    UNWIND $kgs AS kg_name
    OPTIONAL MATCH (e:Entity {tenant_id: $tenant_id, kg: kg_name})
    WITH kg_name, count(e) AS entity_count
    OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: kg_name})
    RETURN kg_name AS name, entity_count, count(a) AS triple_count
    """
    try:
        rows = await run(
            cypher,
            {"tenant_id": tenant_id, "kgs": list(kg_names)},
            writing=False,
            database=None,
        )
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict[str, int]] = {}
    for r in rows or ():
        name = str(r.get("name") or "")
        if not name:
            continue
        try:
            ent = int(r.get("entity_count") or 0)
        except (TypeError, ValueError):
            ent = 0
        try:
            trips = int(r.get("triple_count") or 0)
        except (TypeError, ValueError):
            trips = 0
        # edge_count ≈ object Assertions / typed rels; use assertion count as
        # a better "something is here" signal than sticky registry zeros.
        out[name] = {
            "entity_count": ent,
            "triple_count": trips,
            "edge_count": max(0, trips - ent) if trips else 0,
        }
    return out


async def _live_triple_count(
    client: Any, tenant_id: str, name: str
) -> int:
    """Full-scan COUNT(*) for one KG graph. Slow — fallback path only.

    Fails soft per KG on an un-IRI-able name, because ``list_kgs`` fans this out
    over EVERY registered KG under ``asyncio.gather``: since ONTA-414
    ``kg_graph_uri`` raises :class:`InvalidKGName` (→ 422 app-wide), so one bad
    registration used to 422 the WHOLE workspace's KG listing rather than
    degrading that single row to 0. This is a best-effort count, not a validation
    boundary — routes that act on ONE user-named KG still 422 by design.

    Such a name does NOT require out-of-band DB access to arrive. Both KG
    registration paths validate (``KGCreate.name``'s pattern and
    ``ensure_kg_registered``'s ``is_valid_kg_name`` branch) — but
    ``POST /graphs/{tenant}/triples`` (an ordinary API-key-authenticated route)
    writes arbitrary triples via ``insert_triples`` straight into
    ``tenant_graph_uri``, the SAME base graph ``list_kgs`` reads registrations
    from, and SPARQL literal escaping does not escape ``>``. So a caller with
    write on their own tenant can plant a ``kg_name`` literal this module will
    later read back. A pre-ONTA-414 registration (the ``$``→``\\Z`` tightening
    invalidated trailing-newline names) is the other arrival vector.

    EVERYTHING that can raise lives inside the ``try`` — including the
    ``_skip_invalid_kg_name`` pre-check and its log call, not just
    ``kg_graph_uri``. ``list_kgs`` gathers this WITHOUT ``return_exceptions``, so
    anything escaping here 500s the whole listing — the exact all-or-nothing
    failure mode this helper exists to prevent. Don't hoist a statement out.
    """
    try:
        if _skip_invalid_kg_name(name, "live_triple_count"):
            return 0
        graph = kg_graph_uri(tenant_id, name)
        sparql = f"SELECT (COUNT(*) as ?c) FROM <{graph}> WHERE {{ ?s ?p ?o }}"
        _, rows = parse_sparql_results(await client.query(sparql))
        return int(rows[0].get("c", "0")) if rows else 0
    except Exception:
        return 0


async def _store_triple_count(
    client: Any, tenant_id: str, name: str, count: int
) -> None:
    """Persist a KG's triple count in the tenant metadata graph (best-effort)."""
    if _skip_invalid_kg_name(name, "store_triple_count"):
        return
    base = tenant_graph_uri(tenant_id)
    kg_uri = _kg_meta_uri(tenant_id, name)
    try:
        # GRAPH-form (not WITH … DELETE WHERE): pyoxigraph's update parser
        # rejects WITH-style DELETE WHERE (dogfood R4: invalidation silently
        # failed, stored 0 stuck forever on the local OSS store).
        await client.update(
            f"DELETE {{ GRAPH <{base}> {{ <{kg_uri}> <{KG_TRIPLE_COUNT}> ?old }} }}\n"
            f"INSERT {{ GRAPH <{base}> {{ <{kg_uri}> <{KG_TRIPLE_COUNT}> {int(count)} }} }}\n"
            f"WHERE {{ OPTIONAL {{ GRAPH <{base}> {{ <{kg_uri}> <{KG_TRIPLE_COUNT}> ?old }} }} }}"
        )
    except Exception:
        pass


async def invalidate_triple_count(
    client: Any, tenant_id: str, name: str
) -> None:
    """Drop a KG's stored triple count so the next `list_kgs` recomputes it.

    Called from the shared post-write path (`kg_writer.refresh_after_write`)
    after every successful instance write, and again from Explorer type-stats
    recompute. Without this, a stored ``0`` (or any pre-write count) sticks and
    ``list_kgs`` / ``kg list`` reports ``triple_count: 0`` after ingest.
    Best-effort: a failure just means the stale count lingers until the next
    successful invalidation.
    """
    if _skip_invalid_kg_name(name, "invalidate_triple_count"):
        return
    base = tenant_graph_uri(tenant_id)
    kg_uri = _kg_meta_uri(tenant_id, name)
    try:
        # GRAPH-form required for pyoxigraph (WITH DELETE WHERE → SyntaxError).
        await client.update(
            f"DELETE WHERE {{ GRAPH <{base}> {{ "
            f"<{kg_uri}> <{KG_TRIPLE_COUNT}> ?old }} }}"
        )
    except Exception:
        pass


class KGCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = ""


class KGInfo(BaseModel):
    name: str
    description: str = ""
    triple_count: int = 0
    # Dashboard-summary stats, served from the durable per-KG stats store (no
    # Neptune scan on the hot path). Default to zeros/active for KGs whose row
    # isn't materialized yet — the next list lazily backfills it from the
    # precomputed stats graph (mirrors triple_count's lazy materialization).
    entity_count: int = 0
    edge_count: int = 0
    # "active" | "enriching" — derived live from the tenant's in-flight jobs.
    status: str = "active"
    stats_updated_at: Optional[str] = None
    # One-line AI summary of what the graph is about, synthesized from its type
    # breakdown and served from the same durable stats row (see
    # graph/kg_summary.py). Empty until generated (no key / empty graph / first
    # list before backfill). Distinct from the user-set ``description`` above.
    ai_description: str = ""
