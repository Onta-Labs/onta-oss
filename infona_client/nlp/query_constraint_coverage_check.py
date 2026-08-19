"""Check constraint coverage of a generated plan (filter-miss / silent-total)."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from infona_client.nlp.cypher_filter_integrity import (
    cypher_has_constraining_filter,
    pure_type_scan_without_filter,
    question_has_filter_intent,
)
from infona_client.nlp.query_constraint_coverage_dim import (
    _dim_bind_label,
    _plan_is_aggregate_or_count,
)
from infona_client.nlp.query_constraint_coverage_types import (
    CoverageResult,
    DimBindLike,
    QueryConfidence,
    _MEASURE_ONLY_TEMPLATES,
    _PURE_TYPE_TEMPLATES,
    _host,
)
from infona_client.nlp.query_intent import QueryIntentSketch, sketch_query_intent


def check_constraint_coverage(
    question: str,
    cypher: str,
    *,
    params: dict[str, Any] | None = None,
    template: str | None = None,
    integrity_reason: str | None = None,
    schema_reason: str | None = None,
    sketch: QueryIntentSketch | None = None,
    dim_binds: Sequence[DimBindLike] | None = None,
    populated_types: Sequence[str] | None = None,
    type_counts: Mapping[str, int] | None = None,
) -> CoverageResult:
    """Return coverage + confidence for a generated plan.

    ``integrity_reason`` non-empty forces ``low`` / fail-closed (compose with
    :func:`check_cypher_filter_integrity` without deleting it).

    ``schema_reason`` non-empty forces ``low`` / fail-closed even when filter
    tokens appear "bound" in the plan text (invented hops like ``HAS_OFFERED_IN``
    can still embed the NL value while returning empty/zero — see
    :mod:`schema_valid_cypher`).

    ``dim_binds`` — unique :class:`~infona_client.nlp.dim_registry.DimBind`
    list from the dim registry for this question. Each unique bind is a
    **required predicate** (leaf + value). On aggregate/count plans, any
    missing unique bind fails closed — even if a *different* leaf provides
    ``plan_has_dimension_filter`` True (wrong-leaf / multi-filter drop class).
    Ambiguous registry tokens are never passed here (bind path is unique-only).

    ``populated_types`` / ``type_counts`` — live inventory; empty primary types
    while the question matches populated types fail closed. Omitted → skip.
    """
    params = dict(params or {})
    tmpl = (template or "").strip() or None
    sk = sketch or sketch_query_intent(question)
    early = _host().count_vs_list_fail_closed(sk, tmpl)
    if early is not None:
        return early
    tokens = list(sk.filter_tokens)
    bound, unbound = _host().tokens_bound_in_plan(tokens, cypher, params)
    has_dim = _host().effective_has_dim_filter(
        cypher, params=params, template=tmpl, sketch=sk, unbound=unbound
    )
    is_agg_or_count = _plan_is_aggregate_or_count(cypher, tmpl, sk)
    filterish = sk.has_filter_intent or bool(tokens) or question_has_filter_intent(
        question
    )

    covered_binds, missing_binds = _host().split_dim_binds_coverage(
        dim_binds, cypher, params=params, template=tmpl
    )
    bound_bind_labels = tuple(_dim_bind_label(b) for b in covered_binds)
    unbound_bind_labels = tuple(_dim_bind_label(b) for b in missing_binds)
    n_unique_binds = len(covered_binds) + len(missing_binds)

    def _with_binds(**kwargs: Any) -> CoverageResult:
        """Inject registry bind labels into CoverageResult kwargs."""
        kwargs.setdefault("bound_dim_binds", bound_bind_labels)
        kwargs.setdefault("unbound_dim_binds", unbound_bind_labels)
        return CoverageResult(**kwargs)

    if integrity_reason:
        clarify = _host().build_clarification_prompt(unbound, sketch=sk)
        return _with_binds(
            ok=False,
            confidence="low",
            reason=f"filter integrity failed: {integrity_reason}",
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            clarification_prompt=clarify,
            fail_closed=True,
            sketch=sk,
        )

    # Invented schema hop: tokens may look bound (value appears as a literal)
    # but the relationship/attr is not in the ontology → high-conf zeros.
    # Fail closed regardless of aggregate vs list — invalid hop is never high.
    if schema_reason:
        clarify = _host().build_clarification_prompt(unbound or tokens, sketch=sk)
        return _with_binds(
            ok=False,
            confidence="low",
            reason=f"schema-invalid predicates: {schema_reason}",
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            clarification_prompt=clarify,
            fail_closed=True,
            sketch=sk,
            extra={"schema_reason": schema_reason},
        )

    # --- Zero-instance primary types (live inventory) -----------------------
    # Pollution type with 0 entities (e.g. empty Product shell) while the
    # question matches a populated type (Widget/Sensor). Even with a dim
    # filter this returns 0 @ high conf — fail closed + regenerate.
    zero_hit = _host().zero_instance_type_coverage(
        question,
        cypher,
        params=params,
        populated_types=populated_types,
        type_counts=type_counts,
    )
    if zero_hit is not None:
        empty_plan, matched_pops = zero_hit
        reason = (
            f"plan primary type(s) have 0 entities in this KG "
            f"({', '.join(empty_plan)}) while question matches populated "
            f"type(s) ({', '.join(matched_pops)}) — pollution/empty-type "
            f"total risk (would return 0 with false high confidence)"
        )
        return _with_binds(
            ok=False,
            confidence="low",
            reason=reason,
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            clarification_prompt="",
            fail_closed=True,
            sketch=sk,
            empty_plan_types=empty_plan,
            matched_populated_types=matched_pops,
            extra={
                "zero_instance_types": list(empty_plan),
                "matched_populated_types": list(matched_pops),
            },
        )

    # --- Registry unique binds as required predicates (aggregate/count) ------
    # Residual class: wrong leaf still looks "filtered" (has_dim True) and
    # token string appears, so pre-bind coverage returned high. Fail closed.
    if missing_binds and is_agg_or_count:
        labels = ", ".join(unbound_bind_labels)
        if n_unique_binds >= 2:
            reason = (
                f"multi-bind dim-registry coverage fail: {len(missing_binds)}/"
                f"{n_unique_binds} unique binds missing from plan "
                f"({labels}) — aggregate/count would drop filter(s)"
            )
        else:
            reason = (
                f"dim-registry unique bind not applied in aggregate plan: "
                f"{labels} — wrong leaf or missing value constraint "
                f"(token string alone is not enough)"
            )
        # Prefer clarifying with the bound leaf so the user (and LLM retry)
        # know which field is required, not just the raw token.
        clarify_toks = [
            f"{b.token}→{b.dim.leaf}" for b in missing_binds
        ] or list(unbound or tokens)
        clarify = _host().build_clarification_prompt(clarify_toks, sketch=sk)
        return _with_binds(
            ok=False,
            confidence="low",
            reason=reason,
            unbound_tokens=tuple(unbound or [b.token for b in missing_binds]),
            bound_tokens=tuple(bound),
            clarification_prompt=clarify,
            fail_closed=True,
            sketch=sk,
            extra={
                "dim_binds_total": n_unique_binds,
                "dim_binds_missing": len(missing_binds),
            },
        )

    # Non-aggregate list/detail: report unbound registry binds as soft medium.
    if missing_binds and not is_agg_or_count:
        labels = ", ".join(unbound_bind_labels)
        reason = (
            f"soft gap: dim-registry unique bind(s) not applied in plan "
            f"({labels}); plan is not an aggregate/count total"
        )
        # If there is also no dim filter at all under filter intent, still
        # fall through to the harder pure-type gates below when applicable;
        # otherwise soft-pass with medium confidence.
        if has_dim or not filterish:
            return _with_binds(
                ok=True,
                confidence="medium",
                reason=reason,
                unbound_tokens=tuple(unbound or [b.token for b in missing_binds]),
                bound_tokens=tuple(bound),
                clarification_prompt=_host().build_clarification_prompt(
                    [b.token for b in missing_binds], sketch=sk
                ),
                fail_closed=False,
                sketch=sk,
            )
        # No dim filter + missing registry binds on a list plan: still soft
        # unless pure type scan (handled below). Keep labels for later returns
        # by continuing with filterish path.

    # Fail-closed: filter intent + aggregate/count, no dim (silent totals).
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
        clarify = _host().build_clarification_prompt(unbound or tokens, sketch=sk)
        return _with_binds(
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
        clarify = _host().build_clarification_prompt(unbound or tokens, sketch=sk)
        return _with_binds(
            ok=False,
            confidence="low",
            reason=reason,
            unbound_tokens=tuple(unbound or tokens),
            bound_tokens=tuple(bound),
            clarification_prompt=clarify,
            fail_closed=True,
            sketch=sk,
        )

    # --- Multi-filter fail-closed (aggregate/count) --------------------------------
    # Product class: DockA + ready total cost → silent unfiltered (or single-
    # filter) SUM of the measure with high conf. When the question has ≥2
    # filter constraints (tokens and/or unique dim-registry binds), the plan
    # must apply ≥2 real dim filters (or cover every unique registry bind).
    # A single-filter aggregate under multi-filter intent is fail-closed —
    # never medium/high silent total.
    required_filters = max(len(tokens), n_unique_binds)
    if n_unique_binds >= 2:
        # Registry is authoritative when it uniquely bound ≥2 dims.
        applied_filters = len(covered_binds)
    elif n_unique_binds == 1:
        applied_filters = len(covered_binds) + max(
            0, len(bound) - 1
        )  # one bind + any extra bound token
        if has_dim and applied_filters < 1:
            applied_filters = 1
    else:
        # No registry binds: count bound tokens when a dim filter is present.
        applied_filters = len(bound) if has_dim else 0
        if has_dim and applied_filters == 0:
            applied_filters = 1  # weak single filter signal

    multi_filter_intent = required_filters >= 2 or (
        len(tokens) >= 2 and (filterish or sk.has_filter_intent)
    )
    if multi_filter_intent and is_agg_or_count and applied_filters < min(
        2, required_filters if required_filters >= 2 else 2
    ):
        # Need at least 2 real dim filters when ≥2 constraints were asked.
        need = max(2, n_unique_binds) if n_unique_binds >= 2 else 2
        reason = (
            f"multi-filter intent ({required_filters} constraint(s); tokens="
            f"{list(tokens)[:6]!r}"
            + (
                f", dim_binds={list(unbound_bind_labels) + list(bound_bind_labels)}"
                if n_unique_binds
                else ""
            )
            + f") but plan applies only {applied_filters} real dim filter(s) "
            f"(need ≥{need}) — silent wrong total risk; apply ALL filters "
            "before SUM/COUNT"
        )
        clarify_toks = list(unbound) or list(tokens)
        if missing_binds:
            clarify_toks = [
                f"{b.token}→{b.dim.leaf}" for b in missing_binds
            ] + clarify_toks
        clarify = _host().build_clarification_prompt(clarify_toks, sketch=sk)
        # Explicit dual-constraint feedback when we know both tokens.
        if len(tokens) >= 2:
            a, b = tokens[0], tokens[1]
            extra_fb = (
                f"Apply BOTH filters (e.g. constrain by '{a}' AND '{b}') "
                "before SUM/COUNT — do not emit a single-filter or unfiltered "
                "measure aggregate."
            )
            if clarify:
                clarify = f"{clarify} {extra_fb}"
            else:
                clarify = extra_fb
        return _with_binds(
            ok=False,
            confidence="low",
            reason=reason,
            unbound_tokens=tuple(unbound or tokens),
            bound_tokens=tuple(bound),
            clarification_prompt=clarify,
            fail_closed=True,
            sketch=sk,
            extra={
                "multi_filter_required": required_filters,
                "multi_filter_applied": applied_filters,
            },
        )

    # Multi-token AND: only fail-closed when also aggregate/count-ish ---
    if len(tokens) >= 2 and len(bound) <= 1 and is_agg_or_count and not has_dim:
        reason = (
            f"multi-constraint question ({len(tokens)} filter-like tokens) but "
            f"only {len(bound)} appear in the plan (aggregate/count risk)"
        )
        clarify = _host().build_clarification_prompt(unbound, sketch=sk)
        return _with_binds(
            ok=False,
            confidence="low",
            reason=reason,
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            clarification_prompt=clarify,
            fail_closed=True,
            sketch=sk,
        )

    # Multi-token partial with a dim filter on non-aggregate plans: soft medium
    # (lists/details may still be useful). Aggregates already fail-closed above.
    if len(tokens) >= 2 and len(bound) == 1 and has_dim and not is_agg_or_count:
        reason = (
            f"partial multi-constraint coverage: {len(bound)}/{len(tokens)} filter "
            "tokens bound; plan has a dimension filter"
        )
        clarify = _host().build_clarification_prompt(unbound, sketch=sk)
        conf: QueryConfidence = "medium"
        # When registry unique binds are all covered, upgrade signal.
        if n_unique_binds >= 1 and not missing_binds and covered_binds:
            conf = "high"
            reason = (
                "constraint coverage ok (dim-registry unique binds applied; "
                f"text tokens partial {len(bound)}/{len(tokens)})"
            )
        return _with_binds(
            ok=True,
            confidence=conf,
            reason=reason,
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            clarification_prompt=clarify if conf == "medium" else "",
            fail_closed=False,
            sketch=sk,
        )

    # --- Soft: tokens exist, some unbound, but dim filter present ---
    if tokens and unbound and has_dim and bound:
        reason = (
            f"soft gap: {len(unbound)} filter token(s) unbound but plan has a "
            "dimension filter"
        )
        conf2: QueryConfidence = "medium"
        if n_unique_binds >= 1 and not missing_binds and covered_binds:
            conf2 = "high"
            reason = (
                "constraint coverage ok (dim-registry unique binds applied; "
                f"{len(unbound)} non-registry text token(s) unbound)"
            )
        return _with_binds(
            ok=True,
            confidence=conf2,
            reason=reason,
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            clarification_prompt=(
                _host().build_clarification_prompt(unbound, sketch=sk)
                if conf2 == "medium"
                else ""
            ),
            fail_closed=False,
            sketch=sk,
        )

    # Unbound tokens on a non-aggregate free-form plan: soft medium, still OK.
    if tokens and not bound and not has_dim and not is_agg_or_count:
        return _with_binds(
            ok=True,
            confidence="medium",
            reason=(
                "filter tokens not visible in plan params/text, but plan is not an "
                "aggregate/count total — soft gap only"
            ),
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            clarification_prompt=_host().build_clarification_prompt(unbound, sketch=sk),
            fail_closed=False,
            sketch=sk,
        )

    # --- Soft: filter intent, has dim filter, no extractable tokens ---
    if filterish and has_dim and not tokens:
        # Still require registry binds when present (already handled for agg;
        # for non-agg missing_binds soft path above). All good if no missing.
        if missing_binds:
            return _with_binds(
                ok=True,
                confidence="medium",
                reason=(
                    "filter intent with dim filter, but dim-registry unique bind(s) "
                    f"missing: {', '.join(unbound_bind_labels)}"
                ),
                unbound_tokens=(),
                bound_tokens=(),
                fail_closed=False,
                sketch=sk,
            )
        return _with_binds(
            ok=True,
            confidence="high",
            reason="filter intent covered by dimension filter in plan"
            + (
                f"; dim-registry binds: {', '.join(bound_bind_labels)}"
                if bound_bind_labels
                else ""
            ),
            unbound_tokens=(),
            bound_tokens=(),
            fail_closed=False,
            sketch=sk,
        )

    # --- Tokens all bound (or none) + dim filter or no filter intent ---
    if filterish and has_dim:
        # Registry all covered (or none supplied) → high.
        if missing_binds:
            # Non-agg already soft-returned above; agg already hard-failed.
            # Defensive medium.
            return _with_binds(
                ok=True,
                confidence="medium",
                reason=(
                    "dimension filter present but dim-registry unique bind(s) "
                    f"missing: {', '.join(unbound_bind_labels)}"
                ),
                unbound_tokens=tuple(unbound),
                bound_tokens=tuple(bound),
                fail_closed=False,
                sketch=sk,
            )
        return _with_binds(
            ok=True,
            confidence="high",
            reason="constraint coverage ok (dimension filter present"
            + (f"; tokens bound: {', '.join(bound)}" if bound else "")
            + (
                f"; dim-registry binds: {', '.join(bound_bind_labels)}"
                if bound_bind_labels
                else ""
            )
            + ")",
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            fail_closed=False,
            sketch=sk,
        )

    if not filterish:
        # No filter intent from sketch, but registry uniquely bound tokens
        # on an aggregate still require those binds (already handled above).
        return _with_binds(
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
        return _with_binds(
            ok=True,
            confidence="medium",
            reason="filter intent; free-form plan has constraining filter signals",
            unbound_tokens=tuple(unbound),
            bound_tokens=tuple(bound),
            fail_closed=False,
            sketch=sk,
        )

    # Remaining filter-intent free-form (not aggregate, not pure type): soft OK.
    return _with_binds(
        ok=True,
        confidence="medium",
        reason=(
            "filter intent present; free-form plan is not a known unfiltered "
            "aggregate/type-total shape — soft confidence only"
        ),
        unbound_tokens=tuple(unbound),
        bound_tokens=tuple(bound),
        clarification_prompt=_host().build_clarification_prompt(
            unbound or tokens, sketch=sk
        ),
        fail_closed=False,
        sketch=sk,
    )
