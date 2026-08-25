"""Clarification / fail-closed answers and coverage retry feedback."""

from __future__ import annotations

from infona_client.nlp.query_constraint_coverage_types import (
    CoverageResult,
    QueryConfidence,
)
from infona_client.nlp.query_intent import QueryIntentSketch


def build_clarification_prompt(
    unbound: list[str] | tuple[str, ...],
    *,
    sketch: QueryIntentSketch | None = None,
) -> str:
    """General clarification question — never domain-hardcoded field names."""
    toks = [t for t in unbound if (t or "").strip()]
    if not toks:
        if sketch and sketch.has_filter_intent:
            return (
                "Which property should the filter in your question bind to? "
                "I could not confidently map the constraint to a field."
            )
        return ""
    if len(toks) == 1:
        return (
            f"Which field should '{toks[0]}' bind to? "
            "I could not confidently apply that filter in the generated plan."
        )
    quoted = ", ".join(f"'{t}'" for t in toks[:5])
    return (
        f"Which fields should {quoted} bind to? "
        "I could not confidently apply those filters in the generated plan."
    )


def fail_closed_answer(result: CoverageResult) -> str:
    """User-facing honest answer when we refuse to execute an uncovered plan."""
    if result.empty_plan_types and result.matched_populated_types:
        parts = [
            "Could not answer with confidence: the generated plan targeted "
            "type(s) with no instances in this knowledge graph "
            f"({', '.join(result.empty_plan_types)}) while the question matches "
            "populated type(s) "
            f"({', '.join(result.matched_populated_types)}). "
            "Executing it would return a silent zero with false high confidence.",
        ]
        if result.reason:
            parts.append(f"Reason: {result.reason}")
        return " ".join(parts)

    parts = [
        "Could not answer with confidence: the generated plan does not cover "
        "filter constraints from the question, so executing it risked a silent "
        "wrong total (unfiltered aggregate/count).",
    ]
    if result.unbound_tokens:
        parts.append(
            "Unbound constraint token(s): "
            + ", ".join(f"'{t}'" for t in result.unbound_tokens)
            + "."
        )
    if result.unbound_dim_binds:
        parts.append(
            "Missing dim-registry constraint(s): "
            + ", ".join(f"'{b}'" for b in result.unbound_dim_binds)
            + "."
        )
    if result.clarification_prompt:
        parts.append(result.clarification_prompt)
    else:
        parts.append(
            "Prefer clarifying which field each filter value maps to over "
            "returning an unfiltered total."
        )
    if result.reason:
        parts.append(f"Reason: {result.reason}")
    return " ".join(parts)


def coverage_feedback(result: CoverageResult, *, previous_cypher: str = "") -> str:
    """Build LLM error_feedback for a coverage rejection (retry loop)."""
    parts = [
        "CONSTRAINT COVERAGE FAILURE (filter-miss / silent-wrong total risk):",
        result.reason or "plan does not cover question filters",
        "",
        "Rewrite rules (REQUIRED):",
        "1. If the question filters by a value/status/label/term, the Cypher MUST "
        "constrain entities with that filter — do not emit a pure type count or "
        "unfiltered sum/avg of a measure property.",
        "2. Prefer template literal_values_count for how-many/count + equality, "
        "literal_compare_count for how-many/count + inequality, "
        "literal_values for list/show/which, literal_compare / "
        "related_entity_name_filter with $prop_value / $op+$threshold / "
        "$target_name when the shape fits; do NOT use literal_aggregate alone "
        "when a dimension filter is required. Never set template literal_values "
        "or literal_compare on a how-many/count question (those helpers return "
        "rows). "
        "For which-X-has-the-highest-total-Y use literal_argmax_by_dim "
        "($group_key, $prop_key) — never a list helper. "
        "For how-many unique/distinct values of a leaf use "
        "literal_distinct_count ($prop_key), not a type-scan DISTINCT entity.",
        "3. Put filter values in params (or equality predicates) so they appear "
        "in the plan. Do not invent ontology fields — use names from the schema.",
        "4. If you cannot tell which field a filter token binds to, still prefer "
        "a constrained plan over a silent unfiltered total; fail closed is better "
        "than a wrong number.",
        "5. Filtered aggregates: first constrain entities, then aggregate the measure.",
        "6. When the dim registry bound a token to a specific leaf+value, the plan "
        "MUST constrain with THAT leaf (and the stored value) — filtering a "
        "different property while only mentioning the token string is not enough.",
        "7. Prefer $type_names / INSTANCE_OF targets from the LIVE populated type "
        "inventory. Do NOT target empty pollution types (0 entities in this KG) "
        "when the question matches a populated type.",
        "8. Multi-constraint questions (two or more filter values such as a zone "
        "AND a status) MUST apply ALL of those filters before SUM/COUNT/AVG — "
        "a single-filter or unfiltered measure aggregate is a silent wrong total.",
    ]
    if result.empty_plan_types:
        parts.append(
            "Rejected plan targeted zero-instance type(s): "
            + ", ".join(result.empty_plan_types)
        )
    if result.matched_populated_types:
        parts.append(
            "Question matches these POPULATED types (use one of these as primary "
            "$type_names): "
            + ", ".join(result.matched_populated_types)
        )
    if result.unbound_tokens:
        parts.append(
            "Unbound filter tokens from the question: "
            + ", ".join(result.unbound_tokens)
        )
    if result.unbound_dim_binds:
        parts.append(
            "Unbound dim-registry binds (use these leaf = value constraints): "
            + ", ".join(result.unbound_dim_binds)
        )
        parts.append(
            "Required: emit equality / related-name filters for each unbound bind "
            "above (correct leaf, exact stored value from the dim registry block)."
        )
    if result.clarification_prompt:
        parts.append(f"Clarification needed: {result.clarification_prompt}")
    if previous_cypher and previous_cypher.strip():
        parts.extend(["", f"Rejected query was:\n{previous_cypher.strip()}"])
    return "\n".join(parts)


def assign_query_confidence(
    *,
    coverage: CoverageResult,
    integrity_ok: bool = True,
) -> QueryConfidence:
    """Map coverage + integrity to high|medium|low (unit-test seam)."""
    if not integrity_ok:
        return "low"
    return coverage.confidence
