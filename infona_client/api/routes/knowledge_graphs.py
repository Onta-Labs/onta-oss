"""Knowledge graph management — list, create, delete named graphs within a tenant.

All KGs share the tenant's ontology but have separate instance data.
"""

from infona_client.graph.iri import (
    ENTITY_URI_PREFIX,
    IRI_BASE,
    ONTO_BASE,
    TYPE_URI_PREFIX,
)
import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from infona_client.analytics import distinct_id_for, emit
from infona_client.api.deps import (
    get_enrichment_job_store,
    get_neptune_client,
    get_schedule_store,
)
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.auth.access import get_tenant_with_capability, require_tenant_write
from infona_client.auth.capabilities import can_write
from infona_client.enrichment.models import JobCategory, JobStatus
from infona_client.graph.client import NeptuneClient
from infona_client.graph.ontology_queries import (
    get_type_attributes_query,
    type_uri,
)
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import (
    _escape_literal,
    is_valid_kg_name,
    kg_graph_uri,
    kg_meta_uri,
    tenant_graph_uri,
)

router = APIRouter(prefix="/graphs/{tenant}/kgs")

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


async def _live_triple_count(
    client: "NeptuneClient", tenant_id: str, name: str
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
    client: "NeptuneClient", tenant_id: str, name: str, count: int
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
    client: "NeptuneClient", tenant_id: str, name: str
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

# Predicates the resolver attaches to every entity at ingest time.
# Always present, always 100%, drown out the actual columns the user
# cares about — hidden from /type usage by default, opt-in via
# ?include_system=true. Sourced from schema_resolver.py.
SYSTEM_PREDICATES: frozenset[str] = frozenset({
    "http://www.w3.org/2000/01/rdf-schema#label",
    f"{IRI_BASE}/onto/ingested_at",
    f"{IRI_BASE}/onto/source",
})


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


@router.get("", response_model=list[KGInfo])
async def list_kgs(
    tenant: TenantContext = Depends(get_tenant_with_capability),
    client: NeptuneClient = Depends(get_neptune_client),
    job_store=Depends(get_enrichment_job_store),
):
    """List all knowledge graphs for a tenant, with dashboard-summary stats.

    Triple counts are read from the metadata graph (stored alongside the KG
    registration) in the SAME query that lists the KGs — no per-KG scan on the
    hot path. KGs with no stored count yet (legacy, or freshly invalidated
    after ingest) fall back to a live COUNT(*); those run in PARALLEL and are
    written back so the next read is again a single tiny lookup.

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
    persist = can_write(tenant.role)
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
                    _store_triple_count(client, tenant.tenant_id, e["name"], e["count"])
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
    client: "NeptuneClient",
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


@router.post("", response_model=KGInfo, status_code=201)
async def create_kg(
    body: KGCreate,
    tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Create a new knowledge graph for a tenant.

    Idempotent-safe: guarded with ``FILTER NOT EXISTS`` so calling it twice
    never duplicates the registration triples and never clobbers an existing
    registration (or its ``kg_description``). This is the same registration the
    shared write path performs via ``ensure_kg_registered`` (ONTA-153) — here we
    additionally write the description, which only the explicit "New KG" flow
    supplies.

    On a re-POST of an existing KG the guarded INSERT no-ops; we then return the
    *existing* KGInfo (real description + triple count) rather than claiming an
    empty/zero KG, so the response never lies about a KG that may already hold
    real data.

    Safety: ``body.name`` is pattern-validated by ``KGCreate`` (``[a-zA-Z0-9_-]``)
    so it's URI-safe, but the free-text ``description`` and (defensively) the name
    are escaped via the canonical ``_escape_literal`` before going into a SPARQL
    literal — no statement-breakout on a ``"`` / ``\\`` / newline.
    """
    base = tenant_graph_uri(tenant.tenant_id)
    kg_uri = _kg_meta_uri(tenant.tenant_id, body.name)

    insert_lines = [
        f'    <{kg_uri}> <{INFONA_ONTO}/kg_name> "{_escape_literal(body.name)}" .',
        f"    <{kg_uri}> <{KG_TRIPLE_COUNT}> 0 .",
    ]
    if body.description:
        insert_lines.append(
            f'    <{kg_uri}> <{INFONA_ONTO}/kg_description> '
            f'"{_escape_literal(body.description)}" .'
        )
    insert_block = "\n".join(insert_lines)
    sparql = (
        f"WITH <{base}>\n"
        f"INSERT {{\n{insert_block}\n}}\n"
        f"WHERE {{\n"
        f"  FILTER NOT EXISTS {{ <{kg_uri}> <{INFONA_ONTO}/kg_name> ?n }}\n"
        f"}}"
    )

    await client.update(sparql)

    # Product-analytics event (ONTA-323). Fire-and-forget, no-op without a sink,
    # never raises. Attributed to the authenticated subject (Clerk user id), else
    # a stable system:<tenant> id. Emitted on a successful create POST; the route
    # is idempotent, so a re-POST of an existing KG re-emits — a bounded,
    # self-attributed signal, acceptable for a product funnel.
    emit(
        "kg_created",
        distinct_id=distinct_id_for(tenant.subject, tenant.tenant_id),
        tenant=tenant.tenant_id,
        kg=body.name,
        has_description=bool(body.description),
    )

    # The INSERT is idempotent, so a re-POST no-ops it. Read the registration
    # back and report what's actually stored: a pre-existing KG keeps its real
    # description + triple count; a freshly-created one reads back as the values
    # we just wrote (description as given, count 0).
    read = (
        f"SELECT ?desc ?count FROM <{base}> WHERE {{\n"
        f"  <{kg_uri}> <{INFONA_ONTO}/kg_name> ?n .\n"
        f"  OPTIONAL {{ <{kg_uri}> <{INFONA_ONTO}/kg_description> ?desc }}\n"
        f"  OPTIONAL {{ <{kg_uri}> <{KG_TRIPLE_COUNT}> ?count }}\n"
        f"}}"
    )
    try:
        _, rows = parse_sparql_results(await client.query(read))
    except Exception:
        rows = []
    if rows:
        row = rows[0]
        raw_count = row.get("count")
        count = int(raw_count) if raw_count not in (None, "") and raw_count.isdigit() else 0
        return KGInfo(
            name=body.name,
            description=row.get("desc", "") or "",
            triple_count=count,
        )
    # Read-back failed (e.g. Neptune hiccup) — fall back to the values we wrote.
    return KGInfo(name=body.name, description=body.description, triple_count=0)


@router.delete("/{kg_name}")
async def delete_kg(
    kg_name: str,
    tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
    schedule_store=Depends(get_schedule_store),
):
    """Delete a knowledge graph and all its data."""
    base = tenant_graph_uri(tenant.tenant_id)
    graph = kg_graph_uri(tenant.tenant_id, kg_name)
    # The shared builder, not a fourth hand-rolled copy of this URI (ONTA-422):
    # `kg_meta_uri` is the canonical one and now validates the tenant half.
    kg_uri = kg_meta_uri(tenant.tenant_id, kg_name)

    # Drop all triples in the KG graph
    await client.update(f"DROP SILENT GRAPH <{graph}>")

    # Drop the precomputed type-stats graph + in-memory summary cache. The stats
    # graph URI is derived from the KG name, so a KG recreated under the same
    # name would otherwise serve this deleted graph's stale counts.
    from infona_client.api.routes.explore import drop_kg_stats
    await drop_kg_stats(client, tenant.tenant_id, kg_name)

    # Remove KG metadata
    await client.update(
        f"DELETE WHERE {{\n"
        f"  GRAPH <{base}> {{\n"
        f"    <{kg_uri}> ?p ?o .\n"
        f"  }}\n"
        f"}}"
    )

    # Purge stale examples from the example bank for this KG
    try:
        from infona_client.nlp.example_bank import get_example_bank
        bank = get_example_bank()
        if bank and bank._examples:
            before = len(bank._examples)
            bank._examples = [e for e in bank._examples if e.kg_name != kg_name]
            removed = before - len(bank._examples)
            if removed > 0:
                bank.save()
                import structlog
                structlog.get_logger("infona.kg").info(
                    "example_bank_purged", kg=kg_name, removed=removed,
                    remaining=len(bank._examples),
                )
    except Exception:
        pass  # Bank purge is best-effort, don't fail the delete

    # Clear this KG's rows from the spatio-temporal secondary index. Scoped to
    # (tenant_id, kg_name) so a sibling KG's geometry facts are untouched — the
    # whole reason the index carries a kg_name dimension. Best-effort: the
    # eventually-consistent derived index must never block the KG delete.
    try:
        from infona_client.spatiotemporal.registry import get_spatiotemporal_index
        await get_spatiotemporal_index().clear(tenant.tenant_id, kg_name=kg_name)
    except Exception:
        pass  # Derived-index cleanup is best-effort, don't fail the delete

    # Clear this KG's chunks from the SEMANTIC instance index (ONTA-181) — same
    # (tenant_id, kg_name) scoping and same best-effort contract as the
    # spatio-temporal clear above: only THIS KG's rows, never a sibling's.
    try:
        from infona_client.semantic.registry import get_semantic_index
        await get_semantic_index().clear(tenant.tenant_id, kg_name=kg_name)
    except Exception:
        pass  # Derived-index cleanup is best-effort, don't fail the delete

    # Evict the NL-planning caches for this tenant (ONTA-417). They are keyed by
    # the TENANT ontology graph, not by KG, so nothing above touches them:
    # without this, the deleted KG's cached ontology summary and its cached
    # active-type set survive the delete, and a KG recreated under the same name
    # inherits the dead one's cached scope for the rest of the TTL. Same
    # best-effort contract as the derived-index clears above.
    #
    # SCOPE, precisely. The two cache sweeps here are complete. The embedding
    # eviction is NOT: invalidate_cache pops only the IN-MEMORY embedding store,
    # and the next retrieve() reloads identical chunks from S3, which is never
    # cleared. So this does not stop a deleted KG's types from being retrieved.
    # What keeps them from displacing this graph's schema is ONTA-411, which
    # demotes them and marks them "[no instances]" because they carry no
    # instances in the graph being queried.
    #
    # TODO(ONTA-417): evicting the DECLARATIONS (and with them the S3-persisted
    # chunks) is the real fix and is deliberately not attempted here. The deleted
    # KG's types remain declared in the tenant ontology graph, so the next
    # embedding rebuild re-embeds them. Pruning them needs a real "is this type
    # still used by another KG, or was it authored by hand?" guard. Types are
    # shared tenant-wide BY DESIGN, and ONTA-258 deliberately keeps
    # declared-but-unpopulated types visible, so an unguarded prune would delete
    # user-authored schema.
    try:
        from infona_client.nlp.pipeline import NLQueryPipeline
        NLQueryPipeline.invalidate_cache(base)
    except Exception:
        pass  # Cache eviction is best-effort, don't fail the delete

    # Drop the KG's cached "this graph holds data" verdict (ONTA-453). The probe
    # caches POSITIVE verdicts for KG_STATUS_CACHE_TTL and never re-checks inside
    # it, so without this eviction a question asked in the minute after a delete
    # sails past the missing-KG guard and gets answered out of the tenant base
    # graph plus the global layers, which is exactly the confidently-wrong answer
    # that guard exists to stop. Every other derived index is evicted above; this
    # is the one that was missed.
    try:
        from infona_client.graph.kg_status import invalidate_kg_status
        invalidate_kg_status(tenant.tenant_id, kg_name)
    except Exception:
        pass  # Cache eviction is best-effort, don't fail the delete

    # Drop the KG's recurring semantic-reconcile schedule row (ONTA-181) so the
    # runner doesn't keep scanning a graph that no longer exists. Best-effort.
    try:
        from infona_client.semantic.reconciler import remove_reconcile_schedule
        await remove_reconcile_schedule(schedule_store, tenant.tenant_id, kg_name)
    except Exception:
        pass  # Schedule cleanup is best-effort, don't fail the delete

    return {"deleted": kg_name}


# ---------------------------------------------------------------------------
# Semantic instance index: on-demand reindex (ONTA-181).
# ---------------------------------------------------------------------------


class ReindexAccepted(BaseModel):
    """202 body for the on-demand semantic reindex trigger."""

    status: str = "accepted"
    kg_name: str
    schedule_id: str
    # "scheduled"       → a due-now schedule row was seeded; the claim-based
    #                     runner fires it (multi-task safe via SKIP LOCKED).
    # "background-task" → no runner in this deployment (zero-config OSS);
    #                     the reconcile was fired as an in-process task.
    mode: str


@router.post(
    "/{kg_name}/search/reindex", response_model=ReindexAccepted, status_code=202
)
async def reindex_kg_semantic(
    kg_name: str,
    request: Request,
    tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
    schedule_store=Depends(get_schedule_store),
):
    """Trigger an on-demand semantic reconcile (= backfill) for one KG.

    THE entry point for indexing an already-ingested KG without re-ingesting
    (ONTA-181's parliamentary-speeches scenario): the reconciler's first run
    against a KG is the backfill. Deliberately NOT an inline long-running
    request — it seeds the KG's recurring reconcile schedule row with
    ``next_run=now`` and returns 202 immediately; the claim-based schedule
    runner picks it up within one poll interval, so overlapping ECS tasks never
    double-scan. Deployments without a runner (no DSN, scheduler off) fall back
    to a fire-and-forget in-process task — single process, so no claim needed.

    503 when the semantic index is disabled (``INFONA_SEMANTIC_INDEX_ENABLED``
    is the master gate for the write hook AND the reconciler): accepting the
    request would acknowledge work that can never run.
    """
    from infona_client.semantic.reconciler import (
        ensure_reconcile_schedule,
        schedule_reconcile_task,
        semantic_index_enabled,
    )

    if not semantic_index_enabled():
        raise HTTPException(
            status_code=503,
            detail=(
                "Semantic indexing is disabled for this deployment "
                "(set INFONA_SEMANTIC_INDEX_ENABLED=true to enable it)."
            ),
        )

    schedule = await ensure_reconcile_schedule(
        schedule_store, tenant.tenant_id, kg_name, due_now=True
    )
    runner = getattr(request.app.state, "schedule_runner", None)
    if runner is None:
        schedule_reconcile_task(client, tenant.tenant_id, kg_name)
        mode = "background-task"
    else:
        mode = "scheduled"
    return ReindexAccepted(kg_name=kg_name, schedule_id=schedule.id, mode=mode)


# ---------------------------------------------------------------------------
# Browsing: type counts and per-type attribute usage within a KG.
# Read-only convenience endpoints that power the shell's /types and /type
# commands. The ontology itself is tenant-global; what's per-KG is which
# types actually have instances and how often each attribute is populated.
# ---------------------------------------------------------------------------


class TypeCount(BaseModel):
    name: str
    entity_count: int
    # Spatio-temporal index markers, read from the precomputed stats graph
    # (recompute_kg_stats materializes them; absence = False). Spatial = the
    # type's instances carry geo:wktLiteral geometry; temporal = they carry
    # validity bounds or a complete start+end date pair.
    spatially_indexed: bool = False
    temporally_indexed: bool = False


class AttributeUsage(BaseModel):
    name: str
    datatype: str = "string"
    count: int


class RelationshipUsage(BaseModel):
    name: str
    target_type: str | None = None
    count: int


class EntitySample(BaseModel):
    uri: str
    label: str = ""


class TypeUsage(BaseModel):
    name: str
    description: str = ""
    parent_type: str | None = None
    entity_count: int
    attributes: list[AttributeUsage] = []
    relationships: list[RelationshipUsage] = []
    samples: list[EntitySample] = []


@router.get("/{kg_name}/type-counts", response_model=list[TypeCount])
async def list_type_counts(
    kg_name: str,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """List every type that has instances in this KG, sorted by entity count.

    Tenant-global ontology types with zero instances in this KG are not
    returned here — fetch them via /ontology/types if the caller needs the
    full schema.

    **Dual-backend (E5):** when ``INFONA_GRAPH_BACKEND=neo4j`` (or a process
    GraphStore is configured for that backend), counts come from
    :func:`infona_client.graph.explore_store.type_counts` instead of SPARQL.
    Spatio-temporal index flags are still best-effort from the stats graph
    (Neptune path only; Neo4j returns False until stats port).
    """
    # GraphStore path (E5 explore_store) — same response shape.
    from infona_client.graph.explore_store import type_counts as pg_type_counts

    pg_rows = await pg_type_counts(
        tenant_id=tenant.tenant_id, kg_name=kg_name
    )
    if pg_rows is not None:
        return [
            TypeCount(
                name=r.name,
                entity_count=r.entity_count,
                spatially_indexed=False,
                temporally_indexed=False,
            )
            for r in pg_rows
        ]

    graph = kg_graph_uri(tenant.tenant_id, kg_name)
    sparql = (
        f"SELECT ?type (COUNT(DISTINCT ?e) AS ?cnt) FROM <{graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> ?type .\n"
        f'  FILTER(STRSTARTS(STR(?type), "{TYPE_URI_PREFIX}"))\n'
        f"}} GROUP BY ?type ORDER BY DESC(?cnt)"
    )
    raw, index_flags = await asyncio.gather(
        client.query(sparql),
        _read_type_index_flags(client, tenant.tenant_id, kg_name),
    )
    _, bindings = parse_sparql_results(raw)
    out: list[TypeCount] = []
    for row in bindings:
        t = row.get("type", "")
        if not t.startswith(TYPE_URI_PREFIX):
            continue
        # Skip nested URIs like .../types/{Type}/attrs/{name} which aren't types
        leaf = t[len(TYPE_URI_PREFIX):]
        if "/" in leaf:
            continue
        try:
            count = int(row.get("cnt", "0"))
        except ValueError:
            count = 0
        spatial, temporal = index_flags.get(leaf, (False, False))
        out.append(TypeCount(
            name=leaf,
            entity_count=count,
            spatially_indexed=spatial,
            temporally_indexed=temporal,
        ))
    return out


async def _read_type_index_flags(
    client: NeptuneClient, tenant_id: str, kg_name: str
) -> dict[str, tuple[bool, bool]]:
    """Per-type (spatially_indexed, temporally_indexed) from the stats graph.

    The markers are materialized by ``recompute_kg_stats``; a KG whose stats
    were never recomputed (or whose types carry neither marker) simply yields
    no rows — every type then defaults to (False, False). Best-effort: the
    flags decorate the type list, so a stats-graph hiccup must not take down
    the endpoint that powers the Explorer rail.
    """
    # Local import: explore imports this module (locally) for the triple-count
    # invalidation hook, so a module-level import here would create a cycle.
    from infona_client.api.routes.explore import (
        _STAT_SPATIAL,
        _STAT_TEMPORAL,
        _stats_graph_uri,
    )

    stats = _stats_graph_uri(tenant_id, kg_name)
    sparql = (
        f"SELECT ?type ?sp ?tp FROM <{stats}> WHERE {{\n"
        f"  {{ ?type <{_STAT_SPATIAL}> ?sp }} UNION {{ ?type <{_STAT_TEMPORAL}> ?tp }}\n"
        f"}}"
    )
    flags: dict[str, tuple[bool, bool]] = {}
    try:
        _, rows = parse_sparql_results(await client.query(sparql))
    except Exception:  # noqa: BLE001 — decoration only, never fail the list
        return flags
    for row in rows:
        t = row.get("type", "")
        if not t.startswith(TYPE_URI_PREFIX):
            continue
        leaf = t[len(TYPE_URI_PREFIX):]
        spatial, temporal = flags.get(leaf, (False, False))
        # Accept both boolean lexical forms ("true" and "1") — see _read_type_stats.
        if row.get("sp", "") in ("true", "1"):
            spatial = True
        if row.get("tp", "") in ("true", "1"):
            temporal = True
        flags[leaf] = (spatial, temporal)
    return flags


@router.get("/{kg_name}/types/{type_name}/usage", response_model=TypeUsage)
async def get_type_usage(
    kg_name: str,
    type_name: str,
    include_system: bool = False,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Per-type breakdown for one type in one KG.

    Combines the tenant-global ontology definition (attribute names,
    datatypes, parent type) with per-KG instance numbers (entity count,
    attribute usage, sample entities) so the caller doesn't have to make
    three round-trips and re-join the results client-side.
    """
    tenant_graph = tenant_graph_uri(tenant.tenant_id)
    kg_graph = kg_graph_uri(tenant.tenant_id, kg_name)
    t_uri = type_uri(type_name)

    # 1) Ontology definition for this type (tenant-global graph).
    onto_sparql = (
        f"SELECT ?label ?comment ?parent FROM <{tenant_graph}> WHERE {{\n"
        f"  <{t_uri}> <http://www.w3.org/2000/01/rdf-schema#label> ?label .\n"
        f"  OPTIONAL {{ <{t_uri}> <http://www.w3.org/2000/01/rdf-schema#comment> ?comment }}\n"
        f"  OPTIONAL {{ <{t_uri}> <http://www.w3.org/2000/01/rdf-schema#subClassOf> ?parent }}\n"
        f"}}"
    )
    _, onto_rows = parse_sparql_results(await client.query(onto_sparql))

    # Tenant-global attribute definitions for this type — gives us the
    # canonical name + datatype, which we'll join with per-KG usage counts.
    _, attr_def_rows = parse_sparql_results(
        await client.query(get_type_attributes_query(tenant_graph, type_name))
    )
    attr_def: dict[str, dict[str, str]] = {}
    for r in attr_def_rows:
        a_uri = r.get("attr", "")
        if not a_uri:
            continue
        attr_def[a_uri] = {
            "name": r.get("attrLabel", ""),
            "range": r.get("range", ""),
        }

    # 2) Entity count for this type within this KG.
    count_sparql = (
        f"SELECT (COUNT(DISTINCT ?e) AS ?n) FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> <{t_uri}>\n"
        f"}}"
    )
    _, count_rows = parse_sparql_results(await client.query(count_sparql))
    try:
        entity_count = int(count_rows[0].get("n", "0")) if count_rows else 0
    except ValueError:
        entity_count = 0

    if entity_count == 0 and not onto_rows:
        # Nothing in the ontology and nothing in the KG → 404 so the CLI can
        # tell the user "no such type" instead of silently returning zeros.
        raise HTTPException(
            status_code=404,
            detail=f"Type '{type_name}' not found in tenant ontology or KG '{kg_name}'",
        )

    # 3) Per-predicate usage in this KG. SAMPLE(?o) lets us classify
    # attribute (literal) vs. relationship (typed entity) without a second
    # round-trip per predicate.
    pred_sparql = (
        f"SELECT ?p (COUNT(DISTINCT ?e) AS ?cnt) (SAMPLE(?o) AS ?sample)\n"
        f"FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> <{t_uri}> .\n"
        f"  ?e ?p ?o .\n"
        f"  FILTER(?p != <{RDF_TYPE}>)\n"
        f"}} GROUP BY ?p ORDER BY DESC(?cnt)"
    )
    _, pred_rows = parse_sparql_results(await client.query(pred_sparql))

    attributes: list[AttributeUsage] = []
    relationships: list[RelationshipUsage] = []
    for r in pred_rows:
        p_uri = r.get("p", "")
        if not include_system and p_uri in SYSTEM_PREDICATES:
            continue
        try:
            cnt = int(r.get("cnt", "0"))
        except ValueError:
            cnt = 0
        sample = r.get("sample", "")
        defn = attr_def.get(p_uri, {})
        # Predicate name: prefer ontology label, fall back to URI tail.
        name = defn.get("name") or p_uri.rstrip("/").split("/")[-1]
        rng = defn.get("range", "")
        # Classify: object pointing into the entities/types namespace OR
        # ontology-declared range that's another type → relationship.
        is_rel = (
            sample.startswith(ENTITY_URI_PREFIX)
            or sample.startswith(TYPE_URI_PREFIX)
            or rng.startswith(TYPE_URI_PREFIX)
        )
        if is_rel:
            target: str | None = None
            if rng.startswith(TYPE_URI_PREFIX):
                target = rng[len(TYPE_URI_PREFIX):]
            elif sample.startswith(ENTITY_URI_PREFIX):
                # Entity URIs are .../entities/{TypeName}/{slug}; pull the
                # type out so the CLI can render "industries → Industry"
                # even when the ontology hasnf't declared a typed range.
                tail = sample[len(f"{IRI_BASE}/entities/"):]
                head = tail.split("/", 1)[0]
                if head:
                    target = head
            relationships.append(
                RelationshipUsage(name=name, target_type=target, count=cnt)
            )
        else:
            attributes.append(
                AttributeUsage(
                    name=name,
                    datatype=_xsd_to_datatype(rng),
                    count=cnt,
                )
            )

    # 4) Up to 3 sample entities with a name-like label, picked by trying
    # the conventional label attributes in order. Cheap one-shot query.
    label_optionals = "\n".join(
        f'    OPTIONAL {{ ?e <{TYPE_URI_PREFIX}{type_name}/attrs/{a}> ?{a} }}'
        for a in NAME_ATTRS
    )
    label_vars = " ".join(f"?{a}" for a in NAME_ATTRS)
    sample_sparql = (
        f"SELECT ?e {label_vars} FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> <{t_uri}> .\n"
        f"{label_optionals}\n"
        f"}} LIMIT 3"
    )
    samples: list[EntitySample] = []
    try:
        _, sample_rows = parse_sparql_results(await client.query(sample_sparql))
        for r in sample_rows:
            uri = r.get("e", "")
            label = next((r[a] for a in NAME_ATTRS if r.get(a)), "")
            samples.append(EntitySample(uri=uri, label=label))
    except Exception:
        # Sample fetch is decorative; don't blow up the whole response if
        # the SPARQL chokes on something we didn't anticipate.
        samples = []

    onto_row = onto_rows[0] if onto_rows else {}
    parent = onto_row.get("parent", "")
    return TypeUsage(
        name=type_name,
        description=onto_row.get("comment", ""),
        parent_type=parent.rstrip("/").split("/")[-1] if parent else None,
        entity_count=entity_count,
        attributes=attributes,
        relationships=relationships,
        samples=samples,
    )


def _xsd_to_datatype(uri: str) -> str:
    if not uri:
        return "string"
    if uri.startswith(TYPE_URI_PREFIX):
        return uri[len(TYPE_URI_PREFIX):]
    last = uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]
    return {
        "string": "string",
        "integer": "integer",
        "float": "float",
        "boolean": "boolean",
        "dateTime": "datetime",
        "Resource": "uri",
    }.get(last, "string")
