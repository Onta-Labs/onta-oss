"""Enrichment capability — with clean-before-enrich composition.

Reuses the existing enrichment engine (no reimplementation):

* ``plan`` parses the NL instruction into the existing :class:`EnrichRequest`
  shape (attributes + optional scope ``predicate=value`` + tier + confidence).
  THEN it detects a prerequisite: if the **scope predicate's target values are
  composite** (un-normalized — a delimiter shows up in the sampled target
  labels), scoping by ``value`` would MISS the rows packed inside a composite
  cell (e.g. scope ``speaks=Persian`` misses an entity whose ``speaks`` points
  at ``English__Persian``). In that case it emits a NORMALIZE step FIRST (reusing
  :class:`NormalizeCapability.plan` so the cleanup logic isn't duplicated) and
  sets the enrich step's ``depends_on`` to it. Returns ``[normalize_step?,
  enrich_step]``. No writes.

* ``execute`` runs the enrichment as a background job, building the EXACT same
  :class:`EnrichJob` + ``EnrichmentExecutor.run`` the ``/enrich/jobs`` route
  builds (strong-ref ``_spawn`` so the task can't be GC'd). Returns an ack.

The agent never calls the ``/enrich`` HTTP route — it drives the executor + job
store directly via the same primitives.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog

from cograph_client.agent.capabilities.normalize_cap import NormalizeCapability
from cograph_client.agent.registry import AgentContext, PlanStep
from cograph_client.enrichment.models import (
    EnrichJob,
    EnrichScope,
    EnrichmentTier,
    JobStatus,
)
from cograph_client.enrichment.tier_router import (
    DEFAULT_CONFIDENCE_MIN as _DEFAULT_CONFIDENCE_MIN,
)
from cograph_client.enrichment.tier_router import (
    WEB_CONFIDENCE_MIN as _WEB_CONFIDENCE_MIN,
)
from cograph_client.enrichment.tier_router import (
    resolve_chain_cost as _resolve_chain_cost,
)
from cograph_client.graph.ontology_queries import list_types_query
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri
from cograph_client.normalization.inference import (
    list_type_schema,
    sample_predicate_values,
)
from cograph_client.resolver.llm_router import PRIMARY_MODEL, openrouter_chat
from cograph_client.web_sources.url_extract import extract_urls

logger = structlog.stdlib.get_logger("cograph.agent.enrich")

_bg_tasks: set[asyncio.Task] = set()

# Conservative default cap so a large/expensive enrich is BOUNDED by default
# (COG-123). It is written into the plan ``params`` (and the EnrichJob.limit at
# execute time) and surfaced in the preview; the user can override it. 200 keeps
# a first paid run small enough to inspect cheaply while still covering most
# scoped subsets in one pass.
_DEFAULT_PLAN_LIMIT = 200

# Outer safety cap on a resolved subset ("top N", "those", an explicit list) so a
# missing/over-broad LIMIT in the generated subset SPARQL can't fan a paid enrich
# out to thousands of entities. The subset's own N (when given) still applies; this
# only bounds the worst case.
_SUBSET_MAX = 500

# The web confidence floor (``_WEB_CONFIDENCE_MIN``) and the "unset confidence"
# sentinel (``_DEFAULT_CONFIDENCE_MIN``) now live in
# ``cograph_client.enrichment.tier_router`` so the agent path and the /enrich
# route share ONE definition (imported at the top of this module). Web adapters
# (Exa/Parallel/…) return verdicts at a low prior, so the global 0.85 default
# silently filters ALL of them → 0 writes; the floor lets calibrated web verdicts
# land. Applied here in the plan and is overridable; the global default is
# unchanged for direct API callers that don't go through the floor.


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# Delimiters that signal a composite (un-normalized) target value. "__" is the
# slugified list separator the ingest produces; the rest are raw list delimiters.
_COMPOSITE_DELIMS = ["__", ", ", "; ", " / ", " | "]


# A scope value that names MULTIPLE things — "OpenAI, Google, Deepgram and
# ElevenLabs", "Hoag / Kaiser / MemorialCare". The LLM extractor frequently crams
# such a list into a single ``scope.value``; matched as one literal it hits 0 rows
# (the reported persona-eval refresh gap). We split on commas / semicolons /
# slashes / pipes / the word "and" (or "&") so the scope becomes a value SET and
# each member is matched case-insensitively (value IN {…}). Split is done ONLY
# when a delimiter is present, so an ordinary single value ("titanium", "Manager",
# "Persian") is never fragmented.
_LIST_SPLIT_RE = re.compile(r"\s*(?:,|;|/|\||\band\b|&)\s*", re.IGNORECASE)


def _split_scope_values(value: str) -> list[str]:
    """Split a delimited scope value into its members, or ``[]`` if it names one.

    "OpenAI, Google, Deepgram and ElevenLabs" -> the four names; "titanium" -> []
    (no delimiter → a single value, left on the normal single-value scope path).
    De-duped case-insensitively, order-preserving, each trimmed of surrounding
    quotes/whitespace. A trailing/serial-comma "and" ("A, B, and C") collapses to
    three, not four (empty fragments are dropped).
    """
    if not isinstance(value, str) or not value.strip():
        return []
    parts = [
        p.strip().strip("\"'").strip()
        for p in _LIST_SPLIT_RE.split(value)
    ]
    members: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if p and p.lower() not in seen:
            seen.add(p.lower())
            members.append(p)
    # Only treat it as a LIST when it actually decomposed into 2+ members.
    return members if len(members) >= 2 else []


class EnrichCapability:
    name = "enrich"

    def __init__(self, normalize: NormalizeCapability | None = None) -> None:
        # Reuse the normalize capability to BUILD the prerequisite step so the
        # clean-before-enrich logic lives in exactly one place.
        self._normalize = normalize or NormalizeCapability()

    def describe(self) -> str:
        return (
            "Fill in or verify missing attributes on a type by looking them up "
            "from external sources (enrichment). Use for 'enrich', 'fill in', "
            "'look up', 'find the <attribute> for <type>' requests, optionally "
            "scoped (e.g. 'for managers', 'who speak Persian')."
        )

    async def plan(
        self,
        ctx: AgentContext,
        instruction: str,
        parsed: dict | None = None,
    ) -> list[PlanStep]:
        """Build [normalize_step?, enrich_step] from the instruction.

        ``parsed`` (optional) lets the planner pass an already-parsed request
        (attributes/scope/tier/confidence). When absent we ground the extraction
        in the type's REAL schema: we fetch the active type's attribute +
        relationship names from the ontology and feed them to the LLM so an NL
        phrase like "current company" maps to the ``company`` attribute (and the
        tier is chosen with web-fact guidance), instead of the model guessing a
        stray word ("current") and the planner bailing to clarify.

        The target TYPE is resolved from the instruction first, NOT from the
        Explorer's current selection: "enrich brokers with their websites"
        enriches Broker even when PropertyListing is the type selected in the UI.
        ``ctx.type_name`` (the selection) is only a fallback for when the message
        names no known type (see :func:`_resolve_target_type`).
        """
        known_types = await _list_types(ctx)
        # Prefer a type named in the LIVE turn over one lingering in the
        # accumulated instruction window (session-context-bleed defense). The
        # planner stashes the current message on ctx.extras; absent it (a direct
        # call) resolution falls back to the instruction, unchanged.
        current_message = ctx.extras.get("current_message") if ctx.extras else None
        type_name = _resolve_target_type(
            instruction, known_types, ctx.type_name, current_message
        )
        if not type_name:
            return []
        schema = await list_type_schema(ctx.neptune, ctx.tenant_id, type_name)
        req = parsed or await _extract_enrich_request(
            ctx, instruction, type_name, schema
        )
        attributes: list[str] = req.get("attributes") or []
        if not attributes:
            return []
        # URL-targeted enrichment: explicit page(s) the user wants the values
        # read FROM — structured Explorer context (``ctx.urls``) wins, else the
        # links pasted in the message. Read defensively so this works even if
        # ``AgentContext.urls`` hasn't landed yet. Threaded into the step params
        # → the EnrichJob → the adapter lookup context (``target_urls``); a
        # URL-aware premium adapter (e.g. Firecrawl) reads them, free adapters
        # ignore them. No adapter name is hardcoded — selection stays the tier
        # chain's job.
        urls = (getattr(ctx, "urls", None) or []) or extract_urls(instruction)
        # REFRESH-EXISTING mode (ONTA-245 F3): "re-verify / refresh / re-check the
        # <attr> on <subset>" is re-verify-a-subset, NOT discover-new and NOT
        # enrich-all. It routes to the EXISTING scoped enrichment primitive with the
        # `verify` conflict policy — which re-confirms existing values and advances
        # each fact's freshness stamp (`_verified_at`) WITHOUT re-minting entities
        # (no discovery). No new backend primitive; this is agent routing over the
        # same canonical enrichment path. Detected generically from the instruction
        # verb (refresh / re-verify / re-check / update / freshness), never a
        # persona-specific field.
        refresh = _looks_like_refresh(instruction)
        # REFRESH-REPLACE mode (pf10 persona-eval sp-refresh-pricing): an EXPLICIT
        # "replace stale values / make every number current" ask is a refresh that
        # REPLACES rather than re-confirms. It is a strict subset of refresh, so it
        # implies the refresh rail (`refresh = refresh or overwrite`) but flips the
        # conflict policy to `overwrite` at execute time. A plain "refresh /
        # re-verify" stays `verify` (ONTA-245 default preserved). Conservative
        # detector — a false-positive overwrite destroys data.
        overwrite = _looks_like_overwrite(instruction)
        refresh = refresh or overwrite
        tier = _coerce_tier(req.get("tier"))
        requested_confidence = float(
            req.get("confidence_min", _DEFAULT_CONFIDENCE_MIN)
            or _DEFAULT_CONFIDENCE_MIN
        )
        scope = req.get("scope")  # {"predicate":..., "value":...} | None

        # Ranked / specific subset ("the top 5 brokers by listing count", "those",
        # an explicit list). A field=value scope CANNOT express a ranked aggregate,
        # so when the extractor flags a subset we resolve it to the CONCRETE entity
        # IRIs via the shared NL→SPARQL pipeline and enrich exactly those
        # (``entity_uris`` wins over scope in the executor). Fail CLOSED: if the
        # user explicitly named a subset we could not resolve, do NOT silently
        # enrich the whole type — return no plan so the turn clarifies instead.
        subset = req.get("subset")  # {"description": str, "limit": int|None} | None
        entity_uris: list[str] | None = None
        if subset and subset.get("description"):
            entity_uris = await self._resolve_subset_uris(ctx, type_name, subset)
            if not entity_uris:
                # Couldn't pin the subset down (the LLM couldn't form a query, or
                # it matched 0). Don't enrich the whole type and don't bail with a
                # generic message — ask a SHORT, targeted question so the user can
                # guide us to a scope we can find (COG: confirm-the-scope).
                return [_subset_clarify_step(type_name, subset)]
            scope = None  # the explicit entity set supersedes any value-scope

        # MULTI-VALUE scope → resolve to concrete entity_uris deterministically.
        # "refresh pricing for OpenAI, Google, Deepgram and ElevenLabs" extracts a
        # scope whose ``value`` is a delimited LIST. Matched as one literal it hits
        # 0 rows and premature-clarifies (offering discovery, which the caller then
        # picks — the reported refresh-routing gap). Split the list and resolve the
        # entities whose scope value is a case-insensitive MEMBER of the set via the
        # executor's deterministic value-IN select (NOT the NL LLM), landing on the
        # well-tested ``entity_uris`` path. Runs only when a subset didn't already
        # supersede the scope.
        if entity_uris is None and scope and scope.get("predicate"):
            members = _split_scope_values(str(scope.get("value") or ""))
            if members:
                entity_uris = await self._resolve_scope_value_uris(
                    ctx, type_name, scope["predicate"], members
                )
                if not entity_uris:
                    # None of the named values matched an existing record. Ask a
                    # brief, targeted question (naming the values we looked for)
                    # rather than proposing an empty paid job or silently falling
                    # into discovery — the confirm-the-scope contract, list variant.
                    return [
                        _no_value_match_clarify_step(
                            type_name, scope["predicate"], members
                        )
                    ]
                # Echo the interpreted set back so the preview reads naturally.
                subset = {
                    "description": (
                        f"{scope['predicate']} in "
                        f"{', '.join(members)}"
                    ),
                    "limit": None,
                }
                scope = None  # the resolved entity set supersedes the value-scope

        # Resolve the tier's adapter chain ONCE and derive (a) whether it is a
        # paid/web chain and (b) the per-entity paid cost — both driven by
        # adapter-declared metadata, never adapter names (COG-123/COG-121 boundary).
        per_entity_cost, paid_adapters, has_paid = _resolve_chain_cost(tier)

        # COG-121: for a WEB-sourced enrichment (the resolved chain has a paid/web
        # adapter) lower the plan's confidence_min to a functional floor so the
        # low-prior web verdicts aren't all silently filtered → 0 writes. Only
        # override an UNSET (default 0.85) confidence: if the user explicitly asked
        # for a stricter/looser value we respect it. Overridable downstream.
        confidence_min = requested_confidence
        confidence_lowered = False
        user_set_confidence = abs(requested_confidence - _DEFAULT_CONFIDENCE_MIN) > 1e-9
        if has_paid and not user_set_confidence:
            # NOTE (interaction): the executor's per-attribute ontology-confidence
            # override only fires when confidence_min == _DEFAULT_CONFIDENCE_MIN
            # (0.85), i.e. the "unset" sentinel. Lowering to the web floor here is
            # INTENTIONAL and relaxes BOTH the global 0.85 default AND any stricter
            # per-attribute ontology threshold for these web-sourced facts: without
            # the floor the low-prior web verdicts are all filtered → 0 writes. A
            # user who wants per-attribute thresholds honored sets confidence_min
            # explicitly (which keeps user_set_confidence True and skips this floor).
            confidence_min = _WEB_CONFIDENCE_MIN
            confidence_lowered = True

        steps: list[PlanStep] = []
        depends_on: list[str] = []

        # clean-before-enrich: if a scope predicate's target is composite,
        # normalize it FIRST so the scope actually matches the packed rows.
        if scope and scope.get("predicate"):
            samples, _kind = await sample_predicate_values(
                ctx.neptune,
                ctx.tenant_id,
                ctx.kg_name,
                type_name,
                scope["predicate"],
            )
            if _looks_composite(samples):
                norm_steps = await self._normalize.plan(
                    ctx, instruction, predicate_leaves=[scope["predicate"]]
                )
                if norm_steps:
                    norm = norm_steps[0]
                    norm.rationale = (
                        f"Clean '{scope['predicate']}' before enrichment: its "
                        f"values are composite, so scoping by "
                        f"{scope.get('value')!r} would miss packed rows."
                    )
                    steps.append(norm)
                    depends_on = [norm.id]

        # Bound the job + estimate how many entities it will touch. For an explicit
        # entity set the user already chose the size, so there is NO cap and the
        # matched count is exact (= the resolved IRIs). Otherwise apply the
        # conservative default cap (COG-123) and estimate the matched count via the
        # executor's existing index-efficient COUNT — no new query engine. The
        # executor calls the adapter chain once per (entity, attribute) pair
        # (executor.process_entity loops over job.attributes around _lookup_chain),
        # so a paid lookup runs entities × len(attributes) times; cost ≈
        # per-entity-paid-cost × that paid-call count. When the count can't be
        # computed cheaply we fall back to a clearly-labeled estimate (the cap).
        if entity_uris is not None:
            limit = None
            matched, matched_exact = len(entity_uris), True
        else:
            limit = _DEFAULT_PLAN_LIMIT
            matched, matched_exact = await self._estimate_matched(
                ctx, type_name, scope, attributes
            )
            # A value-FILTER the user gave that we can count exactly and that
            # matches NOTHING has no entities to enrich — ask briefly instead of
            # proposing an empty paid job (COG: confirm-the-scope on 0 results).
            # Scoped only: an unfiltered "enrich all X" is never interrupted by a
            # transient 0, and the subset path handles its own empties above.
            if scope is not None and matched_exact and matched == 0:
                # ONTA-244 discover-vs-enrich reconcile: distinguish "the filter is
                # too narrow" (the type HAS entities, none match this value) from
                # "the graph has NONE of these at all" (enrich is the wrong verb —
                # the user wants to MINT them, i.e. discover). A 0 total-type count
                # means the latter, so the clarify offers "Discover these from the
                # web" instead of only "Enrich all" (which would enrich nothing).
                total_matched, total_exact = await self._estimate_matched(
                    ctx, type_name, None, attributes
                )
                empty_type = total_exact and total_matched == 0
                return [_no_match_clarify_step(type_name, scope, empty_type=empty_type)]
        cost = _estimate_cost(
            tier=tier,
            per_entity_cost=per_entity_cost,
            paid_adapters=paid_adapters,
            has_paid=has_paid,
            matched=matched,
            matched_exact=matched_exact,
            limit=limit,
            n_attributes=len(attributes),
        )

        subset_desc = subset.get("description") if subset else None
        n_entities = len(entity_uris) if entity_uris is not None else None
        if n_entities is not None:
            noun = "entity" if n_entities == 1 else "entities"
            # Echo the INTERPRETED subset back so the user can verify we understood
            # their scope before confirming a paid run (COG: confirm-the-scope).
            target_phrase = (
                f"the {n_entities} {type_name} {noun} matching “{subset_desc}”"
                if subset_desc
                else f"the {n_entities} selected {type_name} {noun}"
            )
        else:
            target_phrase = f"matched {type_name} entities (capped at {limit})"
        # When the user supplied page(s), say so in the rationale/preview so they
        # can confirm we'll read the values from THOSE pages (Rail B URL mode).
        n_urls = len(urls)
        url_clause = (
            f" reading values from {n_urls} supplied "
            f"page{'s' if n_urls != 1 else ''}"
            if urls
            else ""
        )
        enrich_step = PlanStep(
            capability=self.name,
            action="run_enrichment",
            params={
                "type_name": type_name,
                "attributes": attributes,
                "tier": tier.value,
                "confidence_min": confidence_min,
                "scope": scope,
                "limit": limit,
                "entity_uris": entity_uris,
                # Explicit page(s) to read attribute values FROM (URL-targeted
                # mode). Threaded into the EnrichJob at execute time. Only set
                # when present so existing (non-URL) plans are byte-for-byte
                # unchanged.
                **({"source_urls": urls} if urls else {}),
                # Refresh-existing mode: route to the `verify` conflict policy at
                # execute time so a re-verify advances the freshness stamp without
                # re-minting. Only set when true → non-refresh plans unchanged.
                **({"refresh": True} if refresh else {}),
                # Refresh-REPLACE mode: route to the `overwrite` conflict policy so a
                # changed value is replaced (not just re-confirmed). Only set when the
                # explicit replace intent is present → plain-refresh plans unchanged.
                **({"overwrite": True} if overwrite else {}),
            },
            rationale=(
                f"{('Refresh (replace)' if overwrite else 'Refresh (re-verify)') if refresh else 'Enrich'} "
                f"{', '.join(attributes)} on {type_name}"
                + (
                    f" for {subset_desc}" if subset_desc
                    else (
                        f" scoped to {scope['predicate']}={scope['value']}"
                        if scope else ""
                    )
                )
                + url_clause
                + f" via the {tier.value} tier."
            ),
            confidence=0.8,
            preview={
                "summary": (
                    f"Look up {', '.join(attributes)} for {target_phrase}"
                    + (
                        f", reading from {n_urls} supplied "
                        f"page{'s' if n_urls != 1 else ''},"
                        if urls
                        else ""
                    )
                    + (
                        (
                            " and REPLACE the existing values with the latest "
                            "(stamping each with its source and verified date)."
                            if overwrite
                            else " and re-verify the existing values (advancing "
                            "their freshness stamp)."
                        )
                        if refresh
                        else " and stage the results for review."
                    )
                ),
                "refresh": refresh,
                # Surface the destructive REPLACE so the confirm UI can flag that
                # changed values will be overwritten (not just re-verified). Only
                # present when true → plain-refresh previews are byte-for-byte
                # unchanged.
                **({"overwrite": True} if overwrite else {}),
                "scope": scope,
                "tier": tier.value,
                "limit": limit,
                "entity_count": n_entities,
                "confidence_min": confidence_min,
                "confidence_note": _confidence_note(
                    confidence_min, confidence_lowered
                ),
                "cost_estimate": cost.get("note", ""),
                # Surface the supplied pages so the confirm UI can show them.
                "source_urls": urls,
            },
            cost=cost,
            depends_on=depends_on,
        )
        steps.append(enrich_step)
        return steps

    async def _estimate_matched(
        self,
        ctx: AgentContext,
        type_name: str,
        scope: dict | None,
        attributes: list[str],
    ) -> tuple[Optional[int], bool]:
        """Estimate how many entities the enrich job will match.

        Reuses the executor's existing index-efficient ``count_entities`` (the
        same SELECT/COUNT path COG-112 built — no new query engine). Returns
        ``(count, exact)``: ``exact=True`` when the COUNT actually ran, else
        ``(None, False)`` so the caller falls back to a labeled estimate rather
        than reporting a misleading 0. Defensive: any executor/Neptune error or a
        missing executor degrades to ``(None, False)`` — the plan must never fail
        on a cost estimate.
        """
        executor = ctx.extras.get("enrichment_executor")
        if executor is None or not hasattr(executor, "count_entities"):
            return None, False
        enrich_scope = None
        if scope and scope.get("predicate") and scope.get("value"):
            try:
                enrich_scope = EnrichScope(
                    predicate=scope["predicate"], value=scope["value"]
                )
            except Exception:  # noqa: BLE001 — a bad scope just means "no count"
                return None, False
        try:
            n = await executor.count_entities(
                ctx.tenant_id,
                ctx.kg_name,
                type_name,
                scope=enrich_scope,
            )
            return int(n), True
        except Exception:  # noqa: BLE001
            logger.warning("agent_enrich_count_failed", exc_info=True)
            return None, False

    async def _resolve_subset_uris(
        self, ctx: AgentContext, type_name: str, subset: dict
    ) -> list[str]:
        """Resolve a ranked/specific subset to the concrete entity IRIs it names.

        Reuses the shared NL→SPARQL engine (:meth:`NLQueryPipeline.select_entity_uris`,
        the same pipeline the question capability/``/ask`` route use) so "the 5
        brokers with the most listings" becomes those 5 IRIs — no new query engine,
        no client-side ranking. The subset's own LIMIT is honored by the generated
        SPARQL; ``_SUBSET_MAX`` is an outer safety cap so a runaway/unbounded subset
        can't fan out to thousands of paid calls. Returns ``[]`` on any failure —
        the caller fails closed rather than enriching the whole type by accident.
        """
        description = str(subset.get("description") or "").strip()
        if not description:
            return []
        raw_limit = subset.get("limit")
        lim = (
            int(raw_limit)
            if isinstance(raw_limit, (int, float))
            and not isinstance(raw_limit, bool)
            and raw_limit > 0
            else None
        )
        lim = min(lim, _SUBSET_MAX) if lim else _SUBSET_MAX

        # Lazy import: keep the heavy NL pipeline (and its anthropic client) out of
        # agent-registry import time, mirroring QueryCapability._build_pipeline.
        from cograph_client.nlp.pipeline import NLQueryPipeline

        pipeline = NLQueryPipeline(ctx.neptune, ctx.anthropic_key)
        onto_graph = tenant_graph_uri(ctx.tenant_id)
        instance_graph = (
            kg_graph_uri(ctx.tenant_id, ctx.kg_name) if ctx.kg_name else onto_graph
        )
        try:
            return await pipeline.select_entity_uris(
                description, type_name, onto_graph, instance_graph, lim
            )
        except Exception:  # noqa: BLE001 — resolution must never crash planning
            logger.warning("agent_enrich_subset_resolve_failed", exc_info=True)
            return []

    async def _resolve_scope_value_uris(
        self,
        ctx: AgentContext,
        type_name: str,
        predicate: str,
        values: list[str],
    ) -> list[str]:
        """Resolve a MULTI-VALUE scope (a list of scope values) to entity IRIs.

        Drives the executor's DETERMINISTIC value-IN select
        (:meth:`EnrichmentExecutor.select_scope_value_uris`) — NOT the NL LLM — so
        "refresh pricing for OpenAI, Google, Deepgram and ElevenLabs" matches the
        existing records whose ``predicate`` value is any of those names
        (case/normalization-insensitive), rather than the single crammed literal
        that matches nothing. Bounded by ``_SUBSET_MAX`` so a huge list can't fan a
        paid enrich out unboundedly. Returns ``[]`` on any failure (no executor, no
        select method, Neptune error) so the caller fails closed.
        """
        executor = ctx.extras.get("enrichment_executor")
        if executor is None or not hasattr(executor, "select_scope_value_uris"):
            return []
        try:
            return await executor.select_scope_value_uris(
                ctx.tenant_id,
                ctx.kg_name,
                type_name,
                predicate,
                values,
                limit=_SUBSET_MAX,
            )
        except Exception:  # noqa: BLE001 — resolution must never crash planning
            logger.warning("agent_enrich_scope_value_resolve_failed", exc_info=True)
            return []

    async def execute(self, ctx: AgentContext, step: PlanStep) -> dict:
        """Create + run an EnrichJob in the background (same as /enrich/jobs)."""
        p = step.params
        executor = ctx.extras.get("enrichment_executor")
        job_store = ctx.extras.get("enrichment_job_store")
        if executor is None or job_store is None:
            raise RuntimeError(
                "enrichment executor/job_store not available in agent context"
            )
        scope = None
        if p.get("scope") and p["scope"].get("predicate"):
            scope = EnrichScope(
                predicate=p["scope"]["predicate"], value=p["scope"]["value"]
            )
        # Explicit entity set (resolved from a ranked/specific subset at plan time);
        # the executor uses a VALUES block and lets it win over scope.
        entity_uris = p.get("entity_uris") or None
        # URL-targeted mode: the page(s) to read values FROM (set at plan time).
        # Threaded onto the job → the executor's adapter lookup context
        # (``target_urls``). Empty by default → unchanged behavior.
        source_urls = p.get("source_urls") or []
        limit = p.get("limit")
        job = EnrichJob(
            id=str(uuid.uuid4()),
            tenant_id=ctx.tenant_id,
            kg_name=ctx.kg_name,
            type_name=p["type_name"],
            attributes=p["attributes"],
            tier=_coerce_tier(p.get("tier")),
            status=JobStatus.queued,
            created_at=datetime.now(timezone.utc),
            # Conflict-policy selection (checked most-specific first):
            #  * `overwrite`  — an EXPLICIT replace intent (pf10 sp-refresh-pricing):
            #    a changed value is REPLACED with the fresh one (+ its source stamp).
            #  * `verify`     — a plain refresh: re-confirm existing values and
            #    advance the freshness stamp WITHOUT clobbering (ONTA-245 F3 default).
            #  * `stage`      — a normal enrich: stage conflicts for review.
            conflict_policy=_overwrite_conflict_policy()
            if p.get("overwrite")
            else _refresh_conflict_policy()
            if p.get("refresh")
            else _default_conflict_policy(),
            confidence_min=float(
                p.get("confidence_min", _DEFAULT_CONFIDENCE_MIN)
                or _DEFAULT_CONFIDENCE_MIN
            ),
            scope=scope,
            entity_uris=entity_uris,
            source_urls=source_urls,
            # Carry the plan's proposed cap so the job actually honors the bound
            # surfaced to the user at plan time (COG-123). int() guards a stray
            # non-int; None leaves whole-subset behavior unchanged. bool is a
            # subclass of int, so exclude it explicitly — a stray True/False must
            # not be coerced to a 1/0 limit.
            limit=int(limit)
            if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit
            else None,
            # Chat provenance: link the job to the conversation that spawned it.
            thread_id=getattr(ctx, "session_id", None),
        )
        await job_store.create(job)
        _spawn(executor.run(job, ctx.tenant_id))
        return {
            "kind": "ack",
            "capability": self.name,
            "action": step.action,
            "job_id": job.id,
            "job_status": job.status.value,
            "message": (
                f"Enriching {', '.join(job.attributes)} on {job.type_name} "
                "in the background; results will be staged for review."
            ),
        }


# --- target-type resolution: prefer the type NAMED in the instruction --------- #
# The Explorer sends the currently-selected type as ``ctx.type_name``. That
# selection must NEVER override a type the user actually names in their message:
# "enrich brokers with their websites" enriches Broker even when PropertyListing
# is the selected type. We resolve the target type from the instruction text
# (case-insensitive, CamelCase- and plural-tolerant) and fall back to the
# selection ONLY when the message names no known type — so a missing/wrong UI
# selection no longer bails the plan to "couldn't determine the specifics".
#
# Three matcher failure modes this block defends against (grounded RCA against the
# voice-models persona eval — the `sp-refresh-pricing` arc):
#   1. An INCIDENTAL type that appears only as a SCOPE qualifier ("…whose
#      organization is X") or a NEGATION ("NOT Organization entities") must not
#      beat the HEAD type the user targeted. The old "longest name named anywhere
#      wins" tie-break picked Organization(12) over Model(5) — and the persona's
#      "(NOT Organization…)" workaround BACKFIRED by injecting the very token the
#      matcher then selected. Fix (A-3): first-STANDALONE-mention wins (the head
#      noun precedes its scope/negation qualifiers in English), longest only as a
#      same-position tie-break (so PropertyListing still beats Property).
#   2. A type named only INSIDE an attribute name ("supported_languages" →
#      Language) must not be selected. Fix (A-3): a snake/kebab/CamelCase COMPOUND
#      token contributes its parts ONLY to phrase matching, never as a standalone
#      single-word candidate — so "supported_languages" can't mint a bare
#      ``Language`` match (only a genuine standalone "language(s)" can).
#   3. A solidly-spelled multi-word type the user writes EXACTLY as the ontology
#      spells it ("RealtimeModel", "GeminiModel") must match. Fix (A-2): the text
#      tokenizer CamelCase-splits each word into adjacent sub-tokens, so the
#      type's phrase ['realtime','model'] matches the fused "RealtimeModel".
#
# A single "word" for matching keeps ``_``/``-`` joined so a snake/kebab compound
# stays atomic (mode 2); CamelCase splitting happens per-word in _camel_words.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


async def _list_types(ctx: AgentContext) -> list[str]:
    """The tenant's declared type names, for resolving the target type from text.

    Reuses the SAME ontology query the ``/ontology/types`` route and the ontology
    capability use (:func:`list_types_query`) — a bounded single round-trip read,
    never an instance scan. Defensive: any read error degrades to ``[]`` so type
    resolution falls back to the selected type rather than failing the plan.
    """
    try:
        onto_graph = tenant_graph_uri(ctx.tenant_id)
        _, rows = parse_sparql_results(
            await ctx.neptune.query(list_types_query(onto_graph))
        )
    except Exception:  # noqa: BLE001 — a type-list read must never break planning
        logger.warning("agent_enrich_list_types_failed", exc_info=True)
        return []
    seen: set[str] = set()
    names: list[str] = []
    for r in rows:
        label = (r.get("label") or "").strip()
        if label and label not in seen:
            seen.add(label)
            names.append(label)
    return names


def _singularize(word: str) -> str:
    """Tiny dependency-free English singularizer — for MATCHING only, not display."""
    w = word.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"  # companies -> company, agencies -> agency
    if len(w) > 4 and w.endswith(("ses", "xes", "zes", "ches", "shes")):
        return w[:-2]  # addresses -> address, boxes -> box
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]  # brokers -> broker, listings -> listing
    return w


def _camel_words(type_name: str) -> list[str]:
    """Split a type name into lowercase words: ``PropertyListing`` -> ['property',
    'listing'], ``URL`` -> ['url'], ``real_estate_agent`` -> ['real', 'estate',
    'agent']. Lets a multi-word type be phrase-matched against the instruction.

    Also used to CamelCase-/underscore-split each raw word of the instruction text
    (A-2), so a solidly-spelled multi-word type ("RealtimeModel") matches the same
    way the user wrote it (fused) — the text word and the type name are tokenized
    identically, so their sub-token phrases line up."""
    parts: list[str] = []
    for chunk in re.split(r"[\s_\-]+", type_name or ""):
        parts.extend(
            re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", chunk)
        )
    return [p.lower() for p in parts if p]


def _tokenize_for_match(text: str) -> tuple[dict[str, int], list[str]]:
    """Tokenize ``text`` for type-name matching → (simple_first, phrase).

    ``phrase`` is EVERY singularized sub-token in order, with each raw word's
    CamelCase/compound parts kept ADJACENT — so a multi-word type's phrase
    (``['realtime','model']``) matches a fused ``RealtimeModel`` the user typed
    (A-2). Position in ``phrase`` preserves word order for first-mention ordering.

    ``simple_first`` maps a singularized token → the earliest ``phrase`` index at
    which it appeared as a STANDALONE SIMPLE word (a raw word that is not a
    multi-part compound). A snake/kebab/CamelCase COMPOUND (typically an attribute
    the user named, e.g. ``supported_languages``, or a solid multi-word type) does
    NOT register its individual parts here — so a bare single-word type like
    ``Language`` cannot be matched by ``supported_languages`` (the attribute-name
    guard, A-3), only by a genuine standalone "language(s)".
    """
    phrase: list[str] = []
    simple_first: dict[str, int] = {}
    for raw in _WORD_RE.findall(text or ""):
        parts = _camel_words(raw)
        if not parts:
            continue
        singular = [_singularize(p) for p in parts]
        start = len(phrase)
        phrase.extend(singular)
        if len(parts) == 1 and singular[0] not in simple_first:
            simple_first[singular[0]] = start
    return simple_first, phrase


def _first_phrase_index(phrase: list[str], words: list[str]) -> int | None:
    """The earliest start index at which ``words`` appears as a contiguous run in
    ``phrase`` (both already singularized), or None."""
    span = len(words)
    if span == 0 or span > len(phrase):
        return None
    for i in range(len(phrase) - span + 1):
        if phrase[i : i + span] == words:
            return i
    return None


def _type_match_index(
    name: str, simple_first: dict[str, int], phrase: list[str]
) -> int | None:
    """Earliest token index at which type ``name`` is NAMED in the tokenized text,
    or None. A single-word type must appear as a STANDALONE simple word (the
    attribute-name guard, A-3); a multi-word (CamelCase) type as a contiguous
    phrase (A-2)."""
    words = _camel_words(name)
    if not words:
        return None
    if len(words) == 1:
        return simple_first.get(_singularize(words[0]))
    return _first_phrase_index(phrase, [_singularize(w) for w in words])


def _match_type_in_text(text: str, known_types: list[str]) -> str | None:
    """Return the known type NAMED in ``text``, or None.

    Selection order (A-3): the type whose STANDALONE mention appears EARLIEST in
    the text wins — the head noun the user targets precedes the scope-qualifier
    ("…whose organization is X") and negation ("NOT Organization") clauses that
    follow it in English, so first-mention naturally prefers the head over an
    incidental co-mention without a fragile clause parser. On a tie (same start
    position) the LONGER name wins, so a specific ``PropertyListing`` still beats a
    bare ``Property``.
    """
    simple_first, phrase = _tokenize_for_match(text)
    if not phrase or not known_types:
        return None
    best: str | None = None
    best_key: tuple[int, int] | None = None
    for name in known_types:
        idx = _type_match_index(name, simple_first, phrase)
        if idx is None:
            continue
        key = (idx, -len(name))
        if best_key is None or key < best_key:
            best, best_key = name, key
    return best


def _resolve_target_type(
    instruction: str,
    known_types: list[str],
    selected: str | None,
    current_message: str | None = None,
) -> str | None:
    """Pick the type to enrich, PREFERRING the one named in the LIVE turn.

    Order:
      1. a known type named in the CURRENT message wins — the user just said it,
         so it beats a stale mention still sitting in the accumulated
         ``instruction`` window (the session-context-bleed defense: without this,
         "longest type named anywhere in the instruction" let a type from a
         COMPLETED earlier request hijack the new one);
      2. else a known type named anywhere in the accumulated ``instruction`` —
         the clarify-chain fallback, where the type was named an earlier turn of
         the SAME open ask and the current reply is a terse scope answer;
      3. else the selected (UI) type, when it is a real KG type OR when we
         couldn't list types at all (preserve the legacy selection behavior) —
         this also honors a deliberate MCP ``type_name`` on a terse call that
         names no type in prose;
      4. else, when the KG has exactly one type, that type;
      5. else None — the caller asks which type to enrich.

    ``current_message`` is optional: when omitted (a direct/legacy call) step 1 is
    skipped and this collapses to the prior instruction-first behavior, so
    existing callers are unaffected.

    How the pf9 RCA matcher fixes land WITHOUT an explicit-type override
    -------------------------------------------------------------------
    The three tokenizer fixes below make first-STANDALONE-mention (steps 1-2)
    resolve the RCA cases directly: the head type the user targets appears BEFORE
    its scope/negation qualifiers in English, and a type named only inside an
    attribute name ("supported_languages" → Language) or a solidly-spelled
    CamelCase type ("RealtimeModel") is handled by the tokenizer — so
    ``type_name='Model'`` / ``RealtimeModel`` resolve correctly from prose alone,
    no separate "explicit arg wins" branch needed. Crucially this keeps a stale
    sticky UI ``selected`` from hijacking: it only wins via step 3, when the live
    turn names no type at all. (Honoring a DELIBERATE MCP ``type_name`` over a
    scope-FIRST phrasing whose head is a different type — "for each Organization,
    enrich its Models", type_name=Model — is a separate, plumbing-level follow-up:
    ``selected`` alone can't be told apart from a sticky Explorer default, so we
    don't guess. That case resolves to the head today, same as before this change.)
    """
    if current_message:
        named_now = _match_type_in_text(current_message, known_types)
        if named_now:
            return named_now
    named = _match_type_in_text(instruction, known_types)
    if named:
        return named
    if selected and (not known_types or selected in known_types):
        return selected
    if len(known_types) == 1:
        return known_types[0]
    return None


# --- brief scope clarifications (action="clarify" steps the planner surfaces) -- #
# When we can't turn the user's described scope into a concrete entity set, a SHORT
# targeted question + clickable options beats either silently enriching the whole
# type or a vague "be more specific". The planner short-circuits a single
# action="clarify" step into a {kind:"clarify"} reply; the user's answer is
# accumulated into the next turn's instruction so resolution re-runs with it.


# ONTA-244: the option label that routes a 0-match enrich clarify to DISCOVERY.
# Phrased as an imperative the deterministic web-discovery guard
# (planner._is_web_discovery_request) recognizes, so clicking it (the option text
# is sent verbatim as the next turn) reliably re-routes to the discover rail
# instead of looping back into an empty enrich. Domain-agnostic — the type name is
# interpolated, never a specific type.
def _discover_option(type_name: str) -> str:
    return f"Discover {type_name} records from the web"


def _subset_clarify_step(type_name: str, subset: dict) -> PlanStep:
    """Brief clarify when a described subset can't be resolved to any entities —
    guide the user toward a scope we CAN find via SPARQL (by name, all, or rank)."""
    desc = str(subset.get("description") or "").strip()
    by = f" by “{desc}”" if desc else ""
    return PlanStep(
        capability="enrich",
        action="clarify",
        params={
            # Guidance lives in the question (names / a ranking); the one chip is a
            # self-contained quick action, since a clicked option is sent verbatim.
            "question": (
                f"I couldn't pin down which {type_name} you mean{by}. Tell me by "
                "name or a ranking (e.g. “top 5 by listings”), or enrich them all?"
            ),
            "options": [f"Enrich all {type_name}"],
        },
        rationale="The described subset did not resolve to any entities.",
    )


def _no_value_match_clarify_step(
    type_name: str, predicate: str, values: list[str]
) -> PlanStep:
    """Brief clarify when a MULTI-VALUE scope matched no existing records.

    The user named a SET of scope values (e.g. "OpenAI, Google, Deepgram,
    ElevenLabs") and none matched an existing entity. Unlike the single-value
    0-match clarify, we do NOT lead with a discovery option: the user asked to
    REFRESH an existing subset, so guide them back onto the enrich rail (fix the
    names, or enrich all) rather than nudging a fresh discovery build — the exact
    mis-route this fix closes. Naming the values we looked for lets them correct a
    typo / stale label quickly."""
    shown = ", ".join(values[:6]) + ("…" if len(values) > 6 else "")
    return PlanStep(
        capability="enrich",
        action="clarify",
        params={
            "question": (
                f"None of the {type_name} records match {predicate} in "
                f"[{shown}]. Check the names/values, or refresh all {type_name}?"
            ),
            # Enrich-rail options ONLY — the user asked to refresh EXISTING
            # records, so we don't offer discovery here (that was the mis-route).
            "options": [f"Enrich all {type_name}"],
        },
        rationale=(
            f"No {type_name} matched any of the {len(values)} requested "
            f"{predicate} values — guiding back to the enrich rail, not discovery."
        ),
    )


def _no_match_clarify_step(
    type_name: str, scope: dict, *, empty_type: bool = False
) -> PlanStep:
    """Brief clarify when the user's value-FILTER matched 0 entities — nothing to
    enrich, so ask rather than propose an empty paid job.

    ``empty_type`` (ONTA-244): the graph has ZERO entities of this type at all, so
    enrichment is the wrong verb — the user almost certainly wants to DISCOVER
    (mint) them from the web. In that case we LEAD with a "Discover … from the web"
    option and word the question around minting-new, instead of only offering
    "Enrich all" (which would enrich nothing). When the type is non-empty (just the
    filter is too narrow) we keep the original enrich-all guidance."""
    if empty_type:
        return PlanStep(
            capability="enrich",
            action="clarify",
            params={
                "question": (
                    f"There are no {type_name} in this graph yet, so there is "
                    f"nothing to enrich. Do you want to discover {type_name} "
                    "records from the web and add them?"
                ),
                # Discovery option FIRST — it's the likely intent for an empty type.
                "options": [
                    _discover_option(type_name),
                    f"Enrich all {type_name}",
                ],
            },
            rationale=(
                f"No {type_name} exist yet — offering discovery (mint new) rather "
                "than an empty enrichment."
            ),
        )
    return PlanStep(
        capability="enrich",
        action="clarify",
        params={
            "question": (
                f"No {type_name} matched {scope.get('predicate')} = "
                f"{scope.get('value')!r}. Adjust the filter, enrich all "
                f"{type_name}, or discover more from the web?"
            ),
            "options": [
                f"Enrich all {type_name}",
                _discover_option(type_name),
            ],
        },
        rationale=f"No {type_name} matched the requested filter.",
    )


def _default_conflict_policy():
    from cograph_client.enrichment.models import ConflictPolicy

    return ConflictPolicy.stage


def _refresh_conflict_policy():
    """Refresh-existing mode uses the `verify` policy: it re-confirms existing
    values and advances each fact's freshness stamp (`_verified_at`) WITHOUT
    overwriting the primary value or holding conflicts for review — the decay-
    refresh contract (ONTA-245 F2/F3). This stays the DEFAULT for a plain
    "refresh / re-verify / re-check / re-confirm" so ONTA-245's contract is
    preserved; only an EXPLICIT replace intent (see `_looks_like_overwrite`)
    escalates to `overwrite`."""
    from cograph_client.enrichment.models import ConflictPolicy

    return ConflictPolicy.verify


def _overwrite_conflict_policy():
    """Refresh-REPLACE mode uses the `overwrite` policy: it REPLACES a changed
    existing value with the fresh one (delete-old + insert-new, the ONTA-236
    attribute-update contract) and stamps the new value's source + `_verified_at`,
    instead of re-confirming the stale value in place.

    Reached ONLY when the instruction carries an EXPLICIT replace / update-to-
    current intent (`_looks_like_overwrite`), NOT for a bare refresh — a
    false-positive overwrite destroys data, so the default stays `verify`
    (ONTA-245). Motivated by the pf10 Speko persona-eval task sp-refresh-pricing
    ("refresh … so every number is CURRENT and sourced"), where `verify` correctly
    fetched the fresh value but dropped it, leaving the stale one in place."""
    from cograph_client.enrichment.models import ConflictPolicy

    return ConflictPolicy.overwrite


# Verbs that signal a REFRESH-EXISTING (re-verify a subset) intent rather than a
# discover-new or first-fill enrich. Matched as whole words, case-insensitively,
# so "refresh the pricing", "re-verify affiliations", "re-check the numbers",
# "update the verified dates", "keep the address current" all route to the
# verify-policy refresh. Kept in lockstep with the planner's deterministic
# refresh-existing router (`_REFRESH_EXISTING_RE`) so the SAME verb set that forces
# the enrich rail also flips the run to verify mode — a message the planner treats
# as a refresh must not land as a plain first-fill enrich. Generic — no persona
# field is referenced.
_REFRESH_RE = re.compile(
    r"\b(re-?verif\w*|re-?check\w*|re-?confirm\w*|refresh\w*|update(?:d|s)?|"
    r"re-?validat\w*|keep\s+(?:it\s+|them\s+)?current|"
    r"make\s+(?:it\s+|them\s+)?current|freshness|decay(?:ing|s)?)\b",
    re.IGNORECASE,
)


def _looks_like_refresh(instruction: str) -> bool:
    """True when the instruction asks to REFRESH / re-verify existing values."""
    return bool(_REFRESH_RE.search(instruction or ""))


# EXPLICIT replace/update-to-current intent — the SUBSET of refresh asks that want
# a changed value REPLACED, not just re-confirmed. Kept deliberately CONSERVATIVE:
# a false-positive overwrite destroys data (ONTA-245 warns the default must stay
# `verify`), so this fires ONLY on an unmistakable replace signal and NEVER on a
# bare "refresh / re-verify / re-check / re-confirm". Five signal shapes:
#   (A) an explicit replace/overwrite/supersede verb;
#   (B) "correct/fix the <stale> <data-noun>" (a value-targeted correction);
#   (C) predicative "… is/are/stay/remain current|up-to-date|accurate" — NO
#       article between the verb and "current", so attributive "the current
#       price" (a plain re-verify of the *current* pricing) does NOT match;
#   (D) "to/with (the) current|latest|newest|up-to-date|most recent" — the
#       update-target form ("update the prices to current", "with the latest");
#   (E) imperative "make/keep/ensure/bring [it/them/the <noun>] current|latest".
# Grounded in the pf10 Speko persona-eval (sp-refresh-pricing: "refresh … so every
# number is CURRENT and sourced"). Generic — no persona field is referenced.
_REPLACE_RE = re.compile(
    r"(?:"
    r"\b(?:replace|replacing|replaces|overwrite|over-write|overwriting|"
    r"supersede|supersedes|superseding)\b"
    r"|"
    r"\b(?:correct|correcting|corrects|corrected|fix|fixing|fixes|fixed)\s+"
    r"(?:the\s+|these\s+|those\s+|any\s+|all\s+|our\s+)?"
    r"(?:outdated\s+|out-of-date\s+|stale\s+|old\s+|wrong\s+|incorrect\s+|bad\s+)?"
    r"(?:value|values|number|numbers|price|prices|figure|figures|entr\w*|"
    r"record|records|field|fields|data|datum|rate|rates|score|scores|stat\w*|"
    r"amount|amounts|address|addresses)\b"
    r"|"
    r"\b(?:is|are|be|been|being|stay|stays|staying|remain|remains|remaining)\s+"
    r"(?:all\s+|now\s+|fully\s+|completely\s+|truly\s+|always\s+)?"
    r"(?:current|up-?to-?date|accurate)\b"
    r"|"
    r"\b(?:to|with)\s+(?:the\s+|their\s+|its\s+)?"
    r"(?:current|latest|newest|up-?to-?date|freshest|most\s+recent)\b"
    r"|"
    r"\b(?:make|makes|making|keep|keeps|keeping|ensure|ensures|ensuring|"
    r"bring|brings|bringing)\s+"
    r"(?:sure\s+)?(?:it|them|these|those|the|all|every|each|everything|our)?"
    r"(?:\s+\w+){0,2}\s+"
    r"(?:is\s+|are\s+|stay\s+|stays\s+|remain\s+|remains\s+|be\s+)?"
    r"(?:current|up-?to-?date|latest|newest|freshest|accurate)\b"
    r")",
    re.IGNORECASE,
)


def _looks_like_overwrite(instruction: str) -> bool:
    """True when the instruction EXPLICITLY asks to REPLACE existing values with
    fresh ones (route refresh → `overwrite`), not merely re-verify them (`verify`).
    Conservative by design — see `_REPLACE_RE`."""
    return bool(_REPLACE_RE.search(instruction or ""))


def _looks_composite(samples: list[str]) -> bool:
    """Cheap composite check: any sampled target value carries a list delimiter."""
    for v in samples:
        for d in _COMPOSITE_DELIMS:
            if d in v:
                return True
    return False


def _coerce_tier(tier) -> EnrichmentTier:
    if isinstance(tier, EnrichmentTier):
        return tier
    try:
        return EnrichmentTier(str(tier))
    except ValueError:
        return EnrichmentTier.lite


# ``_resolve_chain_cost`` is imported from ``cograph_client.enrichment.tier_router``
# (single source of truth — see the imports at the top of this module). It derives
# the per-entity paid cost / has_paid for a tier GENERICALLY from adapter-declared
# metadata, never adapter names (COG-123).


def _estimate_cost(
    tier: EnrichmentTier,
    per_entity_cost: float,
    paid_adapters: int,
    has_paid: bool,
    matched: Optional[int],
    matched_exact: bool,
    limit: Optional[int],
    n_attributes: int = 1,
) -> dict:
    """Honest plan-time cost estimate (COG-123).

    Cost ≈ per-entity-paid-cost × min(matched, limit) × ``n_attributes``. The
    executor calls the adapter chain once per (entity, attribute) pair (see
    ``EnrichmentExecutor.process_entity`` looping over ``job.attributes`` around
    ``_lookup_chain``), so a multi-attribute enrich multiplies the paid-call
    count — quoting only by entities under-counts by ``n_attributes×``. The
    per-entity cost and the paid/free decision are driven by adapter-declared
    metadata (see :func:`_resolve_chain_cost`), so this never special-cases an
    adapter by name.

    - **All-free chain** (no paid adapter — e.g. the OSS ``lite`` Wikidata-only
      tier): ``paid_calls=0`` and an explicit "no paid calls" note.
    - **Paid chain**: report the estimated paid-call count (= entities to process,
      capped at ``limit``, times ``n_attributes``) and the dollar estimate. When
      the matched count was computed exactly we say ``N``; when it couldn't be
      computed cheaply we fall back to the ``limit`` as a clearly-labeled
      UPPER-BOUND estimate ("up to N") — NEVER a silent 0 for a paid tier.
    """
    if not has_paid:
        return {
            "paid_calls": 0,
            # Key names match the web plan-step cost contract EXACTLY
            # (``step.cost.estimated_usd`` / ``step.cost.paid_calls`` —
            # web/app/components/explore/useAgentChat.ts AgentStepCost +
            # AgentChat.tsx PlanStepRow). Do NOT rename without updating both.
            "estimated_usd": 0.0,
            "per_entity_cost_usd": 0.0,
            "note": f"{tier.value} tier — no paid calls (all sources are free).",
        }

    # Number of ENTITIES the paid adapters will be called for, capped at limit.
    if matched_exact and matched is not None:
        entities = matched if limit is None else min(matched, limit)
        estimated = True
    else:
        # Couldn't compute the matched count cheaply — bound by the proposed
        # limit and label it an upper bound rather than reporting a bogus 0.
        entities = limit if limit is not None else 0
        estimated = False

    # The chain runs once per (entity, attribute) pair, so the paid-call count
    # (and dollar cost) scales by the number of attributes being enriched.
    n_attributes = max(int(n_attributes), 1)
    paid_calls = entities * n_attributes
    estimated_cost = round(per_entity_cost * paid_calls, 4)

    entity_phrase = f"{entities}" if estimated else (
        f"up to {entities}" if entities else "an unknown number of"
    )
    matched_clause = (
        f"~{matched} matched" if (matched_exact and matched is not None)
        else "matched count unavailable (using the cap as an upper bound)"
    )
    if n_attributes > 1:
        # Multi-attribute: state the basis so the entities × attributes = calls
        # arithmetic is transparent.
        note = (
            f"{tier.value} tier (paid): ≈ {entity_phrase} entities × "
            f"{n_attributes} attributes = {paid_calls} paid lookups "
            f"(${per_entity_cost:.4f}/call) ≈ ${estimated_cost:.2f} "
            f"[{matched_clause}]."
        )
    else:
        note = (
            f"{tier.value} tier (paid): {entity_phrase} paid lookups "
            f"(${per_entity_cost:.4f}/entity × {entities}) ≈ ${estimated_cost:.2f} "
            f"[{matched_clause}]."
        )
    return {
        "paid_calls": paid_calls,
        "paid_calls_estimated": not estimated,  # True = upper-bound, not exact
        "paid_adapters": paid_adapters,
        "attributes": n_attributes,
        "per_entity_cost_usd": round(per_entity_cost, 4),
        # Key names match the web plan-step cost contract EXACTLY
        # (``step.cost.estimated_usd`` / ``step.cost.paid_calls`` —
        # web/app/components/explore/useAgentChat.ts AgentStepCost +
        # AgentChat.tsx PlanStepRow). Do NOT rename without updating both.
        "estimated_usd": estimated_cost,
        "matched_entities": matched if matched_exact else None,
        "limit": limit,
        "note": note,
    }


def _confidence_note(confidence_min: float, lowered: bool) -> str:
    """Human-facing explanation of the chosen ``confidence_min`` (COG-121)."""
    if lowered:
        return (
            f"Web-sourced facts: confidence_min lowered to {confidence_min:g} so "
            f"low-prior web verdicts are written instead of all being filtered out "
            f"(the strict {_DEFAULT_CONFIDENCE_MIN:g} default would write nothing). "
            f"Overridable."
        )
    return f"confidence_min = {confidence_min:g}."


# --- LLM extraction grounded in the type's real schema ----------------------- #

# Open-web / person / company facts the FREE Wikidata tier usually can't answer
# well — these should default to the paid web ``core`` tier (Parallel/Exa). Used
# only as a deterministic backstop when the LLM omits a tier.
_WEB_FACT_HINTS = {
    "company", "employer", "organization", "organisation", "website", "url",
    "homepage", "description", "bio", "summary", "reviews", "rating", "founder",
    "headquarters", "hq", "location", "address", "email", "phone", "title",
    "role", "position", "industry", "revenue", "funding", "ceo", "linkedin",
}

_EXTRACT_SYSTEM = """\
You extract an enrichment request from a user's instruction, GROUNDED in the \
active type's real schema. You are given the type's actual ATTRIBUTE names and \
RELATIONSHIP names (with their target types). Map the natural-language phrases \
in the instruction onto those real predicate names — never invent a stray word.

Return STRICT JSON only (no markdown):
{
  "attributes": ["<attribute name(s) to enrich>"],
  "scope": {"predicate": "<an attribute OR relationship name>", "value": "<v>"} \
or null,
  "subset": {"description": "<self-contained description of WHICH entities>", \
"limit": <int or null>} or null,
  "tier": "lite" | "base" | "core" | "pro",
  "confidence_min": 0.85
}

RULES:
- "attributes" are the field(s) to FILL IN / look up. Map the noun in the \
instruction to the nearest existing ATTRIBUTE name. Examples: "current company" \
/ "employer" -> "company"; "the website" -> "website"; "their bio" -> \
"description". If NO existing attribute fits but the user clearly names a new \
fact to add, propose a clean lowercase singular noun for it (e.g. "company") — \
NEVER emit a modifier word like "current", "their", "the", "missing".
- "scope" restricts WHICH entities to enrich by a simple FIELD=VALUE match ("for \
managers", "who speak Persian"). Its "predicate" MUST be one of the given \
attribute or relationship names. "languages" / "what they speak" -> the "speaks" \
relationship; "level" / "who are managers" -> the level attribute/relationship. \
If there is no such filter, return null.
- "subset" pins enrichment to a RANKED or SPECIFIC set of entities that a simple \
field=value "scope" CANNOT express — "the top 5 <type> by <metric>", "the 10 \
most recent ...", "those"/"them"/"these" (entities referenced earlier in the \
conversation), or an explicit named list. Write "description" as a SELF-CONTAINED \
phrase naming exactly which entities (resolve pronouns using the whole \
conversation, e.g. turn "those" into "the 5 brokers with the most property \
listings"), and "limit" = the count if the user gave one (else null). Use \
"subset" ONLY for ranked/specific sets; for "all <type>" or a plain field=value \
filter leave it null. "scope" and "subset" are mutually exclusive — prefer \
"subset" when the request is ranked or refers to specific earlier entities.
- "tier" selects the data source. Choose "core" (paid web search: \
Parallel/Exa) for OPEN-WEB facts about people or companies — employer, company, \
website, description, bio, reviews, founder, headquarters, email, role, title, \
industry, etc. Wikidata (the free "lite" tier) does NOT have these. Use "lite" \
ONLY for structured, catalogued identifiers Wikidata reliably holds (e.g. a \
country's ISO code, a film's release year, a well-known org's founding date). \
When unsure for a web-lookup attribute, default to "core".
- "confidence_min" defaults to 0.85 unless the user asks for stricter/looser."""

_EXTRACT_USER_TEMPLATE = """\
Type: {type_name}
Attributes: {attributes}
Relationships: {relationships}

Instruction: {instruction}

Extract the enrichment request as strict JSON."""


async def _extract_enrich_request(
    ctx: AgentContext,
    instruction: str,
    type_name: str,
    schema: dict,
) -> dict:
    """LLM-extract {attributes, scope, tier, confidence_min}, schema-grounded.

    Falls back to the deterministic regex parser when there is no key or the LLM
    errors, so the agent never 500s on extraction. The extracted attributes /
    scope predicate are validated against the type's real schema; the tier is
    backstopped from the web-fact heuristic when the model omits it.
    """
    attr_names = [a for a in schema.get("attributes", []) if a]
    rel_names = [r.get("name") for r in schema.get("relationships", []) if r.get("name")]
    parsed: dict | None = None
    if ctx.openrouter_key:
        rels_block = ", ".join(
            f"{r['name']} (-> {r.get('target_type') or '?'})"
            for r in schema.get("relationships", [])
            if r.get("name")
        ) or "(none)"
        user = _EXTRACT_USER_TEMPLATE.format(
            type_name=type_name,
            attributes=", ".join(attr_names) or "(none)",
            relationships=rels_block,
            instruction=instruction,
        )
        try:
            text = await openrouter_chat(
                ctx.openrouter_key,
                _EXTRACT_SYSTEM,
                user,
                model=PRIMARY_MODEL,
                temperature=0,
                max_tokens=400,
                timeout=30,
            )
            parsed = _parse_json_object(text)
        except Exception:
            logger.warning("agent_enrich_extract_failed", exc_info=True)
            parsed = None
    if not parsed:
        parsed = _parse_enrich_instruction(instruction)
    return _validate_enrich_request(parsed, attr_names, rel_names, type_name)


def _validate_enrich_request(
    parsed: dict,
    attr_names: list[str],
    rel_names: list[str],
    type_name: str | None = None,
) -> dict:
    """Sanitize an extracted request against the type's real schema.

    - attributes: each raw entry is first SPLIT into individual tokens (an
      extractor over multi-attribute phrasing sometimes crams a whole list — or a
      stray ``attributes:`` label — into one string, which would otherwise fuse
      into a single garbled token; see :func:`_split_attr_list`). Each token is
      normalized (a stray modifier word is dropped). Then, GROUNDED in the type's
      real schema: if ANY token names a declared attribute, keep ONLY the declared
      ones (canonical-cased) — a non-member sitting alongside real fields is a
      hallucination/garble and is dropped. If NONE match, the user is naming a
      brand-new attribute to add (e.g. "company"), so keep the clean new nouns —
      minus the target TYPE name itself, which is never a valid attribute of its
      own type (a common hallucination, e.g. "Physician" extracted for Physician).
    - scope.predicate: kept only if it resolves to a real attribute/relationship
      (case-insensitively); otherwise the scope is dropped (a bad scope would
      match nothing).
    - tier: web-fact backstop applied when missing/invalid.
    """
    known = {n.lower(): n for n in (*attr_names, *rel_names)}
    attr_lookup = {n.lower(): n for n in attr_names}

    raw_attrs = parsed.get("attributes") or []
    if isinstance(raw_attrs, str):
        raw_attrs = [raw_attrs]
    # Expand each raw entry into individual tokens, then normalize + de-dupe.
    candidates: list[str] = []
    seen: set[str] = set()
    for a in raw_attrs:
        for frag in _split_attr_list(a):
            norm = _normalize_attr(frag)
            if norm and norm.lower() not in seen:
                seen.add(norm.lower())
                candidates.append(norm)
    # Strict schema intersection when the type HAS declared attributes and at
    # least one candidate matches one: keep only the declared (canonical) members
    # — this drops hallucinated attrs mixed in with real ones. Otherwise (no
    # match, or an empty/uningested schema) keep the clean new nouns as proposed
    # attributes, never the type name itself.
    matched = [attr_lookup[c.lower()] for c in candidates if c.lower() in attr_lookup]
    if matched:
        attributes = matched
    else:
        attributes = [c for c in candidates if not _is_type_name(c, type_name)]

    scope = parsed.get("scope")
    if isinstance(scope, dict) and scope.get("predicate") and scope.get("value"):
        pred = str(scope["predicate"]).strip()
        # Resolve against the real schema. When the schema is EMPTY (no ontology
        # available — e.g. a brand-new/uningested type) we can't validate, so we
        # keep the extracted predicate rather than silently dropping a valid scope.
        resolved = known.get(pred.lower(), pred if not known else None)
        scope = (
            {"predicate": resolved, "value": str(scope["value"]).strip()}
            if resolved
            else None
        )
    else:
        scope = None

    # Ranked/specific subset → a self-contained description + optional positive
    # int limit. Kept independent of the type schema (it is resolved later via a
    # SPARQL select, not validated against predicate names). A subset supersedes a
    # value-scope, so drop the scope when a subset is present.
    subset = parsed.get("subset")
    if isinstance(subset, dict) and str(subset.get("description") or "").strip():
        raw_limit = subset.get("limit")
        s_limit = (
            int(raw_limit)
            if isinstance(raw_limit, (int, float))
            and not isinstance(raw_limit, bool)
            and raw_limit > 0
            else None
        )
        subset = {"description": str(subset["description"]).strip(), "limit": s_limit}
        scope = None
    else:
        subset = None

    tier = parsed.get("tier")
    if tier not in {t.value for t in EnrichmentTier}:
        tier = _tier_for_attributes(attributes)

    return {
        "attributes": attributes,
        "scope": scope,
        "subset": subset,
        "tier": tier,
        "confidence_min": parsed.get("confidence_min", 0.85),
    }


# Stray modifier / filler words an extractor must never emit as an attribute.
_STOPWORDS = {
    "current", "the", "a", "an", "their", "its", "his", "her", "missing",
    "this", "that", "these", "those", "all", "each", "every", "some", "new",
    "of", "for", "in", "on", "with",
}


# A leading "attributes:" / "field:" / "column:" label an extractor sometimes
# keeps on a crammed attribute string ("attributes: group_affiliation, npi").
# Stripped before splitting so it doesn't fuse into the first token.
_ATTR_LABEL_RE = re.compile(
    r"^\s*(?:attributes?|fields?|columns?|properties?)\s*[:=]\s*", re.IGNORECASE
)


def _split_attr_list(value) -> list[str]:
    """Split one extracted attribute entry into individual attribute tokens.

    An extractor over MULTI-attribute phrasing sometimes crams a whole list into
    a single string ("group_affiliation, board_certifications, npi") or keeps a
    stray ``attributes:`` label ("attributes: group_affiliation"). Left as one
    string, :func:`_normalize_attr` would fuse it into a single garbled token
    (e.g. ``attributes_group_affiliation``), silently collapsing four named
    fields into one bogus one. We strip a leading label and split on the same
    list delimiters the scope splitter uses, so each named field is validated on
    its own. A single clean value ("company", "group affiliation") returns as a
    lone element — ordinary single-attribute extraction is byte-for-byte
    unchanged.
    """
    if not isinstance(value, str):
        return []
    stripped = _ATTR_LABEL_RE.sub("", value.strip())
    if not stripped:
        return []
    return [p.strip() for p in _LIST_SPLIT_RE.split(stripped) if p.strip()]


def _is_type_name(candidate: str, type_name: str | None) -> bool:
    """True when ``candidate`` names the target type itself (singular/plural,
    case-insensitive) — never a valid attribute OF that type, so it is dropped as
    a hallucination (e.g. "Physician" extracted as an attribute of Physician)."""
    if not candidate or not type_name:
        return False
    return _singularize(candidate.lower()) == _singularize(type_name.lower())


def _normalize_attr(value) -> str:
    """Reduce an extracted attribute phrase to a clean predicate noun, or "".

    Strips a leading modifier ("current company" -> "company"), drops pure
    stopwords ("current" -> ""), and slugs spaces to underscores so the result
    is a usable attribute leaf name.
    """
    if not isinstance(value, str):
        return ""
    words = [w for w in re.split(r"\s+", value.strip()) if w]
    # Drop leading stopwords ("current company" -> "company").
    while words and words[0].lower() in _STOPWORDS:
        words.pop(0)
    # Stop at the first trailing stopword ("company for" -> "company").
    kept: list[str] = []
    for w in words:
        if w.lower() in _STOPWORDS:
            break
        kept.append(w)
    if not kept:
        return ""
    cleaned = "_".join(kept).strip("_-")
    return cleaned if cleaned and cleaned.lower() not in _STOPWORDS else ""


def _tier_for_attributes(attributes: list[str]) -> str:
    """Default tier: ``core`` (paid web) when any attribute is an open-web fact,
    else ``core`` anyway for safety — Wikidata-only ``lite`` is opt-in via the
    LLM (structured identifiers), not the silent default for a web lookup."""
    for a in attributes:
        if a.lower() in _WEB_FACT_HINTS:
            return EnrichmentTier.core.value
    # No clear structured-identifier signal → prefer the paid web tier so a
    # person/company lookup isn't silently downgraded to a Wikidata miss.
    return EnrichmentTier.core.value if attributes else EnrichmentTier.lite.value


def _parse_json_object(text: str) -> dict | None:
    """Best-effort parse of an LLM JSON object reply (tolerant of code fences)."""
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


# --- Deterministic fallback parser (no LLM key / LLM error) ------------------ #

_ATTR_TRIGGER = re.compile(
    r"\b(?:enrich|fill in|fill|look up|lookup|find|get|add)\s+(?:the\s+)?"
    r"([A-Za-z_][\w-]*(?:\s+[A-Za-z_][\w-]*)?)",
    re.IGNORECASE,
)
# Relationship scope: "<verb> <Value>" e.g. "speak Persian", "speaks French".
# group(1) = verb, group(2) = value. Verb is lemmatized to its predicate leaf.
_SCOPE_REL = re.compile(
    r"\b(speak|speaks|speaking|knows?|knowing|using|uses?)\s+"
    r"([A-Z][\w-]+)",
)


def _parse_enrich_instruction(instruction: str) -> dict:
    """Deterministic best-effort parse used only when the LLM is unavailable.

    Extracts attribute noun(s) after the enrich verb (dropping a leading
    modifier like "current") and an optional relationship scope. Tier is left
    unset so :func:`_validate_enrich_request` applies the web-fact default.

    Examples:
      "enrich the current company for managers"
        → attributes=["company"]   (the "current" modifier is dropped)
      "enrich company for mentors who speak Persian"
        → attributes=["company"], scope={"predicate":"speaks","value":"Persian"}
    """
    attributes: list[str] = []
    m = _ATTR_TRIGGER.search(instruction)
    if m:
        norm = _normalize_attr(m.group(1))
        if norm:
            attributes = [norm]

    scope = None
    rel = _SCOPE_REL.search(instruction)
    if rel:
        verb = rel.group(1).lower()
        pred = _SCOPE_VERB_LEMMA.get(verb, verb)
        scope = {"predicate": pred, "value": rel.group(2)}
    return {"attributes": attributes, "scope": scope, "tier": None}


# Map inflected scope verbs to their predicate leaf (the ontology stores the
# bare relationship name, e.g. "speaks").
_SCOPE_VERB_LEMMA = {
    "speak": "speaks",
    "speaks": "speaks",
    "speaking": "speaks",
    "know": "knows",
    "knows": "knows",
    "knowing": "knows",
    "use": "uses",
    "uses": "uses",
    "using": "uses",
}
