"""Dim-bind / token / plan-shape helpers for constraint coverage."""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

from infona_client.nlp.cypher_filter_integrity import (
    cypher_has_constraining_filter,
    pure_type_scan_without_filter,
)
from infona_client.nlp.dim_registry import normalize_dim_token
from infona_client.nlp.query_constraint_coverage_types import (
    DimBindLike,
    _AGG_RETURN_RE,
    _DIM_FILTER_TEMPLATES,
    _DIM_PARAM_KEYS,
    _DIM_VALUE_IN_CYPHER_RE,
    _MEASURE_ONLY_TEMPLATES,
    _PURE_TYPE_TEMPLATES,
)
from infona_client.nlp.query_intent import QueryIntentSketch


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
        if tmpl in ("literal_compare", "literal_compare_count") and (
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


_COUNT_DISTINCT_RE = re.compile(r"(?i)\bcount\s*\(\s*distinct\b")
_YEAR_TOKEN_RE = re.compile(r"(?:19|20)\d{2}")


def plan_has_distinct_count(cypher: str) -> bool:
    """True when the plan counts DISTINCT — covers unique-count intent."""
    return bool(_COUNT_DISTINCT_RE.search(cypher or ""))


def effective_has_dim_filter(
    cypher: str,
    *,
    params: dict[str, Any] | None = None,
    template: str | None = None,
    sketch: QueryIntentSketch | None = None,
    unbound: Sequence[str] | None = None,
) -> bool:
    """Dim-filter signal for fail-closed, with unique-count / year overlays.

    ``count(DISTINCT …)`` covers unique/distinct count intent. An unbound
    19xx/20xx year on an aggregate is still missing — another dim must not
    mask it.
    """
    has = plan_has_dimension_filter(cypher, params=params, template=template)
    if sketch is not None and getattr(sketch, "has_unique_count_intent", False):
        if plan_has_distinct_count(cypher):
            has = True
    if has and sketch is not None and (
        getattr(sketch, "has_aggregate_intent", False)
        or getattr(sketch, "has_unique_count_intent", False)
    ):
        for tok in unbound or ():
            if _YEAR_TOKEN_RE.fullmatch(str(tok).strip()):
                return False
    return has


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
