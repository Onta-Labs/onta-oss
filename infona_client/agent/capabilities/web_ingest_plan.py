"""Plan mixin: resolve spec, confirm shape, optionally preview, emit PlanStep.

BYOR: with no web-source provider registered, ``plan`` returns a plain
"not enabled" answer. Does not write to the graph.
"""
from __future__ import annotations

import asyncio

from infona_client.agent.kg_scope import CTX_KG_AVAILABLE, CTX_KG_STATUS
from infona_client.agent.registry import AgentContext, PlanStep
from infona_client.api_registry import build_registry_sources, get_api_source_catalog
from infona_client.graph.kg_status import KG_MISSING
from infona_client.graph.queries import tenant_graph_uri
from infona_client.obs import timed
from infona_client.web_sources.base import get_web_source
from infona_client.web_sources.url_extract import extract_urls
from infona_client.agent.capabilities import web_ingest_cap as _wic
from infona_client.agent.capabilities.web_ingest_fetch import (
    _attach_source_urls,
    _merge_registry_ensemble,
    _registry_card,
    _registry_route,
)
from infona_client.agent.capabilities.web_ingest_plan_enum import (
    _ensure_enumeration_partition,
    _expand_enumeration_ensemble,
    _norm_subqueries,
)
from infona_client.agent.capabilities.web_ingest_plan_preview import (
    _build_resolver,
    _empty_sample_message,
    _estimate_cost_multi,
    _flat_shape,
    _lean_discover_step,
    _preview_shape,
    _provider_context,
    _rich_discover_step,
)
from infona_client.agent.capabilities.web_ingest_plan_spec import (
    _clarify_step,
    _core_attrs,
    _refuse_if_unavailable,
    _resolve_spec,
    _select_plan_ensemble,
)
from infona_client.agent.capabilities.web_ingest_text import (
    _answer_step,
    _as_list,
    _clean_query,
    _dedupe,
    _explicit_user_fields,
    _explicit_user_type,
    _snap_to_declared,
)


class WebIngestPlanMixin:
    """Shape confirmation + plan-time sample. No graph writes."""

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

        refused = _refuse_if_unavailable(urls)
        if refused is not None:
            return [refused]
        general = get_web_source(for_urls=bool(urls))

        # 1. Resolve the entity type, the attributes to collect, a CLEAN search
        #    subject, and a generic query_kind — so we search for "OpenRouter TTS
        #    models", NOT the user's raw conversational sentence ("can we ingest
        #    open-router's TTS models that it currently offers"). If the user only
        #    named the entity, propose a set and confirm before spending anything.
        if parsed:
            spec = parsed
        else:
            async with timed(_wic.logger, "spec_resolve"):
                spec = await _resolve_spec(ctx, instruction)

        ensemble, refuse = _select_plan_ensemble(urls, spec, general)
        if refuse is not None:
            return refuse if isinstance(refuse, list) else [refuse]
        provider = ensemble[0]

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

        # ONTA-428: discovery is the one rail the planner's KG gate does NOT refuse
        # when the target graph is missing, because minting records into a graph that does
        # not exist yet is a legitimate cold start, and the shared write path
        # registers it. What was wrong was doing it SILENTLY: a typo'd kg_name
        # created a second, near-identical graph and the user was told the ingest
        # succeeded. The planner leaves its probe verdict on ctx.extras; surface it
        # on the plan card so the confirm is an informed one.
        _extras = getattr(ctx, "extras", None) or {}
        creates_kg = bool(ctx.kg_name) and (
            _extras.get(CTX_KG_STATUS) == KG_MISSING
        )
        new_kg_note = (
            f"Knowledge graph '{ctx.kg_name}' does not exist yet and will be "
            "created by this run. "
            if creates_kg
            else ""
        )
        # A missing target in a workspace that ALREADY HAS graphs is the typo
        # shape (the reported ONTA-428 case); a missing target in a workspace with
        # none is a genuine cold start. Only the former withholds the lean path's
        # server-owned auto-confirm below, so a typo costs one human confirm while
        # a first-ever discovery run stays frictionless.
        looks_like_a_typo = creates_kg and bool(_extras.get(CTX_KG_AVAILABLE))

        # ONTA-239 (Cluster 2b) — ONTOLOGY GROUNDING. Fetch the target type's
        # already-declared attribute names so this second rail converges on the
        # first rail's names instead of minting a synonym for the same concept
        # (``per_minute_pricing`` vs an existing ``realtime_audio_duration_per_minute``).
        # Mirrors what the enrich rail does via ``_validate_enrich_request``. Best-
        # effort: a brand-new type / read hiccup yields an empty schema → snapping
        # is a no-op and nothing diverges from today's behavior.
        declared_attrs: list[str] = []
        try:
            schema = await _wic.list_type_schema(ctx.neptune, ctx.tenant_id, type_name)
            declared_attrs = [a for a in (schema.get("attributes") or []) if a]
        except Exception:  # noqa: BLE001 — grounding is best-effort, never a 500
            _wic.logger.warning("web_ingest_type_schema_failed", exc_info=True)

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
            _wic.logger.info(
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
        async with timed(_wic.logger, "registry_route"):
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
        cap = _wic._DEFAULT_PLAN_CAP
        lean_cost = _estimate_cost_multi(
            ensemble, cap, cap, subqueries=len(subqueries)
        )
        if lean_cost["estimated_usd"] <= _wic._PREVIEW_GATE_USD:
            # SERVER-owned auto-confirm contract: this plan was built lean
            # BECAUSE it is at/under the gate — say so explicitly, so clients
            # obey the server's judgment instead of re-deriving it from a
            # hardcoded twin constant (interface-drift risk: a client whose
            # threshold skews from INFONA_WEB_PREVIEW_GATE_USD would either
            # show a preview-less spend card or auto-run an ungated plan).
            # ONTA-428: withhold the auto-confirm when the target graph is missing
            # AND the workspace has others, i.e. the typo shape. Everywhere else
            # (existing graph, or a first-ever graph in an empty workspace) the
            # gate keeps its previous behaviour exactly.
            if not looks_like_a_typo:
                lean_cost["auto_confirm"] = True
            return [
                _lean_discover_step(
                    capability=self.name,
                    query=query,
                    subqueries=subqueries,
                    type_name=type_name,
                    attributes=attributes,
                    attributes_exhaustive=attributes_exhaustive,
                    hint_columns=hint_columns,
                    cap=cap,
                    kg_name=ctx.kg_name,
                    provider=provider,
                    ensemble=ensemble,
                    urls=urls,
                    registry_params=registry_params,
                    degraded_prefix=degraded_prefix,
                    new_kg_note=new_kg_note,
                    registry_card=registry_card,
                    lean_cost=lean_cost,
                    creates_kg=creates_kg,
                )
            ]

        # 2. Cheap SAMPLE fetched with the COMPREHENSIVE hint so the preview sees
        #    the same rich table the commit will. In URL mode the provider extracts
        #    the sample FROM the supplied pages. Bounded by _wic._SAMPLE_BUDGET_S: a
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
                    max_rows=_wic._SAMPLE_ROWS,
                    hint_columns=hint_columns,
                    context=_provider_context(ctx),
                    urls=urls or None,
                ),
                timeout=_wic._SAMPLE_BUDGET_S,
            )
        except asyncio.TimeoutError:
            # Slow web source, not a failure — degrade to a flat, confirmable plan.
            _wic.logger.warning(
                "web_ingest_sample_timeout", query=query, budget_s=_wic._SAMPLE_BUDGET_S
            )
        except Exception:  # noqa: BLE001 — a sample ERROR must never 500 the turn
            _wic.logger.warning("web_ingest_sample_failed", exc_info=True)
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
        #    _wic._SHAPE_BUDGET_S — the extraction LLM's own timeout (60s) is longer than
        #    the whole request budget — and degraded to a flat preview on timeout /
        #    error / no sample so the plan stays confirmable.
        est_total = (
            (getattr(sample, "estimated_total", 0) or len(sample_rows))
            if sample is not None
            else 0
        )
        # cap was set before the lean fast path above (_wic._DEFAULT_PLAN_CAP).
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
                    _estimate_shape(), timeout=_wic._SHAPE_BUDGET_S
                )
            except asyncio.TimeoutError:
                _wic.logger.warning(
                    "web_ingest_preview_timeout",
                    query=query,
                    budget_s=_wic._SHAPE_BUDGET_S,
                )
            except Exception:  # noqa: BLE001 — preview must NEVER 500 the turn
                _wic.logger.warning("web_ingest_preview_failed", exc_info=True)
        if shape is None:
            # No usable sample, or the shape estimate timed out / failed → a flat
            # single-type preview keeps the plan card confirmable.
            preview_degraded = True
            shape = _flat_shape(type_name, attributes, set())
        discovered_types = shape["discovered_types"]
        relationships = shape["relationships"]

        step = _rich_discover_step(
            capability=self.name,
            query=query,
            subqueries=subqueries,
            type_name=type_name,
            attributes=attributes,
            attributes_exhaustive=attributes_exhaustive,
            hint_columns=hint_columns,
            cap=cap,
            kg_name=ctx.kg_name,
            provider=provider,
            ensemble=ensemble,
            urls=urls,
            registry_params=registry_params,
            degraded_prefix=degraded_prefix,
            new_kg_note=new_kg_note,
            registry_card=registry_card,
            cost=cost,
            creates_kg=creates_kg,
            discovered_types=discovered_types,
            relationships=relationships,
            sample_rows=sample_rows,
            sample_sources=sample_sources,
            est_total=est_total,
            preview_degraded=preview_degraded,
        )
        return [step]
