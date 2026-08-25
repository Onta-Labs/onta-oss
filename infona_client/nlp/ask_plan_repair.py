"""Deterministic post-LLM /ask plan repair (always-LLM; no golden Cypher).

The model still emits row-returning list helpers on how-many questions and
mints ``average_<leaf>`` columns. This module rewrites the **plan** onto
allowlisted templates / declared leaves. It never invents free-form Cypher.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from infona_client.nlp.query_intent import sketch_query_intent

# List helpers that already have a named count twin. Fail-close + repair share
# this map. Do not include related_entity_name_filter until that twin exists.
LIST_COUNT_TWINS: dict[str, str] = {
    "literal_values": "literal_values_count",
    "literal_compare": "literal_compare_count",
    "entities_of_type": "entities_of_type_count",
}

_PROP_KEYS = (
    "prop_key",
    "cost_prop",
    "cost_prop_key",
    "price_prop",
    "measure_prop",
    "prop",
)

# average_X / avg_X / mean_X / total_X — inner must already be a known leaf.
_AGG_WRAP_RE = re.compile(r"^(?:average|avg|mean|total)_(.+)$")
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class RepairedAskPlan:
    """Plan fields after repair. ``changed`` is True when anything rewrote."""

    template: str | None
    params: dict[str, Any]
    cypher: str
    changed: bool
    notes: tuple[str, ...] = ()


def known_leaves_from_inventory(
    inventory: Any | None,
    extra: Iterable[str] = (),
) -> frozenset[str]:
    """Collect declared/populated attribute leaves already on the plan."""
    out: set[str] = set()
    if inventory is not None:
        for group in (
            getattr(inventory, "attribute_leaves", None) or (),
            getattr(inventory, "allowed_prop_keys", None) or (),
        ):
            for item in group:
                s = str(item).strip()
                if s:
                    out.add(s)
    for item in extra:
        s = str(item or "").strip()
        if s:
            out.add(s)
    return frozenset(out)


def unwrap_agg_prefixed_prop(
    key: str | None,
    known_leaves: Iterable[str],
) -> str | None:
    """Return the wrapped leaf when ``key`` is average_/avg_/mean_/total_ of it.

    Only rewrites when the prefixed name is **not** itself a declared leaf and
    the inner noun is. Unrelated keys (``unit_cost``, ``vendor_code``, a wrap
    whose noun is absent) are left alone.
    """
    from infona_client.nlp.numeric_attr_resolve import normalize_leaf_key

    raw = (key or "").strip()
    if not raw:
        return None
    index: dict[str, str] = {}
    for leaf in known_leaves:
        s = str(leaf).strip()
        n = normalize_leaf_key(s)
        if s and n and n not in index:
            index[n] = s
    if not index:
        return None
    norm = normalize_leaf_key(raw)
    if not norm or norm in index:
        return None
    m = _AGG_WRAP_RE.match(norm)
    if not m:
        return None
    inner = m.group(1)
    hit = index.get(inner)
    if not hit or hit == raw:
        return None
    return hit


def repair_ask_plan(
    *,
    question: str,
    template: str | None,
    params: Mapping[str, Any] | None = None,
    cypher: str = "",
    known_leaves: Iterable[str] = (),
) -> RepairedAskPlan:
    """Rewrite list→count twins and agg-wrapped prop keys on an LLM plan.

    Count repair inspects NL intent + template **name**, then swaps Cypher to
    the allowlisted twin body. Prop repair uses ``known_leaves`` from the
    plan/schema — never a dataset-specific hardcode.
    """
    params_out = dict(params or {})
    tmpl = (template or "").strip() or None
    cypher_out = cypher or ""
    notes: list[str] = []

    if tmpl and "count" in (sketch_query_intent(question).aggregate_ops or ()):
        twin = LIST_COUNT_TWINS.get(tmpl)
        body = _allowlisted_cypher(twin) if twin else None
        if twin and body:
            tmpl = twin
            cypher_out = body
            notes.append(f"count_twin:{twin}")

    leaves = known_leaves_from_inventory(None, extra=known_leaves)
    for pk in _PROP_KEYS:
        if pk not in params_out:
            continue
        cur = params_out.get(pk)
        cur_s = "" if cur is None else str(cur).strip()
        unwrapped = unwrap_agg_prefixed_prop(cur_s, leaves)
        if not unwrapped:
            continue
        params_out[pk] = unwrapped
        cypher_out = _rewrite_ident(cypher_out, cur_s, unwrapped)
        notes.append(f"unwrap:{pk}:{cur_s}->{unwrapped}")

    changed = bool(notes)
    return RepairedAskPlan(
        template=tmpl,
        params=params_out,
        cypher=cypher_out,
        changed=changed,
        notes=tuple(notes),
    )


def apply_ask_plan_repair(
    gen: dict[str, Any],
    *,
    question: str,
    params: Mapping[str, Any] | None,
    cypher: str,
    inventory: Any | None = None,
    extra_leaves: Iterable[str] = (),
) -> tuple[dict[str, Any], str, bool]:
    """Mutate ``gen`` when repair applies. Returns ``(params, cypher, changed)``."""
    leaves = known_leaves_from_inventory(inventory, extra=extra_leaves)
    repaired = repair_ask_plan(
        question=question,
        template=gen.get("template"),
        params=params,
        cypher=cypher,
        known_leaves=leaves,
    )
    if not repaired.changed:
        return dict(params or {}), cypher, False
    gen["template"] = repaired.template
    gen["params"] = repaired.params
    if repaired.cypher:
        gen["cypher"] = repaired.cypher
    return repaired.params, repaired.cypher, True


def _allowlisted_cypher(name: str | None) -> str | None:
    if not name:
        return None
    try:
        from infona_client.graph.schema_bootstrap import TEMPLATES
    except Exception:
        return None
    tmpl = TEMPLATES.get(name)
    if tmpl is None or getattr(tmpl, "writing", False):
        return None
    body = (getattr(tmpl, "cypher", None) or "").strip()
    return body or None


def _rewrite_ident(text: str, old: str, new: str) -> str:
    """Replace identifier ``old`` with ``new``; no-op on unsafe names."""
    if not text or not old or old == new:
        return text
    if not _SAFE_IDENT_RE.match(old) or not _SAFE_IDENT_RE.match(new):
        return text
    return re.sub(rf"\b{re.escape(old)}\b", new, text)


__all__ = [
    "LIST_COUNT_TWINS",
    "RepairedAskPlan",
    "apply_ask_plan_repair",
    "known_leaves_from_inventory",
    "repair_ask_plan",
    "unwrap_agg_prefixed_prop",
]
