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
    ConflictPolicy,
    EnrichJob,
    EnrichmentTier,
    JobCategory,
    JobErrorItem,
    JobStatus,
    ProviderLog,
)
from cograph_client.graph.kg_writer import refresh_after_write
from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri
from cograph_client.resolver.llm_router import PRIMARY_MODEL, openrouter_chat
from cograph_client.retrieval.errors import LLMError
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
# Conservative default cap so a first (paid) discovery is BOUNDED and cheap to
# inspect. Mirrors the enrich plan's _DEFAULT_PLAN_LIMIT. User-overridable.
_DEFAULT_PLAN_CAP = 200

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


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


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
        confirmed = _dedupe([key_attr, *spec.get("confirmed_attributes", [])])
        suggested = _dedupe([key_attr, *spec.get("suggested_attributes", [])])

        already_asked = int(ctx.extras.get("prior_clarify_count", 0)) >= 1
        if len(confirmed) <= 1 and not already_asked:
            # Only the key is "confirmed" (i.e. the user just named the entity).
            # Ask which attributes to collect — clickable options carry a SHORT
            # recommended set (the most-important few), pre-selected, so the next
            # turn converges without confronting the user with every column.
            core = _core_attrs(key_attr, spec.get("core_attributes", []), suggested)
            return [_clarify_step(type_name, key_attr, core)]

        # Commit: use the confirmed set, or fall back to the suggested set if we
        # already asked once (don't loop). These drive entity naming + the
        # preview card — NOT the fetch breadth.
        attributes = confirmed if len(confirmed) > 1 else suggested

        # Decouple the PROVIDER FETCH from the user's minimal named attributes
        # (Cause 1): every provider PROJECTS rows to hint_columns, so passing the
        # confirmed minimal set (e.g. [name, score]) drops the rest of the table
        # (provider, rating, latency, price, votes) before extraction can model
        # the domain. Build a COMPREHENSIVE hint = key ∪ confirmed ∪ suggested
        # (the suggested set is the LLM's richer guess at web-discoverable
        # columns), so the provider returns a rich table the extractor can
        # normalize into Model/Organization/Score/etc. The confirmed set still
        # drives naming + preview above.
        hint_columns = _dedupe([key_attr, *confirmed, *suggested])

        # Enumeration partition (fan-out, ONTA-192): for an "all X in Y and Z"
        # ask the spec splits the scope into self-contained sub-queries; execute()
        # runs one discovery per sub-query and merges (deduped) into ONE job.
        # Empty → classic single-query discovery. Priced below as n sub-runs.
        # NEVER in URL mode: the pages are fixed, so partitioned queries would
        # just re-scrape (and re-bill) the same URLs for fully-deduped batches.
        subqueries = [] if urls else _norm_subqueries(spec.get("subqueries"))

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
                        (f"{registry_card}. " if registry_card else "")
                        + f"Find {query} on the web and add them to this graph as "
                        f"{type_name} records."
                    ),
                    confidence=0.7,
                    preview={
                        "summary": (
                            (f"{registry_card}. " if registry_card else "")
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
                (f"{registry_card}. " if registry_card else "")
                + f"Find {query} on the web and add them to this graph as "
                f"{type_name} records."
            ),
            confidence=0.7,
            preview={
                "summary": (
                    (f"{registry_card}. " if registry_card else "")
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
        resolver = _build_resolver(ctx)
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
            job = EnrichJob(
                id=str(uuid.uuid4()),
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
                # Chat provenance: link the job to the conversation that spawned it.
                thread_id=getattr(ctx, "session_id", None),
            )
            await job_store.create(job)

        # Thread the tracked job id into the provider context so a URL-targeted
        # provider that resumes asynchronously (e.g. a webhook-driven adapter) can
        # correlate its callback back to THIS job. Generic + optional: providers
        # that don't need it ignore the key, and it's absent when discovery runs
        # without a job store (bare/test context), so nothing depends on it.
        if job is not None:
            pctx = {**pctx, "job_id": job.id}

        async def _run_inner() -> None:
            if job is not None and job_store is not None:
                job.status = JobStatus.running
                job.started_at = datetime.now(timezone.utc)
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
            # Cross-batch dedupe on the KEY attribute (the record identifier):
            # sub-query partitions overlap ("…in Tustin" and a directory row
            # listed under both cities) and so do ENSEMBLE members (the same
            # physician on Places AND a directory page) — re-ingesting the same
            # key would double-write. Specialized runs first, so its (more
            # structured) row wins; the general provider only contributes NEW keys.
            seen_keys: set[str] = set()
            key_attr = (attributes[0] if attributes else "name") or "name"
            last_provider_err: Optional[str] = None
            last_err_provider: Optional[str] = None
            errors_total = 0
            # Set when a FATAL billing/auth error (402/401) aborts the run mid-way
            # (ONTA-201). Carries the clear, user-facing message out of the nested
            # sub-query/provider loops so we can fail the WHOLE job honestly —
            # rows-landed vs rows-lost — instead of swallowing it as one failed
            # batch and reporting "complete".
            fatal_llm_err: Optional[LLMError] = None
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
                            if not rows_found:
                                plog.no_match += 1
                                continue
                            batch = _dedupe_rows(rows_found, key_attr, seen_keys)
                            if not batch:
                                continue  # found rows; all already contributed
                            # Per-record source-URL provenance (ONTA-151): stamp
                            # each row with the page it was drawn from BEFORE
                            # serialization, so it rides through the SAME extract →
                            # ingest → insert_facts path as the rest of the row's
                            # data and lands as a `source_url` citation.
                            _attach_source_urls(
                                batch, getattr(full, "provenance", None) or {}
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
                            # the whole extraction (adversarial-review F5).
                            if job is not None and job_store is not None:
                                job.platforms = platforms
                                job.provider_logs = list(plogs.values())
                                await job_store.update(job)
                            # BATCHED ingest — one commit per (sub-query, provider)
                            # batch, so the job card streams ("N of ~M records
                            # added") instead of jumping 0 → all. Source names the
                            # provider that actually produced the batch.
                            content = json.dumps(
                                batch, default=str, ensure_ascii=False
                            )
                            result = await resolver.ingest(
                                content,
                                ctx.tenant_id,
                                content_type="json",
                                source=f"web:{prov.name}:{query}",
                                instance_graph=instance_graph,
                                # Discovery CONFIRMED the target type + attribute set
                                # with the user, so it passes them to extraction as a
                                # focus. SOFT (default): a PRIOR that keeps extraction
                                # compact yet still decomposes faithfully (subtypes,
                                # real-world nodes, multi-valued splits) — the ONTA-199
                                # follow-up that fixed the flat single-type mis-modeling
                                # (NPs typed as Physician, city/specialty as literals)
                                # without the open-ended reifier's ~20-type blowup.
                                # HARD (kill-switch): the original flat cage.
                                constrain_types=[proposed_type],
                                constrain_attributes={proposed_type: list(attributes)},
                                constrain_soft=_DISCOVERY_SOFT_EXTRACT,
                            )
                            processed += len(batch)
                            entities_total += int(
                                getattr(result, "entities_resolved", 0) or 0
                            )
                            affected_types |= set(result.types_created)
                            for attr_added in result.attributes_added:
                                affected_types.add(attr_added.split(".")[0])
                            if job is not None and job_store is not None:
                                # Rolling, honest total: what landed + the average
                                # per-sub-query yield extrapolated over the
                                # sub-queries still to run, never above the cap.
                                # Settles to == processed at the end.
                                subs_done = sub_i + 1
                                subs_left = len(subqueries) - subs_done
                                avg = math.ceil(processed / subs_done)
                                job.progress.processed = processed
                                job.progress.total = min(
                                    cap, processed + subs_left * avg
                                )
                                job.platforms = platforms
                                job.provider_logs = list(plogs.values())
                                await job_store.update(job)
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
                    # A fatal billing/auth error (402/401) broke the inner
                    # provider loop — abort the whole sub-query fan-out too; every
                    # remaining call would fail identically (ONTA-201). The
                    # terminal FAILED state (with honest partials) is set below.
                    if fatal_llm_err is not None:
                        break

                # FATAL billing/auth failure: fail the WHOLE job with the clear,
                # user-facing message, recording rows-landed vs rows-lost so the
                # run is NEVER presented as complete when a batch was dropped to a
                # systemic backend error (ONTA-201). This precedes the normal
                # roll-up because it is a run-level abort, not a per-provider
                # outcome.
                if fatal_llm_err is not None:
                    for plog in plogs.values():
                        plog.status = (
                            "error" if plog.attempts and not plog.matches
                            else ("ok" if plog.matches else "skipped")
                        )
                    await _fail_billing_job(
                        job, job_store, list(plogs.values()), fatal_llm_err,
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
                        job, job_store, processed=0, entities=0,
                        platforms=platforms,
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
                    job, job_store, processed=processed, entities=entities_total,
                    platforms=platforms,
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
the web and add them to a knowledge graph. From the WHOLE conversation, output \
STRICT JSON only (no markdown):
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
- subqueries: ONLY for an ENUMERATION ask — the user wants ALL/every instance \
across a scope that no single search covers well (multiple cities/regions, several \
named categories). Partition it into 2-6 SELF-CONTAINED queries, each complete on \
its own and together covering the whole ask without overlap. "all primary care \
physicians in Tustin and Santa Ana" -> ["primary care physicians in Tustin, CA", \
"primary care physicians in Santa Ana, CA"]. "coffee shops and bakeries in SF" -> \
["coffee shops in San Francisco", "bakeries in San Francisco"]. A single-list ask \
("OpenRouter models", "S&P 500 companies", "coffee shops in SF") needs NO \
partitioning -> []. Never split a scope the source already returns whole; when \
unsure, use [].
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


async def _resolve_spec(ctx: AgentContext, instruction: str) -> dict:
    """LLM-resolve {entity_type, key_attribute, confirmed/suggested attributes}.

    Degrades to a minimal deterministic spec when there is no key or the LLM
    errors, so the turn never 500s — that minimal spec triggers the clarify path.
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
                # On timeout _resolve_spec degrades to the minimal spec (→ clarify),
                # never 500s.
                timeout=15,
            )
            parsed = _parse_json_object(text)
            if parsed:
                return _normalize_spec(parsed)
        except Exception:  # noqa: BLE001
            logger.warning("web_ingest_spec_failed", exc_info=True)
    # No-LLM fallback: name the records generically and ask. No kind classification
    # without the LLM → query_kind stays None (general default provider).
    return {
        "entity_type": "WebRecord",
        "key_attribute": "name",
        "query_kind": None,
        "confirmed_attributes": [],
        "core_attributes": ["description"],
        "suggested_attributes": ["name", "description", "url"],
    }


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


def _clarify_step(type_name: str, key_attr: str, core: list[str]) -> PlanStep:
    """Ask which attributes to collect. Shows a SHORT recommended set (``core`` —
    the few most-important attributes), pre-selected, as clickable chips; the user
    can drop some, add their own, or keep just the name. The concrete list rides in
    ``options`` so whichever the user picks lands in the accumulated instruction and
    the next turn converges. The question stays terse and does NOT re-list the
    attributes — they're already the chips below it. The comprehensive fetch
    projection is chosen server-side (``hint_columns``), independent of this minimal
    recommendation, so a lean chip list never narrows what actually gets pulled."""
    shown = _dedupe([key_attr, *core])
    question = (
        f"I'll collect **{type_name}** records and always include **{key_attr}**. "
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


def _build_resolver(ctx: AgentContext):
    """Build a SchemaResolver from the agent context (same wiring the ingest
    route uses). Constructed per call — cheap, and keeps no cross-request state."""
    import tempfile
    from pathlib import Path

    from cograph_client.resolver.schema_resolver import SchemaResolver
    from cograph_client.resolver.verdict_cache import JsonVerdictCache

    cache = JsonVerdictCache(Path(tempfile.gettempdir()) / "omnix-verdict-cache.json")
    return SchemaResolver(
        neptune=ctx.neptune,
        anthropic_key=ctx.anthropic_key,
        verdict_cache=cache,
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


def _clean_query(instruction: str) -> str:
    """Best-effort tidy of the instruction into a discovery query. Uses the FIRST
    line (the original ask), dropping later attribute-confirmation replies, then
    strips one leading filler phrase."""
    if not instruction:
        return ""
    first = next(
        (ln.strip() for ln in instruction.splitlines() if ln.strip()),
        instruction.strip(),
    )
    q = _LEAD_FILLER.sub("", first, count=1).strip()
    return q or first


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
# multi-type LLM extractor. So it is best-effort: exactly as reliable as the row's
# OTHER discovered attributes (name, pricing, …) — the same extractor decides them
# all — but not a hard guarantee, and on a multi-type row the extractor chooses
# which entity it lands on. `uri` is a declared attribute datatype, so a field
# named `source_url` is overwhelmingly kept as a literal at temperature 0. If
# GUARANTEED per-record citations are ever required, stamp this deterministically
# post-extraction keyed by entity id (a follow-up; would touch the shared resolver).
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
    is known for the row (e.g. a free/stub provider that supplied no provenance)."""
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


async def _finish_job(
    job: Optional[EnrichJob],
    job_store,
    *,
    processed: int,
    entities: int,
    platforms: list[str],
) -> None:
    """Mark a discovery job applied with its result count + final progress."""
    if job is None or job_store is None:
        return
    now = datetime.now(timezone.utc)
    job.progress.processed = processed
    job.progress.filled = entities
    job.result_count = entities
    if platforms:
        job.platforms = platforms
    job.status = JobStatus.applied
    job.completed_at = now
    job.last_run = now
    await job_store.update(job)


async def _fail_job(job: Optional[EnrichJob], job_store, error: str) -> None:
    """Mark a discovery job failed, carrying a (truncated) error for the UI."""
    if job is None or job_store is None:
        return
    now = datetime.now(timezone.utc)
    job.status = JobStatus.failed
    job.error = (error or "discovery failed")[:500]
    job.completed_at = now
    job.last_run = now
    await job_store.update(job)


async def _fail_billing_job(
    job: Optional[EnrichJob],
    job_store,
    provider_logs: list[ProviderLog],
    error: LLMError,
    *,
    processed: int,
    platforms: list[str],
) -> None:
    """Fail a discovery job on a FATAL LLM billing/auth error (402/401), recording
    HONEST PARTIALS (ONTA-201).

    Unlike :func:`_fail_job`, this fires when the run ABORTED mid-way because the
    shared LLM backend went unbillable/unauthorized. Some batches may already have
    landed, so the terminal state must reflect rows-LANDED vs rows-LOST — never a
    silent "complete". We stamp:

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
    job.completed_at = now
    job.last_run = now
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


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        s = (x or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out
