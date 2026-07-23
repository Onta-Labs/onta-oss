"""Web-discovery capability — find a NEW set of records on the web and ingest them.

This is the discovery counterpart to enrichment. Enrichment fills a missing
``(entity, attribute)`` cell on entities that ALREADY exist; discovery CREATES a
whole set of new entities from a natural-language query ("a list of models
offered by OpenRouter"). So it reuses the **ingest** engine, not the enrichment
engine.

The flow deliberately confirms the SHAPE before fetching, so the ontology expands
accurately and the user doesn't have to run a separate enrichment afterward:

1. ``plan`` resolves the target ENTITY type and the ATTRIBUTES to collect. If the
   user only named the entity ("a list of models"), it proposes a sensible
   attribute set and returns a CLARIFY turn ("I'll collect Model records, always
   including name — pick the ones to collect"), pre-selecting a SHORT recommended
   set (the few most-important attributes) while keeping a comprehensive fetch hint
   behind the scenes. The user's reply (a clicked option carrying the list, or free
   text) enters the accumulated instruction so the next turn converges.
2. Once attributes are confirmed, ``plan`` fetches a cheap SAMPLE constrained to
   those attributes and runs the SAME multi-type + relationship extractor the
   commit uses against it — so the plan card shows an ESTIMATE of the ontology
   shape the ingest will mint (the distinct entity types, their attributes, and
   the edges between them), not a flat pre-named type. The estimate comes from an
   8-row sample run through a non-deterministic extractor, so the full commit
   (over many more records) may surface additional types/relationships or differ
   in detail. What IS stable across preview and commit is the FETCH hint
   (``hint_columns``) — the column projection sent to the provider. If the
   extractor can't run, the preview degrades to a flat single-type card (the turn
   never 500s).
3. ``execute`` fetches the FULL set (targeting the same attributes) and ingests
   it through :meth:`SchemaResolver.ingest` (``content_type="json"``) — the
   identical extract→resolve→insert path document ingest commits through, which
   infers MULTIPLE types and registers relationships as object-properties — as a
   background job. Returns an ack. For an ENUMERATION ask ("all X in Y and Z"),
   the spec partitions the scope into self-contained ``subqueries``; execute runs
   one discovery per sub-query, dedupes on the key attribute across batches, and
   ingests each batch as it lands (one merged job, streaming progress) — one page
   never caps a population query (ONTA-192).

OSS ships with NO web-source provider registered, so the capability degrades
gracefully: ``plan`` returns a plain "not enabled" answer until a downstream
deployment registers a provider (the dev stub, or a paid Exa/Perplexity fan-out).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import structlog

from cograph_client.agent.registry import AgentContext, PlanStep
from cograph_client.obs import timed
from cograph_client.api_registry import (
    MODE_API_ONLY,
    RoutingDecision,
    RoutingPick,
    build_registry_sources,
    get_api_source_catalog,
    load_tenant_custom_catalog,
    make_tenant_api_source_store,
    route_query,
)
from cograph_client.enrichment.models import (
    ApiRequestTrace,
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobCategory,
    JobErrorItem,
    JobStatus,
    ProviderLog,
)
from cograph_client.graph.kg_writer import refresh_after_write
from cograph_client.graph.ontology_queries import entity_uri
from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri
from cograph_client.graph.suppression import fetch_suppressed_entities
from cograph_client.normalization.inference import list_type_schema
from cograph_client.config import settings
from cograph_client.pipeline.a1_validators import screen_row
from cograph_client.pipeline.manifest import (
    HaltReasonKind,
    RunManifest,
    resolve_spend_ceiling,
)
from cograph_client.pipeline.envelope import ArtifactEnvelope, derive_fact_id
from cograph_client.pipeline.source_bundle import (
    TIER_AUTHORITATIVE,
    TIER_WEB,
    SourceBundle,
    build_source_bundle,
)
from cograph_client.pipeline.stage_trace import (
    StageProjectId,
    StageStatus,
    attach_recorder,
    merge_a1_summaries,
    merge_a3_counts,
    summarize_a1_source_bundle,
    summarize_a2_candidates,
    summarize_a3_clean_report,
    summarize_a6_graph_delta,
    finalize_job_stage_trace,
    open_job_stage_trace,
)
from cograph_client.resolver.llm_router import (
    OPENROUTER_BASE,
    PRIMARY_MODEL,
    openrouter_chat,
)
from cograph_client.retrieval.errors import (
    CostCeilingExceeded,
    LLMError,
    RateLimitEscalator,
    RetrievalError,
    is_rate_limit_status,
)
from cograph_client.web_sources.base import (
    WebSourceProvider,
    get_web_source,
    get_web_source_for_kind,
    has_kind_specialized_provider,
    provider_cost,
)
from cograph_client.web_sources.url_extract import extract_urls

logger = structlog.stdlib.get_logger("cograph.agent.web_ingest")

_bg_tasks: set[asyncio.Task] = set()

# Rows requested for the cheap plan-time sample (preview + datatype inference).
_SAMPLE_ROWS = 8
_PREVIEW_SAMPLE = 5
_PREVIEW_SOURCES = 5
# Default cap on rows a single interactive discovery run pulls. Sized so a first
# (paid) discovery is BOUNDED, cheap to inspect, AND — critically — SETTLES to a
# terminal state WITHIN the run's own wall-clock budget, so an interactive session
# gets a usable, CONSISTENT populated subset instead of a slow job that never
# finishes in-session (persona-eval m3 RCA).
#
# Why 50 and not 200: discovery's dominant cost is the per-record LLM extraction
# (~one extract call per _DISCOVERY_INGEST_SUBBATCH rows), which runs SEQUENTIALLY.
# Measured on the UCI oc-physicians build (deployed b66e2ef2): ~9-10s/record end to
# end. Against the hard _RUN_TIMEOUT_S wall (600s default) that means:
#   cap=200 -> ~32 min to fill -> ALWAYS hits the 600s wall at ~60 records and
#              flips to ``failed`` — the graph is left a partial MOVING TARGET
#              (rolling ``total`` 25->200->134->159) that different follow-up tasks
#              see contradictory snapshots of.
#   cap=50  -> ~8 min to fill -> settles to a real terminal state under the wall,
#              and needs only ~4 sequential wait_for_job (120s) calls to observe.
# 50 records is enough for a persona to query a coherent subset; pulling MORE is a
# clean follow-up (re-run the discovery, or raise this via the env knob / a future
# per-query cap) rather than a single slow job that never lands. Env-overridable so
# ops can retune (e.g. a batch/back-office deployment that wants the old 200 and
# accepts a longer _RUN_TIMEOUT_S) without a deploy. Mirrors the enrich plan's
# _DEFAULT_PLAN_LIMIT pattern; still user-overridable per plan.
_DEFAULT_PLAN_CAP = max(1, int(os.environ.get("COGRAPH_DISCOVERY_DEFAULT_CAP", "50")))

# Wall-clock budgets for building the plan-time PREVIEW, sized well under the
# Explorer proxy's 55s backend abort (web/app/api/demo/agent/route.ts
# BACKEND_TIMEOUT_MS). The preview chains a paid web-search fan-out (the sample)
# and one extraction LLM call (the shape estimate); each provider/LLM carries its
# OWN timeout (30-60s) that rivals or EXCEEDS that whole request budget, so with no
# outer bound a broad, source-less query (e.g. "physicians across two cities")
# runs 65-75s and the proxy kills the request → the client's "took too long".
# Bounding each heavy step keeps the turn IN budget; on a timeout we DEGRADE to a
# confirmable flat-preview plan (execute() still runs the FULL discovery as a
# background job, so no data is lost — only the rich preview is skipped). Env-
# overridable so ops can retune without a deploy if the proxy budget changes.
# Together (22 + 15 = 37s) they leave headroom under the 55s budget for the small
# upstream classify + spec-resolve LLM calls; the sample's web fan-out is the
# bigger variable, so it gets the larger share.
_SAMPLE_BUDGET_S = float(os.environ.get("COGRAPH_WEB_SAMPLE_BUDGET_S", "22"))
_SHAPE_BUDGET_S = float(os.environ.get("COGRAPH_WEB_SHAPE_BUDGET_S", "15"))

# Hard wall-clock budget for the WHOLE background discovery run (all sub-queries,
# providers, and the LLM-extraction ingest of every batch). Without it a run
# whose extraction pathologically stalls — e.g. a dense chunk that overflows the
# token cap and falls into the recursive split-and-retry recovery, ~30-40
# sequential ~70s LLM calls — sits on ``running`` for 45+ minutes with no way to
# flip to a terminal state (ONTA-196). On timeout we route to _fail_job so the
# job honestly shows ``failed``, never a stuck ``running``. Generous default so a
# legitimately large pull isn't cut short; env-overridable for ops tuning.
_RUN_TIMEOUT_S = float(os.environ.get("OMNIX_DISCOVERY_RUN_TIMEOUT_S", "600"))

# Auto-confirm gate. Discovery plans whose provider cost is at or under this are
# treated as CHEAP: clients start the job straight from the attribute confirm
# (no human spend gate), so the expensive plan-time preview — a paid sample
# fetch (~22s) plus an extraction LLM call (~15s) — would be pure latency
# building a card nobody sees. plan() skips it and returns a lean, immediately-
# confirmable step; the full sample+shape preview is reserved for providers
# ABOVE the gate, where a human reviews real money and the estimate earns its
# cost. The web client auto-confirms plans up to this same figure.
_PREVIEW_GATE_USD = float(os.environ.get("COGRAPH_WEB_PREVIEW_GATE_USD", "0.50"))

# ONTA-199 follow-up (the decomposition fix). Discovery extraction defaults to
# SOFT (seed) mode: the user-confirmed target type + attributes are passed as a
# PRIOR that keeps extraction focused and compact (the cost/fragmentation win the
# HARD constraint chased) WHILE letting the extractor decompose faithfully —
# most-specific subtypes (a nurse practitioner stays a NursePractitioner, not a
# Physician), real-world values lifted to nodes (city -> City, specialty ->
# Specialty), multi-valued fields split, measurements kept literal. The old HARD
# cage (flat single literal-only type) is retained behind this flag purely as a
# kill-switch: set COGRAPH_DISCOVERY_SOFT_EXTRACT=0 to revert without a deploy.
_DISCOVERY_SOFT_EXTRACT = (
    os.environ.get("COGRAPH_DISCOVERY_SOFT_EXTRACT", "1") != "0"
)

# ONTA-272: pre-structured fast-path. A provider that returns ALREADY-structured
# rows keyed by the confirmed attribute set (an API-registry pull with a known
# field mapping, a structured capture) does not need the open-ended LLM extractor
# — running it is a non-deterministic detour. When this flag is ON and a provider
# self-declares ``structured=True`` (read DEFENSIVELY via getattr, so no provider
# change is required for the default path), that provider's rows commit through the
# deterministic mapping seam (``resolver.ingest_structured_rows`` →
# ``ingest_mapped_records``) with NO ``_extract``. Default OFF: the ``resolver.ingest``
# JSON path is byte-for-byte unchanged (and stays frozen by test_web_ingest_registry),
# so this is an opt-in rollout switch with a kill-switch, mirroring
# ``_DISCOVERY_SOFT_EXTRACT`` above.
_DISCOVERY_STRUCTURED_FASTPATH = (
    os.environ.get("COGRAPH_DISCOVERY_STRUCTURED_FASTPATH", "0") != "0"
)

# In-session progress observability (ONTA-243). A single (sub-query, provider)
# batch's ``resolver.ingest`` is one opaque LLM-extraction await — for the classic
# single-list ask (one sub-query, one provider) it is the WHOLE run, so
# ``processed``/``filled`` otherwise stay 0/0 until it completes (minutes), and a
# poller reads the job as stalled and gives up on a job that is in fact working
# (the persona-eval RCA: 7 of 15 tool calls burned polling identical running/0/0).
# The fix mirrors enrichment's per-record flush cadence (executor.py
# PROGRESS_FLUSH_EVERY): split each batch's rows into sub-batches, ingest each,
# and flush ``processed``/``filled`` after every one — so both headline counters
# move WHILE the run is still ``running``, in ANY domain, without a resolver
# signature change. A small sub-batch trades a few extra (cheap) LLM extraction
# calls for real streaming progress; env-overridable so ops can retune the
# progress-granularity vs call-count balance without a deploy.
_DISCOVERY_INGEST_SUBBATCH = max(
    1, int(os.environ.get("COGRAPH_DISCOVERY_INGEST_SUBBATCH", "5"))
)

# ONTA-394: entity fan-out observability. Soft extract can multiply a keyed A1
# table (44 rows) into ~2x entities (91) by over-minting subtypes / promoting
# skewed cells into nodes. The AC#3 (value-gated node promotion) and AC#4
# (subtype collapse) guards bring the count back down; this ratio makes any
# remaining amplification VISIBLE on the Job Trace instead of silent. When
# a2_entities_extracted / a1_row_count exceeds this ratio, the run logs a warning
# and stamps ``entity_fanout_high`` on the stage-trace summary. Observability
# ONLY — never drops entities (a page can legitimately name several subjects).
_DISCOVERY_FANOUT_WARN_RATIO = float(
    os.environ.get("COGRAPH_DISCOVERY_FANOUT_WARN_RATIO", "2.0")
)


def _chunk_rows(rows: list, size: int) -> list[list]:
    """Split ``rows`` into consecutive sub-batches of at most ``size`` (order
    preserved). ``size <= 0`` degrades to one whole chunk — never an empty split."""
    if size <= 0 or len(rows) <= size:
        return [rows] if rows else []
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def _group_rows_by_source_url(rows: list) -> list[list]:
    """Partition a batch into consecutive groups that are HOMOGENEOUS in their
    ``source_url`` (order preserved), so every row in a group cites the same page.

    This is the deterministic half of the citation-binding fix. The ``source_url``
    is stamped on each row BEFORE extraction (keyed by the provider's per-record
    provenance), but the multi-type LLM extractor then re-decides which minted
    entity each field lands on — so a batch mixing rows from page A and page B can
    have A's URL copied onto an entity drawn from B (the observed mis-binding: one
    page-level URL broadcast across every model on the page). By committing one
    ``resolver.ingest`` call PER distinct source URL, an extraction can only ever
    see rows that share ONE page, so the only URL available to stamp on any entity
    it mints is that page's URL — the cross-record placement decision is taken away
    from the LLM. Rows with no ``source_url`` (free/stub providers) form their own
    group and are unaffected.

    Groups are consecutive runs, not a global regroup, so row order within a batch
    is preserved and a provider that already returns rows page-by-page pays no
    reshuffle. Returns ``[]`` for an empty batch, ``[rows]`` when every row shares
    one URL (or none carry one) — the previous single-partition behavior.
    """
    if not rows:
        return []
    groups: list[list] = []
    current: list = []
    current_key: object = object()  # sentinel: no group started yet
    for row in rows:
        # Accept either a raw dict row OR an A1 ``SourceRow`` (ONTA-371: the
        # extract loop now drives from bundle rows). A ``SourceRow`` groups by the
        # SAME per-record source_url its snapshot ``data`` carries, so grouping is
        # byte-identical whether driven from the raw batch or the bundle.
        if isinstance(row, dict):
            key = row.get(SOURCE_URL_ATTR)
        else:
            data = getattr(row, "data", None)
            key = data.get(SOURCE_URL_ATTR) if isinstance(data, dict) else None
        if not current or key == current_key:
            current.append(row)
            current_key = key
        else:
            groups.append(current)
            current = [row]
            current_key = key
    if current:
        groups.append(current)
    return groups


def _drop_suppressed_rows(
    rows: list,
    proposed_type: str,
    key_attr: str,
    suppressed_entities: set[str],
    *,
    provider: str = "",
    job_id: Optional[str] = None,
) -> list:
    """Drop discovered rows whose would-be canonical subject is on the ENTITY-level
    suppression list (ONTA-345) — the FIND-path re-acquisition guard.

    For each surviving (post-dedupe) row this computes the SAME canonical instance
    IRI the resolver would mint for it — ``entity_uri(proposed_type,
    row[key_attr])`` — and DROPS the row when that subject is entity-suppressed
    (erased / tombstoned). So an ERASED entity is never re-minted by discovery or a
    refresh (the P1 'never re-acquire erased data' rule; GDPR erasure blast
    radius). Membership is a set check against ``suppressed_entities`` — fetched
    ONCE per run (:func:`fetch_suppressed_entities`) — so this is O(1) per row, no
    per-row query. Each drop is logged (structured). A no-op returning ``rows``
    unchanged when the suppression set is empty (the common case), so the happy
    path pays only one set-emptiness check.
    """
    if not suppressed_entities or not rows:
        return rows
    kept: list = []
    for row in rows:
        raw_id = row.get(key_attr) if isinstance(row, dict) else None
        subject = entity_uri(proposed_type, str(raw_id)) if raw_id else None
        if subject is not None and subject in suppressed_entities:
            logger.info(
                "web_ingest_suppressed_entity_dropped",
                subject=subject,
                type=proposed_type,
                key=str(raw_id),
                provider=provider,
                job_id=job_id,
            )
            continue
        kept.append(row)
    return kept


def _screen_a1_rows(
    rows: list,
    key_attr: str,
    attributes: list[str],
    *,
    provider: str = "",
    job_id: Optional[str] = None,
) -> tuple[list, int, int, list[str]]:
    """A1 validators (ONTA-393): reject nav-chrome NAMES and type-invalid CELLS from
    a post-dedupe batch BEFORE the SourceBundle is built and BEFORE resolver.ingest*,
    so garbage never becomes a graph entity.

    A row whose key/name cell is chrome ("About", "Skip to content", …) is DROPPED
    whole; a real row carrying a type-invalid cell (city that is a year, website with
    no host, address that is an enrolment phrase) keeps the row but SCRUBS that cell.
    Per-row decisions come from the pure :func:`screen_row`; here we only log each
    drop and never mutate the provider's row dict (scrubbing copies).

    Returns ``(kept_rows, rows_dropped, cells_scrubbed, drop_reasons)``. A no-op
    returning ``rows`` unchanged when nothing is invalid — the happy path pays one
    screen pass, mirroring :func:`_drop_suppressed_rows`."""
    if not rows:
        return rows, 0, 0, []
    kept: list = []
    rows_dropped = 0
    cells_scrubbed = 0
    reasons: list[str] = []
    for row in rows:
        verdict = screen_row(row, key_attr, list(attributes))
        if verdict.drop_row:
            rows_dropped += 1
            reasons.append(verdict.row_reason)
            logger.info(
                "web_ingest_a1_row_dropped",
                reason=verdict.row_reason,
                key=str(row.get(key_attr)) if isinstance(row, dict) else "",
                provider=provider,
                job_id=job_id,
            )
            continue
        if verdict.scrubbed:
            # Copy before scrubbing — the provider's row (and its provenance) is
            # shared; we remove only the offending cells from OUR view of it.
            row = dict(row)
            for attr, reason in verdict.scrubbed.items():
                row.pop(attr, None)
                cells_scrubbed += 1
                reasons.append(reason)
                logger.info(
                    "web_ingest_a1_cell_scrubbed",
                    attribute=attr,
                    reason=reason,
                    provider=provider,
                    job_id=job_id,
                )
        kept.append(row)
    return kept, rows_dropped, cells_scrubbed, reasons


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# --------------------------------------------------------------------------- #
# A1 Source Bundle boundary (ONTA-346)
# --------------------------------------------------------------------------- #
def _provider_tier(prov) -> str:
    """The source TIER a provider's rows belong to: :data:`TIER_AUTHORITATIVE`
    for a registry source-of-truth (Tier -1, consulted before web search), else
    :data:`TIER_WEB`. Read DEFENSIVELY — a plain web provider declares no
    ``is_source_of_truth`` and lands on ``web``."""
    return TIER_AUTHORITATIVE if getattr(prov, "is_source_of_truth", False) else TIER_WEB


def _provider_secret_refs(prov) -> tuple[str, ...]:
    """The LOGICAL secret reference(s) a provider uses — NEVER a resolved
    credential. A registry source carries a per-tenant ``secret_ref`` on its
    spec's auth (decrypted only at FETCH time, inside the executor). Read it
    defensively (a public ``secret_ref`` convention first, then the registry
    spec's ``auth.secret_ref``); a web/free provider has none, so the bundle
    carries an empty tuple. This reads the reference NAME only — it does not
    touch the secret store or decrypt anything."""
    ref = getattr(prov, "secret_ref", "") or ""
    if not ref:
        spec = getattr(prov, "_spec", None)
        auth = getattr(spec, "auth", None)
        ref = getattr(auth, "secret_ref", "") or ""
    return (ref,) if ref else ()


def _emit_source_bundle(ctx: AgentContext, bundle: SourceBundle) -> None:
    """Hand the assembled A1 :class:`SourceBundle` to an OPTIONAL observer on the
    context (``ctx.extras['source_bundle_sink']`` — a callable, or a list to
    append to). This is a SUPPLEMENTARY observability hook only: as of ONTA-371 the
    bundle is the LIVE extract driver — the micro-batch extract/write loop iterates
    ``bundle.rows`` and threads each row's ``fact_id`` / ``tier`` into the resolver
    ingest calls (the real A1→A2 handoff) — so the bundle is genuinely consumed
    whether or not a sink is wired. Absent a sink this is a no-op. NEVER raises — an
    observer error must not sink a discovery run."""
    extras = getattr(ctx, "extras", None) or {}
    sink = extras.get("source_bundle_sink")
    if sink is None:
        return
    try:
        if callable(sink):
            sink(bundle)
        else:
            append = getattr(sink, "append", None)
            if callable(append):
                append(bundle)
    except Exception:  # noqa: BLE001 — observability must never break the run
        logger.warning("source_bundle_sink_failed", exc_info=True)


# --------------------------------------------------------------------------- #
# API source registry routing (ONTA-194 phase 2)
# --------------------------------------------------------------------------- #
async def _registry_route(
    ctx: AgentContext, query: str, spec: dict, urls: list
) -> RoutingDecision:
    """Consult the API source registry on every query-mode discovery. Never raises.

    URL-targeted extraction skips it (the pages are fixed). Otherwise the router
    self-degrades to ``web_only`` — no OpenRouter key, an empty catalog, or no
    entry that genuinely covers the ask all leave discovery exactly as it was —
    so "consult the registry" is safe to run unconditionally.
    """
    if urls:
        return RoutingDecision()
    try:
        catalog = await _tenant_catalog(ctx.tenant_id)
        if not catalog.enabled():
            return RoutingDecision()
        return await route_query(
            query,
            catalog,
            openrouter_key=getattr(ctx, "openrouter_key", "") or "",
            entity_type=spec.get("entity_type") or "",
            query_kind=spec.get("query_kind") or "",
        )
    except Exception:  # noqa: BLE001 — routing must never break discovery
        logger.warning("registry_route_failed", exc_info=True)
        return RoutingDecision()


async def _tenant_catalog(tenant_id: str):
    """The catalog scoped to ``tenant_id`` — global layers + that tenant's own
    custom entries. Loads the tenant's custom layer from the durable store into
    the per-tenant cache, then returns the merged catalog. Never raises: on any
    store error it falls back to the global catalog so discovery is unchanged."""
    try:
        return await load_tenant_custom_catalog(
            tenant_id, make_tenant_api_source_store()
        )
    except Exception:  # noqa: BLE001 — a store hiccup must not break discovery
        logger.warning("tenant_custom_catalog_load_failed", exc_info=True)
        return get_api_source_catalog()


def _merge_registry_ensemble(web_ensemble: list, registry_sources: list, mode: str) -> list:
    """Splice registry sources into the discovery ensemble ahead of web.

    ``api_only`` → the registry alone (no web spend), falling back to web only if
    the registry yielded no usable source. Otherwise registry-first then web (the
    cross-provider key dedupe makes the overlap free; the source-of-truth rows win).
    """
    if not registry_sources:
        return web_ensemble
    if mode == MODE_API_ONLY:
        return list(registry_sources) or list(web_ensemble)
    merged = list(registry_sources)
    for p in web_ensemble:
        if all(p is not q for q in merged):
            merged.append(p)
    return merged


async def _rebuild_registry_sources(params: dict, tenant_id: str) -> tuple[list, str]:
    """Rebuild registry providers from the picks persisted at plan time.

    Uses the tenant-scoped catalog so a pick that named a tenant_custom source is
    rebuilt against that tenant's own entry (the catalog is re-loaded here because
    execute() may run in a different request than plan(), so the per-tenant cache
    may be cold)."""
    raw = params.get("registry_picks") or []
    picks = [RoutingPick.from_dict(x) for x in raw if isinstance(x, dict)]
    if not picks:
        return [], MODE_API_ONLY
    mode = str(params.get("registry_mode") or "api_plus_web")
    decision = RoutingDecision(mode=mode, picks=picks)
    catalog = await _tenant_catalog(tenant_id)
    return build_registry_sources(catalog, decision, tenant_id=tenant_id), mode


def _registry_card(registry_sources: list) -> str:
    """Human plan-card line naming the registered API(s) consulted."""
    if not registry_sources:
        return ""
    names = []
    for s in registry_sources:
        tag = " (registered source of truth)" if getattr(s, "is_source_of_truth", False) else ""
        names.append(f"{getattr(s, 'title', None) or s.name}{tag}")
    return "Using " + ", ".join(names)


class WebIngestCapability:
    name = "web_ingest"

    def describe(self) -> str:
        return (
            "Discover a NEW set of records from the web and ingest them as a new "
            "dataset/type. Use for 'find a list of X from the web', 'pull all Y', "
            "'add data about Z from the web', 'get me <records> and add them'. Use "
            "when the user wants to CREATE entities that don't exist in the graph "
            "yet — NOT to fill attributes on existing entities (that is enrich)."
        )

    async def plan(
        self,
        ctx: AgentContext,
        instruction: str,
        parsed: dict | None = None,
    ) -> list[PlanStep]:
        # Explicit URLs the user handed us — from structured request context
        # (ctx.urls, read defensively so this works before that field lands) or
        # parsed out of the message. When present we run URL-TARGETED extraction:
        # pull records FROM those pages instead of web-searching for a query.
        urls = (getattr(ctx, "urls", None) or []) or extract_urls(instruction)

        # AVAILABILITY GATE (runs BEFORE _resolve_spec, so the query_kind isn't
        # known yet). URL mode needs a URL-capable provider. Query mode is available
        # if EITHER a general query provider OR at least one kind-specialized
        # provider is registered — a place-only deployment (kind provider registered
        # but no general default, e.g. only GOOGLE_PLACES_API_KEY set) can still
        # serve place queries, so we must NOT refuse before checking the kind
        # routing. We bail early only when NEITHER exists; the exact provider is
        # picked after the spec resolves the query_kind.
        general = get_web_source(for_urls=bool(urls))
        if urls:
            if general is None:
                return [
                    _answer_step(
                        "I can see the link(s) you shared, but URL extraction isn't "
                        "enabled in this deployment. An admin can configure a "
                        "URL-capable web-source provider to parse pages like these "
                        "into ingested data."
                    )
                ]
        elif general is None and not has_kind_specialized_provider():
            # No general provider AND no kind-specialized provider → nothing can
            # serve ANY query. (With a kind provider present we press on; a query
            # that doesn't match its kind is refused gracefully after the spec.)
            return [
                _answer_step(
                    "Web discovery isn't enabled in this deployment. An admin can "
                    "configure a web-source provider (e.g. Exa or Perplexity) to "
                    "turn a request like this into ingested data."
                )
            ]

        # 1. Resolve the entity type, the attributes to collect, a CLEAN search
        #    subject, and a generic query_kind — so we search for "OpenRouter TTS
        #    models", NOT the user's raw conversational sentence ("can we ingest
        #    open-router's TTS models that it currently offers"). If the user only
        #    named the entity, propose a set and confirm before spending anything.
        if parsed:
            spec = parsed
        else:
            async with timed(logger, "spec_resolve"):
                spec = await _resolve_spec(ctx, instruction)

        # Query-kind routing (ONTA-190): PREFER a provider specializing in the
        # spec's generic query_kind (e.g. "place" for a location/business-finding
        # query) when one is registered — ADDITIVELY, not exclusively: a kind match
        # builds an ENSEMBLE of [specialized, general] consulted in that order at
        # execute time, because neither source is complete alone (Places lists the
        # mappable businesses; the general web finds directory/roster pages Places
        # misses). The cross-batch key dedupe makes the overlap free. In a
        # place-only deployment `general` is None → the ensemble is just the
        # specialized provider; a NON-matching query there has no provider —
        # refused gracefully below (not a crash). URL mode always uses the URL
        # extractor (`general`) selected above; kind routing never applies to it.
        if urls:
            ensemble = [general] if general else []
        else:
            query_kind = spec.get("query_kind")
            specialized = (
                get_web_source_for_kind(query_kind) if query_kind else None
            )
            ensemble = []
            for p in (specialized, general):
                if p is not None and all(p is not q for q in ensemble):
                    ensemble.append(p)
            if not ensemble:
                # A general query in a kind-only deployment (e.g. place-only): the
                # only registered provider can't serve this query's kind. Refuse
                # gracefully instead of proceeding with no provider.
                return [
                    _answer_step(
                        "Web discovery for this kind of request isn't enabled in "
                        "this deployment. The configured web source only handles "
                        "certain queries (e.g. finding physical places); an admin "
                        "can add a general web-source provider for other requests."
                    )
                ]
        # Primary provider: drives the plan-time sample, naming, and legacy params.
        provider = ensemble[0] if ensemble else None
        if provider is None:
            # URL mode with no URL-capable provider was already refused above;
            # defensive guard for any future path.
            return []

        type_name = spec.get("entity_type") or "WebRecord"
        query = (spec.get("query") or "").strip() or _clean_query(instruction)
        if not query:
            return []
        key_attr = spec.get("key_attribute") or "name"
        # A GENUINELY degraded spec (the resolver LLM failed AND no explicit field
        # list could be recovered) carries a user-facing note so the thinning to a
        # bare name/description capture is SURFACED, not silent. Empty on the happy
        # path / when a field floor was recovered → no prefix is shown. Prepended to
        # the clarify question and to a committed thin plan's rationale/summary so the
        # user always learns the planning degraded instead of quietly getting a thin
        # dataset.
        degraded_note = str(spec.get("degraded_note") or "").strip()
        degraded_prefix = f"{degraded_note} " if degraded_note else ""

        # ONTA-239 (Cluster 2b) — ONTOLOGY GROUNDING. Fetch the target type's
        # already-declared attribute names so this second rail converges on the
        # first rail's names instead of minting a synonym for the same concept
        # (``per_minute_pricing`` vs an existing ``realtime_audio_duration_per_minute``).
        # Mirrors what the enrich rail does via ``_validate_enrich_request``. Best-
        # effort: a brand-new type / read hiccup yields an empty schema → snapping
        # is a no-op and nothing diverges from today's behavior.
        declared_attrs: list[str] = []
        try:
            schema = await list_type_schema(ctx.neptune, ctx.tenant_id, type_name)
            declared_attrs = [a for a in (schema.get("attributes") or []) if a]
        except Exception:  # noqa: BLE001 — grounding is best-effort, never a 500
            logger.warning("web_ingest_type_schema_failed", exc_info=True)

        # ONTA-239 (Cluster 2a) — DETERMINISTIC FIELD FLOOR. When the user handed
        # over an explicit field list, parse it straight from the accumulated
        # instruction WITHOUT the LLM, so the plan can GUARANTEE none of their named
        # fields is silently dropped or renamed by the non-deterministic spec
        # resolver (the RCA: 18 named fields collapsed to a generic 9).
        #
        # ONTA-382 — EXHAUSTIVE vs ILLUSTRATIVE. A non-empty explicit user list is
        # a CLOSED set: it is both the FLOOR (ONTA-239) and the CEILING (allowlist
        # extraction). The LLM's ``confirmed_attributes`` may NOT extend it. An
        # empty explicit list keeps the open/illustrative default: the LLM set may
        # extend the floor, and soft extraction may keep extra attributes.
        user_floor = _snap_to_declared(
            _explicit_user_fields(instruction), declared_attrs
        )
        llm_confirmed = _snap_to_declared(
            _as_list(spec.get("confirmed_attributes")), declared_attrs
        )
        # Exhaustive signal: user enumerated a closed field list (chip "Use these:"
        # or natural "with fields a, b, c"). Threaded request → plan params → A1
        # extract handoff → ExtractionConstraint.attributes_exhaustive.
        attributes_exhaustive = bool(user_floor)
        if attributes_exhaustive:
            # CEILING = FLOOR: only the user's named fields (+ key). LLM may not
            # extend the committed attribute set.
            confirmed = _dedupe([key_attr, *user_floor])
        else:
            # ILLUSTRATIVE / open: floor-first so the user's own names + order win
            # over the LLM's rephrasing; the LLM set contributes ADDITIONAL fields.
            confirmed = _dedupe([key_attr, *user_floor, *llm_confirmed])
        suggested = _dedupe([key_attr, *spec.get("suggested_attributes", [])])

        # ONTA-244 (schema fidelity) — NEVER downgrade a user-named type to the
        # generic WebRecord. The spec LLM's degrade default (and an under-classified
        # reply) is ``WebRecord``; when the user actually named a type in the
        # message we must commit to THAT, not the placeholder. Deterministic +
        # domain-agnostic: parse the type straight from the accumulated instruction
        # (no LLM), so even a flaky/absent spec keeps the caller's type. Only
        # OVERRIDES the placeholder — a real LLM-resolved type is left untouched.
        if type_name == "WebRecord":
            explicit_type = _explicit_user_type(instruction)
            if explicit_type:
                type_name = explicit_type

        # ONTA-244 (already-scoped — skip the picker). The attribute-confirmation
        # clarify exists ONLY for the genuinely under-specified "just find <X>" ask.
        # The turn is ALREADY scoped — and must commit without re-asking — when
        # EITHER the user handed over an explicit field list (``user_floor``/LLM
        # ``confirmed`` gave us >1) OR the target type already exists in the
        # ontology with declared attributes (``declared_attrs``: the schema is known,
        # so there is nothing to confirm). This is the shared "already scoped, commit"
        # signal that stops the two clarify gates from thrashing a fully-specified
        # request. ``already_asked`` (the prior-clarify guard) still commits after
        # one round for the under-specified path.
        already_asked = int(ctx.extras.get("prior_clarify_count", 0)) >= 1
        already_scoped = len(confirmed) > 1 or bool(declared_attrs)
        if not already_scoped and not already_asked:
            # Only the key is "confirmed" (i.e. the user just named the entity and
            # gave no explicit field list, and the type is new to the ontology). Ask
            # which attributes to collect — clickable options carry a SHORT
            # recommended set (the most-important few), pre-selected, so the next
            # turn converges without confronting the user with every column.
            core = _core_attrs(key_attr, spec.get("core_attributes", []), suggested)
            return [_clarify_step(type_name, key_attr, core, note=degraded_note)]

        # Already scoped by an existing ontology type but the user named no explicit
        # fields this turn: adopt the type's declared attributes as the floor so the
        # plan collects the schema that already exists instead of falling to a bare
        # [name] set (or re-asking). The LLM confirmed/suggested sets still extend it.
        if declared_attrs and len(confirmed) <= 1:
            confirmed = _dedupe([key_attr, *declared_attrs, *llm_confirmed])

        # Commit: use the confirmed set, or fall back to the suggested set if we
        # already asked once (don't loop). These drive entity naming + the
        # preview card — NOT the fetch breadth.
        attributes = confirmed if len(confirmed) > 1 else suggested

        # FLOOR GUARANTEE (ONTA-239): every field the user explicitly named MUST
        # survive into the plan's ``attributes``. The primary guarantee is already
        # provided by the ``confirmed`` construction above (a non-empty ``user_floor``
        # forces ``len(confirmed) > 1`` → ``attributes = confirmed`` ⊇ floor). This
        # is a belt-and-suspenders reinstatement guarding the ``attributes =
        # suggested`` fallback branch, so a future refactor of that selection can
        # never silently drop a user field; the log makes any such regression
        # visible instead of silent.
        missing_floor = [f for f in user_floor if f not in attributes]
        if missing_floor:
            attributes = _dedupe([*attributes, *missing_floor])
            logger.info(
                "web_ingest_user_floor_reinstated",
                fields=missing_floor,
                type=type_name,
            )

        # Decouple the PROVIDER FETCH from the user's minimal named attributes
        # (Cause 1): every provider PROJECTS rows to hint_columns, so passing the
        # confirmed minimal set (e.g. [name, score]) drops the rest of the table
        # (provider, rating, latency, price, votes) before extraction can model
        # the domain. Build a COMPREHENSIVE hint = key ∪ confirmed ∪ suggested
        # (the suggested set is the LLM's richer guess at web-discoverable
        # columns), so the provider returns a rich table the extractor can
        # normalize into Model/Organization/Score/etc. The confirmed set still
        # drives naming + preview above. ONTA-382: even under an exhaustive
        # attribute CEILING the fetch stays comprehensive — the ceiling is
        # enforced at extraction (allowlist), not by starving the provider.
        hint_columns = _dedupe([key_attr, *confirmed, *suggested])

        # Enumeration partition (fan-out, ONTA-192 + ONTA-379): for a population
        # inventory ask the scope is split into self-contained sub-queries;
        # execute() runs one discovery per sub-query and merges (deduped) into
        # ONE job. The LLM may already partition multi-city/category asks
        # (ONTA-192); ONTA-379 adds a DETERMINISTIC backstop so a single-scope
        # inventory ("universities in British Columbia") still fans out into
        # authoritative-list angles instead of collapsing to 1 thin page.
        # Empty → classic single-query discovery. Priced below as n sub-runs.
        # NEVER in URL mode: the pages are fixed, so partitioned queries would
        # just re-scrape (and re-bill) the same URLs for fully-deduped batches.
        subqueries = (
            []
            if urls
            else _ensure_enumeration_partition(
                query=query,
                instruction=instruction,
                llm_subqueries=_norm_subqueries(spec.get("subqueries")),
            )
        )

        # ONTA-194 phase 2: consult the API source registry. If a registered
        # authoritative API covers the ask, run it BEFORE web search (source-of-
        # truth = registry Tier -1) — alone (api_only) or alongside web
        # (api_plus_web). Runs on every query-mode discovery; the router
        # self-degrades to web_only (no key / no match) so a non-covered ask is
        # unchanged. The picks persist on the step so execute() rebuilds the same
        # registry providers without a second LLM call.
        async with timed(logger, "registry_route"):
            registry_decision = await _registry_route(ctx, query, spec, urls)
        registry_sources = (
            build_registry_sources(
                get_api_source_catalog(ctx.tenant_id), registry_decision,
                tenant_id=ctx.tenant_id,
            )
            if registry_decision.uses_api
            else []
        )
        registry_card = _registry_card(registry_sources)
        registry_params = (
            {
                "registry_picks": [pk.to_dict() for pk in registry_decision.picks],
                "registry_mode": registry_decision.mode,
            }
            if registry_sources
            else {}
        )
        if registry_sources:
            ensemble = _merge_registry_ensemble(
                ensemble, registry_sources, registry_decision.mode
            )
            provider = ensemble[0]

        # ONTA-379: for an enumeration fan-out, also consult nested fallback
        # providers (e.g. source_first's Tier-1 web-search fallback). A thin
        # Tier-0 hit alone under-collects; the ensemble's cross-batch key
        # dedupe makes the overlap free. No-op when no nested fallback exists.
        if subqueries:
            ensemble = _expand_enumeration_ensemble(ensemble)
            provider = ensemble[0]

        # 2a. LEAN fast path — cheap providers skip the plan-time preview.
        #     At or under the auto-confirm gate the client starts the job straight
        #     from the attribute confirm, so the rich preview (paid sample fetch +
        #     extraction LLM call, 20-35s of "Thinking…") would build a card that
        #     is never rendered — and double-fetch the same source the job reads
        #     seconds later. Return a lean, immediately-confirmable step instead;
        #     "found nothing" / "source unreachable" surface honestly on the JOB
        #     card (execute()'s _run finishes 0-record or failed). Providers above
        #     the gate keep the full sample+shape preview below: there a human is
        #     about to approve real spend, and the estimate earns its cost.
        #     Gate on the WHOLE-RUN estimate (cost_per_call × paginated requests,
        #     same figure the client's auto-confirm reads) — not the raw per-call
        #     price, which under-counts paginating providers.
        cap = _DEFAULT_PLAN_CAP
        lean_cost = _estimate_cost_multi(
            ensemble, cap, cap, subqueries=len(subqueries)
        )
        if lean_cost["estimated_usd"] <= _PREVIEW_GATE_USD:
            # SERVER-owned auto-confirm contract: this plan was built lean
            # BECAUSE it is at/under the gate — say so explicitly, so clients
            # obey the server's judgment instead of re-deriving it from a
            # hardcoded twin constant (interface-drift risk: a client whose
            # threshold skews from COGRAPH_WEB_PREVIEW_GATE_USD would either
            # show a preview-less spend card or auto-run an ungated plan).
            lean_cost["auto_confirm"] = True
            return [
                PlanStep(
                    capability=self.name,
                    action="discover_ingest",
                    params={
                        "query": query,
                        "subqueries": subqueries,
                        "proposed_type": type_name,
                        "attributes": attributes,
                        # ONTA-382: exhaustive (closed) vs illustrative (open) attr set.
                        "attributes_exhaustive": attributes_exhaustive,
                        "hint_columns": hint_columns,
                        "max_rows": cap,
                        "kg_name": ctx.kg_name,
                        # Primary provider (legacy key) + the full ensemble the
                        # execute-time fan-out consults, specialized first.
                        "provider": provider.name,
                        "providers": [pr.name for pr in ensemble],
                        "urls": urls,
                        **registry_params,
                    },
                    rationale=(
                        degraded_prefix
                        + (f"{registry_card}. " if registry_card else "")
                        + f"Find {query} on the web and add them to this graph as "
                        f"{type_name} records."
                    ),
                    confidence=0.7,
                    preview={
                        "summary": (
                            degraded_prefix
                            + (f"{registry_card}. " if registry_card else "")
                            + f"Search the web for {query} and add the results as "
                            f"{type_name} records (up to {cap})."
                        ),
                    },
                    cost=lean_cost,
                )
            ]

        # 2. Cheap SAMPLE fetched with the COMPREHENSIVE hint so the preview sees
        #    the same rich table the commit will. In URL mode the provider extracts
        #    the sample FROM the supplied pages. Bounded by _SAMPLE_BUDGET_S: a
        #    broad, source-less query can fan out for 60s+ and blow the proxy's 55s
        #    request budget → the client's "took too long". On a TIMEOUT we don't
        #    strand the user — we press on to a degraded-but-confirmable plan below
        #    (the full discovery still runs on confirm as a background job). Only an
        #    outright provider ERROR is a dead end worth surfacing.
        sample = None
        try:
            sample = await asyncio.wait_for(
                provider.discover(
                    query,
                    sample=True,
                    max_rows=_SAMPLE_ROWS,
                    hint_columns=hint_columns,
                    context=_provider_context(ctx),
                    urls=urls or None,
                ),
                timeout=_SAMPLE_BUDGET_S,
            )
        except asyncio.TimeoutError:
            # Slow web source, not a failure — degrade to a flat, confirmable plan.
            logger.warning(
                "web_ingest_sample_timeout", query=query, budget_s=_SAMPLE_BUDGET_S
            )
        except Exception:  # noqa: BLE001 — a sample ERROR must never 500 the turn
            logger.warning("web_ingest_sample_failed", exc_info=True)
            return [
                _answer_step(
                    "I couldn't reach the web source to preview that just now. "
                    "Try again in a moment or rephrase the request."
                )
            ]
        # An empty (but successful) sample means the search genuinely found nothing
        # — surface the informative message. A TIMEOUT (sample is None) is
        # different: the discovery is viable, we just couldn't render its preview in
        # time, so we proceed to a degraded-but-confirmable plan.
        if sample is not None and not sample.rows:
            return [_answer_step(_empty_sample_message(query, urls, sample))]

        preview_degraded = sample is None
        sample_rows = list(getattr(sample, "rows", None) or [])
        sample_sources = list(getattr(sample, "sources", None) or [])

        # Thread the per-record source URL onto the sampled rows so the PREVIEW
        # matches the COMMIT (the same invariant the URL persistence keeps): the
        # discovered-types card + sample rows show the `source_url` citation column
        # the ingest will mint. No-op when the provider supplied no provenance.
        if sample_rows:
            _attach_source_urls(
                sample_rows, getattr(sample, "provenance", None) or {}
            )

        # 3. Estimate the DISCOVERED ontology shape from the sample — run the same
        #    multi-type + relationship extractor the commit will, so the plan card
        #    shows the LIKELY types/edges the ingest will mint (not a flat mapping).
        #    It's an estimate from the small sample, not a guarantee: the full
        #    commit may surface more types/edges or differ in detail. Bounded by
        #    _SHAPE_BUDGET_S — the extraction LLM's own timeout (60s) is longer than
        #    the whole request budget — and degraded to a flat preview on timeout /
        #    error / no sample so the plan stays confirmable.
        est_total = (
            (getattr(sample, "estimated_total", 0) or len(sample_rows))
            if sample is not None
            else 0
        )
        # cap was set before the lean fast path above (_DEFAULT_PLAN_CAP).
        cost = _estimate_cost_multi(
            ensemble, est_total, cap, subqueries=len(subqueries)
        )
        shape = None
        if sample_rows:
            resolver = _build_resolver(ctx)

            async def _estimate_shape():
                existing_types, _existing_attrs = await resolver._fetch_ontology(
                    tenant_graph_uri(ctx.tenant_id)
                )
                return await _preview_shape(
                    resolver, sample_rows, set(existing_types.keys())
                )

            try:
                shape = await asyncio.wait_for(
                    _estimate_shape(), timeout=_SHAPE_BUDGET_S
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "web_ingest_preview_timeout",
                    query=query,
                    budget_s=_SHAPE_BUDGET_S,
                )
            except Exception:  # noqa: BLE001 — preview must NEVER 500 the turn
                logger.warning("web_ingest_preview_failed", exc_info=True)
        if shape is None:
            # No usable sample, or the shape estimate timed out / failed → a flat
            # single-type preview keeps the plan card confirmable.
            preview_degraded = True
            shape = _flat_shape(type_name, attributes, set())
        discovered_types = shape["discovered_types"]
        relationships = shape["relationships"]

        step = PlanStep(
            capability=self.name,
            action="discover_ingest",
            params={
                "query": query,
                "subqueries": subqueries,
                "proposed_type": type_name,
                "attributes": attributes,
                # ONTA-382: exhaustive (closed) vs illustrative (open) attr set.
                "attributes_exhaustive": attributes_exhaustive,
                # Full ensemble for the execute-time fan-out (primary kept in
                # "provider" for older persisted steps).
                "providers": [pr.name for pr in ensemble],
                # The COMPREHENSIVE fetch hint (key ∪ confirmed ∪ suggested) —
                # persisted so the full fetch in execute() uses the SAME rich
                # projection the sample did. The FETCH is the part that's stable
                # preview→commit; the discovered TYPES/edges are only an estimate
                # from the sample.
                "hint_columns": hint_columns,
                "max_rows": cap,
                "kg_name": ctx.kg_name,
                "provider": provider.name,
                # Persist the explicit URLs so execute() re-passes them (the same
                # pages are fetched at commit). Empty in plain query-discovery mode.
                "urls": urls,
                **registry_params,
            },
            rationale=(
                degraded_prefix
                + (f"{registry_card}. " if registry_card else "")
                + f"Find {query} on the web and add them to this graph as "
                f"{type_name} records."
            ),
            confidence=0.7,
            preview={
                "summary": (
                    degraded_prefix
                    + (f"{registry_card}. " if registry_card else "")
                    + _preview_summary(
                        discovered_types, relationships, cap, degraded=preview_degraded
                    )
                ),
                "discovered_types": discovered_types,
                "relationships": relationships,
                "sample_rows": sample_rows[:_PREVIEW_SAMPLE],
                "sources": sample_sources[:_PREVIEW_SOURCES],
                "estimated_total": est_total,
                "cost_estimate": cost.get("note", ""),
            },
            cost=cost,
        )
        return [step]

    async def execute(self, ctx: AgentContext, step: PlanStep) -> dict:
        p = step.params
        # URLs persisted at plan time (empty for plain query discovery). Provider
        # selection mirrors plan(): the persisted ENSEMBLE (specialized first,
        # then general — both are consulted because neither is complete alone),
        # falling back to the legacy single "provider" name, then to the
        # mode-appropriate default (for_urls=bool(urls)) for steps persisted
        # before either key existed. Names that no longer resolve are skipped.
        urls = list(p.get("urls") or [])
        web_ensemble = [
            prov
            for prov in (
                get_web_source(n)
                for n in (p.get("providers") or [])
                if isinstance(n, str) and n
            )
            if prov is not None
        ]
        if not web_ensemble:
            single = get_web_source(p.get("provider")) or get_web_source(
                for_urls=bool(urls)
            )
            web_ensemble = [single] if single is not None else []
        # ONTA-379: nested fallbacks (e.g. source_first's Tier-1 web provider)
        # are NOT separately registered — plan() lists them by name for the cost
        # card, but get_web_source can't resolve them. Re-unwrap from the resolved
        # primary so execute still consults the full enumeration ensemble.
        if p.get("subqueries"):
            web_ensemble = _expand_enumeration_ensemble(web_ensemble)
        # ONTA-194 phase 2: rebuild the registry providers from the picks the plan
        # persisted (no second LLM call) and splice them ahead of web, honoring the
        # persisted mode. A registry-only run (api_only, or a registry-only
        # deployment) proceeds even when no web provider is available.
        registry_sources, registry_mode = await _rebuild_registry_sources(
            p, ctx.tenant_id
        )
        ensemble = (
            _merge_registry_ensemble(web_ensemble, registry_sources, registry_mode)
            if registry_sources
            else web_ensemble
        )
        if not ensemble:
            raise RuntimeError("web-source provider not available at execute time")
        provider = ensemble[0]  # primary — naming + default error attribution

        query = p["query"]
        # Enumeration fan-out (ONTA-192): the plan may carry self-contained
        # sub-queries partitioning an "all X in Y and Z" ask. One discovery runs
        # per sub-query, all merged (deduped on the key attribute) into THIS one
        # job. Absent/empty → the single primary query, the classic path.
        subqueries = (
            [query]
            if urls
            else [
                q
                for q in (p.get("subqueries") or [])
                if isinstance(q, str) and q.strip()
            ]
            or [query]
        )
        attributes = p.get("attributes") or []
        # ONTA-382: exhaustive attribute set (closed allowlist). Older persisted
        # steps predate this key → treat as illustrative (open), matching the
        # pre-382 soft-extract default.
        attributes_exhaustive = bool(p.get("attributes_exhaustive"))
        # COMPREHENSIVE fetch hint persisted at plan time so the full pull uses the
        # SAME rich projection the sample did — the column projection is the stable
        # part of the preview (the discovered shape was only an estimate). Older
        # persisted steps predate this key — fall back to the named attributes so
        # they still run (graceful degradation).
        hint_columns = p.get("hint_columns") or attributes
        proposed_type = p.get("proposed_type") or "WebRecord"
        cap = int(p.get("max_rows") or _DEFAULT_PLAN_CAP)
        kg_name = p.get("kg_name") or ctx.kg_name
        instance_graph = kg_graph_uri(ctx.tenant_id, kg_name) if kg_name else None
        # ONTA-268: one ontology-write lock PER JOB, shared by every per-sub-query
        # resolver built below. A fresh resolver is constructed inside the
        # sub-query loop so no two sub-queries share the resolver's per-ingest
        # state (`_instance_graph` / `_parent_of` / the TypeMatcher graph URI) —
        # the reentrancy hazard — while the shared lock serializes their ontology
        # mutations so concurrent (or future-parallelized) sub-queries can't race
        # type creation and fragment the ontology.
        ontology_lock = asyncio.Lock()
        pctx = _provider_context(ctx)

        # Track the discovery as a real job so the client polls a LIVE status
        # (queued → running → applied/failed) with a result count, the platforms
        # consulted, and the run cost — instead of a synchronous "done" the
        # instant the background task is spawned. The job store is the same
        # unified store enrichment/dedupe use (injected on ctx.extras by the
        # agent route); when it's absent (a bare/test context) we degrade to the
        # previous fire-and-forget behavior so nothing breaks.
        job_store = ctx.extras.get("enrichment_job_store")
        cost_usd, cost_note = _step_cost(step)
        job: Optional[EnrichJob] = None
        if job_store is not None:
            job_id = str(uuid.uuid4())
            job = EnrichJob(
                id=job_id,
                tenant_id=ctx.tenant_id,
                kg_name=kg_name or "",
                type_name=proposed_type,
                attributes=attributes,
                tier=EnrichmentTier.lite,
                status=JobStatus.queued,
                created_at=datetime.now(timezone.utc),
                conflict_policy=ConflictPolicy.stage,
                category=JobCategory.discovery,
                cost=cost_usd,
                cost_note=cost_note,
                # A9 Run Manifest (ONTA-273): the run as a first-class object. The
                # discovery run records per-batch coverage into it and settles it to
                # a terminal state (completed / failed-with-reason) at every exit,
                # so a run halted by provider exhaustion caveats "N of M items
                # completed before halt" instead of a silent partial. run_id = the
                # job id (the EnrichJob IS the run — no separate id to mint).
                manifest=RunManifest(run_id=job_id, stage="discovery"),
                # Chat provenance: link the job to the conversation that spawned it.
                thread_id=getattr(ctx, "session_id", None),
                # Per-run HARD spend ceiling (ONTA-378): a per-turn ceiling
                # threaded from the /agent request. The resolve_spend_ceiling(...)
                # call just below reads job.spend_ceiling_usd as the explicit
                # override, so it WINS over the deployment default and bounds THIS
                # discovery run. None → deployment default (unchanged behavior).
                spend_ceiling_usd=getattr(ctx, "spend_ceiling_usd", None),
            )
            # A9 cost envelope (ONTA-282): stamp the HARD per-run spend ceiling on
            # the manifest. A per-job override wins; else the deployment default
            # (config). None/0 ⇒ unlimited (unchanged behavior). The per-batch spend
            # feed + ceiling check in _run_inner then halt the run cleanly if it
            # crosses this envelope.
            if job.manifest is not None:
                job.manifest.spend_ceiling_usd = resolve_spend_ceiling(
                    getattr(job, "spend_ceiling_usd", None),
                    settings.enrich_spend_ceiling_usd,
                )
            # Operator Job Trace (P0–P9): open the run + stamp P1 Find input.
            # try/except: observability must never block job creation.
            try:
                rec = attach_recorder(job)
                if rec is not None:
                    rec.begin(
                        StageProjectId.p0,
                        input={
                            "job_id": job.id,
                            "category": "discovery",
                            "spend_ceiling_usd": job.spend_ceiling_usd,
                        },
                    )
                    rec.action(
                        StageProjectId.p0, "create_job", detail="discovery job queued"
                    )
                    rec.begin(
                        StageProjectId.p1,
                        input={
                            "goal": (
                                query or getattr(step, "instruction", None) or ""
                            )[:500],
                            "type_name": proposed_type,
                            "attributes": attributes,
                            "kg_name": kg_name,
                            "cap": cap,
                            "subqueries": subqueries[:20],
                            "providers": [
                                getattr(pr, "name", str(pr)) for pr in ensemble[:10]
                            ],
                            # Notion contract: P1 consumes user goal (+ A8 when refresh).
                            "contract_consumes": "user goal · A8 Refresh Delta",
                            "contract_emits": "A1 Source Bundle",
                        },
                    )
                    rec.action(
                        StageProjectId.p1, "plan", detail="spec resolved; ready to search"
                    )
            except Exception:
                logger.warning(
                    "stage_trace_open_failed",
                    job_id=getattr(job, "id", None),
                    exc_info=True,
                )
            # open_job_stage_trace handles P0 begin (ONTA-388 cross-category helper).
            rec = open_job_stage_trace(
                job,
                input={
                    "job_id": job.id,
                    "category": "discovery",
                    "spend_ceiling_usd": job.spend_ceiling_usd,
                },
                action_detail="discovery job queued",
            )
            if rec is not None:
                rec.begin(
                    StageProjectId.p1,
                    input={
                        "goal": (query or getattr(step, "instruction", None) or "")[:500],
                        "type_name": proposed_type,
                        "attributes": attributes,
                        "kg_name": kg_name,
                        "cap": cap,
                        "subqueries": subqueries[:20],
                        "providers": [
                            getattr(pr, "name", str(pr)) for pr in ensemble[:10]
                        ],
                    },
                )
                rec.action(StageProjectId.p1, "plan", detail="spec resolved; ready to search")
            await job_store.create(job)

        # Thread the tracked job id into the provider context so a URL-targeted
        # provider that resumes asynchronously (e.g. a webhook-driven adapter) can
        # correlate its callback back to THIS job. Generic + optional: providers
        # that don't need it ignore the key, and it's absent when discovery runs
        # without a job store (bare/test context), so nothing depends on it.
        if job is not None:
            pctx = {**pctx, "job_id": job.id}

        # A1 Source Bundle run identity (ONTA-346): ONE run_id for the whole
        # discovery run — the tracked job id when present (the EnrichJob IS the
        # run; its manifest already keys off job.id), else a fresh uuid for a
        # bare/test context. workspace_id = the tenant (ADR 0011: pipeline code
        # says workspace_id, infra keeps tenant_id — no blanket rename).
        run_id = job.id if job is not None else str(uuid.uuid4())
        # ONTA-372 (keystone): mint ONE run-scoped ArtifactEnvelope at the P1 entry
        # and thread ITS run_id through the WHOLE discovery pipeline — the A1
        # Source Bundle (below) AND both resolver ingest paths, which key the A6
        # Graph Delta off it. Before this, the resolver minted its own unrelated
        # uuid4, so the A1 bundle's lineage and the A6 Graph Delta's lineage
        # DIVERGED and the A6 delta was effectively dead on the discovery path.
        # workspace_id = the tenant (ADR 0011). fact_id is the run's A1 root.
        run_envelope = ArtifactEnvelope(
            workspace_id=ctx.tenant_id,
            run_id=run_id,
            fact_id=derive_fact_id(run_id=run_id, stage="A1"),
        )

        async def _run_inner() -> None:
            if job is not None and job_store is not None:
                job.status = JobStatus.running
                job.started_at = datetime.now(timezone.utc)
                # EARLY total estimate (ONTA-238): a discovery run's total/processed
                # otherwise stay 0/0 until the FIRST (sub-query × provider) batch
                # completes — minutes into a working job — so a poller sees a flat
                # 0/0 and concludes the job is stalled (the persona polled 81× and
                # gave up on jobs that were in fact progressing). Seed ``total`` with
                # the plan cap up front — the honest UPPER bound on rows this run
                # will land — so the very first poll reads ~N/0, not 0/0. The rolling
                # per-sub-query estimate below refines it downward and the terminal
                # settle pins it to the exact count. Also stamp the first phase so a
                # poll during the (slow) initial search reads "searching", not a bare
                # status=running.
                job.progress.total = cap
                job.progress.phase = "searching"
                # A9 manifest: enter `running`, seeding the planned item total (M)
                # with the plan cap — the honest upper bound this run intends to
                # fill. `coverage()` then reads "N of M" from the first poll, and a
                # halt rolls the unfilled remainder (M − N) into `dropped`.
                if job.manifest is not None:
                    job.manifest.start(total=cap)
                try:
                    rec = attach_recorder(job)
                    if rec is not None:
                        rec.action(
                            StageProjectId.p0,
                            "start_run",
                            detail=f"status=running total_cap={cap}",
                        )
                        rec.action(
                            StageProjectId.p1,
                            "searching",
                            detail="phase=searching",
                        )
                except Exception:
                    logger.warning(
                        "stage_trace_start_failed",
                        job_id=getattr(job, "id", None),
                        exc_info=True,
                    )
                await job_store.update(job)
            # Observability for the WRITE TARGET (ONTA-198): record the exact graph
            # this run writes into. Without it a run that reports "N filled" but whose
            # rows never appear in the Explorer is undiagnosable — you cannot tell
            # WHICH graph the resolver wrote to (the kg_name the plan/agent resolved
            # is invisible in the logs, so a write to the wrong / an unregistered KG
            # looks identical to a write that landed). Emitted once per run, up front.
            logger.info(
                "web_ingest_run_start",
                tenant=ctx.tenant_id,
                kg_name=kg_name or None,
                instance_graph=instance_graph,
                providers=[pr.name for pr in ensemble],
                subqueries=len(subqueries),
                cap=cap,
                proposed_type=proposed_type,
                job_id=job.id if job is not None else None,
            )
            # A discovery run with no resolvable target KG (empty kg_name) falls
            # through to instance_graph=None, and resolver.ingest then writes the
            # INSTANCE data into the tenant BASE graph (schema_resolver: instance_graph
            # or tenant_graph_uri) — where the Explorer's per-KG views never read it,
            # so the rows are silently invisible. Flag it loudly; the run still
            # proceeds (behavior unchanged) so nothing that relied on it breaks, but
            # the warning makes the misroute obvious instead of a silent black hole.
            if instance_graph is None:
                logger.warning(
                    "web_ingest_no_target_kg",
                    tenant=ctx.tenant_id,
                    detail=(
                        "kg_name is empty; instance data will land in the tenant "
                        "base graph and will NOT be visible in any per-KG Explorer "
                        "view. The run likely lost its KG context upstream."
                    ),
                    job_id=job.id if job is not None else None,
                )
            # Per-provider activity log for "which providers we used" + their
            # outcomes, surfaced in the run-detail view alongside the platforms
            # list — one entry PER ENSEMBLE MEMBER, each accumulating its own
            # attempts/matches/errors across every sub-query.
            plogs: dict[str, ProviderLog] = {
                prov.name: ProviderLog(provider=prov.name) for prov in ensemble
            }
            any_discover_ok = False
            processed = 0  # unique rows ingested across all sub-queries/providers
            entities_total = 0
            affected_types: set[str] = set()
            platforms: list[str] = []
            # Live stage_trace contract accumulators (ONTA-385): A1/A2/A3/A6
            # summaries folded across every SourceBundle + ingest micro-batch so
            # the terminal P1/P2/P3/P6 outputs are Notion-contract-shaped.
            a1_acc: Optional[dict] = None
            # A1 validators (ONTA-393): run-level tallies of nav-chrome rows dropped
            # and type-invalid cells scrubbed at the A1 boundary, surfaced on the
            # terminal A1 contract so the Job Trace stays honest about them.
            a1_rows_dropped = 0
            a1_cells_scrubbed = 0
            a1_drop_reasons: list[str] = []
            a2_extracted = 0
            a2_resolved = 0
            a2_source_rows = 0
            a2_batches = 0
            a2_structured_batches = 0
            a3_counts: Optional[dict] = None
            a3_drop_reasons: list[str] = []
            a3_transforms_sample: list[dict] = []
            a4_verified_count = 0
            a6_fact_count = 0
            a6_fan_in_count = 0
            a6_triples = 0
            a6_facts_sample: list = []
            a6_run_id: Optional[str] = run_envelope.run_id
            a6_instance_graph: Optional[str] = instance_graph
            # Cross-batch dedupe on the KEY attribute (the record identifier):
            # sub-query partitions overlap ("…in Tustin" and a directory row
            # listed under both cities) and so do ENSEMBLE members (the same
            # physician on Places AND a directory page) — re-ingesting the same
            # key would double-write. Specialized runs first, so its (more
            # structured) row wins; the general provider only contributes NEW keys.
            seen_keys: set[str] = set()
            key_attr = (attributes[0] if attributes else "name") or "name"
            # ONTA-345: entity-level RE-ACQUISITION guard. Consult the STICKY
            # suppression / tombstone list ONCE per run (batched — one query, then
            # an O(1) set-membership check per row below), so an ERASED entity is
            # never silently re-minted by discovery/refresh (the P1 'never
            # re-acquire erased data' rule; GDPR erasure blast radius). A row whose
            # would-be canonical subject is on this list is DROPPED post-dedupe,
            # BEFORE the SourceBundle is built and BEFORE resolver.ingest*, so a
            # suppressed entity never enters the bundle and never reaches the
            # writer. Best-effort + empty when there is no target KG — a suppression
            # read must never fail the run.
            suppressed_entities = await fetch_suppressed_entities(
                ctx.neptune, instance_graph
            )
            last_provider_err: Optional[str] = None
            last_err_provider: Optional[str] = None
            errors_total = 0
            # Set when a FATAL billing/auth error (402/401) aborts the run mid-way
            # (ONTA-201). Carries the clear, user-facing message out of the nested
            # sub-query/provider loops so we can fail the WHOLE job honestly —
            # rows-landed vs rows-lost — instead of swallowing it as one failed
            # batch and reporting "complete".
            fatal_llm_err: Optional[LLMError] = None
            # A9 cost envelope (ONTA-282): set when the run crosses its HARD per-run
            # spend ceiling mid-flight. A GOVERNANCE halt (not provider exhaustion),
            # but routed through the SAME abort-and-settle path as a 402
            # (_fail_billing_job → halt_from_exception) so the terminal state carries
            # an honest partial (rows-landed vs rows-dropped) instead of a silent
            # overspend. A parallel flag (not fatal_llm_err) keeps the proven
            # billing path untouched.
            fatal_ceiling_err: Optional[CostCeilingExceeded] = None
            # 429 policy (ONTA-273): a single rate-limit blip is a transient the
            # per-batch degrade retries; only SUSTAINED 429s (a run throttled to a
            # standstill) escalate to a run-level halt. This run-scoped escalator
            # draws that line — a non-429 outcome resets the streak, and only once
            # it crosses the threshold does it return a fatal LLMRateLimitError we
            # route through the SAME billing-halt machinery below.
            rate_escalator = RateLimitEscalator()
            # Each (sub-query, provider) call is bounded to the per-sub-query row
            # share the plan PRICED (cost = n_sub × pages(cap / n_sub)). Passing
            # the whole remaining cap instead let overlapping sub-queries spend up
            # to n_sub× the quoted estimate — the figure the ≤gate auto-confirm
            # trusted (adversarial-review F2).
            per_sub_budget = math.ceil(cap / max(1, len(subqueries)))
            try:
                for sub_i, sub_query in enumerate(subqueries):
                    if cap - processed <= 0:
                        break
                    # ONTA-268: a fresh resolver PER sub-query (cheap — keeps no
                    # cross-request state), all sharing the job's one ontology-write
                    # lock. Per-sub-query resolvers eliminate the shared per-ingest
                    # state that made a single reused resolver non-reentrant; the
                    # shared lock keeps their ontology mutations serialized.
                    resolver = _build_resolver(ctx, ontology_lock=ontology_lock)
                    for prov in ensemble:
                        remaining = cap - processed
                        if remaining <= 0:
                            break
                        plog = plogs[prov.name]
                        # The WHOLE batch (discover → dedupe → ingest) is guarded:
                        # one provider returning garbage, or one batch failing to
                        # ingest, must not sink batches already landed — partial
                        # coverage beats nothing (adversarial-review F3).
                        plog.attempts += 1
                        phase = "discover"
                        # User-facing progress phase (ONTA-238): each provider
                        # iteration starts by SEARCHING the web (the phase flips to
                        # "ingesting" once rows are found, above). Re-set per batch
                        # so after an ingest the next batch's search reads honestly.
                        if job is not None and job_store is not None:
                            job.progress.phase = "searching"
                            await job_store.update(job)
                        try:
                            full = await prov.discover(
                                sub_query,
                                sample=False,
                                max_rows=min(per_sub_budget, remaining),
                                hint_columns=hint_columns,
                                context=pctx,
                                urls=urls or None,
                            )
                            any_discover_ok = True
                            # A9 cost envelope (ONTA-282): a paid provider request
                            # was actually issued — feed its cost into the manifest's
                            # spend-to-date (once per discover call, matching how
                            # _estimate_cost prices a request). The ceiling is then
                            # checked as rows land in the micro-batch loop below. A
                            # free provider adds $0 (provider_cost → 0.0).
                            if job is not None and job.manifest is not None:
                                _paid, _cost_per_call = provider_cost(prov)
                                if _cost_per_call > 0.0:
                                    job.manifest.add_spend(_cost_per_call)
                            phase = "ingest"
                            rows_found = list(getattr(full, "rows", None) or [])[
                                : min(per_sub_budget, remaining)
                            ]
                            # matches = rows the provider FOUND (pre-dedupe): a
                            # provider whose 50 finds were all already contributed
                            # by an earlier member still shows matches=50, not the
                            # "ran but found nothing" no_match the model reserves
                            # for genuinely empty results (adversarial-review F4).
                            plog.matches += len(rows_found)
                            # Request-level trace (API-source providers only):
                            # record every HTTP request this discover() issued so
                            # the run-detail view can show the requests + their
                            # payloads/statuses/record-counts. A request that
                            # returned zero rows is still worth showing, so this
                            # runs BEFORE the no-match continue below.
                            _record_requests(plog, getattr(full, "calls", None))
                            # ONTA-391: surface the provider's locate→select→fetch
                            # step counts as P1 stage-trace actions — BEFORE the
                            # no-match continue, so even a page-minimising run that
                            # located nothing (or pages with no rows) shows its
                            # locate/fetch work + skip reason. A provider that doesn't
                            # locate+scrape leaves locate_trace None → no-op.
                            _record_locate_trace(
                                job,
                                getattr(full, "locate_trace", None),
                                prov.name,
                                sub_query,
                            )
                            if not rows_found:
                                # Distinguish hard locate/API failure from a clean
                                # empty (dogfood 802b2672): when the provider stamps
                                # locate_errors (HTTP 4xx/5xx / transport on Parallel
                                # or Gemini), count as error + last_error so Job Trace
                                # / provider_logs show status=error, not silent
                                # no_match. Soft empty (searched OK, no list page)
                                # stays no_match.
                                lt = getattr(full, "locate_trace", None) or {}
                                locate_errs = [
                                    str(e)
                                    for e in (lt.get("locate_errors") or [])
                                    if e
                                ]
                                hard_err = None
                                if locate_errs:
                                    hard_err = "; ".join(locate_errs)[:300]
                                elif getattr(full, "error", None) and str(
                                    full.error
                                ).startswith("locate APIs failed"):
                                    hard_err = str(full.error)[:300]
                                if hard_err:
                                    plog.errors += 1
                                    plog.last_error = hard_err
                                    last_provider_err = hard_err
                                    last_err_provider = prov.name
                                    errors_total += 1
                                else:
                                    plog.no_match += 1
                                continue
                            # Per-record source-URL provenance (ONTA-151): stamp
                            # each row with the page it was drawn from BEFORE
                            # serialization, so it rides through the SAME extract →
                            # ingest → insert_facts path as the rest of the row's
                            # data and lands as a `source_url` citation.
                            #
                            # ONTA-256: bind the URL BEFORE _dedupe_rows drops rows.
                            # Dedupe SHIFTS every surviving row's positional index;
                            # the provenance map is keyed by each row's ORIGINAL
                            # position, so stamping AFTER the drop (re-derived by the
                            # shifted index) mis-binds a survivor to a DROPPED
                            # neighbour's page. Binding first — indices still
                            # original — and carrying the URL on the row object makes
                            # the citation immune to the reindex.
                            batch = _dedupe_rows_with_source_urls(
                                rows_found,
                                key_attr,
                                seen_keys,
                                getattr(full, "provenance", None) or {},
                            )
                            # ONTA-345: entity-level re-acquisition guard. DROP any
                            # row whose would-be canonical subject
                            # (entity_uri(proposed_type, row[key_attr])) is on the
                            # entity-suppression list fetched once per run above —
                            # BEFORE the SourceBundle is built and BEFORE
                            # resolver.ingest*, so an ERASED entity never enters the
                            # bundle and never reaches the writer. Each drop is
                            # logged. No-op (returns `batch` unchanged) when the run
                            # has nothing suppressed.
                            batch = _drop_suppressed_rows(
                                batch,
                                proposed_type,
                                key_attr,
                                suppressed_entities,
                                provider=prov.name,
                                job_id=job.id if job is not None else None,
                            )
                            # A1 VALIDATORS (ONTA-393): reject nav-chrome NAMES (drop
                            # the whole row) and type-invalid CELLS (city=year,
                            # website=no-host, address=enrolment phrase — scrub the
                            # cell) at the A1 boundary — same seam as the suppression
                            # guard, BEFORE the SourceBundle is built and BEFORE
                            # resolver.ingest*, so chrome never becomes a graph
                            # entity. Fill rate ≠ correctness: this is the gate that
                            # keeps a non-empty-but-wrong cell out of the write.
                            (
                                batch,
                                _rows_dropped,
                                _cells_scrubbed,
                                _drop_reasons,
                            ) = _screen_a1_rows(
                                batch,
                                key_attr,
                                attributes,
                                provider=prov.name,
                                job_id=job.id if job is not None else None,
                            )
                            if _rows_dropped or _cells_scrubbed:
                                a1_rows_dropped += _rows_dropped
                                a1_cells_scrubbed += _cells_scrubbed
                                for _r in _drop_reasons:
                                    if (
                                        _r not in a1_drop_reasons
                                        and len(a1_drop_reasons) < 20
                                    ):
                                        a1_drop_reasons.append(_r)
                                # Keep the operator Job Trace honest: a P1 action
                                # explaining the A1 rejections. Isolated so a trace
                                # hiccup can never sink the write path.
                                try:
                                    if job is not None:
                                        rec = attach_recorder(job)
                                        if rec is not None:
                                            rec.action(
                                                StageProjectId.p1,
                                                "a1_validate",
                                                detail=(
                                                    f"dropped {_rows_dropped} "
                                                    f"nav-chrome rows, scrubbed "
                                                    f"{_cells_scrubbed} type-invalid "
                                                    f"cells"
                                                ),
                                                meta={
                                                    "rows_dropped": _rows_dropped,
                                                    "cells_scrubbed": _cells_scrubbed,
                                                    "reasons": _drop_reasons[:8],
                                                    "provider": prov.name,
                                                },
                                            )
                                except Exception:  # noqa: BLE001 — trace never breaks the run
                                    logger.warning(
                                        "web_ingest_a1_validate_trace_failed",
                                        job_id=job.id if job is not None else None,
                                        exc_info=True,
                                    )
                            if not batch:
                                continue  # found rows; all deduped/suppressed/chrome
                            # A1 SOURCE BUNDLE (ONTA-346): materialize the
                            # Find→Extract boundary artifact from THIS provider's
                            # post-dedupe batch, BEFORE the extract/write below.
                            # The rows already carry their per-record `source_url`
                            # (bound above, pre-dedupe); the bundle stamps run
                            # identity + per-row fact-id lineage + the source tier
                            # (registry Tier -1 = authoritative vs web) + the
                            # provider's LOGICAL secret_ref (never a resolved
                            # credential). This is a PRE-write artifact — it does
                            # NOT write; ONTA-371 makes it the extract DRIVER below
                            # (the loop iterates `bundle.rows`), and because each
                            # row's `data` is a snapshot copy of the batch row the
                            # KG write stays byte-identical — lineage rides along.
                            bundle = build_source_bundle(
                                batch,
                                workspace_id=ctx.tenant_id,
                                run_id=run_id,
                                provider=prov.name,
                                tier=_provider_tier(prov),
                                secret_refs=_provider_secret_refs(prov),
                                key_attribute=key_attr,
                                bundle_key=f"{prov.name}:{sub_query}",
                            )
                            # ONTA-371: the bundle is now the LIVE extract driver —
                            # the micro-batch loop below iterates ``bundle.rows``
                            # (not the built-then-dropped raw ``batch``) and hands
                            # each row's A1 ``fact_id`` / ``tier`` to the resolver
                            # ingest call (the real A1→A2 handoff). The observer
                            # sink stays as a SUPPLEMENTARY hook (a no-op when
                            # unset); the bundle is genuinely consumed regardless.
                            _emit_source_bundle(ctx, bundle)
                            # Operator Job Trace: A1 Source Bundle boundary (P1→P2).
                            # Contract-shaped A1 summary (ONTA-385); try/except so
                            # observability never sinks discovery.
                            try:
                                a1_piece = summarize_a1_source_bundle(bundle)
                                a1_acc = merge_a1_summaries(a1_acc, a1_piece)
                                a2_source_rows += len(bundle.rows)
                                if job is not None:
                                    rec = attach_recorder(job)
                                    if rec is not None:
                                        rec.action(
                                            StageProjectId.p1,
                                            "source_bundle",
                                            detail=(
                                                f"A1 provider={prov.name} "
                                                f"rows={len(bundle.rows)} "
                                                f"tier={_provider_tier(prov)}"
                                            ),
                                            meta={
                                                **{
                                                    k: a1_piece.get(k)
                                                    for k in (
                                                        "artifact",
                                                        "run_id",
                                                        "root_fact_id",
                                                        "row_count",
                                                        "tiers",
                                                        "providers",
                                                    )
                                                },
                                                "provider": prov.name,
                                                "sub_query": (sub_query or "")[:200],
                                            },
                                        )
                                        # Progressive P1 output = run-level A1 aggregate.
                                        for _p in job.stage_trace.projects:
                                            if _p.project_id == StageProjectId.p1:
                                                _p.output = {
                                                    **_p.output,
                                                    **(a1_acc or {}),
                                                }
                                                break
                                        rec.begin(
                                            StageProjectId.p2,
                                            input={
                                                "artifact": "A1",
                                                "name": "Source Bundle",
                                                "source_row_count": len(bundle.rows),
                                                "provider": prov.name,
                                                "run_id": a1_piece.get("run_id"),
                                                "root_fact_id": a1_piece.get(
                                                    "root_fact_id"
                                                ),
                                                "tier": _provider_tier(prov),
                                                "contract_consumes": (
                                                    "A1 Source Bundle (or uploaded file)"
                                                ),
                                                "contract_emits": "A2 Candidate Facts",
                                            },
                                        )
                                        rec.action(
                                            StageProjectId.p2,
                                            "extract_from_bundle",
                                            detail="A1→A2 extract driver",
                                        )
                                        rec.begin(
                                            StageProjectId.p6,
                                            input={
                                                "artifact": "A5",
                                                "name": "Placement Plan (fused)",
                                                "kg_name": kg_name,
                                                "type_name": proposed_type,
                                                "instance_graph": instance_graph,
                                                "run_id": run_envelope.run_id,
                                                "contract_consumes": "A5 Placement Plan",
                                                "contract_emits": "A6 Graph Delta",
                                            },
                                        )
                            except Exception:
                                logger.warning(
                                    "stage_trace_a1_failed",
                                    job_id=getattr(job, "id", None) if job else None,
                                    exc_info=True,
                                )
                            platforms = list(
                                dict.fromkeys(
                                    [
                                        *platforms,
                                        *_platforms(
                                            getattr(full, "sources", None), prov
                                        ),
                                    ]
                                )
                            )
                            # Live status BEFORE the (slower) LLM-extraction
                            # ingest, so a poll mid-batch already shows which
                            # providers were consulted + what they found — the
                            # single-batch classic path otherwise sits at 0/0 for
                            # the whole extraction (adversarial-review F5). Flip the
                            # user-facing phase to "ingesting" (ONTA-238): we have
                            # rows and are about to run the extract→insert path, the
                            # slowest leg of the run.
                            if job is not None and job_store is not None:
                                job.progress.phase = "ingesting"
                                job.platforms = platforms
                                job.provider_logs = list(plogs.values())
                                await job_store.update(job)
                            # SUB-BATCHED ingest (ONTA-243) — split the batch's
                            # rows into small sub-batches, commit each, and flush
                            # ``processed``/``filled`` AFTER EVERY ONE so both
                            # headline counters move WHILE the job is still
                            # ``running`` — not just once the whole (slow)
                            # extraction of the entire batch completes. This is the
                            # single-list ask's fix: one sub-query × one provider is
                            # the WHOLE run, so without sub-batching a poller sees a
                            # flat 0/0 for the entire extraction and concludes the
                            # job stalled (persona-eval RCA). Mirrors enrichment's
                            # per-record flush cadence. Source names the provider
                            # that actually produced the batch.
                            #
                            # CITATION BINDING (persona-eval RCA — citation
                            # mis-binding): partition the batch by source_url FIRST,
                            # then size-chunk within each group, so every micro-batch
                            # handed to the extractor is homogeneous in its citation.
                            # An extraction that sees rows from exactly one page can
                            # only stamp THAT page's URL on any entity it mints — the
                            # LLM can no longer copy page A's URL onto an entity drawn
                            # from page B. Groups are consecutive, so order + counts
                            # are unchanged; a batch that already shares one URL (or
                            # none) is one group — identical to the prior behavior.
                            # ONTA-371: drive the extract loop from the A1
                            # SourceBundle's rows (each a ``SourceRow`` carrying the
                            # row's snapshot ``data`` + its A1 ``fact_id`` + source
                            # ``tier``), not the built-then-dropped raw ``batch``.
                            # ``bundle.rows`` is index-aligned with ``batch``, so
                            # grouping/chunking (and the records extracted) are
                            # byte-identical — the change threads lineage, it does
                            # not change WHAT is extracted.
                            micro_batches = [
                                micro
                                for group in _group_rows_by_source_url(bundle.rows)
                                for micro in _chunk_rows(
                                    group, _DISCOVERY_INGEST_SUBBATCH
                                )
                            ]
                            # ONTA-272: a provider whose rows are ALREADY structured
                            # (keyed by the confirmed attribute set — API-registry
                            # pulls, structured captures) commits through the
                            # deterministic mapping seam with NO LLM extractor, when
                            # the fast-path is enabled and the provider opts in. All
                            # other providers keep the byte-for-byte unchanged
                            # ``resolver.ingest`` JSON detour below.
                            structured_fastpath = (
                                _DISCOVERY_STRUCTURED_FASTPATH
                                and getattr(prov, "structured", False)
                            )
                            for micro in micro_batches:
                                # ONTA-371: unpack the A1 SourceRows. ``micro_rows``
                                # is the row DATA (a snapshot copy of the batch
                                # dicts) — byte-identical to what the extractor saw
                                # before, so the write stays unchanged.
                                # ``micro_fact_ids`` / ``micro_tier`` are the per-row
                                # A1 lineage handed off to the resolver (A1→A2). All
                                # rows in a bundle share one tier.
                                micro_rows = [r.data for r in micro]
                                micro_fact_ids = [r.fact_id for r in micro]
                                micro_tier = micro[0].tier if micro else None
                                if structured_fastpath:
                                    # Pre-structured rows already carry ``source_url``
                                    # (stamped above), which becomes the per-record
                                    # citation + the A2 evidence link. Deterministic:
                                    # preview == commit, no ``_extract``.
                                    result = await resolver.ingest_structured_rows(
                                        micro_rows,
                                        ctx.tenant_id,
                                        type_name=proposed_type,
                                        attributes=list(attributes),
                                        source=f"web:{prov.name}:{query}",
                                        instance_graph=instance_graph,
                                        key_attribute=key_attr,
                                        # ONTA-372: same run_id as the A1 bundle so
                                        # the A6 delta keys off ONE run lineage.
                                        run_id=run_envelope.run_id,
                                        # ONTA-371: per-row A1 lineage handoff.
                                        fact_ids=micro_fact_ids,
                                        tier=micro_tier,
                                    )
                                else:
                                    content = json.dumps(
                                        micro_rows, default=str, ensure_ascii=False
                                    )
                                    result = await resolver.ingest(
                                        content,
                                        ctx.tenant_id,
                                        content_type="json",
                                        source=f"web:{prov.name}:{query}",
                                        instance_graph=instance_graph,
                                        # Discovery CONFIRMED the target type + attribute
                                        # set with the user, so it passes them to
                                        # extraction as a focus. SOFT (default): a PRIOR
                                        # that keeps extraction compact yet still
                                        # decomposes faithfully (subtypes, real-world
                                        # nodes, multi-valued splits) — the ONTA-199
                                        # follow-up that fixed the flat single-type
                                        # mis-modeling (NPs typed as Physician,
                                        # city/specialty as literals) without the
                                        # open-ended reifier's ~20-type blowup. HARD
                                        # (kill-switch): the original flat cage.
                                        constrain_types=[proposed_type],
                                        constrain_attributes={
                                            proposed_type: list(attributes)
                                        },
                                        constrain_soft=_DISCOVERY_SOFT_EXTRACT,
                                        # ONTA-382: exhaustive (closed) attribute
                                        # set → extraction allowlist/ceiling even
                                        # under soft mode. Illustrative/open keeps
                                        # the soft prior-only behavior.
                                        constrain_attributes_exhaustive=(
                                            attributes_exhaustive
                                        ),
                                        # ONTA-372: same run_id as the A1 bundle so
                                        # the resolver keys the A6 Graph Delta off
                                        # ONE run lineage instead of a fresh uuid4.
                                        run_id=run_envelope.run_id,
                                        # ONTA-371: per-row A1 lineage handoff.
                                        fact_ids=micro_fact_ids,
                                        tier=micro_tier,
                                    )
                                processed += len(micro)
                                entities_total += int(
                                    getattr(result, "entities_resolved", 0) or 0
                                )
                                affected_types |= set(result.types_created)
                                for attr_added in result.attributes_added:
                                    affected_types.add(attr_added.split(".")[0])
                                # Live stage_trace: fold A2/A3/A4/A6 from this
                                # IngestResult (ONTA-385). Isolated so a ledger
                                # shape surprise cannot fail the write path.
                                try:
                                    a2_batches += 1
                                    if structured_fastpath:
                                        a2_structured_batches += 1
                                    a2_extracted += int(
                                        getattr(result, "entities_extracted", 0) or 0
                                    )
                                    a2_resolved += int(
                                        getattr(result, "entities_resolved", 0) or 0
                                    )
                                    a6_triples += int(
                                        getattr(result, "triples_inserted", 0) or 0
                                    )
                                    a3_piece = summarize_a3_clean_report(
                                        getattr(result, "clean_report", None)
                                    )
                                    if a3_piece is not None:
                                        a3_counts = merge_a3_counts(a3_counts, a3_piece)
                                        for r in a3_piece.get("drop_reasons_sample") or []:
                                            if r not in a3_drop_reasons:
                                                a3_drop_reasons.append(r)
                                        for t in a3_piece.get("transforms_sample") or []:
                                            if len(a3_transforms_sample) < 8:
                                                a3_transforms_sample.append(t)
                                    verified = getattr(result, "verified_facts", None) or []
                                    a4_verified_count += len(verified)
                                    gd = getattr(result, "graph_delta", None)
                                    if gd is not None:
                                        gd_d = (
                                            gd.to_dict()
                                            if hasattr(gd, "to_dict")
                                            and not isinstance(gd, dict)
                                            else gd
                                        )
                                        if isinstance(gd_d, dict):
                                            facts = list(gd_d.get("facts") or [])
                                            fan = list(gd_d.get("fan_in") or [])
                                            a6_fact_count += len(facts)
                                            a6_fan_in_count += len(fan)
                                            if gd_d.get("run_id"):
                                                a6_run_id = gd_d.get("run_id")
                                            if gd_d.get("instance_graph"):
                                                a6_instance_graph = gd_d.get(
                                                    "instance_graph"
                                                )
                                            for f in facts:
                                                if len(a6_facts_sample) < 3:
                                                    a6_facts_sample.append(f)
                                    if job is not None:
                                        rec = attach_recorder(job)
                                        if rec is not None:
                                            rec.action(
                                                StageProjectId.p2,
                                                "extract_batch",
                                                detail=(
                                                    f"entities_extracted="
                                                    f"{getattr(result, 'entities_extracted', 0)} "
                                                    f"resolved="
                                                    f"{getattr(result, 'entities_resolved', 0)}"
                                                ),
                                                meta={
                                                    "entities_extracted": getattr(
                                                        result, "entities_extracted", 0
                                                    ),
                                                    "entities_resolved": getattr(
                                                        result, "entities_resolved", 0
                                                    ),
                                                    "structured_fastpath": structured_fastpath,
                                                    "micro_rows": len(micro),
                                                },
                                            )
                                            if a3_piece is not None:
                                                rec.begin(
                                                    StageProjectId.p3,
                                                    input={
                                                        "artifact": "A2",
                                                        "name": "Candidate Facts",
                                                        "contract_consumes": (
                                                            "A2 Candidate Facts"
                                                        ),
                                                        "contract_emits": "A3 Clean Facts",
                                                    },
                                                )
                                                rec.action(
                                                    StageProjectId.p3,
                                                    "clean_ledger",
                                                    detail=(
                                                        "A3 counts="
                                                        f"{a3_piece.get('counts')}"
                                                    ),
                                                    meta=a3_piece.get("counts") or {},
                                                )
                                            if verified:
                                                rec.begin(
                                                    StageProjectId.p4,
                                                    input={
                                                        "artifact": "A3",
                                                        "name": "Clean Facts",
                                                        "verified_batch": len(verified),
                                                    },
                                                )
                                                rec.action(
                                                    StageProjectId.p4,
                                                    "verify",
                                                    detail=f"{len(verified)} A4 verdicts",
                                                )
                                            _gd_facts = 0
                                            if isinstance(gd, dict):
                                                _gd_facts = len(gd.get("facts") or [])
                                            elif gd is not None and hasattr(gd, "facts"):
                                                _gd_facts = len(gd.facts)
                                            rec.action(
                                                StageProjectId.p6,
                                                "write_batch",
                                                detail=(
                                                    f"triples="
                                                    f"{getattr(result, 'triples_inserted', 0)} "
                                                    f"entities="
                                                    f"{getattr(result, 'entities_resolved', 0)}"
                                                ),
                                                meta={
                                                    "triples_inserted": getattr(
                                                        result, "triples_inserted", 0
                                                    ),
                                                    "entities_resolved": getattr(
                                                        result, "entities_resolved", 0
                                                    ),
                                                    "graph_delta_facts": _gd_facts,
                                                },
                                            )
                                except Exception:
                                    logger.warning(
                                        "stage_trace_ingest_fold_failed",
                                        job_id=getattr(job, "id", None)
                                        if job is not None
                                        else None,
                                        exc_info=True,
                                    )
                                # A9 manifest: this micro-batch's rows LANDED —
                                # record them as completed items so a later halt can
                                # say exactly how many of the planned cap made it in
                                # before the failure (honest partial coverage).
                                if job is not None and job.manifest is not None:
                                    for _row in micro:
                                        # ONTA-371: ``_row`` is an A1 SourceRow now;
                                        # its snapshot ``data`` carries the key value.
                                        job.manifest.record_completed(
                                            str(_row.data.get(key_attr, ""))
                                        )
                                    # A9 cost envelope (ONTA-282): the paid
                                    # provider spend for this run landed on the
                                    # manifest above; if cumulative spend has now
                                    # reached the HARD per-run ceiling, ABORT
                                    # CLEANLY — set the fatal flag and break out of
                                    # the micro-batch loop. The run then settles via
                                    # _fail_billing_job (terminal `failed`,
                                    # `cost_ceiling` kind, honest partial coverage),
                                    # never a silent overspend. None/0 ⇒ never trips.
                                    _ceiling_err = job.manifest.check_ceiling()
                                    if _ceiling_err is not None:
                                        fatal_ceiling_err = _ceiling_err
                                        break
                                if job is not None and job_store is not None:
                                    # Rolling, honest total: what landed + the
                                    # average per-sub-query yield extrapolated over
                                    # the sub-queries still to run, never above the
                                    # cap. Settles to == processed at the end.
                                    # ``filled`` is the persona's success signal —
                                    # it MUST move mid-run, so we set it to the
                                    # entities resolved so far after each sub-batch
                                    # (it was previously written ONLY at
                                    # _finish_job, so it read 0 the whole session).
                                    subs_done = sub_i + 1
                                    subs_left = len(subqueries) - subs_done
                                    avg = math.ceil(processed / subs_done)
                                    job.progress.processed = processed
                                    job.progress.filled = entities_total
                                    job.progress.total = min(
                                        cap, processed + subs_left * avg
                                    )
                                    job.platforms = platforms
                                    job.provider_logs = list(plogs.values())
                                    await job_store.update(job)
                            # The batch went through end-to-end (no 429/throttle) —
                            # a successful call breaks any pending rate-limit streak.
                            rate_escalator.record_success()
                            # A9 cost envelope (ONTA-282): the ceiling tripped inside
                            # the micro-batch loop — abort the provider fan-out too;
                            # every remaining call would only spend past the envelope.
                            if fatal_ceiling_err is not None:
                                break
                        except LLMError as exc:
                            # FATAL, SYSTEMIC LLM-backend failure (402 billing /
                            # 401 auth) surfaced by the extraction call inside
                            # resolver.ingest (ONTA-201). It WILL recur on every
                            # remaining chunk/sub-query, so aborting the whole run
                            # now is the honest, cheap answer — NOT swallowing it
                            # as one failed batch (`web_ingest_subquery_failed`)
                            # and letting the run report "complete". Record it and
                            # break out of BOTH loops; the terminal state below
                            # reflects rows-landed vs rows-lost.
                            fatal_llm_err = exc
                            logger.error(
                                "web_ingest_llm_backend_fatal",
                                query=sub_query,
                                provider=prov.name,
                                phase=phase,
                                processed=processed,
                                error=str(exc),
                            )
                            break
                        except Exception as exc:  # noqa: BLE001 — one batch
                            # 429 policy (ONTA-273): a rate-limit response is NOT a
                            # per-batch failure to attribute — it is a transient the
                            # escalator counts. A single/occasional 429 falls through
                            # to the per-batch degrade below (retry the next batch);
                            # only a SUSTAINED streak returns a fatal error we route
                            # through the billing-halt machinery (fail-fast, honest
                            # partials) instead of spinning on doomed calls.
                            _status = getattr(
                                getattr(exc, "response", None), "status_code", None
                            )
                            if _status is not None and is_rate_limit_status(_status):
                                _rate_fatal = rate_escalator.record_rate_limited(
                                    provider="openrouter",
                                    host=urlparse(OPENROUTER_BASE).hostname,
                                    detail=str(exc)[:120],
                                )
                                if _rate_fatal is not None:
                                    fatal_llm_err = _rate_fatal
                                    logger.error(
                                        "web_ingest_llm_backend_fatal",
                                        query=sub_query,
                                        provider=prov.name,
                                        phase=phase,
                                        processed=processed,
                                        error=str(_rate_fatal),
                                    )
                                    break
                            else:
                                # Any non-429 outcome breaks the 429 streak.
                                rate_escalator.record_success()
                            # failing must not sink the run. Attribution follows
                            # the phase: a discover crash is the PROVIDER's; an
                            # ingest/bookkeeping crash after a clean discover is
                            # a JOB-side error — the provider log is never
                            # mis-blamed for it.
                            last_provider_err = str(exc)
                            errors_total += 1
                            if phase == "discover":
                                last_err_provider = prov.name
                                plog.errors += 1
                                plog.last_error = last_provider_err[:300]
                            else:
                                last_err_provider = None
                            logger.warning(
                                "web_ingest_subquery_failed",
                                query=sub_query,
                                provider=prov.name,
                                phase=phase,
                                exc_info=True,
                            )
                            continue
                    # A fatal billing/auth error (402/401) OR a cost-ceiling breach
                    # (ONTA-282) broke the inner provider loop — abort the whole
                    # sub-query fan-out too; every remaining call would fail
                    # identically (402) or only overspend (ceiling). The terminal
                    # FAILED state (with honest partials) is set below.
                    if fatal_llm_err is not None or fatal_ceiling_err is not None:
                        break

                # FATAL run-level abort: a billing/auth failure (402/401, ONTA-201)
                # or a cost-ceiling breach (ONTA-282). Fail the WHOLE job with the
                # clear, user-facing message, recording rows-landed vs rows-lost so
                # the run is NEVER presented as complete when batches were dropped to
                # a systemic backend error or the spend envelope. This precedes the
                # normal roll-up because it is a run-level abort, not a per-provider
                # outcome. Both flow through _fail_billing_job → halt_from_exception,
                # which classifies the reason kind (billing / cost_ceiling) from the
                # error type — so a ceiling halt reads "cost envelope exceeded", not
                # "provider exhaustion".
                _fatal_run_err: Optional[RetrievalError] = (
                    fatal_llm_err or fatal_ceiling_err
                )
                if _fatal_run_err is not None:
                    for plog in plogs.values():
                        plog.status = (
                            "error" if plog.attempts and not plog.matches
                            else ("ok" if plog.matches else "skipped")
                        )
                    await _fail_billing_job(
                        job, job_store, list(plogs.values()), _fatal_run_err,
                        processed=processed, platforms=platforms,
                    )
                    return

                for plog in plogs.values():
                    # Roll-up per the ProviderLog contract: "skipped" = named but
                    # never consulted (cap filled before its turn), NOT no_match.
                    if plog.attempts == 0:
                        plog.status = "skipped"
                    elif plog.matches:
                        plog.status = "ok"
                    elif plog.errors:
                        plog.status = "error"
                    else:
                        plog.status = "no_match"
                if processed == 0:
                    if errors_total and last_provider_err is not None:
                        # Nothing landed AND something errored (every discover
                        # died, or the found rows could not be ingested) → a
                        # failed job carrying the attributed error, not a silent
                        # empty success.
                        if job is not None:
                            job.provider_logs = list(plogs.values())
                            job.error_summary = [
                                JobErrorItem(
                                    # provider set only when a DISCOVER died;
                                    # a job-side (ingest) failure carries
                                    # kind="job" with no provider blamed.
                                    provider=last_err_provider,
                                    kind="error" if last_err_provider else "job",
                                    message=last_provider_err[:300],
                                )
                            ]
                        await _fail_job(job, job_store, last_provider_err)
                        return
                    logger.info(
                        "web_ingest_no_rows", query=query,
                        kg_name=kg_name or None, instance_graph=instance_graph,
                    )
                    if job is not None and job_store is not None:
                        job.provider_logs = list(plogs.values())
                    await _finish_job(
                        job,
                        job_store,
                        processed=0,
                        entities=0,
                        platforms=platforms,
                        stage_contracts=_build_stage_contracts(
                            a1_acc=a1_acc,
                            a1_rows_dropped=a1_rows_dropped,
                            a1_cells_scrubbed=a1_cells_scrubbed,
                            a1_drop_reasons=a1_drop_reasons,
                            a2_extracted=a2_extracted,
                            a2_resolved=a2_resolved,
                            a2_source_rows=a2_source_rows,
                            a2_batches=a2_batches,
                            a2_structured_batches=a2_structured_batches,
                            a3_counts=a3_counts,
                            a3_drop_reasons=a3_drop_reasons,
                            a3_transforms_sample=a3_transforms_sample,
                            a4_verified_count=a4_verified_count,
                            a6_fact_count=a6_fact_count,
                            a6_fan_in_count=a6_fan_in_count,
                            a6_triples=a6_triples,
                            a6_facts_sample=a6_facts_sample,
                            a6_run_id=a6_run_id,
                            a6_instance_graph=a6_instance_graph,
                            entities_written=0,
                            focus_type=proposed_type,
                            focus_attributes=list(attributes),
                            run_id=run_envelope.run_id,
                        ),
                    )
                    return
                logger.info(
                    "web_ingest_complete",
                    query=query,
                    subqueries=len(subqueries),
                    providers=[pr.name for pr in ensemble],
                    rows=processed,
                    entities=entities_total,
                    types=sorted(affected_types) or None,
                    # The graph the rows actually landed in — pair this with the row
                    # count so "N filled" is always attributable to a concrete graph.
                    kg_name=kg_name or None,
                    instance_graph=instance_graph,
                )
                # Single shared post-write housekeeping path (graph/kg_writer.py) —
                # the SAME refresh ingestion + enrichment run: invalidate the
                # NL-planning ontology cache, re-embed affected types (new types +
                # types that gained an attribute), and recompute Explorer type-stats.
                # ONE refresh for the whole fan-out (not per batch): the union of
                # affected types is what downstream caches care about. Best-effort:
                # a refresh hiccup must NOT present as a failed ingest — the data +
                # ontology already landed.
                try:
                    await refresh_after_write(
                        ctx.neptune,
                        tenant_id=ctx.tenant_id,
                        kg_name=kg_name,
                        affected_types=affected_types,
                    )
                except Exception:  # noqa: BLE001 — refresh failure must not fail a landed ingest
                    logger.warning("web_ingest_refresh_failed", exc_info=True)
                if job is not None:
                    # Settle the rolling estimate to the exact final count.
                    job.progress.total = processed
                await _finish_job(
                    job,
                    job_store,
                    processed=processed,
                    entities=entities_total,
                    platforms=platforms,
                    stage_contracts=_build_stage_contracts(
                        a1_acc=a1_acc,
                        a1_rows_dropped=a1_rows_dropped,
                        a1_cells_scrubbed=a1_cells_scrubbed,
                        a1_drop_reasons=a1_drop_reasons,
                        a2_extracted=a2_extracted,
                        a2_resolved=a2_resolved,
                        a2_source_rows=a2_source_rows,
                        a2_batches=a2_batches,
                        a2_structured_batches=a2_structured_batches,
                        a3_counts=a3_counts,
                        a3_drop_reasons=a3_drop_reasons,
                        a3_transforms_sample=a3_transforms_sample,
                        a4_verified_count=a4_verified_count,
                        a6_fact_count=a6_fact_count,
                        a6_fan_in_count=a6_fan_in_count,
                        a6_triples=a6_triples,
                        a6_facts_sample=a6_facts_sample,
                        a6_run_id=a6_run_id,
                        a6_instance_graph=a6_instance_graph,
                        entities_written=entities_total,
                        focus_type=proposed_type,
                        focus_attributes=list(attributes),
                        run_id=run_envelope.run_id,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — background job self-contains errors
                logger.error(
                    "web_ingest_failed", query=query,
                    kg_name=kg_name or None, instance_graph=instance_graph,
                    exc_info=True,
                )
                msg = str(exc)
                # Per-(sub-query, provider) errors are handled in the loop, so a
                # crash HERE is past discovery (ingest/refresh/bookkeeping) — a
                # job-level failure — unless no discover ever returned (setup
                # crash), which stays provider-attributed. Matches the enrichment
                # executor's fatal-path classification.
                if not any_discover_ok:
                    primary_plog = plogs[provider.name]
                    primary_plog.errors += 1
                    primary_plog.status = "error"
                    primary_plog.last_error = msg[:300]
                if job is not None:
                    job.provider_logs = list(plogs.values())
                    job.error_summary = [
                        JobErrorItem(
                            provider=provider.name if not any_discover_ok else None,
                            kind="error" if not any_discover_ok else "job",
                            message=msg[:300],
                        )
                    ]
                await _fail_job(job, job_store, msg)

        async def _run() -> None:
            # Per-run wall-clock guard (ONTA-196): bound the WHOLE discovery so a
            # pathologically slow extraction can never leave the job stuck on
            # ``running`` indefinitely. On timeout we cancel the inner run and
            # flip the job to ``failed`` with a clear message — the same terminal
            # signal the client polls for, instead of an eternal spinner.
            try:
                await asyncio.wait_for(_run_inner(), timeout=_RUN_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.error(
                    "web_ingest_run_timeout",
                    query=query,
                    timeout_s=_RUN_TIMEOUT_S,
                )
                await _fail_job(
                    job,
                    job_store,
                    f"Discovery timed out after {int(_RUN_TIMEOUT_S)}s "
                    "(the web fetch or extraction took too long).",
                )

        _spawn(_run())
        ack = {
            "kind": "ack",
            "capability": self.name,
            "action": step.action,
            # Clean, distilled job title for client job cards — the search subject
            # the spec LLM extracted, NOT the user's raw conversational sentence.
            "title": query,
            "message": (
                f"Searching the web for “{query}” and ingesting the results "
                f"as {proposed_type} ({', '.join(attributes)}) in the background."
            ),
        }
        if job is not None:
            # Hand the job id + initial status back so the client can poll the
            # live status (GET /enrich/jobs/{id} or the unified /jobs feed).
            ack["job_id"] = job.id
            ack["job_status"] = job.status.value
        return ack


# --- entity + attribute resolution ------------------------------------------- #

_SPEC_SYSTEM = """\
You plan a web-discovery ingest: the user wants to pull a NEW set of records from \
the web and add them to a knowledge graph. Read the whole conversation for context, \
but treat the user's CURRENT (latest) request as the PRIMARY intent — earlier turns \
only fill gaps it leaves and must NEVER override the entity type, fields, or search \
subject the current request names. Output STRICT JSON only (no markdown):
{
  "entity_type": "<PascalCase singular type for the records, e.g. Model, Company, Drug>",
  "key_attribute": "<the natural identifier, usually 'name', snake_case>",
  "query": "<a clean, concise SEARCH SUBJECT — the thing to find on the web, with all conversational framing removed>",
  "query_kind": "<'place' when the records are physical places / businesses / real-world locations to find; otherwise null>",
  "subqueries": ["<2-6 SELF-CONTAINED sub-queries that PARTITION an enumeration ask; [] for a single-list ask>"],
  "confirmed_attributes": ["<attributes the user EXPLICITLY named; [] if they only named the entity>"],
  "core_attributes": ["<the 2-4 MOST IMPORTANT attributes for this entity — a strict subset of suggested_attributes, snake_case, excluding the key; these are PRE-SELECTED and shown to the user>"],
  "suggested_attributes": ["<a COMPREHENSIVE set (6-12) of web-discoverable columns for this entity, snake_case, excluding the key>"]
}
RULES:
- query: the SUBJECT to search for, NOT the user's literal sentence. Strip \
questions, meta-framing and filler. "can we ingest open-router's TTS models that \
it currently offers" -> "OpenRouter text-to-speech (TTS) models". "I'm looking \
for a list of models offered by OpenRouter" -> "models offered by OpenRouter". \
Keep it short and specific; do NOT include words like "ingest", "add", "list of", \
"can we", "I'm looking for".
- entity_type: specific but clean — "a list of models offered by OpenRouter" -> \
"Model" (prefer the domain term the user used; singular).
- query_kind: set to "place" ONLY when the records are physical places, \
businesses, venues, or real-world locations you would find on a map — restaurants, \
coffee shops, hotels, stores, clinics, gyms, parks, landmarks, offices \
("coffee shops in SF", "hardware stores near Austin", "urgent care clinics in \
Boston"). Otherwise set it to null. NON-place examples (null): "top LLMs", "S&P \
500 companies", "Nobel laureates", "npm packages", "movies from 2020" — these are \
not physical locations even when they mention an organization. When unsure, use \
null.
- subqueries: for an ENUMERATION / population inventory ask — the user wants \
ALL/every instance of a class across a scope that no single search page covers \
well. Partition into 2-6 SELF-CONTAINED queries, each complete on its own and \
together covering the whole ask with minimal overlap. Prefer AUTHORITATIVE list \
phrasing ("List of …") so directory/wiki/registry pages rank first. Partition by \
(a) region/city when the scope names several places, (b) natural subtypes when the \
class has them (universities vs colleges; coffee shops vs bakeries), and/or (c) \
complementary inventory angles (public vs private; accredited registry vs directory) \
when the scope is a single large region/population. Examples: "all primary care \
physicians in Tustin and Santa Ana" -> ["List of primary care physicians in \
Tustin, CA", "List of primary care physicians in Santa Ana, CA"]. "universities \
in British Columbia" / "BC universities" -> ["List of universities in British \
Columbia", "List of colleges in British Columbia", "List of public universities \
in British Columbia"]. "coffee shops and bakeries in SF" -> ["List of coffee \
shops in San Francisco", "List of bakeries in San Francisco"]. A non-inventory \
ask that one catalogue already returns whole ("OpenRouter models", "S&P 500 \
companies", a single named product line) needs NO partitioning -> []. When \
unsure whether it is a population inventory, PARTITION (prefer recall).
- key_attribute: the human-readable identifier (name/title), snake_case.
- confirmed_attributes: ONLY what the user actually asked for. "models with their \
names and pricing" -> ["name","pricing"]; "a list of models" -> []. When the user \
replies with a list (e.g. "Use these: name, provider, pricing" or "just the name") \
treat THOSE as confirmed. snake_case; exclude nothing they named.
- core_attributes: a SHORT list (aim for 2-4) of the few attributes that matter \
MOST for this entity — the ones a user almost always wants and that best identify \
or differentiate a record. MUST be a subset of suggested_attributes. These are the \
ones we PRE-SELECT and show as chips; the rest of suggested_attributes stays a \
behind-the-scenes fetch hint, NOT shown pre-checked. For Model: \
["provider","context_length","input_price"]. For a Physician: \
["specialty","city","phone"]. Keep it minimal — do NOT just repeat all of \
suggested_attributes.
- suggested_attributes: a COMPREHENSIVE set (aim for 6-12) of the columns this \
entity is typically described by ON THE WEB — every web-discoverable property a \
rich source table (leaderboard, catalog, listing) would carry, snake_case, \
EXCLUDING the key. This is the FETCH hint: the provider projects rows to it, so a \
thin list silently drops the rest of the table before extraction. Be generous and \
include any recurring provider/vendor/organization column and any score/rating/ \
price/ranking column (those become reified entities downstream). For Model: \
["provider","organization","open_source","context_length","input_price",\
"output_price","modality","latency","rating","score","votes","release_date"]."""


# How many leading named fields the DETERMINISTIC fallback pre-selects as the
# "core" recommendation (mirrors the LLM path's short core set). Kept small so a
# long field list doesn't pre-check every column.
_FALLBACK_CORE_MAX = 4

# The user-facing note attached to a GENUINELY degraded spec — the resolver LLM was
# unavailable AND no explicit field list could be recovered, so discovery falls back
# to a bare name/description capture. Surfaced (not swallowed) so the user learns the
# planning degraded and can re-state the fields they want, instead of silently
# receiving a thin dataset.
_DEGRADED_NOTE = (
    "Automated field planning was unavailable, so I set up a basic "
    "name/description capture. Tell me the specific fields you want "
    '(e.g. "with field_a, field_b, field_c") and I\'ll collect those too.'
)


def _current_request(instruction: str) -> str:
    """The user's CURRENT turn within the accumulated instruction.

    The planner concatenates the session's user turns oldest-first with newlines
    (``_effective_instruction``), so the ask in front of us is the LAST non-empty
    line. Weighting it keeps a STALE earlier turn from overriding the fields / type /
    search subject the current message names. Collapses to the whole instruction when
    there is only one turn (no newline)."""
    if not instruction:
        return ""
    lines = [ln for ln in instruction.splitlines() if ln.strip()]
    return lines[-1].strip() if lines else instruction.strip()


def _fallback_spec(instruction: str) -> dict:
    """Deterministic spec for when the resolver LLM is unavailable / errored / timed
    out / returned nothing usable — NEVER the bare ``[name, description, url]``
    default that silently drops a field list the user explicitly named (the
    persona-eval RCA: a ~15s spec-LLM timeout thinned a fully-specified ask to
    name/description, so the NPI/taxonomy/affiliation fields the user listed never
    landed).

    Recovers the enumerated fields + the named type straight from the message with
    the SAME deterministic parsers the plan-time floor uses, WEIGHTING the current
    request: current-turn fields lead (earlier turns only fill the gaps they leave),
    and the current turn's type / search subject win over a stale earlier turn's.
    When no field list can be parsed at all the spec still degrades to name/
    description, but SURFACES it (``degraded`` + ``degraded_note``) instead of
    thinning silently, so the caller can tell the user rather than quietly hand back
    a thin dataset. When the LLM path succeeds it may ENRICH this set — it must never
    shrink below the fields recovered here."""
    current = _current_request(instruction)
    # Current-turn fields FIRST (weighted), then any additional the earlier turns
    # named — a union, so no explicitly-named field is ever lost, but the current
    # ask leads the ordering. ``_explicit_user_fields`` already scans the whole
    # instruction; the current-first splice is what makes the latest request
    # dominate rather than a stale earlier list.
    fields = _dedupe(
        [*_explicit_user_fields(current), *_explicit_user_fields(instruction)]
    )
    # Type: the current turn wins; the whole instruction only fills a gap.
    etype = _explicit_user_type(current) or _explicit_user_type(instruction)
    # Search SUBJECT: let the CURRENT turn drive it ONLY when that turn actually
    # names a subject/type (a genuine PIVOT like "actually discover Beta records
    # with …"). A bare confirmation ("yes go ahead") or a "Use these: …" chip reply
    # is NOT a subject — but its ``_clean_query`` is still truthy, so a naive
    # ``current or instruction`` would search the web for the confirmation/chip text
    # and return an empty/garbage dataset (reviewer-reproduced regression on the
    # multi-turn confirm path). Gating on ``_explicit_user_type(current)`` falls back
    # to the ORIGINAL first-line ask on any confirm/chip turn while still honoring a
    # real pivot turn.
    query = (
        _clean_query(current) if _explicit_user_type(current) else ""
    ) or _clean_query(instruction)
    key = "name"
    if fields:
        suggested = [a for a in fields if a and a != key]
        return {
            "entity_type": etype or "WebRecord",
            "key_attribute": key,
            "query": query,
            # No LLM ran → no kind classification (general default provider).
            "query_kind": None,
            "subqueries": [],
            "confirmed_attributes": fields,
            "core_attributes": suggested[:_FALLBACK_CORE_MAX],
            "suggested_attributes": suggested,
            "degraded": False,
        }
    # No explicit field list to recover → genuinely degraded. Keep a recovered type
    # if the user named one; SURFACE the thinning so it is not silent.
    return {
        "entity_type": etype or "WebRecord",
        "key_attribute": key,
        "query": query,
        "query_kind": None,
        "subqueries": [],
        "confirmed_attributes": [],
        "core_attributes": ["description"],
        "suggested_attributes": ["name", "description", "url"],
        "degraded": True,
        "degraded_note": _DEGRADED_NOTE,
    }


async def _resolve_spec(ctx: AgentContext, instruction: str) -> dict:
    """LLM-resolve {entity_type, key_attribute, confirmed/suggested attributes}.

    Degrades to a DETERMINISTIC fallback spec (``_fallback_spec``) when there is no
    LLM key or the call errors / times out / returns nothing usable, so the turn
    never 500s AND an explicitly-named field list is never silently dropped by a
    resolver timeout — the fallback recovers the user's fields + type from the
    CURRENT request instead of collapsing to a bare name/description default.
    """
    if ctx.openrouter_key:
        try:
            text = await openrouter_chat(
                ctx.openrouter_key,
                _SPEC_SYSTEM,
                instruction,
                model=PRIMARY_MODEL,
                temperature=0,
                max_tokens=400,
                # Kept well under the preview budget: this small spec call runs
                # BEFORE the sample fetch, so a slow one eats the sample's time.
                # On timeout _resolve_spec degrades to the fallback spec, never 500s.
                timeout=15,
            )
            parsed = _parse_json_object(text)
            if parsed:
                return _normalize_spec(parsed)
            # Non-empty text that didn't parse as a JSON object — the exception path
            # below never sees this, so surface it instead of a SILENT fall-through
            # to _fallback_spec (a future non-JSON degrade would otherwise vanish).
            logger.warning("web_ingest_spec_unparsed")
        except Exception:  # noqa: BLE001
            logger.warning("web_ingest_spec_failed", exc_info=True)
    return _fallback_spec(instruction)


def _normalize_spec(parsed: dict) -> dict:
    et = str(parsed.get("entity_type") or "WebRecord").strip() or "WebRecord"
    key = _slug(parsed.get("key_attribute") or "name") or "name"
    confirmed = [_slug(a) for a in _as_list(parsed.get("confirmed_attributes"))]
    core = [_slug(a) for a in _as_list(parsed.get("core_attributes"))]
    suggested = [_slug(a) for a in _as_list(parsed.get("suggested_attributes"))]
    return {
        "entity_type": _pascal(et),
        "key_attribute": key,
        # Free-text search subject (NOT slugged — it's prose for the provider/card).
        "query": str(parsed.get("query") or "").strip(),
        # Generic query category for kind-routing (ONTA-190). Normalized to a
        # lowercase slug so "Place"/"PLACE" all match a provider's query_kinds; a
        # missing / null / literal-"null" value collapses to None → no routing.
        "query_kind": _norm_query_kind(parsed.get("query_kind")),
        # Enumeration partition (free-text prose like `query`, NOT slugged).
        # Non-empty → execute() fans the discovery out across these instead of
        # the single query. Deduped, capped at the fan-out limit.
        "subqueries": _norm_subqueries(parsed.get("subqueries")),
        "confirmed_attributes": [a for a in confirmed if a],
        "core_attributes": [a for a in core if a],
        "suggested_attributes": [a for a in suggested if a],
    }


# Hard ceiling on the enumeration fan-out — the LLM is asked for 2-6 sub-queries;
# this guards against an over-eager reply multiplying paid calls.
_MAX_SUBQUERIES = 6


# Secondary identity signals (checked in order) that distinguish same-NAME rows:
# "Starbucks" per branch, "Dr. John Smith" per city. A bare-name dedupe key would
# collapse all of them to one record (adversarial-review F1).
_DEDUPE_SIGNAL_COLS = (
    "address", "street_address", "city", "location", "phone", "phone_number",
)


def _norm_key_part(v) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(v).lower()).strip() if v else ""


def _row_key(row: dict, key_attr: str) -> str:
    """Composite dedupe key: normalized KEY attribute + the first present
    identity signal (address/city/phone). Same name + same signal → duplicate;
    same name + DIFFERENT signal → distinct records (two branches, two cities) —
    both kept, with downstream entity resolution as the deeper merge net. A row
    with no key value returns "" (never deduped)."""
    name = _norm_key_part(row.get(key_attr))
    if not name:
        return ""
    for col in _DEDUPE_SIGNAL_COLS:
        sig = _norm_key_part(row.get(col))
        if sig:
            return f"{name}|{sig}"
    return name


def _dedupe_rows(
    rows: list[dict], key_attr: str, seen: set[str]
) -> list[dict]:
    """Drop rows whose composite key (see :func:`_row_key`) was already seen
    (mutating ``seen``) — the cross-batch merge for the fan-out/ensemble. Keys
    are normalized (lowercased, punctuation collapsed) so "Dr. Alina Reyes" and
    "dr alina reyes" dedupe; rows with NO key value are kept (nothing to match
    on)."""
    out: list[dict] = []
    for r in rows:
        key = _row_key(r, key_attr)
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(r)
    return out


def _dedupe_rows_with_source_urls(
    rows: list[dict],
    key_attr: str,
    seen: set[str],
    provenance: dict[str, str],
) -> list[dict]:
    """Bind each row's per-record ``source_url`` provenance BEFORE deduping, then
    dedupe — the ORDER is the whole fix (ONTA-256).

    :func:`_dedupe_rows` drops already-seen rows, which SHIFTS every surviving
    row's positional index. The provider's ``provenance`` map is keyed by each
    row's ORIGINAL position (or name), so re-deriving a URL by position AFTER the
    drop binds a surviving row to a DROPPED neighbour's page — the citation
    mis-binds (a row shows a source URL that isn't its own). Stamping first, while
    indices are still original, and carrying the URL ON the row object itself makes
    the citation immune to the reindex: :func:`_dedupe_rows` returns the SAME row
    objects, so each survivor keeps exactly the URL that was bound to it. And
    because :func:`_attach_source_urls` never clobbers a row that already carries a
    ``source_url``, the position-based derivation is a last resort that only runs
    while indices are still faithful — never on a reindexed survivor.

    Behaviour-preserving when nothing is dropped: attach-then-dedupe and
    dedupe-then-attach are identical for an unshifted list."""
    _attach_source_urls(rows, provenance)
    return _dedupe_rows(rows, key_attr, seen)


def _norm_subqueries(v) -> list[str]:
    """Sanitize the LLM's enumeration partition: non-empty strings, stripped,
    case-insensitively deduped, capped at ``_MAX_SUBQUERIES``. Anything malformed
    (not a list, numbers, nulls) degrades to [] — single-query behavior."""
    if not isinstance(v, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in v:
        if not isinstance(item, str):
            continue
        q = item.strip()
        key = q.lower()
        if not q or key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= _MAX_SUBQUERIES:
            break
    return out


# --- enumeration intent + deterministic partition (ONTA-379) ----------------- #
#
# ONTA-192 taught the spec LLM to fan out multi-city/category asks, but a
# single-scope population inventory ("universities in British Columbia", "BC
# universities") still collapsed to ONE subquery → ONE thin source page → ~5
# rows. These helpers are the DETERMINISTIC backstop: detect inventory intent,
# synthesize 2-6 authoritative-list angles (subtype + complementary directory
# phrasings), and expand the provider ensemble so a thin Tier-0 hit cannot
# single-source the whole population. LLM-provided partitions (≥2) still win.

# STRONG completeness language — enough (with a class-shaped subject) to fan
# out even without a geographic scope. Deliberately excludes bare "list of",
# which is everyday discovery phrasing ("list of OpenRouter models") and must
# NOT alone trigger a multi-subquery partition.
_ENUM_STRONG = re.compile(
    r"\b("
    r"all|every|every\s+single|complete|entire|full\s+list|"
    r"complete\s+list|inventory|as\s+many\s+as|comprehensive"
    r")\b",
    re.IGNORECASE,
)
# Leading inventory noise stripped before building "List of …" queries.
# Includes an optional article so "a list of …" / "the complete …" both clean.
_ENUM_LEAD = re.compile(
    r"^(?:(?:the|a|an)\s+)?(?:"
    r"list\s+of|all|every|complete|full|entire|"
    r"complete\s+list\s+of|full\s+list\s+of|directory\s+of|"
    r"catalogue\s+of|catalog\s+of"
    r")\s+",
    re.IGNORECASE,
)
# ``<head> in|across|within|throughout <scope>`` — the classic population shape.
# Deliberately omits bare ``of``: "list of X" / "University of Y" are not
# geographic partitions and would false-positive every "list of …" discovery.
_POPULATION_SCOPE = re.compile(
    r"^(?P<head>.+?)\s+"
    r"(?P<prep>in|across|within|throughout)\s+"
    r"(?P<scope>.+)$",
    re.IGNORECASE,
)
# Compound category joiners inside the head: "universities and colleges".
_COMPOUND_SPLIT = re.compile(r"\s+(?:and|&|/|or)\s+", re.IGNORECASE)
# Short local scopes (city nicknames / neighborhoods) stay single-query —
# Places + one directory usually cover them. Multi-token scopes
# (``British Columbia``, ``New South Wales``) and long single-token admin
# regions (``California``) count as broad. ``Mission`` / ``Tustin`` / ``SF``
# alone stay local unless the user used strong completeness language.
_SHORT_SCOPE_MAX = 8

# HEAVY inventory classes — multi-page provincial/state populations that a
# single directory almost never returns whole. Used as a DETECTION gate so
# everyday place queries ("coffee shops in the Mission San Francisco",
# "cardiologists in Austin TX") stay single-query even when the scope is
# multi-token. Sibling expansion (below) may still widen coffee shops once an
# ask is already classified as enumeration via ``all`` / compound heads.
_HEAVY_INVENTORY = re.compile(
    r"\b("
    r"universit(?:y|ies)|colleges?|hospitals?|"
    r"schools?|polytechnics?|institutes?"
    r")\b",
    re.IGNORECASE,
)

# Lightweight inventory siblings: when the head matches a known dual-category
# population, expand both so one subtype's thin directory cannot cap the ask.
# Deliberately small + generic; unknown heads still get complementary angles.
_INVENTORY_SIBLINGS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(r"\buniversit(?:y|ies)\b", re.IGNORECASE),
        ("universities", "colleges", "public universities", "private universities"),
    ),
    (
        re.compile(r"\bcolleges?\b", re.IGNORECASE),
        ("colleges", "universities", "community colleges"),
    ),
    (
        re.compile(r"\bhospitals?\b", re.IGNORECASE),
        ("hospitals", "medical centers", "clinics"),
    ),
    (
        re.compile(r"\b(coffee\s+shops?|cafes?|cafés?)\b", re.IGNORECASE),
        ("coffee shops", "cafes", "bakeries"),
    ),
)


def _authoritative_list_query(q: str) -> str:
    """Prefer directory/wiki phrasing: ``List of <subject>``.

    Locate/search rank official inventory pages much higher on "List of
    universities in British Columbia" than on the bare subject, which is the
    under-collection root cause for single-query source_first runs (ONTA-379)."""
    s = (q or "").strip()
    if not s:
        return s
    if re.match(r"(?i)^list\s+of\b", s):
        return s
    body = _ENUM_LEAD.sub("", s).strip() or s
    return f"List of {body}"


def _scope_is_broad(scope: str) -> bool:
    """True when the geographic/organizational scope is large enough that a
    single page rarely holds the whole population. Multi-token scopes and long
    single tokens qualify; short nicknames (``SF``, ``NYC``) do not."""
    s = (scope or "").strip()
    if not s:
        return False
    # Drop a leading article so "the Mission" counts as one meaningful token.
    s = re.sub(r"^(?i:the|a|an)\s+", "", s).strip()
    tokens = [t for t in re.split(r"\s+", s) if t]
    if len(tokens) >= 2:
        return True
    return bool(tokens) and len(tokens[0]) >= _SHORT_SCOPE_MAX


def _split_compound_head(head: str) -> list[str]:
    """Split ``universities and colleges`` → [``universities``, ``colleges``].

    Only fires when every part is a short noun phrase (≤4 words) so prose
    ("hospitals that accept Medicaid and Medicare") is not shattered."""
    parts = [p.strip(" ,;") for p in _COMPOUND_SPLIT.split(head or "") if p.strip()]
    if len(parts) < 2:
        return []
    clean: list[str] = []
    for p in parts:
        words = [w for w in re.split(r"\s+", p) if w]
        if not words or len(words) > 4:
            return []
        clean.append(" ".join(words))
    # Dedup case-insensitively while preserving order.
    return _dedupe(clean)


def _inventory_siblings(head: str) -> list[str]:
    """Return alternate inventory labels for ``head`` (including itself when
    matched), or ``[]`` when no sibling table applies."""
    h = (head or "").strip()
    if not h:
        return []
    for pat, siblings in _INVENTORY_SIBLINGS:
        if pat.search(h):
            # Prefer the caller's own wording first, then the table.
            return _dedupe([h, *siblings])
    return []


def _is_heavy_inventory_head(head: str) -> bool:
    """True for HE / hospital-style classes whose provincial inventory no single
    page covers. Everyday place nouns (coffee shops, cardiologists, gadgets)
    return False so multi-token city scopes don't fan out by accident."""
    return bool(_HEAVY_INVENTORY.search(head or ""))


def _is_enumeration_ask(instruction: str, query: str) -> bool:
    """True when the ask is a population inventory that should fan out.

    Triggers on (any one):
    * a **compound** category head (``universities and colleges in …``),
    * **strong** completeness language (``all`` / ``every`` / ``complete``) on
      a population shape — including narrow scopes (``all coffee shops in SF``),
    * a **heavy inventory class** over a broad scope (``universities in
      British Columbia``) — HE/hospitals/schools, not everyday place finds.

    Multi-token city scopes alone are NOT enough: ``cardiologists in Austin TX``
    and ``coffee shops in the Mission San Francisco`` stay single-query (the
    P1 Find offline bar + Places path). Bare ``list of X`` catalogues
    (``list of OpenRouter models``) also stay single-query."""
    text = f"{instruction or ''}\n{query or ''}".strip()
    if not text:
        return False
    has_strong = bool(_ENUM_STRONG.search(text))
    subject = (query or "").strip() or _current_request(instruction)
    subject = _ENUM_LEAD.sub("", subject).strip() or subject
    m = _POPULATION_SCOPE.match(subject)
    if not m:
        # Also try the current request line when the cleaned query lost the scope.
        m = _POPULATION_SCOPE.match(
            _ENUM_LEAD.sub("", _current_request(instruction)).strip()
        )
    if not m:
        # No ``head prep scope`` shape. Strong completeness alone on a bare
        # catalogue name ("all OpenRouter models") is still a single source —
        # do NOT fan out without a scope or compound head to partition on.
        return False
    head = m.group("head").strip()
    scope = m.group("scope").strip()
    if _split_compound_head(head):
        return True
    if has_strong:
        return True
    # Broad scope alone is not enough (city+state is multi-token). Require a
    # heavy inventory class so BC universities fan out while Austin cardiologists
    # and Mission coffee shops do not.
    return _scope_is_broad(scope) and _is_heavy_inventory_head(head)


def _synthesize_enumeration_subqueries(query: str, instruction: str) -> list[str]:
    """Build 2-6 complementary inventory angles for a population ask.

    Prefers authoritative ``List of …`` phrasing and expands (in order):
    compound heads → inventory siblings → complementary directory angles.
    Always capped/deduped by :func:`_norm_subqueries`."""
    subject = (query or "").strip() or _clean_query(instruction)
    subject = _ENUM_LEAD.sub("", subject).strip() or subject
    if not subject:
        return []

    m = _POPULATION_SCOPE.match(subject)
    if m:
        head = _ENUM_LEAD.sub("", m.group("head")).strip()
        prep = m.group("prep")
        scope = m.group("scope").strip()
        compounds = _split_compound_head(head)
        if compounds:
            return _norm_subqueries(
                [_authoritative_list_query(f"{c} {prep} {scope}") for c in compounds]
            )
        siblings = _inventory_siblings(head)
        if siblings:
            out = [
                _authoritative_list_query(f"{sib} {prep} {scope}") for sib in siblings
            ]
            # One extra complementary angle so a 2-sibling table still reaches ≥3
            # subqueries when the acceptance fixture needs multi-source coverage.
            out.append(
                f"complete directory of {head} {prep} {scope}"
            )
            out.append(
                f"{head} {prep} {scope} accreditation or government registry"
            )
            return _norm_subqueries(out)
        return _norm_subqueries(
            [
                _authoritative_list_query(f"{head} {prep} {scope}"),
                f"complete directory of {head} {prep} {scope}",
                f"{head} {prep} {scope} accreditation or government registry",
                f"public {head} {prep} {scope}",
                f"private {head} {prep} {scope}",
            ]
        )

    # No clear ``head prep scope`` parse — still give complementary inventory
    # angles so an explicit "list of X" / "all X" ask does not collapse to one
    # thin page.
    return _norm_subqueries(
        [
            _authoritative_list_query(subject),
            f"complete directory of {subject}",
            f"{subject} official registry or accreditation list",
        ]
    )


def _ensure_enumeration_partition(
    *,
    query: str,
    instruction: str,
    llm_subqueries: list[str],
) -> list[str]:
    """Return the sub-query partition execute() should fan out over.

    * LLM already partitioned (≥2) → keep it (ONTA-192 path; do not rewrite,
      so existing multi-city plans stay byte-stable).
    * Enumeration intent + empty/singleton LLM partition → synthesize
      authoritative-list angles (ONTA-379 backstop).
    * Non-enumeration → ``[]`` (classic single-query discovery)."""
    subs = list(llm_subqueries or [])
    if len(subs) >= 2:
        return _norm_subqueries(subs)
    if not _is_enumeration_ask(instruction, query):
        return []
    synthesized = _synthesize_enumeration_subqueries(query, instruction)
    # Need a real partition (≥2). A singleton synthesis is not worth the
    # fan-out overhead — fall back to single-query.
    return synthesized if len(synthesized) >= 2 else []


def _expand_enumeration_ensemble(ensemble: list) -> list:
    """For enumeration goals, also consult nested fallback providers.

    ``source_first`` short-circuits to a thin Tier-0 page when one JSON/HTML
    directory validates — never reaching its web-search fallback. Unwrapping
    that fallback into the ensemble (specialized/primary first, fallback next)
    keeps the thin source AND the broader web harvest, with cross-batch key
    dedupe making the overlap free. Providers without a nested fallback are
    unchanged. Reads ``fallback`` (public) then ``_fallback`` (legacy private
    attr on source_first) defensively so OSS stays decoupled from the premium
    wrapper's attribute name."""
    out: list = []
    seen: set[int] = set()

    def _add(p) -> None:
        if p is None:
            return
        pid = id(p)
        if pid in seen:
            return
        seen.add(pid)
        out.append(p)

    for p in ensemble or []:
        _add(p)
        fb = getattr(p, "fallback", None)
        if fb is None:
            fb = getattr(p, "_fallback", None)
        _add(fb)
    return out or list(ensemble or [])


def _norm_query_kind(v) -> Optional[str]:
    """Normalize the LLM's ``query_kind`` to a lowercase slug, or ``None``.

    The prompt asks for ``null`` on a non-specialized query, but LLMs sometimes
    emit the string ``"null"``/``"none"`` or an empty value — all collapse to
    ``None`` (no routing). A real kind is lowercased + slugged so it matches a
    provider's generic ``query_kinds`` regardless of casing/punctuation."""
    s = _slug(v)
    if not s or s in {"null", "none"}:
        return None
    return s


# The clarify PRE-SELECTS at most this many attributes — a short, most-important
# recommendation, NOT the comprehensive fetch set (that stays server-side in
# hint_columns). Keeps the chip list lean so the user isn't confronted with a dozen
# pre-checked columns.
_DEFAULT_CORE_CAP = 4


def _core_attrs(key_attr: str, core: list[str], suggested: list[str]) -> list[str]:
    """The SHORT, most-important attribute set to pre-select + show as chips —
    distinct from the comprehensive ``suggested`` FETCH hint. Prefer the LLM's
    ``core_attributes`` (kept to real, non-key members that are also suggested);
    when it gave none (older specs / the no-LLM fallback), degrade to the first few
    suggested extras. Always a small set — never the whole comprehensive list — so
    the UI recommends a minimum, not everything."""
    sugg_extras = [a for a in suggested if a and a != key_attr]
    picked = _dedupe([a for a in core if a and a != key_attr and a in sugg_extras])
    if not picked:
        picked = sugg_extras[:_DEFAULT_CORE_CAP]
    return picked[:_DEFAULT_CORE_CAP]


def _clarify_step(
    type_name: str, key_attr: str, core: list[str], note: str = ""
) -> PlanStep:
    """Ask which attributes to collect. Shows a SHORT recommended set (``core`` —
    the few most-important attributes), pre-selected, as clickable chips; the user
    can drop some, add their own, or keep just the name. The concrete list rides in
    ``options`` so whichever the user picks lands in the accumulated instruction and
    the next turn converges. The question stays terse and does NOT re-list the
    attributes — they're already the chips below it. The comprehensive fetch
    projection is chosen server-side (``hint_columns``), independent of this minimal
    recommendation, so a lean chip list never narrows what actually gets pulled.

    ``note`` is an optional leading advisory (e.g. the degraded-planning note) so a
    resolver-LLM failure is SURFACED in the question the user reads, not swallowed."""
    shown = _dedupe([key_attr, *core])
    question = (
        (f"{note}\n\n" if note else "")
        + f"I'll collect **{type_name}** records and always include **{key_attr}**. "
        "Pick the ones to collect below, add your own, or keep just the name."
    )
    options = [f"Use these: {', '.join(shown)}", f"Just the {key_attr}"]
    return PlanStep(
        capability=WebIngestCapability.name,
        action="clarify",
        params={"question": question, "options": options},
        rationale="Confirm the entity and attributes before fetching from the web.",
        confidence=1.0,
    )


async def _preview_shape(
    resolver, sample_rows: list[dict], existing_types: set[str]
) -> dict:
    """Run the SAME multi-type extractor the commit uses against the sample so the
    plan card ESTIMATES the ontology shape the ingest will mint: the distinct
    entity types (with their attributes + parent chain + is_new flag) and the
    relationships between them, mapped from entity ids to their types.

    This is an estimate from the small sample, not a guarantee — the extractor is
    non-deterministic and the full commit runs over many more records, so it may
    surface additional types/relationships or differ in detail. Mirrors the engine
    that document ingest routes through — instead of forcing one flat pre-named
    type. Caller wraps this in try/except so any extractor failure degrades to a
    flat single-type preview (the turn never 500s)."""
    extraction = await resolver._extract(
        json.dumps(sample_rows, default=str, ensure_ascii=False),
        "json",
        existing_types,
    )
    id_to_type: dict[str, str] = {e.id: e.type_name for e in extraction.entities}

    discovered: list[dict] = []
    seen_types: set[str] = set()
    for e in extraction.entities:
        if e.type_name in seen_types:
            continue
        seen_types.add(e.type_name)
        discovered.append(
            {
                "name": e.type_name,
                "attributes": [a.name for a in e.attributes],
                "parent_chain": list(e.parent_chain),
                "is_new": e.type_name not in existing_types,
            }
        )

    relationships: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for r in extraction.relationships:
        src = id_to_type.get(r.source_id)
        tgt = id_to_type.get(r.target_id)
        if not src or not tgt:
            continue
        edge = (src, r.predicate, tgt)
        if edge in seen_edges:
            continue
        seen_edges.add(edge)
        relationships.append({"source": src, "predicate": r.predicate, "target": tgt})

    return {"discovered_types": discovered, "relationships": relationships}


def _preview_summary(
    discovered_types: list[dict],
    relationships: list[dict],
    cap: int,
    *,
    degraded: bool,
) -> str:
    """The plan-card summary line.

    Normal path: frame the discovered types/edges as an ESTIMATE from the sample
    (only the column projection is stable preview→commit). Degraded path: the live
    preview couldn't render within the request budget (a slow/broad web source), so
    say that plainly and make clear the FULL discovery still runs on confirm — the
    user gets a confirmable plan instead of a timeout."""
    if degraded:
        return (
            "Couldn't fully preview this within the time limit — confirm to run "
            f"the full discovery in the background, capped at {cap} and staged for "
            "review."
        )
    return (
        f"Estimated ~{len(discovered_types)} type(s) and "
        f"{len(relationships)} relationship(s) from a sample (the full pull may "
        f"differ); capped at {cap}, staged for review."
    )


def _flat_shape(
    type_name: str, attributes: list[str], existing_types: set[str]
) -> dict:
    """Degraded preview when the multi-type extractor can't run: a single
    discovered type carrying the confirmed/suggested attributes, no relationships.
    Keeps the plan card confirmable so the turn never 500s."""
    return {
        "discovered_types": [
            {
                "name": type_name,
                "attributes": list(attributes),
                "parent_chain": [],
                "is_new": type_name not in existing_types,
            }
        ],
        "relationships": [],
    }


# --- helpers ----------------------------------------------------------------- #


def _provider_context(ctx: AgentContext) -> dict:
    return {
        "tenant_id": ctx.tenant_id,
        "kg_name": ctx.kg_name,
        "type_name": ctx.type_name,
    }


def _build_resolver(ctx: AgentContext, *, ontology_lock: "asyncio.Lock | None" = None):
    """Build a SchemaResolver from the agent context (same wiring the ingest
    route uses). Constructed per call — cheap, and keeps no cross-request state.

    ``ontology_lock`` (ONTA-268): pass ONE shared lock to every per-sub-query
    resolver in a discovery job so their ontology mutations serialize (no
    type-creation race). ``None`` → the resolver makes its own private lock."""
    import tempfile
    from pathlib import Path

    from cograph_client.resolver.schema_resolver import SchemaResolver
    from cograph_client.resolver.verdict_cache import JsonVerdictCache

    cache = JsonVerdictCache(Path(tempfile.gettempdir()) / "omnix-verdict-cache.json")
    return SchemaResolver(
        neptune=ctx.neptune,
        anthropic_key=ctx.anthropic_key,
        verdict_cache=cache,
        ontology_lock=ontology_lock,
    )


# Leading filler we can safely drop so the provider sees a cleaner query. We also
# strip a leading "Use these:" / "just the …" confirmation prefix so the cleaned
# query is the discovery subject, not the attribute reply.
_LEAD_FILLER = re.compile(
    r"^\s*(?:i['’]?m\s+looking\s+for|i\s+want|i\s+need|please\s+|can\s+you\s+|"
    r"could\s+you\s+|find\s+me|find|get\s+me|get|pull|fetch|add|search\s+for)\s+"
    r"(?:a\s+|an\s+|the\s+|me\s+)?",
    re.IGNORECASE,
)


def _empty_sample_message(query: str, urls: list[str], sample) -> str:
    """The user-facing message when a discovery SAMPLE came back with no rows.

    URL mode and query mode fail for DIFFERENT reasons and warrant DIFFERENT
    advice, so we never tell a user who pasted a specific page to "rephrase their
    search" (the old bug — a search-flavoured dead-end shown after a URL scrape):

    * URL mode + provider ERROR (``DiscoverResult.error`` set) → we couldn't READ
      the page(s): surface the reason and suggest retry, not rephrasing.
    * URL mode + no error → we read the page(s) but found no extractable records:
      the page may render its data in a way we can't parse, or hold no list.
    * query mode → an open-web search genuinely found nothing: rephrase/narrow.
    """
    err = getattr(sample, "error", None)
    if urls:
        target = urls[0] if len(urls) == 1 else f"the {len(urls)} pages you shared"
        if err:
            return (
                f"I couldn't read {target}: {err}. The page may be blocking "
                "automated reading or be temporarily unavailable — try again in a "
                "moment, or share a different link."
            )
        return (
            f"I reached {target} but couldn't find a list or table of records to "
            "pull from it. The data may be rendered in a way I can't parse, or the "
            "page may not hold a structured list — try a page whose main content "
            "is the records you want."
        )
    return (
        f"I couldn't find anything on the web for “{query}”. "
        "Try rephrasing or narrowing it."
    )


# A leading META-FRAMING clause the user prepends to steer routing rather than to
# name the search subject — "this is a new discovery task, not enrichment — …",
# "note: not enrichment, …". Left in the query it leaks into the search string
# (persona-eval RCA: the executed job searched for "This is a new discovery task,
# not enrichment…"). We strip such a clause up to its trailing separator (dash /
# colon / semicolon / comma) so the REAL subject after it survives. Conservative:
# only fires on an explicit discovery/enrichment self-label, so a normal query is
# untouched. Case-insensitive.
_META_FRAMING_RE = re.compile(
    r"^\s*(?:note[:,]?\s*)?(?:this\s+is\s+)?(?:a\s+)?"
    r"(?:new\s+discovery(?:\s+task)?|not\s+(?:an?\s+)?enrichment"
    r"|discovery\s+task)\b[^-:;.]*[-:;,]\s*",
    re.IGNORECASE,
)


def _clean_query(instruction: str) -> str:
    """Best-effort tidy of the instruction into a discovery query. Uses the FIRST
    line (the original ask), strips a leading routing META-FRAME ("this is a new
    discovery task, not enrichment — …") if present, then one leading filler phrase,
    so the executed query is the SUBJECT, never the user's meta-correction."""
    if not instruction:
        return ""
    first = next(
        (ln.strip() for ln in instruction.splitlines() if ln.strip()),
        instruction.strip(),
    )
    # Drop a leading discover-vs-enrich self-label so it never becomes the search
    # string; keep looping in case the user stacked two (rare).
    stripped = first
    for _ in range(2):
        nxt = _META_FRAMING_RE.sub("", stripped, count=1).strip()
        if nxt == stripped:
            break
        stripped = nxt
    q = _LEAD_FILLER.sub("", stripped, count=1).strip()
    return q or stripped or first


def _estimate_cost(
    provider: WebSourceProvider, estimated_total: int, cap: int,
    *, subqueries: int = 0,
) -> dict:
    """Plan-time cost estimate, using the SAME contract keys the plan card reads
    (``estimated_usd`` / ``paid_calls`` / ``note``).

    ``cost_per_call`` is the cost of ONE paid REQUEST. A provider that FANS OUT a
    run across paginated requests declares ``rows_per_call`` (records per request);
    we then price the whole run as ``cost_per_call × ceil(rows / rows_per_call)``
    instead of a single call — so a multi-page pull isn't under-quoted. Unset /
    ``0`` ``rows_per_call`` means "one paid call per run" (the default), so a
    single-call provider is unchanged.

    ``subqueries`` (0/1 = single-query run) prices an ENUMERATION fan-out: the row
    cap splits across the sub-queries, each priced as its own run — a paginating
    provider costs ≈ the same total pages, a single-call-per-run provider costs one
    call per sub-query."""
    is_paid, cost_per_call = provider_cost(provider)
    rows = min(estimated_total or 0, cap) if cap else (estimated_total or 0)
    if not is_paid:
        return {
            "paid_calls": 0,
            "estimated_usd": 0.0,
            "note": "No paid calls (the configured web source is free).",
        }
    # How many paid REQUESTS the run fans out into: one per rows_per_call records
    # (rounded up), min 1 — per SUB-QUERY when the run is an enumeration fan-out
    # (each sub-query gets an equal share of the row cap and is billed as its own
    # run). A provider that doesn't paginate (rows_per_call unset/0) is one billed
    # call per run — the previous behavior.
    n_sub = max(1, int(subqueries or 0))
    per_sub_rows = math.ceil(rows / n_sub) if rows else rows
    paid_calls = n_sub * _paid_call_count(provider, per_sub_rows)
    estimated_usd = round(cost_per_call * paid_calls, 4)
    fanout = (
        f" across ~{paid_calls} paginated request(s)" if paid_calls > n_sub else ""
    )
    split = f" across {n_sub} sub-queries" if n_sub > 1 else ""
    return {
        "paid_calls": paid_calls,
        "paid_calls_estimated": True,
        "estimated_usd": estimated_usd,
        "per_call_cost_usd": round(cost_per_call, 4),
        "note": (
            f"Paid web discovery via '{provider.name}': ≈ ${estimated_usd:.2f} "
            f"to fetch up to {rows} record(s){split}{fanout} (estimate; provider "
            f"may fan out across sub-queries)."
        ),
    }


def _estimate_cost_multi(
    providers: list, estimated_total: int, cap: int, *, subqueries: int = 0,
) -> dict:
    """Whole-run estimate for a provider ENSEMBLE (kind-specialized + general
    consulted together): the sum of each provider's own run estimate, with one
    merged note naming every source generically. A single-provider ensemble is
    exactly :func:`_estimate_cost` — no behavior change for the classic path."""
    if len(providers) == 1:
        return _estimate_cost(
            providers[0], estimated_total, cap, subqueries=subqueries
        )
    parts = [
        _estimate_cost(p, estimated_total, cap, subqueries=subqueries)
        for p in providers
    ]
    paid_calls = sum(part["paid_calls"] for part in parts)
    estimated_usd = round(sum(part["estimated_usd"] for part in parts), 4)
    if paid_calls == 0:
        return {
            "paid_calls": 0,
            "estimated_usd": 0.0,
            "note": "No paid calls (the configured web sources are free).",
        }
    rows = min(estimated_total or 0, cap) if cap else (estimated_total or 0)
    names = " + ".join(f"'{p.name}'" for p in providers)
    return {
        "paid_calls": paid_calls,
        "paid_calls_estimated": True,
        "estimated_usd": estimated_usd,
        "note": (
            f"Paid web discovery via {names}: ≈ ${estimated_usd:.2f} to fetch "
            f"up to {rows} record(s) across {len(providers)} sources (estimate; "
            f"providers may fan out across sub-queries)."
        ),
    }


def _paid_call_count(provider: WebSourceProvider, rows: int) -> int:
    """Number of paid REQUESTS a run of ``rows`` records fans out into.

    Generic pagination pricing: a provider that yields ``rows_per_call`` records
    per paid request bills ``ceil(rows / rows_per_call)`` requests (min 1). Read
    ``rows_per_call`` defensively (default 0 → one billed call for the whole run,
    the backward-compatible behavior for a non-paginating provider). Never raises
    on a malformed value; coerces to the single-call default."""
    try:
        per = int(getattr(provider, "rows_per_call", 0) or 0)
    except (TypeError, ValueError):
        per = 0
    if per <= 0 or rows <= 0:
        return 1
    return max(1, math.ceil(rows / per))


# --- per-record source-URL provenance (ONTA-151) ----------------------------- #

# Attribute minted on each discovered entity citing the exact page it was drawn
# from — the discovery counterpart to enrichment's `<attr>_source_url` citations
# and the user-facing source the Explorer renders (any URL-valued attribute is a
# clickable link in the records table). The run-level provenance the resolver
# already writes (`onto/source` = web:<provider>:<query>, `onto/ingested_at`, the
# batch id) is unchanged; this adds the missing PER-RECORD citation so "this exact
# data point came from this exact page" is answerable, not just "this came from a
# discovery for query X".
#
# Threaded as an ordinary row field so it flows through the SAME ingest →
# insert_facts write path as every other attribute (write-path convergence) — no
# bespoke writer, no separate provenance graph. NOTE on the reliability contract:
# unlike enrichment, which writes `<attr>_source_url` DETERMINISTICALLY onto the
# entity URI (no LLM), discovery carries `source_url` as a row field THROUGH the
# multi-type LLM extractor. `uri` is a declared attribute datatype, so a field
# named `source_url` is overwhelmingly kept as a literal at temperature 0.
#
# CITATION MIS-BINDING FIX (persona-eval RCA): the previously-open risk was CROSS-
# RECORD placement — when one ingest batch mixed rows from several pages, the
# extractor could copy page A's `source_url` onto an entity minted from page B
# (observed: one page-level URL broadcast across every model on the page). We now
# commit one `resolver.ingest` call PER distinct source URL (see
# ``_group_rows_by_source_url`` + the sub-batch loop), so an extraction only ever
# sees rows that share ONE page — the only URL it can stamp on any entity it mints
# is that page's URL. The citation is therefore bound deterministically to the
# originating source record by the PARTITION, not by the LLM re-deciding placement.
# (When a single page genuinely lists N distinct entities, they all correctly cite
# that one page — which is the intended page-level citation, not a mis-bind.)
SOURCE_URL_ATTR = "source_url"


def _row_source_url(
    row: dict, index: int, provenance: dict[str, str]
) -> Optional[str]:
    """Resolve the source URL a discovered ``row`` was drawn from, using the
    provider's per-row ``provenance`` map (:attr:`DiscoverResult.provenance`).

    Providers key the map by the row's natural name, falling back to the row's
    positional index as a string — the convention every bundled adapter and the
    stub use (``{r.get("name", str(i)): url}``). Mirror that exact key here (name
    when the row carries one, else the index), then fall back to the positional
    index so an index-keyed provider also resolves. Returns ``None`` when no URL
    is known for the row (e.g. a free/stub provider that supplied no provenance).

    ORDERING CONTRACT (ONTA-256): the positional-index fallback is only sound
    while ``index`` still matches the row's ORIGINAL position in the provider's
    output. Callers MUST resolve/stamp the URL BEFORE any step that reindexes the
    list (e.g. :func:`_dedupe_rows` dropping rows) — see
    :func:`_dedupe_rows_with_source_urls`. Re-deriving by position on a reindexed
    survivor binds it to a dropped neighbour's page."""
    if not provenance or not isinstance(row, dict):
        return None
    key = row.get("name", str(index))
    url = provenance.get(str(key))
    if url:
        return url
    return provenance.get(str(index))


def _attach_source_urls(rows: list[dict], provenance: dict[str, str]) -> int:
    """Stamp each discovered row (in place) with its per-record ``source_url`` so
    the entity it mints carries a traceable citation to its origin page. Returns
    the number of rows stamped.

    A no-op when the provider supplied no provenance (free/stub providers may omit
    it). Never clobbers a ``source_url`` the provider already set on the row, and
    leaves a row with no resolvable URL untouched rather than stamping a blank — so
    the column appears only where there is a real citation to show."""
    if not provenance:
        return 0
    stamped = 0
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or row.get(SOURCE_URL_ATTR):
            continue
        url = _row_source_url(row, i, provenance)
        if url:
            row[SOURCE_URL_ATTR] = url
            stamped += 1
    return stamped


# --- job tracking ------------------------------------------------------------ #


def _step_cost(step: PlanStep) -> tuple[Optional[float], Optional[str]]:
    """Pull the plan card's cost estimate (estimated_usd + note) off the step so
    it can be stamped on the job — that's the "how much did it cost" detail the
    job-status view shows. Returns (usd, note); either may be None."""
    cost = step.cost or {}
    usd = cost.get("estimated_usd")
    note = cost.get("note")
    usd_f = (
        float(usd)
        if isinstance(usd, (int, float)) and not isinstance(usd, bool)
        else None
    )
    return usd_f, (str(note) if note else None)


def _host(url: str) -> str:
    """Hostname of a URL with a leading ``www.`` dropped; a bare token (already a
    host/provider name) is returned trimmed/lower-cased. '' if unparseable."""
    try:
        netloc = urlparse(url).netloc
    except Exception:  # noqa: BLE001 — never let URL parsing break a run
        netloc = ""
    host = (netloc or url or "").strip().lower()
    return host[4:] if host.startswith("www.") else host


# Cap on request-level traces persisted PER PROVIDER per run, so a heavy
# sub-query fan-out (many pages × many sub-queries) can't bloat the stored job.
_MAX_REQUEST_TRACES_PER_PROVIDER = 200


def _record_requests(plog: ProviderLog, calls) -> None:
    """Accumulate the per-request traces a provider's ``discover()`` returned onto
    its ``ProviderLog``, capped so the persisted job stays bounded. ``calls`` is
    the list of plain dicts from ``DiscoverResult.calls`` — API-source (registry)
    providers populate it; web-search providers pass ``None``/empty. Malformed
    entries are skipped defensively; a bad trace never sinks the run."""
    if not calls:
        return
    for c in calls:
        if len(plog.requests) >= _MAX_REQUEST_TRACES_PER_PROVIDER:
            logger.info(
                "web_ingest_request_trace_truncated",
                provider=plog.provider,
                cap=_MAX_REQUEST_TRACES_PER_PROVIDER,
            )
            break
        try:
            plog.requests.append(
                ApiRequestTrace(**c)
                if isinstance(c, dict)
                else ApiRequestTrace.model_validate(c)
            )
        except Exception:  # noqa: BLE001 — a malformed trace is not fatal
            continue


def _record_locate_trace(job, locate_trace, provider_name: str, sub_query: str) -> None:
    """Surface a locate-then-scrape provider's ``locate → select_urls → fetch`` step
    counts as P1 stage-trace actions (ONTA-391).

    A provider that searches only to LOCATE list/directory pages and then scrapes a
    few sets ``DiscoverResult.locate_trace`` = ``{locate_calls, urls_located,
    urls_selected, pages_fetched, escalated, skip_reason, locate_errors?}``. We
    project those into the operator Job Trace so P1 shows the page-MINIMISATION
    work — the number of search calls, candidate URLs, URLs selected, and pages
    actually fetched — instead of only the terminal ``source_bundle rows=N``. This
    is the direct evidence for "search located pages; we scraped a FEW", the
    ONTA-391 objective.

    Hard locate failures (``locate_errors``: Parallel 422, Gemini 429, …) get an
    explicit ``locate_error`` action so the webapp Job Trace never looks like a
    clean empty search when the locate API actually rejected the request.

    A no-op when there is no job/recorder, or when ``locate_trace`` is None (an
    enumeration provider that never locates+scrapes). Wrapped so observability never
    sinks discovery — the same contract as ``_record_requests``."""
    if job is None or not isinstance(locate_trace, dict):
        return
    try:
        rec = attach_recorder(job)
        if rec is None:
            return
        lt = locate_trace
        sq = (sub_query or "")[:200]
        base_meta = {"provider": provider_name, "sub_query": sq}
        locate_errs = [str(e) for e in (lt.get("locate_errors") or []) if e]
        rec.action(
            StageProjectId.p1,
            "locate",
            detail=(
                f"search calls={lt.get('locate_calls', 0)} "
                f"urls_found={lt.get('urls_located', 0)}"
                + (f" errors={len(locate_errs)}" if locate_errs else "")
            ),
            meta={
                **base_meta,
                "locate_calls": lt.get("locate_calls", 0),
                "urls_located": lt.get("urls_located", 0),
                "escalated": bool(lt.get("escalated", False)),
                "locate_errors": locate_errs[:8] if locate_errs else [],
            },
        )
        # Surface each hard locate API failure as its own action so the Job Trace
        # UI shows "Parallel HTTP 422" / "Gemini HTTP 429" as first-class steps,
        # not only buried under a soft locate_miss skip_reason.
        for err in locate_errs[:8]:
            rec.action(
                StageProjectId.p1,
                "locate_error",
                detail=str(err)[:200],
                meta={**base_meta, "error": str(err)[:200]},
            )
        rec.action(
            StageProjectId.p1,
            "select_urls",
            detail=(
                f"selected={lt.get('urls_selected', 0)} of "
                f"{lt.get('urls_located', 0)} candidate urls"
            ),
            meta={**base_meta, "urls_selected": lt.get("urls_selected", 0)},
        )
        # ONTA-395: surface extract_mode (agent | deterministic | empty |
        # agent_failed) + trim_chars so Job Trace shows whether the agent path
        # ran / failed instead of only pages_fetched. Absent keys stay omitted
        # so enumeration providers' traces are unchanged.
        extract_mode = lt.get("extract_mode")
        trim_chars = lt.get("trim_chars")
        fetch_meta = {
            **base_meta,
            "pages_fetched": lt.get("pages_fetched", 0),
        }
        if extract_mode is not None:
            fetch_meta["extract_mode"] = extract_mode
        if trim_chars is not None:
            fetch_meta["trim_chars"] = trim_chars
        fetch_detail = (
            f"pages_fetched={lt.get('pages_fetched', 0)}"
            + (" (escalated)" if lt.get("escalated") else "")
        )
        if extract_mode:
            fetch_detail += f" extract_mode={extract_mode}"
        rec.action(
            StageProjectId.p1,
            "fetch",
            detail=fetch_detail,
            meta=fetch_meta,
        )
        skip = lt.get("skip_reason")
        if skip:
            # Honest miss — located no fetchable list page (or pages had no rows),
            # OR locate APIs failed (skip_reason then carries the API errors).
            # Recorded so the trace shows WHY A1 is empty, not a silent gap.
            action_name = "locate_error" if locate_errs else "locate_miss"
            rec.action(
                StageProjectId.p1,
                action_name,
                detail=str(skip)[:200],
                meta={
                    **base_meta,
                    "skip_reason": str(skip)[:200],
                    "locate_errors": locate_errs[:8] if locate_errs else [],
                },
            )
    except Exception:  # noqa: BLE001 — observability must never sink discovery
        logger.warning("web_ingest_locate_trace_record_failed", exc_info=True)


def _platforms(sources, provider) -> list[str]:
    """Distinct platforms consulted during a discovery run — the host of each
    source URL (de-duplicated, order-preserved, capped), falling back to the
    provider name when no URLs were returned. Surfaced in the job-details view
    as "what platforms were used"."""
    out: list[str] = []
    seen: set[str] = set()
    for s in sources or []:
        host = _host(str(s))
        if host and host not in seen:
            seen.add(host)
            out.append(host)
        if len(out) >= 8:
            break
    if not out:
        name = (getattr(provider, "name", "") or "").strip()
        if name:
            out.append(name)
    return out


def _build_stage_contracts(
    *,
    a1_acc: Optional[dict],
    a1_rows_dropped: int = 0,
    a1_cells_scrubbed: int = 0,
    a1_drop_reasons: Optional[list] = None,
    a2_extracted: int,
    a2_resolved: int,
    a2_source_rows: int,
    a2_batches: int,
    a2_structured_batches: int,
    a3_counts: Optional[dict],
    a3_drop_reasons: list,
    a3_transforms_sample: list,
    a4_verified_count: int,
    a6_fact_count: int,
    a6_fan_in_count: int,
    a6_triples: int,
    a6_facts_sample: list,
    a6_run_id: Optional[str],
    a6_instance_graph: Optional[str],
    entities_written: int,
    focus_type: Optional[str],
    focus_attributes: list,
    run_id: Optional[str],
) -> dict:
    """Assemble terminal Notion-contract-shaped I/O for P1/P2/P3/P6 (ONTA-385)."""
    a1 = dict(a1_acc or {})
    if a1:
        a1.setdefault("artifact", "A1")
        a1.setdefault("name", "Source Bundle")
        a1.setdefault("run_id", run_id)
    else:
        a1 = {
            "artifact": "A1",
            "name": "Source Bundle",
            "run_id": run_id,
            "row_count": 0,
            "bundles_emitted": 0,
        }
    # A1 validators (ONTA-393): surface the nav-chrome / type-invalid rejections on
    # the terminal A1 contract only when there were any, so a clean run's contract is
    # byte-identical to pre-393.
    if a1_rows_dropped or a1_cells_scrubbed:
        a1["rows_dropped"] = int(a1_rows_dropped)
        a1["cells_scrubbed"] = int(a1_cells_scrubbed)
        a1["drop_reasons_sample"] = list(a1_drop_reasons or [])[:8]
    a2 = summarize_a2_candidates(
        entities_extracted=a2_extracted,
        entities_resolved=a2_resolved,
        source_row_count=a2_source_rows,
        focus_type=focus_type,
        focus_attributes=focus_attributes,
        run_id=run_id,
        soft_typed=True,
        evidence_linked=True,
        structured_fastpath=a2_structured_batches > 0
        and a2_structured_batches == a2_batches
        and a2_batches > 0,
        batches=a2_batches,
    )
    a3: Optional[dict] = None
    if a3_counts and int(a3_counts.get("total") or 0) > 0:
        a3 = {
            "artifact": "A3",
            "name": "Clean Facts",
            "counts": a3_counts,
            "drop_reasons_sample": list(a3_drop_reasons)[:8],
            "transforms_sample": list(a3_transforms_sample)[:8],
        }
    a4: Optional[dict] = None
    if a4_verified_count > 0:
        a4 = {
            "artifact": "A4",
            "name": "Verified Facts",
            "verified_count": a4_verified_count,
        }
    a6 = {
        "artifact": "A6",
        "name": "Graph Delta",
        "run_id": a6_run_id or run_id,
        "instance_graph": a6_instance_graph,
        "fact_count": a6_fact_count,
        "fan_in_count": a6_fan_in_count,
        "entities_written": entities_written,
        "triples_inserted": a6_triples,
        "status": "applied",
        "facts_sample": list(a6_facts_sample)[:3],
    }
    return {"a1": a1, "a2": a2, "a3": a3, "a4": a4, "a6": a6}


async def _finish_job(
    job: Optional[EnrichJob],
    job_store,
    *,
    processed: int,
    entities: int,
    platforms: list[str],
    stage_contracts: Optional[dict] = None,
) -> None:
    """Mark a discovery job applied with its result count + final progress."""
    if job is None or job_store is None:
        return
    now = datetime.now(timezone.utc)
    job.progress.processed = processed
    job.progress.filled = entities
    # Settle the rolling ``total`` estimate to the exact processed count on EVERY
    # terminal-applied path (ONTA-238). The non-empty happy path settles it just
    # before calling this; the EMPTY (0-row) path does not, so without this a
    # completed-empty job would keep the early ``total = cap`` seed and read as a
    # misleading ``0/200`` (looks unfinished) instead of ``0/0``. Settling here
    # makes the invariant caller-independent.
    job.progress.total = processed
    # Terminal phase (ONTA-238): a completed job reads "done", so a client that
    # keyed a spinner off the phase can retire it. Paired with the terminal
    # ``applied`` status + ``result_count``, a completed-EMPTY run (0 records) is
    # now fully distinguishable from a still-running one — same terminal status,
    # phase "done", result_count 0, progress 0/0 — instead of looking identical
    # to "running".
    job.progress.phase = "done"
    job.result_count = entities
    if platforms:
        job.platforms = platforms
    job.status = JobStatus.applied
    # A9 manifest: settle to a terminal COMPLETED state. complete() collapses the
    # seeded cap denominator down to what actually ran, so a clean run reads
    # "N of N — complete", never "N of cap — dropped".
    if job.manifest is not None:
        job.manifest.complete()
    job.completed_at = now
    job.last_run = now
    try:
        rec = attach_recorder(job)
        if rec is not None:
            contracts = stage_contracts or {}
            a1 = contracts.get("a1") or {
                "artifact": "A1",
                "name": "Source Bundle",
                "row_count": processed,
            }
            a2 = contracts.get("a2") or summarize_a2_candidates(
                entities_resolved=entities,
                source_row_count=processed,
                focus_type=job.type_name,
                focus_attributes=list(job.attributes or []),
            )
            a3 = contracts.get("a3")
            a4 = contracts.get("a4")
            a6 = contracts.get("a6") or summarize_a6_graph_delta(
                entities_written=entities,
                status="applied",
            )

            # P1 Find → A1 Source Bundle
            rec.end(
                StageProjectId.p1,
                output={
                    **a1,
                    "result_count": entities,
                    "platforms": platforms,
                    "processed": processed,
                },
            )
            # P2 Extract → A2 Candidate Facts
            rec.end(
                StageProjectId.p2,
                output={
                    **a2,
                    "processed": processed,
                    "entities_written": entities,
                },
            )
            # P3 Clean → A3 Clean Facts (complete only when a clean ledger ran).
            # Use end(..., skipped) so a mid-run begin(P3) cannot leave P3 running.
            if a3 and int((a3.get("counts") or {}).get("total") or 0) > 0:
                rec.end(StageProjectId.p3, output=a3)
            else:
                rec.end(
                    StageProjectId.p3,
                    status=StageStatus.skipped,
                    output={
                        "skip_reason": (
                            "no A3 clean ledger on this run "
                            "(empty ingest or clean fused with zero values)"
                        )
                    },
                )
            # P4 Verify → A4 (default-OFF; complete only when verdicts present)
            if a4 and int(a4.get("verified_count") or 0) > 0:
                rec.end(StageProjectId.p4, output=a4)
            else:
                rec.end(
                    StageProjectId.p4,
                    status=StageStatus.skipped,
                    output={
                        "skip_reason": (
                            "verify default-OFF on discovery path (no A4 verdicts)"
                        )
                    },
                )
            # P5 Ontology / Placement stays fused into resolver ingest
            rec.end(
                StageProjectId.p5,
                status=StageStatus.skipped,
                output={
                    "skip_reason": (
                        "type placement happens inside resolver ingest (no separate A5)"
                    )
                },
            )
            # P6 Write → A6 Graph Delta
            rec.end(
                StageProjectId.p6,
                output={
                    **a6,
                    "entities_written": entities,
                    "status": "applied",
                },
            )
            rec.end(
                StageProjectId.p0,
                output={
                    "status": "applied",
                    "result_count": entities,
                    "processed": processed,
                    "platforms": platforms,
                    "cost": job.cost,
                    "run_id": a1.get("run_id") or a6.get("run_id"),
                },
            )
            # Rails not on the discovery write path stay skipped.
            for pid, reason in (
                (StageProjectId.p7, "answer rail not on discovery jobs"),
                (StageProjectId.p8, "not a refresh-delta run"),
                (StageProjectId.p9, "surface is the Jobs UI; no A10 on this path"),
            ):
                rec.skip(pid, reason=reason)
            # ONTA-394: entity fan-out ratio (a2 extracted / a1 rows). Surfaced on
            # the trace summary + warned when high, so soft-extract amplification is
            # never silent. Observability only — nothing is dropped here.
            _a1_rows = a1.get("row_count") or 0
            _a2_extracted = a2.get("entities_extracted") or 0
            _fanout_ratio = (
                round(_a2_extracted / _a1_rows, 2) if _a1_rows else None
            )
            _fanout_high = bool(
                _fanout_ratio is not None
                and _fanout_ratio > _DISCOVERY_FANOUT_WARN_RATIO
            )
            if _fanout_high:
                logger.warning(
                    "discovery_high_entity_fanout",
                    job_id=getattr(job, "id", None),
                    a1_row_count=_a1_rows,
                    a2_entities_extracted=_a2_extracted,
                    fanout_ratio=_fanout_ratio,
                    threshold=_DISCOVERY_FANOUT_WARN_RATIO,
                )
            job.stage_trace.summary = {
                "result_count": entities,
                "processed": processed,
                "platforms": platforms,
                "type_name": job.type_name,
                "attributes": job.attributes,
                "cost": job.cost,
                "a1_row_count": a1.get("row_count"),
                "a2_entities_extracted": a2.get("entities_extracted"),
                "entity_fanout_ratio": _fanout_ratio,
                "entity_fanout_high": _fanout_high,
                "a3_counts": (a3 or {}).get("counts") if a3 else None,
                "a6_fact_count": a6.get("fact_count"),
                "run_id": a1.get("run_id") or a6.get("run_id"),
            }
            job.stage_trace.status = "applied"
            # Safety sweep (ONTA-388): end any leftover running/pending stages.
            finalize_job_stage_trace(
                job,
                terminal_status="applied",
                summary={
                    "result_count": entities,
                    "processed": processed,
                    "platforms": platforms,
                    "type_name": job.type_name,
                    "attributes": job.attributes,
                    "cost": job.cost,
                },
            )
    except Exception:
        logger.warning(
            "stage_trace_finish_failed",
            job_id=getattr(job, "id", None),
            exc_info=True,
        )
    await job_store.update(job)


def _finalize_stage_trace_failed(
    job: EnrichJob,
    error: str,
    *,
    summary: Optional[dict] = None,
) -> None:
    """Stamp an honest terminal-failed stage_trace (delegates to pipeline helper).

    Ends every non-terminal project so mid-run failures never leave P2/P6 stuck
    as ``running`` on a failed job. Isolated in try/except inside the shared
    helper so operator observability cannot fail the discovery write path.
    """
    finalize_job_stage_trace(
        job,
        terminal_status="failed",
        error=error,
        summary={"type_name": getattr(job, "type_name", None), **(summary or {})},
    )


async def _fail_job(job: Optional[EnrichJob], job_store, error: str) -> None:
    """Mark a discovery job failed, carrying a (truncated) error for the UI."""
    if job is None or job_store is None:
        return
    now = datetime.now(timezone.utc)
    job.status = JobStatus.failed
    job.progress.phase = "failed"
    job.error = (error or "discovery failed")[:500]
    # A9 manifest: terminal FAILED with the reason. Any planned items not completed
    # are rolled into `dropped`, so coverage shows the partial honestly.
    if job.manifest is not None:
        job.manifest.halt(HaltReasonKind.error, job.error)
    job.completed_at = now
    job.last_run = now
    _finalize_stage_trace_failed(job, job.error)
    await job_store.update(job)


async def _fail_billing_job(
    job: Optional[EnrichJob],
    job_store,
    provider_logs: list[ProviderLog],
    error: RetrievalError,
    *,
    processed: int,
    platforms: list[str],
) -> None:
    """Fail a discovery job on a FATAL run-level abort — an LLM billing/auth error
    (402/401, ONTA-201) OR a cost-ceiling breach (ONTA-282) — recording HONEST
    PARTIALS.

    Unlike :func:`_fail_job`, this fires when the run ABORTED mid-way: the shared
    LLM backend went unbillable/unauthorized, or the run reached its HARD per-run
    spend envelope. Either way some batches may already have landed, so the
    terminal state must reflect rows-LANDED vs rows-LOST — never a silent
    "complete". ``halt_from_exception`` derives the manifest's reason KIND from the
    error type (``billing`` / ``cost_ceiling``), so a ceiling abort reads "cost
    envelope exceeded", not "provider exhaustion". We stamp:

    * the clear, user-facing ``error`` message (top up / rotate the key);
    * the per-provider logs so the run detail still shows what each source did;
    * an ``error_summary`` ``JobErrorItem`` (``kind="job"`` — a run-level backend
      failure, not any one provider's fault) whose message names the rows that DID
      land, so the partial is explicit;
    * ``progress`` settled to what actually landed (processed == filled).
    """
    if job is None or job_store is None:
        return
    now = datetime.now(timezone.utc)
    landed = (
        f" {processed} record(s) were ingested before the failure; "
        "the remaining batches were not processed."
        if processed
        else " No records were ingested."
    )
    message = f"{error}{landed}"
    job.status = JobStatus.failed
    job.progress.phase = "failed"
    job.error = message[:500]
    job.provider_logs = list(provider_logs)
    job.error_summary = [
        JobErrorItem(provider=None, kind="job", message=message[:300])
    ]
    # Settle the rolling estimate to the exact partial count — the run is NOT
    # complete, but the count of what survived must be honest.
    job.progress.processed = processed
    job.progress.filled = processed
    job.result_count = processed
    if platforms:
        job.platforms = platforms
    # A9 manifest: terminal FAILED with a PROVIDER-EXHAUSTION reason (402 billing /
    # sustained-429 rate-limit) and honest partial coverage. `completed` already
    # tracks the landed rows (recorded per micro-batch); `halt_from_exception`
    # rolls the unfilled planned remainder into `dropped` and stamps the reason.
    if job.manifest is not None:
        job.manifest.halt_from_exception(error, landed_note=landed)
    job.completed_at = now
    job.last_run = now
    _finalize_stage_trace_failed(
        job,
        job.error or str(error),
        summary={
            "processed": processed,
            "platforms": platforms,
            "result_count": processed,
            "halt": "billing_or_cost_ceiling",
        },
    )
    await job_store.update(job)


def _answer_step(text: str) -> PlanStep:
    """A single no-write 'answer' step (planner short-circuits it to kind:answer)."""
    return PlanStep(
        capability=WebIngestCapability.name,
        action="answer",
        params={"answer_payload": {"answer": text, "narrative": text}},
        rationale=text,
        confidence=1.0,
    )


def _parse_json_object(text: str) -> dict | None:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = "\n".join(
            l for l in stripped.split("\n") if not l.strip().startswith("```")
        )
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        stripped = stripped[start : end + 1]
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _as_list(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v]
    return []


def _slug(v) -> str:
    """snake_case a single attribute name; drop surrounding junk."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(v or "").strip().lower()).strip("_")
    return s


def _pascal(v: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", str(v or "").strip())
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or "WebRecord"


# The user's explicitly-named record TYPE, introduced by a discovery verb + the
# "records"/"entities" noun ("add Widget records", "discover Sprocket entities")
# or a "<Type> with <fields>" list frame ("Gadget records with sku, color").
# Deliberately CONSERVATIVE (high precision): the type token is 1-3 capitalized /
# identifier words captured immediately before "records"/"entities" or before a
# field-list "with". This only ever OVERRIDES the WebRecord placeholder, so a false
# negative is harmless (we keep WebRecord and clarify as today) and a false
# positive is bounded — it can't corrupt a real LLM-resolved type. Case-sensitive
# on the leading capital so a lowercased entity phrase ("collect the physicians")
# is NOT mistaken for a type name. Never overfit to a specific domain term.
# Words that lead a discovery ask but are NOT the type — excluded from the type
# capture so "Add Widget records" yields "Widget", never "AddWidget". The type
# token immediately precedes "records"/"entities"/"rows" (or the "<Type> with"
# field frame) and is 1-3 Capitalized words, none of them a lead verb / article.
_TYPE_STOPWORDS = frozenset(
    {
        "add", "discover", "find", "pull", "fetch", "get", "grab", "collect",
        "ingest", "import", "gather", "scrape", "the", "a", "an", "all", "these",
        "some", "more", "new",
    }
)
_TYPE_TOKEN = r"[A-Z][A-Za-z0-9]*(?:[ _-][A-Z][A-Za-z0-9]*){0,2}"
_EXPLICIT_TYPE_RE = re.compile(
    rf"\b(?:add|discover|find|pull|fetch|get|grab|collect|ingest|import|gather|scrape)\b"
    rf"[^.\n]*?\b({_TYPE_TOKEN})\s+(?:records?|entities|rows)\b",
)
_TYPE_WITH_FIELDS_RE = re.compile(
    rf"\b({_TYPE_TOKEN})\s+(?:records?\s+)?with\b",
)

# The "each <noun> record …" frame — a caller describing the SHAPE of the dataset
# ("each **model** record needs …", "every **product** entity should have …")
# names the record type explicitly even when the noun is lowercase, so the
# capitalized-only frames above miss it (the persona-eval RCA: the LLM degraded to
# WebRecord and "each model record needs …" left it there). "each"/"every" + a
# single common-noun word + "record"/"entity"/"row" is a strong, unambiguous "this
# is the per-record type" signal — far tighter than a bare entity phrase, so it
# won't fire on "collect the coffee shops". The noun is a single ``[a-z]`` word
# (an adjective before it, "each voice model record", is dropped — we take the word
# ADJACENT to the record noun); a stopword there ("each one record") is rejected by
# the caller's stopword filter. We singularize a trailing plural and PascalCase it.
_EACH_RECORD_TYPE_RE = re.compile(
    r"\b(?:each|every|per|a)\s+([a-z][a-z0-9]*)\s+(?:records?|entities|entity|rows?)\b",
    re.IGNORECASE,
)


def _singularize(word: str) -> str:
    """Best-effort English singularization for a type noun: "companies" → "company",
    "boxes" → "box", "models" → "model". Conservative — only the common regular
    plural endings, so a non-plural noun ("data", "series") is left unchanged rather
    than mangled. Not a full inflector; good enough to name a type."""
    w = word
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith(("ses", "xes", "zes", "ches", "shes")):
        return w[:-2]
    if len(w) > 2 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _strip_type_stopwords(cand: str) -> str:
    """Drop leading lead-verb / article words from a captured type phrase so
    "Add Widget" → "Widget" and "the SolarPanel" → "SolarPanel"; '' if nothing
    substantive remains."""
    words = [w for w in re.split(r"[ _-]+", cand.strip()) if w]
    while words and words[0].lower() in _TYPE_STOPWORDS:
        words.pop(0)
    return " ".join(words)


def _explicit_user_type(instruction: str) -> str:
    """Deterministically extract a user-NAMED record type, or '' if none is clear.

    ONTA-244: the spec LLM's degrade default is ``WebRecord`` and it sometimes
    under-classifies a fully-specified ask to it too, silently dropping the type
    the user actually named ("Add **Widget** records …" → WebRecord). This parser
    recovers that named type from the raw instruction WITHOUT an LLM, so the plan
    never downgrades a named type to the placeholder. It fires on an unambiguous
    frame — a discovery verb followed by "<Type> records/entities", a "<Type> with
    <fields>" list, or an "each <noun> record …" shape description — and requires
    the type token to be either Capitalized (a lowercased entity phrase is not
    mistaken for a type) or introduced by the strong "each … record" frame. Returns
    a PascalCase type name (via ``_pascal``) or '' when nothing unambiguous is
    present (the caller then keeps WebRecord and clarifies, exactly as before)."""
    if not instruction:
        return ""
    text = instruction[:8000]
    for rx in (_EXPLICIT_TYPE_RE, _TYPE_WITH_FIELDS_RE):
        m = rx.search(text)
        if m:
            cand = _pascal(_strip_type_stopwords(m.group(1)))
            if cand and cand != "WebRecord":
                return cand
    # "each <noun> record …" — a shape description that names the per-record type
    # even in lowercase. Reject the generic record nouns themselves + non-type
    # fillers so "each record", "each data row" don't mint a junk type.
    m = _EACH_RECORD_TYPE_RE.search(text)
    if m:
        noun = m.group(1).lower()
        if noun not in _EACH_NOUN_STOPWORDS:
            cand = _pascal(_singularize(noun))
            if cand and cand != "WebRecord":
                return cand
    return ""


# Nouns that appear in an "each <noun> record" frame but are NOT a real record
# type — the record-noun synonyms themselves ("each record record" can't happen but
# "each data row" / "each result entity" can) and generic fillers. Rejecting these
# keeps the frame from minting a meaningless Data/Result/Item type. Conservative:
# a genuine domain noun (model, product, physician, company) is never in this set.
_EACH_NOUN_STOPWORDS = frozenset(
    {
        "record", "records", "entity", "entities", "row", "rows", "data",
        "result", "results", "item", "items", "thing", "things", "one", "single",
        "new", "such",
    }
)


# A field token in an explicit list: a snake_case / hyphenated identifier, or a
# short multi-word phrase ("word error rate"). We deliberately keep it tight — a
# word made of letters/digits/_/- optionally followed by up to THREE more such
# words (≤4 words total) — so a long trailing prose clause ("… if you can find
# them") is rejected rather than swallowed as one giant field name.
_FIELD_TOKEN = re.compile(
    r"^[A-Za-z][A-Za-z0-9_\-]*(?: [A-Za-z0-9][A-Za-z0-9_\-]*){0,3}$"
)

# An INLINE annotation a user commonly appends to a field name to clarify its
# meaning or enumerate its allowed values — "model_type (LLM/TTS/STT/…)",
# "latency [ms]", "cost_per_1M_tokens (USD)". The annotation is NOT part of the
# field name and would otherwise fail ``_FIELD_TOKEN`` (parens/brackets/slashes
# aren't identifier chars), which — because the harvest ``break``s on the first
# non-field token — silently truncated the whole list at the first annotated
# field (the persona-eval RCA: an explicit 20-field list collapsed to just the
# two un-annotated leading fields ``name, provider``). We blank out each
# ``(...)`` / ``[...]`` / ``{...}`` group so the bare field name survives AND a
# list separator INSIDE the annotation ("LLM/TTS/STT") can't shatter the token.
# Matches one balanced-free (non-nested) group at a time, applied globally, so it
# also protects a mid-list annotation, not just a trailing one. Domain-agnostic.
_FIELD_ANNOTATION_RE = re.compile(r"[\(\[\{][^\(\)\[\]\{\}]*[\)\]\}]")


def _strip_inline_annotations(segment: str) -> str:
    """Replace every inline "(…)"/"[…]"/"{…}" annotation in a field-list segment
    with a single space, so an annotated field collapses to its bare name in place
    — ``model_type (LLM/TTS/STT), latency [ms]`` → ``model_type , latency`` — and
    a separator hidden inside an annotation never fragments the list. Nested
    brackets aren't special-cased (a single pass removes the inner group and leaves
    a stray outer bracket, which then simply fails _FIELD_TOKEN — safe: it ends the
    run rather than harvesting garbage). Whitespace is left for the tokenizer to
    trim."""
    return _FIELD_ANNOTATION_RE.sub(" ", segment)

# STRICT markers that UNAMBIGUOUSLY introduce a field list, so we harvest even a
# single field after them — the "Use these:" chip, a "fields/columns/attributes"
# noun preposition-introduced ("with fields …") or colon-terminated ("fields: …").
# A false positive here would pollute the attribute floor with an entity phrase, so
# these stay conservative. Case-insensitive.
_FIELD_LIST_MARKERS = re.compile(
    r"(?:use\s+these"
    r"|(?:with|of|including|these|the\s+following)\s+"
    r"(?:the\s+)?(?:fields?|columns?|attributes?|properties)"
    r"|(?:fields?|columns?|attributes?|properties)\s*:)"
    r"\s*:?\s*",
    re.IGNORECASE,
)

# LOOSE marker — a "records/entities/rows with" frame ("Add Widget records with
# sku, color, weight"). The record noun before "with" signals a field list, but the
# frame is weaker than the strict markers: a single trailing phrase could be a
# FILTER ("records with high error rates") rather than a field list. So we only
# harvest from this frame when the tail is an actual ENUMERATION — 2+ items joined
# by a comma/semicolon/"and"/"or" — never a lone trailing phrase. This keeps the
# legitimate "with a, b, c" case while rejecting "with <prose filter>".
_LOOSE_FIELD_LIST_MARKER = re.compile(
    r"(?:records?|entities|rows)\s+with\s+",
    re.IGNORECASE,
)
# A tail is a real field ENUMERATION only if it carries a list joiner before the
# first sentence break — a comma/semicolon, or an "and"/"or" between two items.
_LIST_JOINER = re.compile(r"[,;]|\b(?:and|or)\b", re.IGNORECASE)


def _explicit_user_fields(instruction: str) -> list[str]:
    """Deterministically extract the fields the user EXPLICITLY enumerated.

    The persona-eval RCA (ONTA-239, Cluster 2): when the user hands over a concrete
    field list, the LLM spec resolver may non-deterministically drop or rename some
    of them (18 named fields collapsed to a generic 9). This parser is the
    authoritative FLOOR: it reads the user's list straight from the accumulated
    instruction WITHOUT an LLM, so the plan can guarantee no user-named field is
    lost, regardless of what the resolver returned.

    It fires after an unambiguous list MARKER — the server-generated
    ``Use these: …`` chip, a "fields/columns/attributes" noun preposition-introduced
    ("with fields …") or colon-terminated ("fields: …"), or a weaker "records/
    entities/rows with …" frame that requires a real ENUMERATION (2+ comma/"and"-
    joined items) so a lone filter phrase ("records with high error rates") is NOT
    mistaken for a field list — then harvests the comma/newline/semicolon-separated
    tokens on the SAME logical run. Deliberately conservative: bare verbs like
    "collect"/"include" are NOT markers ("collect the coffee shops in SF" is an
    entity phrase). Each token must look like a field name (a short identifier or
    ≤4-word phrase) — a longer prose run breaks the list. Returns snake_case,
    de-duped, order-preserving. Empty when the user gave no explicit list.
    """
    if not instruction:
        return []
    # Bound the work: this runs on the SYNCHRONOUS /agent request path (before the
    # discovery is backgrounded) and the per-marker ``tail`` slice + regex make the
    # scan O(n²) in the number of list markers. A real instruction is well under a
    # few KB; cap the scanned prefix so a pathologically large ``message`` payload
    # can never turn this into a request-path CPU sink. A field list a user cares
    # about always appears early, so truncation never loses a legitimate floor.
    instruction = instruction[:8000]
    out: list[str] = []
    seen: set[str] = set()

    def _harvest(tail: str, *, require_enumeration: bool) -> None:
        # Stop the list at the first hard sentence break so a following sentence of
        # prose is never harvested.
        segment = re.split(r"[.\n?!]", tail, maxsplit=1)[0]
        # LOOSE frame guard: only treat this as a field list when the tail is an
        # actual enumeration (a list joiner present) — a lone trailing phrase after
        # "records with" is a filter/prose, not a field list.
        if require_enumeration and not _LIST_JOINER.search(segment):
            return
        # "a, b, c and d" / "a; b" / "a, b, or c" — normalize joiners to commas.
        segment = re.sub(r"\b(?:and|or)\b", ",", segment, flags=re.IGNORECASE)
        # Blank out inline annotations BEFORE tokenizing, so a separator INSIDE an
        # annotation ("model_type (LLM/TTS/STT)") can't shatter the field into
        # bogus fragments and the annotation itself never becomes a token. We
        # replace each "(…)"/"[…]"/"{…}" group with a single space, collapsing the
        # annotated field down to its bare name in place. Domain-agnostic; keeps
        # slashes that are genuine separators ("a/b/c") splitting as before.
        segment = _strip_inline_annotations(segment)
        raw_tokens = re.split(r"[,;/]", segment)
        matched_any = False
        for tok in raw_tokens:
            t = tok.strip().strip("\"'`*").strip()
            if not t or not _FIELD_TOKEN.match(t):
                # A non-field token ends this list run: stop harvesting past prose
                # (e.g. "name, provider and the pricing if you can find it" keeps
                # name/provider/pricing but not the trailing clause).
                if matched_any:
                    break
                continue
            slug = _slug(t)
            if slug and slug not in seen:
                seen.add(slug)
                out.append(slug)
                matched_any = True

    for m in _FIELD_LIST_MARKERS.finditer(instruction):
        _harvest(instruction[m.end():], require_enumeration=False)
    for m in _LOOSE_FIELD_LIST_MARKER.finditer(instruction):
        _harvest(instruction[m.end():], require_enumeration=True)
    return out


def _snap_to_declared(names: list[str], declared: list[str]) -> list[str]:
    """Snap each attribute name to the type's EXISTING declared attribute (matched
    case-insensitively); keep it verbatim when the type has no such attribute.

    Mirrors enrichment's ``_validate_enrich_request`` (``enrich_cap.py``): the
    enrich rail is ontology-grounded and snaps to declared names, so web-discovery
    minting a divergent synonym for the SAME concept (``per_minute_pricing`` vs the
    already-declared ``realtime_audio_duration_per_minute``) forks the ontology
    across the two rails (ONTA-239, Cluster 2). Grounding discovery the same way
    converges the second rail onto the first's names. Order-preserving; a name with
    no declared match is a legitimately NEW attribute and passes through unchanged
    (soft-extraction still decides its final shape downstream)."""
    lookup = {d.lower(): d for d in declared if d}
    return [lookup.get(n.lower(), n) for n in names]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        s = (x or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out
