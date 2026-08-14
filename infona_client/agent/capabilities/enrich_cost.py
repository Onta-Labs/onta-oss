"""Tier, source-clause, and plan-time cost estimate for enrich.

Owns ``_coerce_tier``, the free-registry coverage probe, the human
"via …" clause, and the honest paid-call estimate (COG-123).

Invariants other agents must not break:
- Per-entity paid cost / has_paid come from adapter-declared metadata
  (``_resolve_chain_cost``), never adapter names.
- Cost keys ``estimated_usd`` / ``paid_calls`` match the web plan-step
  contract. Do not rename without updating both.
- Look up ``logger`` on the public ``enrich_cap`` module via :func:`_host`.
"""

from __future__ import annotations

from typing import Optional

from infona_client.agent.capabilities.enrich_common import _host
from infona_client.enrichment.models import EnrichmentTier
from infona_client.enrichment.tier_router import (
    DEFAULT_CONFIDENCE_MIN as _DEFAULT_CONFIDENCE_MIN,
)


def _coerce_tier(tier) -> EnrichmentTier:
    if isinstance(tier, EnrichmentTier):
        return tier
    try:
        return EnrichmentTier(str(tier))
    except ValueError:
        return EnrichmentTier.lite


def _registry_covers_safe(
    attributes: list[str], type_name: str
) -> dict[str, str] | None:
    """``{attr: catalog_slug}`` when every attribute is covered, else None.

    Thin non-raising wrapper around the tier-router probe so a catalog glitch
    never breaks plan(). Kept local so enrich_cap doesn't hard-import the
    heavy catalog path at module load.
    """
    try:
        from infona_client.enrichment.tier_router import _registry_covers

        return _registry_covers(attributes, type_name)
    except Exception:  # noqa: BLE001 — coverage probe must never break planning
        _host().logger.debug("agent_enrich_registry_probe_failed", exc_info=True)
        return None


# Human titles for well-known free registry slugs in plan copy. Unknown slugs
# fall back to a lightly cleaned slug so we never invent a brand name.
_REGISTRY_SOURCE_LABELS = {
    "clinicaltrials_gov": "ClinicalTrials.gov",
    "fred": "FRED",
    "fred_series_search": "FRED",
    "nppes": "NPPES",
    "geonames_search": "GeoNames",
    "open_food_facts_search": "Open Food Facts",
}


def _source_clause(
    tier: EnrichmentTier,
    covered_by: dict[str, str] | None,
    has_paid: bool,
) -> str:
    """Human-readable 'via …' clause for plan rationale/preview.

    Prefer naming the free registered API (ClinicalTrials.gov, NPPES, …) when
    the job was registry-routed. Fall back to plain language for free vs paid
    web tiers — never the jargon 'via the core tier' that the confirm UI used
    to echo verbatim.
    """
    if covered_by:
        labels: list[str] = []
        seen: set[str] = set()
        for slug in covered_by.values():
            label = _REGISTRY_SOURCE_LABELS.get(slug) or slug.replace("_", " ")
            if label not in seen:
                seen.add(label)
                labels.append(label)
        if labels:
            joined = ", ".join(labels)
            return f" via {joined} (free API)."
    if not has_paid:
        if tier == EnrichmentTier.lite:
            return " via free Wikidata."
        return " via free registered sources."
    return " via paid web search."


# ``_resolve_chain_cost`` is imported from ``infona_client.enrichment.tier_router``
# (single source of truth — see the imports at the top of enrich_cap). It derives
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
