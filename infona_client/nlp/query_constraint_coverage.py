"""Post-gen constraint coverage + query confidence (filter-miss class).

Companion to :mod:`cypher_filter_integrity` (OPTIONAL MATCH smell). This module
checks whether generated Cypher **covers** NL constraints:

* filter intent / extracted filter tokens present in the question
* plan is not a silent unfiltered aggregate / pure type count when filters
  were asked for
* multi-constraint tokens not dropped wholesale
* **dim-registry unique binds** (leaf+value) actually applied — not merely that
  a token string appears next to a *different* leaf (wrong-leaf / multi-filter
  drop class)
* **zero-instance primary types** when live inventory is provided: plan must
  not target only empty pollution types while the question matches other
  *populated* types (high-conf empty totals class)

**Confidence** (attached to timing / NLResult):

* ``high`` — integrity OK + coverage OK
* ``medium`` — ran with soft gaps (plan has a dimension filter but multi-token
  coverage is partial: ≥1 token bound, not all)
* ``low`` — coverage fail, integrity fail, or ambiguous multi-bind with no
  usable filter

**Fail-closed:** never recommend executing an unfiltered aggregate/count when
the question has filter intent (or unbound filter tokens). Prefer clarification
over a silent wrong total. Unique registry binds on aggregate/count plans are
required predicates: missing any unique bind → fail-closed (even if some other
dimension filter is present). When ``populated_types`` / ``type_counts`` is
supplied, a plan whose primary types all have 0 entities while the question
matched other populated types is also fail-closed (retry with inventory
feedback) so we never ship ``0 @ high conf`` for a pollution type.

Anti-overfit: synthetic types/attrs/values only in tests; no persona gold.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, Sequence, runtime_checkable

from infona_client.nlp.cypher_filter_integrity import (
    cypher_has_constraining_filter,
    pure_type_scan_without_filter,
    question_has_filter_intent,
)
from infona_client.nlp.dim_registry import normalize_dim_token
from infona_client.nlp.query_intent import (
    QueryIntentSketch,
    sketch_query_intent,
)

QueryConfidence = Literal["high", "medium", "low"]


@runtime_checkable
class _DimValueLike(Protocol):
    display: str
    normalized: str


@runtime_checkable
class _DimEntryLike(Protocol):
    leaf: str
    kind: str
    subject_type: str

    @property
    def range_type(self) -> str | None: ...


@runtime_checkable
class DimBindLike(Protocol):
    """Minimal protocol for registry binds (avoids hard circular import shape)."""

    token: str
    dim: _DimEntryLike
    matched_value: _DimValueLike


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
    # Registry unique binds that are / aren't applied with the correct leaf.
    unbound_dim_binds: tuple[str, ...] = ()  # "leaf=value" labels
    bound_dim_binds: tuple[str, ...] = ()
    # Live inventory: plan types with 0 entities vs question-matched populated.
    empty_plan_types: tuple[str, ...] = ()
    matched_populated_types: tuple[str, ...] = ()

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
        if self.unbound_dim_binds:
            out["unbound_dim_binds"] = ", ".join(self.unbound_dim_binds)[:300]
        if self.bound_dim_binds:
            out["bound_dim_binds"] = ", ".join(self.bound_dim_binds)[:300]
        if self.empty_plan_types:
            out["empty_plan_types"] = ", ".join(self.empty_plan_types)[:300]
            out["query_zero_instance_type"] = 1.0
        if self.matched_populated_types:
            out["matched_populated_types"] = ", ".join(
                self.matched_populated_types
            )[:300]
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


def _dim_bind_label(bind: DimBindLike) -> str:
    """Human-readable leaf=value label for timing / feedback."""
    leaf = getattr(bind.dim, "leaf", "") or ""
    val = getattr(bind.matched_value, "display", "") or ""
    kind = (getattr(bind.dim, "kind", "") or "").lower()
    if kind == "entity_dim":
        rt = getattr(bind.dim, "range_type", None) or ""
        if rt:
            return f"{leaf}->{rt}={val}"
    return f"{leaf}={val}"


def _leaf_present_in_plan(
    leaf: str,
    cypher: str,
    params: dict[str, Any] | None,
) -> bool:
    """True when the plan constrains (or reads) this dim leaf, not a different one."""
    if not leaf:
        return False
    leaf_l = leaf.strip().lower()
    if not leaf_l:
        return False
    params = params or {}
    for key in ("prop_key", "rel_attr", "filter_prop_key", "filter_key"):
        raw = params.get(key)
        if raw is None:
            continue
        if str(raw).strip().lower() == leaf_l:
            return True
    c = (cypher or "").lower()
    # Free-form entity property: e.leaf / e[`leaf`] / p.name = 'leaf' / rel type.
    patterns = (
        f"e.{leaf_l}",
        f"e[`{leaf_l}`]",
        f'e["{leaf_l}"]',
        f"e['{leaf_l}']",
        f".{leaf_l}",
        f"['{leaf_l}']",
        f'["{leaf_l}"]',
        f"`{leaf_l}`",
        f"name = '{leaf_l}'",
        f'name = "{leaf_l}"',
        f":{leaf_l}",  # relationship type token
        f"[:{leaf_l}",
        f"-[:{leaf_l}",
    )
    if any(p in c for p in patterns):
        return True
    # Compact underscore-free form for camelCase leaves rarely used.
    compact = leaf_l.replace("_", "")
    if compact and compact != leaf_l:
        if f"e.{compact}" in c or f":{compact}" in c:
            return True
    # prop_key / rel_attr params already checked; also accept when $prop_key is
    # used and params.prop_key equals leaf (done above). When cypher has
    # p.name = $prop_key and prop_key is the leaf — covered by params.
    if "$prop_key" in c and str(params.get("prop_key", "")).strip().lower() == leaf_l:
        return True
    if "$rel_attr" in c and str(params.get("rel_attr", "")).strip().lower() == leaf_l:
        return True
    # Whole-word leaf token in cypher body (template free-form).
    if re.search(rf"(?i)(?<![A-Za-z0-9_]){re.escape(leaf_l)}(?![A-Za-z0-9_])", c):
        return True
    return False


def _value_present_in_plan(
    value_display: str,
    value_normalized: str,
    cypher: str,
    params: dict[str, Any] | None,
) -> bool:
    """True when the dim value is applied via params or an equality literal."""
    params = params or {}
    val_norm = (value_normalized or normalize_dim_token(value_display) or "").strip()
    if not val_norm and not (value_display or "").strip():
        return False

    def _matches(raw: Any) -> bool:
        if raw is None:
            return False
        s = str(raw).strip()
        if not s:
            return False
        n = normalize_dim_token(s)
        if val_norm and n == val_norm:
            return True
        # Compact match (NorthFleet vs north fleet).
        if val_norm and n.replace(" ", "") == val_norm.replace(" ", ""):
            return True
        return False

    for key in ("prop_value", "needle", "target_name"):
        if key in params and _matches(params.get(key)):
            return True

    # Equality / contains literals in free-form Cypher.
    display = (value_display or "").strip()
    c = cypher or ""
    candidates = {display, value_normalized or "", normalize_dim_token(display)}
    candidates = {x for x in candidates if x}
    for cand in candidates:
        # Quoted forms
        for q in ("'", '"'):
            if f"{q}{cand}{q}" in c:
                return True
        # Case-insensitive search over plan blob for multi-word / mixed case.
    blob = _plan_blob(cypher, params)
    for cand in candidates:
        for v in _token_variants(cand):
            if v and v in blob:
                # Value alone is not enough without leaf check (caller combines).
                return True
    return False


def plan_covers_dim_bind(
    bind: DimBindLike,
    cypher: str,
    *,
    params: dict[str, Any] | None = None,
    template: str | None = None,
) -> bool:
    """True when the plan constrains by this registry bind's leaf **and** value.

    Wrong-leaf plans (token string present, but different property) return
    False — the residual persona class this gate targets.
    """
    params = params or {}
    dim = bind.dim
    leaf = (getattr(dim, "leaf", None) or "").strip()
    matched = bind.matched_value
    display = getattr(matched, "display", "") or ""
    normalized = getattr(matched, "normalized", "") or normalize_dim_token(display)
    kind = (getattr(dim, "kind", "") or "").lower()

    leaf_ok = _leaf_present_in_plan(leaf, cypher, params)
    value_ok = _value_present_in_plan(display, normalized, cypher, params)

    if kind == "entity_dim":
        # Entity dims may use related_entity_name_filter with $target_name +
        # $rel_attr, or free-form MATCH on the relationship type / target label.
        tmpl = (template or "").strip()
        if tmpl in (
            "related_entity_name_filter",
            "related_entity_name_filter_inverse",
        ):
            rel_ok = _leaf_present_in_plan(leaf, cypher, params) or (
                str(params.get("rel_attr", "")).strip().lower() == leaf.lower()
            )
            # target_name carries the entity label/value.
            tgt_ok = value_ok or _param_nonempty(params, "target_name") and (
                normalize_dim_token(str(params.get("target_name", "")))
                == (normalized or normalize_dim_token(display))
            )
            if rel_ok and tgt_ok:
                return True
        # Free-form: need both relationship/leaf cue and target value.
        if leaf_ok and value_ok:
            return True
        # Some plans only put $target_name when rel is in cypher as type.
        if value_ok and leaf_ok:
            return True
        return False

    # Literal enum / default: require both leaf and value.
    return bool(leaf_ok and value_ok)


def split_dim_binds_coverage(
    binds: Sequence[DimBindLike] | None,
    cypher: str,
    *,
    params: dict[str, Any] | None = None,
    template: str | None = None,
) -> tuple[list[DimBindLike], list[DimBindLike]]:
    """Split unique registry binds into (covered, missing)."""
    if not binds:
        return [], []
    covered: list[DimBindLike] = []
    missing: list[DimBindLike] = []
    # Dedup by dim_id-ish (leaf+value) so multi-token same bind is once.
    seen: set[str] = set()
    for b in binds:
        label = _dim_bind_label(b)
        if label in seen:
            continue
        seen.add(label)
        if plan_covers_dim_bind(b, cypher, params=params, template=template):
            covered.append(b)
        else:
            missing.append(b)
    return covered, missing


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
        "6. When the dim registry bound a token to a specific leaf+value, the plan "
        "MUST constrain with THAT leaf (and the stored value) — filtering a "
        "different property while only mentioning the token string is not enough.",
        "7. Prefer $type_names / INSTANCE_OF targets from the LIVE populated type "
        "inventory. Do NOT target empty pollution types (0 entities in this KG) "
        "when the question matches a populated type.",
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


def _normalize_type_set(names: Sequence[str] | None) -> set[str]:
    out: set[str] = set()
    for n in names or ():
        s = str(n or "").strip()
        if s:
            out.add(s)
    return out


def resolve_populated_type_set(
    *,
    populated_types: Sequence[str] | None = None,
    type_counts: Mapping[str, int] | None = None,
) -> set[str] | None:
    """Return the set of type names with entity_count > 0, or None if unknown.

    ``None`` means inventory was not supplied — caller must skip the
    zero-instance gate (preserves hermetic tests that omit inventory).
    """
    if type_counts is not None:
        pops: set[str] = set()
        for name, cnt in type_counts.items():
            s = str(name or "").strip()
            if not s:
                continue
            try:
                n = int(cnt)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                pops.add(s)
        # Also union explicit populated_types when both given.
        pops |= _normalize_type_set(populated_types)
        return pops
    if populated_types is not None:
        return _normalize_type_set(populated_types)
    return None


def plan_primary_types(
    cypher: str,
    params: dict[str, Any] | None = None,
) -> set[str]:
    """Type names the plan uses as primary INSTANCE_OF / $type_names targets.

    Prefers explicit params (templates / confined generators), then falls back
    to :func:`~infona_client.nlp.empty_type_guard.types_referenced`.
    """
    from infona_client.nlp.empty_type_guard import types_referenced

    return set(types_referenced(cypher or "", params))


def zero_instance_type_coverage(
    question: str,
    cypher: str,
    *,
    params: dict[str, Any] | None = None,
    populated_types: Sequence[str] | None = None,
    type_counts: Mapping[str, int] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Detect pollution-type primary targets under live inventory.

    Returns ``(empty_plan_types, matched_populated_alternatives)`` when the
    plan's primary types are **all** zero-instance (or unknown-empty) in this
    KG **and** the question matches at least one *other* populated type.
    Returns ``None`` when inventory is missing or the plan is inventory-safe.

    Anti-overfit: pure string/count logic; no product type hardcodes.
    """
    populated = resolve_populated_type_set(
        populated_types=populated_types, type_counts=type_counts
    )
    if populated is None:
        return None
    # No populated inventory at all → nothing to prefer; skip gate.
    if not populated:
        return None

    plan_types = plan_primary_types(cypher, params)
    if not plan_types:
        return None

    # Case-insensitive membership against live inventory.
    pop_by_norm = {n.lower(): n for n in populated}

    def _is_populated(name: str) -> bool:
        return name.lower() in pop_by_norm

    empty_plan = sorted(t for t in plan_types if not _is_populated(t))
    populated_plan = [t for t in plan_types if _is_populated(t)]
    # Any primary type with instances → plan can return rows; not this class.
    if populated_plan:
        return None
    if not empty_plan:
        return None

    # Question matched populated types that are *not* the empty plan targets.
    from infona_client.nlp.query_build import match_question_types

    hits = match_question_types(question, sorted(populated))
    empty_norm = {t.lower() for t in empty_plan}
    alternatives = tuple(h for h in hits if h.lower() not in empty_norm)
    if not alternatives:
        return None
    return (tuple(empty_plan), alternatives)


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

    ``populated_types`` / ``type_counts`` — optional live GraphStore inventory
    (from :mod:`query_build`). When supplied, a plan whose primary types all
    have 0 entities while the question matches other populated types fails
    closed (pollution-type / high-conf empty class). Omitted → gate skipped.
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

    covered_binds, missing_binds = split_dim_binds_coverage(
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
        clarify = build_clarification_prompt(unbound, sketch=sk)
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
        clarify = build_clarification_prompt(unbound or tokens, sketch=sk)
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
    zero_hit = zero_instance_type_coverage(
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
        clarify = build_clarification_prompt(clarify_toks, sketch=sk)
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
                clarification_prompt=build_clarification_prompt(
                    [b.token for b in missing_binds], sketch=sk
                ),
                fail_closed=False,
                sketch=sk,
            )
        # No dim filter + missing registry binds on a list plan: still soft
        # unless pure type scan (handled below). Keep labels for later returns
        # by continuing with filterish path.

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
        clarify = build_clarification_prompt(unbound or tokens, sketch=sk)
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

    # --- Multi-token AND: only fail-closed when also aggregate/count-ish ---
    if len(tokens) >= 2 and len(bound) <= 1 and is_agg_or_count and not has_dim:
        reason = (
            f"multi-constraint question ({len(tokens)} filter-like tokens) but "
            f"only {len(bound)} appear in the plan (aggregate/count risk)"
        )
        clarify = build_clarification_prompt(unbound, sketch=sk)
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

    # Multi-token partial with a dim filter: if registry says ≥2 unique binds
    # and all are covered, stay high; if some missing, already handled above
    # for aggregates. For non-agg, soft medium remains.
    if len(tokens) >= 2 and len(bound) == 1 and has_dim:
        reason = (
            f"partial multi-constraint coverage: {len(bound)}/{len(tokens)} filter "
            "tokens bound; plan has a dimension filter"
        )
        clarify = build_clarification_prompt(unbound, sketch=sk)
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
                build_clarification_prompt(unbound, sketch=sk)
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
            clarification_prompt=build_clarification_prompt(unbound, sketch=sk),
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
    "DimBindLike",
    "QueryConfidence",
    "assign_query_confidence",
    "build_clarification_prompt",
    "check_constraint_coverage",
    "coverage_feedback",
    "fail_closed_answer",
    "plan_covers_dim_bind",
    "plan_has_dimension_filter",
    "plan_primary_types",
    "resolve_populated_type_set",
    "split_dim_binds_coverage",
    "tokens_bound_in_plan",
    "zero_instance_type_coverage",
]
