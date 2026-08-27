"""Plan an enrichment (and optional clean-before-enrich normalize).

Owns :meth:`EnrichCapability.plan`. Resolves type / attributes / scope /
subset, estimates cost, and emits ``[normalize_step?, enrich_step]``.

Invariants other agents must not break:
- Look up ``list_type_schema``, ``sample_predicate_values``, ``_list_types``,
  ``_extract_enrich_request``, and ``logger`` on the public ``enrich_cap``
  module via :func:`_host` so monkeypatches keep working.
- Fail closed on an unresolvable named subset (clarify, never enrich-all).
- This mixin does not write the graph.
"""

from __future__ import annotations

from infona_client.agent.capabilities.enrich_clarify import (
    _attr_match_clarify_step,
    _no_match_clarify_step,
    _no_value_match_clarify_step,
    _subset_clarify_step,
)
from infona_client.agent.capabilities.enrich_common import (
    _DEFAULT_PLAN_LIMIT,
    _host,
    _split_scope_values,
)
from infona_client.agent.capabilities.enrich_cost import (
    _coerce_tier,
    _confidence_note,
    _estimate_cost,
    _registry_covers_safe,
    _source_clause,
)
from infona_client.agent.capabilities.enrich_intent import (
    _looks_composite,
    _looks_like_overwrite,
    _looks_like_refresh,
)
from infona_client.agent.capabilities.enrich_types import _resolve_target_type
from infona_client.agent.capabilities.enrich_validate import _validate_enrich_request
from infona_client.agent.registry import AgentContext, PlanStep
from infona_client.enrichment.models import EnrichmentTier
from infona_client.enrichment.tier_router import (
    DEFAULT_CONFIDENCE_MIN as _DEFAULT_CONFIDENCE_MIN,
)
from infona_client.enrichment.tier_router import (
    WEB_CONFIDENCE_MIN as _WEB_CONFIDENCE_MIN,
)
from infona_client.enrichment.tier_router import (
    resolve_chain_cost as _resolve_chain_cost,
)
from infona_client.web_sources.url_extract import extract_urls


class EnrichPlanMixin:
    """``plan`` — parse NL into [normalize_step?, enrich_step]."""

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
        known_types = await _host()._list_types(ctx)
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
        schema = await _host().list_type_schema(
            ctx.neptune, ctx.tenant_id, type_name, tenant=ctx
        )
        req = parsed or await _host()._extract_enrich_request(
            ctx, instruction, type_name, schema
        )
        # Always re-ground against the live schema — even when the planner (or a
        # test) supplies ``parsed``. Auto-match only high-similarity names;
        # weaker unique-suffix hits (e.g. sponsor → lead_sponsor) need agent /
        # user approval via a clarify step — never silent auto-map.
        attr_names = [a for a in schema.get("attributes", []) if a]
        rel_names = [
            r.get("name") for r in schema.get("relationships", []) if r.get("name")
        ]
        req = _validate_enrich_request(req, attr_names, rel_names, type_name)
        pending = req.get("attr_approvals") or []
        if pending:
            return [_attr_match_clarify_step(type_name, pending)]
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
        # Registry-first routing (parity with POST /enrich auto-tier): when every
        # requested attribute is covered by a free registered API (e.g.
        # ClinicalTrial.lead_sponsor → clinicaltrials_gov), force the free ``base``
        # tier. The agent extract prompt historically pushed "core" (paid web) for
        # open-web-ish nouns like "sponsor", so Ask Infona would plan Exa + a dollar
        # cost for facts the NIH registry answers for free. HTTP ``/enrich?tier=auto``
        # already does this via resolve_auto_tier; the agent path must too.
        registry_routing_note = ""
        covered_by = _registry_covers_safe(attributes, type_name)
        if covered_by:
            tier = EnrichmentTier.base
            by_source: dict[str, list[str]] = {}
            for attr in attributes:
                by_source.setdefault(covered_by[attr], []).append(attr)
            detail = "; ".join(
                f"{slug} covers {', '.join(a_list)}"
                for slug, a_list in by_source.items()
            )
            registry_routing_note = (
                f"Prefer free registered API ({detail}) over paid web search."
            )
        elif str(req.get("tier") or "").lower() in ("", "auto"):
            # Only resolve lite/core when the caller left tier unspecified/auto;
            # never override an explicit user-or-LLM core/pro pick for uncovered
            # attributes.
            try:
                from infona_client.enrichment.tier_router import resolve_auto_tier

                decision = await resolve_auto_tier(
                    attributes, type_name, ctx.openrouter_key
                )
                if decision.resolved_tier and not decision.needs_clarification:
                    tier = _coerce_tier(decision.resolved_tier)
                    registry_routing_note = decision.routing_note or ""
            except Exception:  # noqa: BLE001 — routing must never break planning
                _host().logger.warning("agent_enrich_auto_tier_failed", exc_info=True)
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
            samples, _kind = await _host().sample_predicate_values(
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
        elif matched_exact and matched > 0 and scope is None:
            noun = "record" if matched == 1 else "records"
            target_phrase = f"all {matched} {type_name} {noun}"
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
        source_clause = _source_clause(tier, covered_by, has_paid)
        verb = (
            "Refresh (replace)"
            if overwrite
            else ("Refresh (re-verify)" if refresh else "Enrich")
        )
        scope_clause = (
            f" for {subset_desc}"
            if subset_desc
            else (
                f" scoped to {scope['predicate']}={scope['value']}"
                if scope
                else ""
            )
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
                f"{verb} {', '.join(attributes)} on {type_name}"
                + scope_clause
                + url_clause
                + source_clause
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
                    + source_clause.rstrip(".")
                    + (
                        (
                            ", and REPLACE the existing values with the latest "
                            "(stamping each with its source and verified date)."
                            if overwrite
                            else ", and re-verify the existing values (advancing "
                            "their freshness stamp)."
                        )
                        if refresh
                        else ", then write the results into the graph."
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
                "matched": matched,
                "matched_exact": matched_exact,
                "confidence_min": confidence_min,
                "confidence_note": _confidence_note(
                    confidence_min, confidence_lowered
                ),
                "cost_estimate": cost.get("note", ""),
                "routing_note": registry_routing_note,
                "registry_sources": sorted(set(covered_by.values())) if covered_by else [],
                # Surface the supplied pages so the confirm UI can show them.
                "source_urls": urls,
            },
            cost=cost,
            depends_on=depends_on,
        )
        steps.append(enrich_step)
        return steps
