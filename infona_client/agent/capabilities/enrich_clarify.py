"""Brief scope / attribute-match clarify steps for the enrich capability.

When a described subset, multi-value scope, or value-filter cannot be
resolved — or a weak schema-attr mapping needs approval — emit a short
``action="clarify"`` step instead of a silent whole-type enrich or an
empty paid job.

Invariants other agents must not break:
- Clicked options are sent verbatim as the next turn.
- A 0-match on an empty type leads with discovery; a multi-value 0-match
  on existing records does not.
- This module does not write the graph.
"""

from __future__ import annotations

from infona_client.agent.registry import PlanStep


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


def _attr_match_clarify_step(
    type_name: str, pending: list[dict]
) -> PlanStep:
    """Ask the user/agent to approve a weak schema attr mapping before enriching.

    Clicked options are sent verbatim as the next turn. Prefer
    ``Enrich <attr> on <Type>`` so the deterministic attr extractor sees the
    leaf first (exact match next turn — no soft-map / re-clarify loop).
    """
    first = pending[0]
    asked = str(first.get("from") or "")
    suggested = str(first.get("to") or "")
    score = first.get("score")
    score_note = (
        f" (similarity {float(score):.0%})"
        if isinstance(score, (int, float))
        else ""
    )
    if len(pending) == 1:
        question = (
            f"You asked to enrich “{asked}” on {type_name}, but that isn’t an "
            f"exact schema field. Closest match is **{suggested}**{score_note}. "
            "Confirm the schema field to enrich?"
        )
    else:
        pairs = ", ".join(
            f"“{p.get('from')}”→{p.get('to')}" for p in pending[:4]
        )
        question = (
            f"Some attributes you named aren’t exact schema fields on "
            f"{type_name} ({pairs}). Confirm which schema field(s) to enrich."
        )
    options: list[str] = []
    for p in pending:
        dst = str(p.get("to") or "").strip()
        if not dst:
            continue
        # Attr-first phrasing: deterministic _ATTR_TRIGGER captures the leaf.
        options.append(f"Enrich {dst} on {type_name}")
    # De-dupe, cap (clarify UI shows 2–4 chips).
    seen: set[str] = set()
    clean: list[str] = []
    for o in options:
        if o not in seen:
            seen.add(o)
            clean.append(o)
        if len(clean) >= 4:
            break
    return PlanStep(
        capability="enrich",
        action="clarify",
        params={
            "question": question,
            "options": clean or [f"Enrich all {type_name}"],
        },
        rationale=(
            "Weak attribute↔schema match needs approval before enriching "
            "(no silent soft-map)."
        ),
        preview={
            "attr_approvals": pending,
            "summary": question,
        },
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
