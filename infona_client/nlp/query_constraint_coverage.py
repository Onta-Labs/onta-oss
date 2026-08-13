"""Post-gen constraint coverage + query confidence (filter-miss class).

Companion to :mod:`cypher_filter_integrity` (OPTIONAL MATCH smell). This module
checks whether generated Cypher **covers** NL constraints:

* filter intent / extracted filter tokens present in the question
* plan is not a silent unfiltered aggregate / pure type count when filters
  were asked for
* multi-constraint tokens not dropped wholesale

**Confidence** (attached to timing / NLResult):

* ``high`` — integrity OK + coverage OK
* ``medium`` — ran with soft gaps (plan has a dimension filter but multi-token
  coverage is partial: ≥1 token bound, not all)
* ``low`` — coverage fail, integrity fail, or ambiguous multi-bind with no
  usable filter

**Fail-closed:** never recommend executing an unfiltered aggregate/count when
the question has filter intent (or unbound filter tokens). Prefer clarification
over a silent wrong total.

Anti-overfit: synthetic types/attrs/values only in tests; no persona gold.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from infona_client.nlp.cypher_filter_integrity import (
    cypher_has_constraining_filter,
    pure_type_scan_without_filter,
    question_has_filter_intent,
)
from infona_client.nlp.query_intent import (
    QueryIntentSketch,
    sketch_query_intent,
)

QueryConfidence = Literal["high", "medium", "low"]

# Templates that *can* carry a dimension filter when params are populated.
_DIM_FILTER_TEMPLATES = frozenset(
    {
        "literal_values",
        "literal_compare",
        "related_entity_name_filter",
        "related_entity_name_filter_inverse",
    }
)

# Templates that are pure type / unfiltered aggregate — need dim params or
# free-form filters when the question has filter intent.
_MEASURE_ONLY_TEMPLATES = frozenset(
    {
        "literal_aggregate",
    }
)

_PURE_TYPE_TEMPLATES = frozenset(
    {
        "entities_of_type",
        "entities_of_type_count",
        "entity_count_total",
        "entity_count_by_type",
    }
)

# Param keys that bind a dimension / status / compare filter (not the measure).
_DIM_PARAM_KEYS = (
    "prop_value",
    "needle",
    "target_name",
    "threshold",
    "op",
    "rel_attr",
)

# Aggregate shapes in free-form Cypher.
_AGG_RETURN_RE = re.compile(
    r"(?ix)\bRETURN\b[\s\S]{0,200}\b(?:sum|avg|average|min|max|count)\s*\("
)

# Value equality / compare that is not just "raw IS NOT NULL" / prop selection.
_DIM_VALUE_IN_CYPHER_RE = re.compile(
    r"(?ix)"
    r"("
    r"=\s*\$prop_value\b"
    r"|"
    r"=\s*\$needle\b"
    r"|"
    r"=\s*\$target_name\b"
    r"|"
    r"=\s*\$threshold\b"
    r"|"
    r"\b\$op\b"
    r"|"
    r"\bliteral_value\s*(?:=|<>|!=|<=|>=|<|>|=~)"
    r"|"
    r"\be\.[A-Za-z_][A-Za-z0-9_]*\s*(?:=|<>|!=|<=|>=|<|>|=~|CONTAINS)"
    r"|"
    r"(?:CONTAINS|STARTS\s+WITH|ENDS\s+WITH)\s*\("
    r"|"
    r"(?:CONTAINS|STARTS\s+WITH|ENDS\s+WITH)\s+"
    r"|"
    r"=\s*'[^']+'"
    r"|"
    r'=\s*"[^"]+"'
    r")"
)


@dataclass(frozen=True)
class CoverageResult:
    """Outcome of constraint coverage + confidence assignment."""

    ok: bool
    confidence: QueryConfidence
    reason: str
    unbound_tokens: tuple[str, ...] = ()
    bound_tokens: tuple[str, ...] = ()
    clarification_prompt: str = ""
    fail_closed: bool = False
    sketch: QueryIntentSketch | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_timing(self) -> dict[str, float | str]:
        """Sparse timing / debug keys for NLResult.timing."""
        out: dict[str, float | str] = {
            "query_confidence": self.confidence,
            "query_confidence_reason": (self.reason or "")[:500],
        }
        if self.clarification_prompt:
            out["clarification_prompt"] = self.clarification_prompt[:500]
        if self.unbound_tokens:
            out["unbound_filter_tokens"] = ", ".join(self.unbound_tokens)[:300]
        if self.bound_tokens:
            out["bound_filter_tokens"] = ", ".join(self.bound_tokens)[:300]
        if self.fail_closed:
            out["query_constraint_fail_closed"] = 1.0
        return out


def _param_nonempty(params: dict[str, Any], key: str) -> bool:
    if key not in params:
        return False
    v = params[key]
    if v is None or v == "" or v == [] or v == {}:
        return False
    return True


def plan_has_dimension_filter(
    cypher: str,
    *,
    params: dict[str, Any] | None = None,
    template: str | None = None,
) -> bool:
    """True when the plan appears to apply a status/value/name/compare filter.

    Measure-only aggregates (``literal_aggregate`` with only ``prop_key``) and
    pure type counts return False even if they read a numeric property.
    """
    params = params or {}
    tmpl = (template or "").strip()

    if tmpl in _DIM_FILTER_TEMPLATES:
        # literal_values needs prop_value; compare needs op+threshold; name
        # filter needs target_name.
        if tmpl == "literal_values" and _param_nonempty(params, "prop_value"):
            return True
        if tmpl == "literal_compare" and (
            _param_nonempty(params, "threshold") or _param_nonempty(params, "op")
        ):
            return True
        if tmpl in (
            "related_entity_name_filter",
            "related_entity_name_filter_inverse",
        ) and _param_nonempty(params, "target_name"):
            return True
        # Template named as filter but params empty → no dim filter yet.
        # Fall through to free-form / param scan.

    if tmpl in _MEASURE_ONLY_TEMPLATES:
        # literal_aggregate is measure-only unless extra dim params sneaked in.
        if any(_param_nonempty(params, k) for k in ("prop_value", "needle", "target_name")):
            return True
        if _param_nonempty(params, "threshold") and _param_nonempty(params, "op"):
            return True
        # Free-form body attached to the template name still inspected below.

    if any(_param_nonempty(params, k) for k in ("prop_value", "needle", "target_name")):
        return True
    if _param_nonempty(params, "threshold") and _param_nonempty(params, "op"):
        return True

    c = cypher or ""
    if _DIM_VALUE_IN_CYPHER_RE.search(c):
        return True

    # Integrity's constraining-filter heuristic (entity prop / required MATCH).
    # Exclude pure measure reads: e[$prop_key] without a value predicate is not
    # a dimension filter. cypher_has_constraining_filter is broad — only trust
    # it when a value-ish param or equality cue is also present.
    if cypher_has_constraining_filter(c) and (
        any(_param_nonempty(params, k) for k in _DIM_PARAM_KEYS)
        or _DIM_VALUE_IN_CYPHER_RE.search(c)
    ):
        return True

    return False


def _token_variants(token: str) -> set[str]:
    t = (token or "").strip().lower()
    if not t:
        return set()
    variants = {
        t,
        t.replace(" ", "_"),
        t.replace("_", " "),
        t.replace("-", " "),
        t.replace(" ", "-"),
        re.sub(r"\s+", "", t),
    }
    return {v for v in variants if v}


def _plan_blob(cypher: str, params: dict[str, Any] | None) -> str:
    parts = [cypher or ""]
    if params:
        try:
            parts.append(json.dumps(params, default=str, sort_keys=True))
        except Exception:
            parts.append(str(params))
    return " ".join(parts).lower()


def tokens_bound_in_plan(
    tokens: list[str] | tuple[str, ...],
    cypher: str,
    params: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """Split tokens into (bound, unbound) by case-insensitive plan membership."""
    blob = _plan_blob(cypher, params)
    bound: list[str] = []
    unbound: list[str] = []
    for tok in tokens:
        vars_ = _token_variants(tok)
        if any(v in blob for v in vars_):
            bound.append(tok)
        else:
            unbound.append(tok)
    return bound, unbound


def _plan_is_aggregate_or_count(cypher: str, template: str | None, sketch: QueryIntentSketch) -> bool:
    tmpl = (template or "").strip()
    if tmpl in _MEASURE_ONLY_TEMPLATES or tmpl in _PURE_TYPE_TEMPLATES:
        return True
    if sketch.has_aggregate_intent:
        return True
    c = cypher or ""
    if _AGG_RETURN_RE.search(c):
        return True
    if pure_type_scan_without_filter(c):
        return True
    return False


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
        "2. Prefer template literal_values / literal_compare / "
        "related_entity_name_filter with $prop_value / $op+$threshold / "
        "$target_name when the shape fits; do NOT use literal_aggregate alone "
        "when a dimension filter is required.",
        "3. Put filter values in params (or equality predicates) so they appear "
        "in the plan. Do not invent ontology fields — use names from the schema.",
        "4. If you cannot tell which field a filter token binds to, still prefer "
        "a constrained plan over a silent unfiltered total; fail closed is better "
        "than a wrong number.",
        "5. Filtered aggregates: first constrain entities, then aggregate the measure.",
    ]
    if result.unbound_tokens:
        parts.append(
            "Unbound filter tokens from the question: "
            + ", ".join(result.unbound_tokens)
        )
    if result.clarification_prompt:
        parts.append(f"Clarification needed: {result.clarification_prompt}")
    if previous_cypher and previous_cypher.strip():
        parts.extend(["", f"Rejected query was:\n{previous_cypher.strip()}"])
    return "\n".join(parts)


def check_constraint_coverage(
    question: str,
    cypher: str,
    *,
    params: dict[str, Any] | None = None,
    template: str | None = None,
    integrity_reason: str | None = None,
    sketch: QueryIntentSketch | None = None,
) -> CoverageResult:
    """Return coverage + confidence for a generated plan.

    ``integrity_reason`` non-empty forces ``low`` / fail-closed (compose with
    :func:`check_cypher_filter_integrity` without deleting it).
    """
    params = dict(params or {})
    tmpl = (template or "").strip() or None
    sk = sketch or sketch_query_intent(question)
    tokens = list(sk.filter_tokens)
    bound, unbound = tokens_bound_in_plan(tokens, cypher, params)
    has_dim = plan_has_dimension_filter(cypher, params=params, template=tmpl)
    is_agg_or_count = _plan_is_aggregate_or_count(cypher, tmpl, sk)
    filterish = sk.has_filter_intent or bool(tokens) or question_has_filter_intent(
        question
    )

    if integrity_reason:
        clarify = build_clarification_prompt(unbound, sketch=sk)
        return CoverageResult(
            ok=False,
            confidence="low",
            reason=f"filter integrity failed: {integrity_reason}",
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            clarification_prompt=clarify,
            fail_closed=True,
            sketch=sk,
        )

    # --- Fail-closed core: filter intent + aggregate/count/type plan, no dim ---
    # Product P0 is silent wrong *totals*. Entity-detail / free list plans with
    # an unbound name token are softer (medium) so recovery paths like
    # "show details for <id>" still execute after empty-query escalation.
    if filterish and not has_dim and is_agg_or_count:
        if tmpl in _MEASURE_ONLY_TEMPLATES:
            reason = (
                f"question has filter intent but template {tmpl} is measure-only "
                "(no dimension filter params such as prop_value/needle/target_name/"
                "threshold+op) — would yield a silent unfiltered aggregate"
            )
        elif tmpl in _PURE_TYPE_TEMPLATES:
            reason = (
                f"question has filter intent but template {tmpl} is a pure type "
                "scan with no property/value constraint"
            )
        elif pure_type_scan_without_filter(cypher or ""):
            reason = (
                "question has filter intent but Cypher is a type-only scan with no "
                "constraining property filter"
            )
        else:
            reason = (
                "question has filter intent / filter tokens but plan is an "
                "aggregate or count without a dimension filter — silent unfiltered "
                "total risk"
            )
        clarify = build_clarification_prompt(unbound or tokens, sketch=sk)
        return CoverageResult(
            ok=False,
            confidence="low",
            reason=reason,
            unbound_tokens=tuple(unbound or tokens),
            bound_tokens=tuple(bound),
            clarification_prompt=clarify,
            fail_closed=True,
            sketch=sk,
        )

    # Pure type list under filter intent (integrity also catches many of these).
    if filterish and not has_dim and (
        pure_type_scan_without_filter(cypher or "") or tmpl in _PURE_TYPE_TEMPLATES
    ):
        reason = (
            "question has filter intent but plan has no dimension filter "
            "(list/type scan would drop constraints)"
        )
        clarify = build_clarification_prompt(unbound or tokens, sketch=sk)
        return CoverageResult(
            ok=False,
            confidence="low",
            reason=reason,
            unbound_tokens=tuple(unbound or tokens),
            bound_tokens=tuple(bound),
            clarification_prompt=clarify,
            fail_closed=True,
            sketch=sk,
        )

    # --- Multi-token AND: only fail-closed when also aggregate/count-ish ---
    if len(tokens) >= 2 and len(bound) <= 1 and is_agg_or_count and not has_dim:
        reason = (
            f"multi-constraint question ({len(tokens)} filter-like tokens) but "
            f"only {len(bound)} appear in the plan (aggregate/count risk)"
        )
        clarify = build_clarification_prompt(unbound, sketch=sk)
        return CoverageResult(
            ok=False,
            confidence="low",
            reason=reason,
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            clarification_prompt=clarify,
            fail_closed=True,
            sketch=sk,
        )

    if len(tokens) >= 2 and len(bound) == 1 and has_dim:
        reason = (
            f"partial multi-constraint coverage: {len(bound)}/{len(tokens)} filter "
            "tokens bound; plan has a dimension filter"
        )
        clarify = build_clarification_prompt(unbound, sketch=sk)
        return CoverageResult(
            ok=True,
            confidence="medium",
            reason=reason,
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            clarification_prompt=clarify,
            fail_closed=False,
            sketch=sk,
        )

    # --- Soft: tokens exist, some unbound, but dim filter present ---
    if tokens and unbound and has_dim and bound:
        reason = (
            f"soft gap: {len(unbound)} filter token(s) unbound but plan has a "
            "dimension filter"
        )
        return CoverageResult(
            ok=True,
            confidence="medium",
            reason=reason,
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            clarification_prompt=build_clarification_prompt(unbound, sketch=sk),
            fail_closed=False,
            sketch=sk,
        )

    # Unbound tokens on a non-aggregate free-form plan: soft medium, still OK.
    if tokens and not bound and not has_dim and not is_agg_or_count:
        return CoverageResult(
            ok=True,
            confidence="medium",
            reason=(
                "filter tokens not visible in plan params/text, but plan is not an "
                "aggregate/count total — soft gap only"
            ),
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            clarification_prompt=build_clarification_prompt(unbound, sketch=sk),
            fail_closed=False,
            sketch=sk,
        )

    # --- Soft: filter intent, has dim filter, no extractable tokens ---
    if filterish and has_dim and not tokens:
        return CoverageResult(
            ok=True,
            confidence="high",
            reason="filter intent covered by dimension filter in plan",
            unbound_tokens=(),
            bound_tokens=(),
            fail_closed=False,
            sketch=sk,
        )

    # --- Tokens all bound (or none) + dim filter or no filter intent ---
    if filterish and has_dim:
        return CoverageResult(
            ok=True,
            confidence="high",
            reason="constraint coverage ok (dimension filter present"
            + (f"; tokens bound: {', '.join(bound)}" if bound else "")
            + ")",
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            fail_closed=False,
            sketch=sk,
        )

    if not filterish:
        return CoverageResult(
            ok=True,
            confidence="high",
            reason="no filter intent; coverage gate not required",
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            fail_closed=False,
            sketch=sk,
        )

    # Unknown free-form with filter intent but some filter-like shape already
    # accepted by integrity — medium caution.
    if cypher_has_constraining_filter(cypher or ""):
        return CoverageResult(
            ok=True,
            confidence="medium",
            reason="filter intent; free-form plan has constraining filter signals",
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            fail_closed=False,
            sketch=sk,
        )

    # Remaining filter-intent free-form (not aggregate, not pure type): soft OK.
    return CoverageResult(
        ok=True,
        confidence="medium",
        reason=(
            "filter intent present; free-form plan is not a known unfiltered "
            "aggregate/type-total shape — soft confidence only"
        ),
        unbound_tokens=tuple(unbound),
        bound_tokens=tuple(bound),
        clarification_prompt=build_clarification_prompt(unbound or tokens, sketch=sk),
        fail_closed=False,
        sketch=sk,
    )


def assign_query_confidence(
    *,
    coverage: CoverageResult,
    integrity_ok: bool = True,
) -> QueryConfidence:
    """Map coverage + integrity to high|medium|low (unit-test seam)."""
    if not integrity_ok:
        return "low"
    return coverage.confidence


__all__ = [
    "CoverageResult",
    "QueryConfidence",
    "assign_query_confidence",
    "build_clarification_prompt",
    "check_constraint_coverage",
    "coverage_feedback",
    "fail_closed_answer",
    "plan_has_dimension_filter",
    "tokens_bound_in_plan",
]
