"""Render a :class:`GroundedAskPlan` as LLM prompt context.

Looked up on :mod:`infona_client.nlp.ontology_subgraph_match` at call time via
``_host()`` when a sibling needs a patchable name.
"""

from __future__ import annotations

from infona_client.graph.rdfs_helpers import (
    TEMPLATE_ENTITIES_OF_TYPE_COUNT,
    TEMPLATE_RELATED_ENTITY_NAME_FILTER,
)
from infona_client.nlp.ontology_subgraph_types import GroundedAskPlan


def format_grounding_for_prompt(plan: GroundedAskPlan | None) -> str:
    """Render a grounded plan as prompt context for the Cypher LLM.

    Returns empty string when there is nothing useful to inject.
    """
    if plan is None:
        return ""
    lines: list[str] = [
        "Ontology grounding (structured hint — prefer these when confident;",
        "still emit valid Cypher / allowlisted template JSON; do not invent types):",
        f"  intent: {plan.intent}",
    ]
    if plan.subject_type:
        lines.append(f"  subject_type: {plan.subject_type}")
    if plan.sketch.rel_cues:
        lines.append(f"  rel_cues: {', '.join(plan.sketch.rel_cues)}")
    if plan.sketch.dim_mentions:
        lines.append(f"  dim_mentions: {', '.join(plan.sketch.dim_mentions)}")
    if plan.value:
        lines.append(f"  related_entity_name: {plan.value!r}")
    lines.append(f"  confidence: {plan.confidence}")

    if plan.confidence == "unique" and plan.path is not None:
        lines.append(f"  preferred_path: {plan.path.describe()}")
        if plan.path.hop_count > 1:
            lines.append(f"  path_hops: {plan.path.hop_count}")
            lines.append(
                "  note: multi-hop path — emit Cypher that traverses each "
                "relationship in preferred_path in order; bind the related "
                "entity name filter on the **terminal** node "
                f"({plan.path.terminal_range or '?'}). There is no multi-hop "
                "allowlisted template; do not invent intermediate types."
            )
            if plan.intent == "count":
                lines.append(
                    "  note: intent is COUNT — return count of subjects matching "
                    "this multi-hop related-entity name filter."
                )
        if plan.template:
            lines.append(f"  preferred_template: {plan.template}")
        if plan.params:
            # Show only safe keys already filtered.
            param_bits = []
            for k in sorted(plan.params.keys()):
                param_bits.append(f"{k}={plan.params[k]!r}")
            lines.append(f"  template_params: {{{', '.join(param_bits)}}}")
        if (
            plan.intent == "count"
            and plan.template == TEMPLATE_RELATED_ENTITY_NAME_FILTER
        ):
            lines.append(
                "  note: intent is COUNT — return count of subjects matching "
                "this related-entity name filter (do not return the unfiltered "
                "type total; do not filter a literal property with the value)."
            )
        elif plan.intent == "count" and plan.template == TEMPLATE_ENTITIES_OF_TYPE_COUNT:
            lines.append(
                "  note: prefer entities_of_type_count with the given type_names."
            )
    elif plan.confidence == "ambiguous":
        lines.append(
            "  note: multiple ontology paths score equally — do NOT silently "
            "pick one edge; prefer a clarifying query shape or the strongest "
            "schema-supported reading. Shortlist:"
        )
        for i, rp in enumerate(plan.ranked_paths[:5], 1):
            lines.append(
                f"    {i}. {rp.path.describe()} (score={rp.score:.1f}; "
                f"{', '.join(rp.reasons) or '—'})"
            )
    else:
        if plan.ranked_paths:
            lines.append("  candidate_paths:")
            for i, rp in enumerate(plan.ranked_paths[:5], 1):
                lines.append(f"    {i}. {rp.path.describe()} (score={rp.score:.1f})")
        if plan.explanation:
            lines.append(f"  note: {plan.explanation}")

    if plan.explanation and plan.confidence == "unique":
        lines.append(f"  explanation: {plan.explanation}")

    return "\n".join(lines) + "\n"
