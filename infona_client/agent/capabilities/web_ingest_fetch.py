"""Fetch / registry / per-record source-URL provenance.

Consults the web only through registered ``WebSourceProvider``s (BYOR —
OSS registers no default fetcher). Registry picks rebuild at execute
without a second LLM call.
"""
from __future__ import annotations

import asyncio
from typing import Optional
from urllib.parse import urlparse

from infona_client.agent.registry import AgentContext
from infona_client.api_registry import (
    MODE_API_ONLY,
    RoutingDecision,
    RoutingPick,
    build_registry_sources,
    get_api_source_catalog,
    load_tenant_custom_catalog,
    make_tenant_api_source_store,
)
from infona_client.enrichment.models import ApiRequestTrace, ProviderLog
from infona_client.pipeline.source_bundle import (
    TIER_AUTHORITATIVE,
    TIER_WEB,
    SourceBundle,
)
from infona_client.pipeline.stage_trace import StageProjectId, attach_recorder
from infona_client.agent.capabilities import web_ingest_cap as _wic

def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _wic._bg_tasks.add(task)
    task.add_done_callback(_wic._bg_tasks.discard)


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
        _wic.logger.warning("source_bundle_sink_failed", exc_info=True)


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
        return await _wic.route_query(
            query,
            catalog,
            openrouter_key=getattr(ctx, "openrouter_key", "") or "",
            entity_type=spec.get("entity_type") or "",
            query_kind=spec.get("query_kind") or "",
        )
    except Exception:  # noqa: BLE001 — routing must never break discovery
        _wic.logger.warning("registry_route_failed", exc_info=True)
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
        _wic.logger.warning("tenant_custom_catalog_load_failed", exc_info=True)
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


def _record_provider_skip(
    job,
    provider_name: str,
    sub_query: str,
    *,
    reason: str = "out_of_scope",
) -> None:
    """Record an ensemble (provider, sub-query) skip from :func:`provider_accepts`.

    ONTA-461 / R3: when a provider self-declares the sub-query is outside its
    capability scope, the orchestrator does not call ``discover`` and stamps a
    P1 ``provider_skip`` stage-trace action (plus a structured log line) so the
    Job Trace shows *why* that ensemble slot was empty — not a silent gap.
    Observability never sinks the run: failures here are swallowed.
    """
    sq = (sub_query or "")[:200]
    try:
        _wic.logger.info(
            "web_ingest_provider_skip",
            provider=provider_name,
            reason=reason,
            sub_query=sq,
        )
    except Exception:
        pass
    if job is None:
        return
    try:
        rec = attach_recorder(job)
        if rec is None:
            return
        rec.action(
            StageProjectId.p1,
            "provider_skip",
            detail=f"{provider_name}: {reason}",
            meta={
                "provider": provider_name,
                "sub_query": sq,
                "reason": reason,
            },
        )
    except Exception:
        _wic.logger.warning(
            "stage_trace_provider_skip_failed",
            provider=provider_name,
            exc_info=True,
        )


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
            _wic.logger.info(
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
        _wic.logger.warning("web_ingest_locate_trace_record_failed", exc_info=True)


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
