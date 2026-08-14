"""Plan-time preview, cost estimate, and resolver factory.

Sample shape is an ESTIMATE — the full commit may differ. Cost uses the
same ``estimated_usd`` contract the auto-confirm gate reads.
"""
from __future__ import annotations

import json
import math
from typing import Optional

from infona_client.agent.registry import AgentContext, PlanStep
from infona_client.graph.queries import tenant_graph_uri
from infona_client.web_sources.base import WebSourceProvider, provider_cost

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

    from infona_client.resolver.schema_resolver import SchemaResolver
    from infona_client.resolver.verdict_cache import JsonVerdictCache

    cache = JsonVerdictCache(Path(tempfile.gettempdir()) / "infona-verdict-cache.json")
    return SchemaResolver(
        neptune=ctx.neptune,
        anthropic_key=ctx.anthropic_key,
        verdict_cache=cache,
        ontology_lock=ontology_lock,
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

def _lean_discover_step(
    *,
    capability: str,
    query: str,
    subqueries: list,
    type_name: str,
    attributes: list,
    attributes_exhaustive: bool,
    hint_columns: list,
    cap: int,
    kg_name,
    provider,
    ensemble: list,
    urls: list,
    registry_params: dict,
    degraded_prefix: str,
    new_kg_note: str,
    registry_card: str,
    lean_cost: dict,
    creates_kg: bool,
) -> object:
    """Immediately-confirmable plan when provider cost is under the preview gate."""
    from infona_client.agent.registry import PlanStep

    return PlanStep(
        capability=capability,
        action="discover_ingest",
        params={
            "query": query,
            "subqueries": subqueries,
            "proposed_type": type_name,
            "attributes": attributes,
            "attributes_exhaustive": attributes_exhaustive,
            "hint_columns": hint_columns,
            "max_rows": cap,
            "kg_name": kg_name,
            "provider": provider.name,
            "providers": [pr.name for pr in ensemble],
            "urls": urls,
            **registry_params,
        },
        rationale=(
            degraded_prefix
            + new_kg_note
            + (f"{registry_card}. " if registry_card else "")
            + f"Find {query} on the web and add them to this graph as "
            f"{type_name} records."
        ),
        confidence=0.7,
        preview={
            "summary": (
                degraded_prefix
                + new_kg_note
                + (f"{registry_card}. " if registry_card else "")
                + f"Search the web for {query} and add the results as "
                f"{type_name} records (up to {cap})."
            ),
            "creates_kg": creates_kg,
        },
        cost=lean_cost,
    )

def _rich_discover_step(
    *,
    capability: str,
    query: str,
    subqueries: list,
    type_name: str,
    attributes: list,
    attributes_exhaustive: bool,
    hint_columns: list,
    cap: int,
    kg_name,
    provider,
    ensemble: list,
    urls: list,
    registry_params: dict,
    degraded_prefix: str,
    new_kg_note: str,
    registry_card: str,
    cost: dict,
    creates_kg: bool,
    discovered_types: list,
    relationships: list,
    sample_rows: list,
    sample_sources: list,
    est_total,
    preview_degraded: bool,
) -> object:
    """Full sample+shape plan card for a spend that needs a human confirm."""
    from infona_client.agent.registry import PlanStep
    from infona_client.agent.capabilities.web_ingest_plan_preview import (
        _preview_summary,
    )
    from infona_client.agent.capabilities import web_ingest_cap as _wic

    return PlanStep(
        capability=capability,
        action="discover_ingest",
        params={
            "query": query,
            "subqueries": subqueries,
            "proposed_type": type_name,
            "attributes": attributes,
            "attributes_exhaustive": attributes_exhaustive,
            "providers": [pr.name for pr in ensemble],
            "hint_columns": hint_columns,
            "max_rows": cap,
            "kg_name": kg_name,
            "provider": provider.name,
            "urls": urls,
            **registry_params,
        },
        rationale=(
            degraded_prefix
            + new_kg_note
            + (f"{registry_card}. " if registry_card else "")
            + f"Find {query} on the web and add them to this graph as "
            f"{type_name} records."
        ),
        confidence=0.7,
        preview={
            "summary": (
                degraded_prefix
                + new_kg_note
                + (f"{registry_card}. " if registry_card else "")
                + _preview_summary(
                    discovered_types, relationships, cap, degraded=preview_degraded
                )
            ),
            "creates_kg": creates_kg,
            "discovered_types": discovered_types,
            "relationships": relationships,
            "sample_rows": sample_rows[: _wic._PREVIEW_SAMPLE],
            "sources": sample_sources[: _wic._PREVIEW_SOURCES],
            "estimated_total": est_total,
            "cost_estimate": cost.get("note", ""),
        },
        cost=cost,
    )

