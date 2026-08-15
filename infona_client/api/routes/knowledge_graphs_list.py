"""List knowledge graphs for a tenant, with dashboard-summary stats."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Depends

from infona_client.api.deps import get_enrichment_job_store, get_neptune_client
from infona_client.api.routes.knowledge_graphs_common import (
    INFONA_ONTO,
    KG_TRIPLE_COUNT,
    KGInfo,
    _host,
    _live_triple_count,
    _neo4j_live_kg_counts,
)
from infona_client.auth.access import get_tenant_with_capability
from infona_client.auth.api_keys import TenantContext
from infona_client.auth.capabilities import can_write
from infona_client.enrichment.models import JobCategory, JobStatus
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import tenant_graph_uri


async def list_kgs(
    tenant: TenantContext = Depends(get_tenant_with_capability),
    client: Any = Depends(get_neptune_client),
    job_store=Depends(get_enrichment_job_store),
):
    """List all knowledge graphs for a tenant, with dashboard-summary stats.

    **Neo4j:** registry is ``:KnowledgeGraph`` nodes (see
    :mod:`infona_client.graph.kg_registry`). Entity/edge stats still come from
    the durable stats store when available.

    **Legacy SPARQL:** triple counts are read from the metadata graph (stored
    alongside the KG registration) in the SAME query that lists the KGs.

    Entity/edge counts come from the durable per-KG stats store (kept fresh by
    the shared write/refresh path) — a single relational read, no Neptune. Rows
    for KGs that predate the store are backfilled lazily from their existing
    precomputed stats graph the first time they're listed (the same lazy
    materialization pattern as triple counts). ``status`` is derived live from
    the tenant's in-flight enrichment jobs.

    A GET that persists (ONTA-452): every lazy materialization on this path is a
    WRITE, and the route stays open to readers because listing your graphs is a
    read. So the persistence is gated on the caller's write capability instead
    of the route: a read-only member gets the SAME numbers, computed live, and
    writes back nothing. Without this the route was a bypass of the very
    ``recompute-stats`` gate this ticket added, since it schedules the identical
    recompute.
    """
    from infona_client.graph.kg_registry import list_registered_kgs, neo4j_kg_registry_active

    persist = can_write(tenant.role)

    if neo4j_kg_registry_active():
        entries = await list_registered_kgs(tenant.tenant_id)
        # Durable stats only — no SPARQL backfill against decommissioned Neptune.
        stats_by_kg: dict = {}
        try:
            from infona_client.graph.kg_stats_store import get_kg_stats_store

            for r in await get_kg_stats_store().list_for_tenant(tenant.tenant_id):
                stats_by_kg[r.kg_name] = r
        except Exception:  # noqa: BLE001
            stats_by_kg = {}
        enriching = await _enriching_kgs(job_store, tenant.tenant_id)
        # Live GraphStore counts when registry/stats are still zero after ingest.
        # KnowledgeGraph.triple_count is only set at create; without Postgres
        # kg_stats_store, entity_count stayed 0 forever (persona-eval trust bug).
        live_by_kg = await _neo4j_live_kg_counts(
            tenant.tenant_id, [e["name"] for e in entries]
        )
        out: list[KGInfo] = []
        for e in entries:
            s = stats_by_kg.get(e["name"])
            live = live_by_kg.get(e["name"]) or {}
            reg_triples = int(e.get("triple_count") or 0)
            ent = int(s.entity_count) if s and s.entity_count else 0
            edge = int(s.edge_count) if s and s.edge_count else 0
            if ent <= 0:
                ent = int(live.get("entity_count") or 0)
            if edge <= 0:
                edge = int(live.get("edge_count") or 0)
            # Prefer assertion/triple live count when registry stuck at 0.
            triples = reg_triples if reg_triples > 0 else int(
                live.get("triple_count") or 0
            )
            out.append(
                KGInfo(
                    name=e["name"],
                    description=e.get("description") or "",
                    triple_count=triples,
                    entity_count=ent,
                    edge_count=edge,
                    status="enriching" if e["name"] in enriching else "active",
                    stats_updated_at=s.updated_at.isoformat() if s else None,
                    ai_description=s.ai_description if s else "",
                )
            )
        return out

    base = tenant_graph_uri(tenant.tenant_id)

    # One query: KG registrations + their stored triple counts.
    sparql = (
        f"SELECT ?name ?desc ?count FROM <{base}> WHERE {{"
        f"  ?kg <{INFONA_ONTO}/kg_name> ?name ."
        f"  OPTIONAL {{ ?kg <{INFONA_ONTO}/kg_description> ?desc }}"
        f"  OPTIONAL {{ ?kg <{KG_TRIPLE_COUNT}> ?count }}"
        f"}}"
    )
    raw = await client.query(sparql)
    _, bindings = parse_sparql_results(raw)

    # Preserve discovery order; dedupe defensively on name.
    entries: list[dict] = []
    seen: set[str] = set()
    for row in bindings:
        name = row.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        raw_count = row.get("count")
        count = (
            int(raw_count) if raw_count not in (None, "") and raw_count.isdigit() else None
        )
        entries.append({"name": name, "desc": row.get("desc", ""), "count": count})

    # Materialize any missing counts in parallel, then persist them.
    missing = [e for e in entries if e["count"] is None]
    if missing:
        counts = await asyncio.gather(
            *(_live_triple_count(client, tenant.tenant_id, e["name"]) for e in missing)
        )
        for e, c in zip(missing, counts):
            e["count"] = c
        if persist:
            await asyncio.gather(
                *(
                    _host()._store_triple_count(client, tenant.tenant_id, e["name"], e["count"])
                    for e in missing
                ),
                return_exceptions=True,
            )

    stats_by_kg = await _kg_stats_for(
        client, tenant.tenant_id, [e["name"] for e in entries], persist=persist
    )
    enriching = await _enriching_kgs(job_store, tenant.tenant_id)

    out: list[KGInfo] = []
    for e in entries:
        s = stats_by_kg.get(e["name"])
        out.append(
            KGInfo(
                name=e["name"],
                description=e["desc"],
                triple_count=e["count"] or 0,
                entity_count=s.entity_count if s else 0,
                edge_count=s.edge_count if s else 0,
                status="enriching" if e["name"] in enriching else "active",
                stats_updated_at=s.updated_at.isoformat() if s else None,
                ai_description=s.ai_description if s else "",
            )
        )
    return out


async def _kg_stats_for(
    client: Any,
    tenant_id: str,
    kg_names: list[str],
    *,
    persist: bool = True,
):
    """Return {kg_name: KgStats} from the durable store, backfilling misses.

    Steady state: one relational read for the whole tenant (no Neptune). KGs
    without a row yet are backfilled in parallel from their precomputed stats
    graph; a KG whose stats graph isn't materialized either gets a background
    recompute scheduled (which populates the store) and is served as zeros for
    now. Best-effort throughout — a store/Neptune hiccup degrades to zeros, it
    never fails the KG listing.

    ``persist=False`` (a read-only caller, ONTA-452) returns the SAME numbers
    but skips the caller-visible materialization: no store row is written and
    no billed summary backfill is kicked off. The background recompute on a
    stats MISS is the one deliberate exception and still fires for readers.
    See the comment on that branch below for why gating it would leave a reader
    permanently staring at ``entity_count: 0``.
    """
    from infona_client.api.routes.explore import (
        backfill_kg_summary,
        schedule_recompute,
        schedule_summary_backfill,
    )
    from infona_client.graph.kg_stats_store import KgStats, get_kg_stats_store

    store = get_kg_stats_store()
    try:
        rows = await store.list_for_tenant(tenant_id)
    except Exception:  # noqa: BLE001 — degrade to no stats rather than 500
        rows = []
    by_kg: dict[str, KgStats] = {r.kg_name: r for r in rows}

    missing = [n for n in kg_names if n not in by_kg]
    if missing:
        backfilled = await asyncio.gather(
            *(
                backfill_kg_summary(client, tenant_id, n, persist=persist)
                for n in missing
            ),
            return_exceptions=True,
        )
        for name, res in zip(missing, backfilled):
            if isinstance(res, KgStats):
                by_kg[name] = res
            elif not isinstance(res, Exception):
                # res is None: stats graph not materialized yet → schedule a
                # recompute so the store is populated for next time.
                #
                # Deliberately NOT gated on ``persist`` (ONTA-452 review): this
                # is the only thing that can ever make the number right, and
                # gating it would leave a reader in a workspace no writer has
                # listed staring at entity_count=0 forever, indistinguishable
                # from a genuinely empty KG. A confident wrong number with no
                # signal is worse than the scan. It fires only on a MISS, is
                # idempotent, and ``schedule_recompute`` collapses repeats for
                # the same KG, so it is not spammable. The unbounded on-demand
                # twin (POST /recompute-stats) stays write-gated.
                try:
                    schedule_recompute(client, tenant_id, name)
                except Exception:  # noqa: BLE001
                    pass

    # Fill one-line AI summaries for KGs that have entities but no stored
    # description yet — KGs that predate the feature, or whose row was just
    # count-backfilled above. Fire-and-forget so the summary never lands on this
    # (hot) list path: the background sweep persists them and they appear on the
    # next list; recompute writes them at write time going forward.
    # Writers only (ONTA-452): the sweep persists rows and spends on billed
    # summary generation, so a read-only listing must not kick it off.
    if persist:
        try:
            schedule_summary_backfill(list(by_kg.values()))
        except Exception:  # noqa: BLE001 — scheduling a warm-up must never fail listing
            pass
    return by_kg


async def _enriching_kgs(job_store, tenant_id: str) -> set[str]:
    """KG names with an in-flight (queued/running) enrichment or discovery job."""
    try:
        jobs = await job_store.list_for_tenant(tenant_id)
    except Exception:  # noqa: BLE001
        return set()
    active = {JobStatus.queued, JobStatus.running}
    enriching = {JobCategory.enrichment, JobCategory.discovery, JobCategory.ingest}
    return {
        j.kg_name
        for j in jobs
        if j.status in active and j.category in enriching and j.kg_name
    }
