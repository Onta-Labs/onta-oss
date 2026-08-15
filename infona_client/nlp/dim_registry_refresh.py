"""Process-scoped dim-registry cache + GraphStore-backed refresh."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

import structlog

from infona_client.nlp.dim_registry_models import (
    MAX_DIM_CARDINALITY,
    DimBind,
    DimInventorySlot,
    DimRegistry,
    build_registry_from_inventory,
    dim_cardinality_threshold,
    is_dim_eligible_leaf,
    normalize_dim_token,
)

if TYPE_CHECKING:
    from infona_client.graph.store import GraphStore

logger = structlog.stdlib.get_logger("infona.nlp.dim_registry")


def _host():
    """Call-time lookup of the public ``dim_registry`` module."""
    from infona_client.nlp import dim_registry as _mod

    return _mod


_REGISTRY_CACHE: dict[tuple[str, str], DimRegistry] = {}


def get_cached_dim_registry(tenant_id: str, kg: str) -> DimRegistry | None:
    if not tenant_id or not kg:
        return None
    return _REGISTRY_CACHE.get((tenant_id, kg))


def put_cached_dim_registry(registry: DimRegistry) -> None:
    if not registry.tenant_id or not registry.kg:
        return
    _REGISTRY_CACHE[(registry.tenant_id, registry.kg)] = registry


def invalidate_dim_registry(
    tenant_id: str | None = None,
    kg: str | None = None,
) -> None:
    """Drop process cache entries. ``(None, None)`` clears all."""
    if tenant_id is None and kg is None:
        _REGISTRY_CACHE.clear()
        return
    if tenant_id is not None and kg is not None:
        _REGISTRY_CACHE.pop((tenant_id, kg), None)
        return
    # Partial: drop all for tenant or all for kg name.
    drop = [
        k
        for k in _REGISTRY_CACHE
        if (tenant_id is not None and k[0] == tenant_id)
        or (kg is not None and k[1] == kg)
    ]
    for k in drop:
        _REGISTRY_CACHE.pop(k, None)


def reset_dim_registry_for_tests() -> None:
    """Clear process cache (test isolation)."""
    _REGISTRY_CACHE.clear()


# ---------------------------------------------------------------------------
# GraphStore-backed refresh
# ---------------------------------------------------------------------------


async def _distinct_literal_values(
    session: Any,
    *,
    primary_type: str,
    prop_key: str,
    limit: int,
) -> list[str]:
    rows = await session.execute_template(
        "entity_type_prop_distinct",
        {
            "primary_type": primary_type,
            "prop_key": prop_key,
            "limit": int(limit),
        },
    )
    out: list[str] = []
    seen: set[str] = set()
    for r in rows or ():
        d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        val = d.get("value")
        if val is None:
            continue
        s = str(val).strip()
        if not s:
            continue
        key = normalize_dim_token(s)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


async def _distinct_rel_targets(
    session: Any,
    *,
    primary_type: str,
    rel_attr: str,
    limit: int,
) -> tuple[list[str], str | None]:
    rows = await session.execute_template(
        "entity_type_rel_target_distinct",
        {
            "primary_type": primary_type,
            "rel_attr": rel_attr,
            "limit": int(limit),
        },
    )
    out: list[str] = []
    seen: set[str] = set()
    range_type: str | None = None
    for r in rows or ():
        d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        val = d.get("value")
        if val is None:
            continue
        s = str(val).strip()
        if not s:
            continue
        key = normalize_dim_token(s)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s)
        if range_type is None:
            rt = d.get("target_type")
            if rt:
                range_type = str(rt)
    return out, range_type


async def collect_dim_inventory_from_store(
    store: "GraphStore",
    *,
    tenant_id: str,
    kg: str,
    type_names: Sequence[str] | None = None,
) -> list[DimInventorySlot]:
    """Probe instance inventory for dim candidates (capped distinct queries).

    Uses type_summary for leaf inventory + allowlisted distinct templates.
    Does **not** full-scan every entity attribute without a type filter.
    """
    if store is None or not tenant_id or not kg:
        return []

    from infona_client.graph.explore_store import type_summary
    from infona_client.graph.ontology_catalog import schema_types_for_kg
    from infona_client.graph.scope import GraphScope

    slots: list[DimInventorySlot] = []
    try:
        rows = await schema_types_for_kg(
            store, tenant_id=tenant_id, kg=kg, include_attrs=True
        )
    except Exception:
        logger.debug("dim_registry_schema_list_failed", exc_info=True)
        rows = []

    wanted = {n for n in (type_names or ()) if n}
    probe: list[str] = []
    for r in rows or ():
        name = getattr(r, "name", None)
        if not name:
            continue
        if wanted and name not in wanted:
            continue
        # Prefer types with instances; still probe force-wanted.
        ent = int(getattr(r, "entity_count", 0) or 0)
        if ent > 0 or (wanted and name in wanted):
            probe.append(name)
    # Also probe wanted names missing from catalog (instance-only types).
    seen = set(probe)
    for n in wanted:
        if n not in seen:
            probe.append(n)
            seen.add(n)

    if not probe:
        return []

    session = store.session(GraphScope.for_instance(tenant_id, kg))
    for tname in probe:
        try:
            summary = await type_summary(
                store=store,
                tenant_id=tenant_id,
                kg_name=kg,
                type_name=tname,
            )
        except Exception:
            summary = None
        if summary is None:
            continue
        type_n = int(getattr(summary, "entity_count", 0) or 0)
        if type_n <= 0:
            continue
        thr = dim_cardinality_threshold(type_n)
        # Oversample slightly so we can detect "above threshold".
        fetch_limit = min(MAX_DIM_CARDINALITY + 5, thr + 5)

        for attr in getattr(summary, "attributes", ()) or ():
            leaf = getattr(attr, "name", None) or ""
            if not leaf:
                continue
            dt = getattr(attr, "datatype", None) or "string"
            if not is_dim_eligible_leaf(leaf, kind="literal", datatype=dt):
                continue
            coverage = int(getattr(attr, "count", 0) or 0)
            if coverage <= 0:
                continue
            try:
                vals = await _distinct_literal_values(
                    session,
                    primary_type=tname,
                    prop_key=leaf,
                    limit=fetch_limit,
                )
            except Exception:
                logger.debug(
                    "dim_registry_literal_distinct_failed",
                    type=tname,
                    leaf=leaf,
                    exc_info=True,
                )
                continue
            slots.append(
                DimInventorySlot(
                    subject_type=tname,
                    leaf=leaf,
                    kind="literal",
                    datatype=dt,
                    values=tuple(vals),
                    distinct_count=len(vals),
                    coverage=coverage,
                    type_entity_count=type_n,
                )
            )

        for rel in getattr(summary, "relationships", ()) or ():
            leaf = getattr(rel, "name", None) or ""
            if not leaf:
                continue
            if not is_dim_eligible_leaf(leaf, kind="relationship"):
                continue
            coverage = int(getattr(rel, "count", 0) or 0)
            if coverage <= 0:
                continue
            range_type = getattr(rel, "target_type", None)
            try:
                vals, inferred_rt = await _distinct_rel_targets(
                    session,
                    primary_type=tname,
                    rel_attr=leaf,
                    limit=fetch_limit,
                )
            except Exception:
                logger.debug(
                    "dim_registry_rel_distinct_failed",
                    type=tname,
                    leaf=leaf,
                    exc_info=True,
                )
                continue
            if not range_type and inferred_rt:
                range_type = inferred_rt
            slots.append(
                DimInventorySlot(
                    subject_type=tname,
                    leaf=leaf,
                    kind="relationship",
                    range_type=str(range_type) if range_type else None,
                    values=tuple(vals),
                    distinct_count=len(vals),
                    coverage=coverage,
                    type_entity_count=type_n,
                )
            )
    return slots


async def refresh_dim_registry(
    store: "GraphStore",
    *,
    tenant_id: str,
    kg: str,
    type_names: Sequence[str] | None = None,
) -> DimRegistry:
    """Rebuild and cache the dim registry for ``tenant_id``+``kg``."""
    slots = await collect_dim_inventory_from_store(
        store, tenant_id=tenant_id, kg=kg, type_names=type_names
    )
    reg = build_registry_from_inventory(slots, tenant_id=tenant_id, kg=kg)
    put_cached_dim_registry(reg)
    logger.info(
        "dim_registry_refreshed",
        tenant_id=tenant_id,
        kg=kg,
        dims=len(reg.dims),
    )
    return reg


async def ensure_dim_registry(
    store: "GraphStore | None",
    *,
    tenant_id: str,
    kg: str,
    force: bool = False,
) -> DimRegistry | None:
    """Return cached registry or rebuild lazily (best-effort)."""
    if not tenant_id or not kg:
        return None
    if not force:
        cached = get_cached_dim_registry(tenant_id, kg)
        if cached is not None:
            return cached
    if store is None:
        return None
    try:
        return await refresh_dim_registry(store, tenant_id=tenant_id, kg=kg)
    except Exception:
        logger.debug("dim_registry_ensure_failed", exc_info=True)
        return get_cached_dim_registry(tenant_id, kg)


async def planning_dim_binds(
    store: "GraphStore | None",
    *,
    tenant_id: str,
    kg: str,
    question: str,
    type_hint: str | None = None,
) -> list[DimBind]:
    """Ensure registry + return unique :class:`DimBind` list for the question.

    Same bind path as the planning prompt (``bind_tokens_in_question``).
    Callers that only need structured binds for post-gen coverage use this;
    ambiguous tokens are omitted (fail-closed unique only).
    """
    reg = await ensure_dim_registry(store, tenant_id=tenant_id, kg=kg)
    if reg is None or not reg.dims:
        return []
    return _host().bind_tokens_in_question(question, reg, type_hint=type_hint)


async def planning_dim_context(
    store: "GraphStore | None",
    *,
    tenant_id: str,
    kg: str,
    question: str,
    type_hint: str | None = None,
) -> tuple[str, list[DimBind]]:
    """Ensure registry once; return ``(prompt_text, unique_binds)`` for /ask.

    Prefer this over calling :func:`planning_dim_grounding` +
    :func:`planning_dim_binds` separately so bind lists stay consistent
    between prompt grounding and constraint-coverage gates.
    """
    reg = await ensure_dim_registry(store, tenant_id=tenant_id, kg=kg)
    if reg is None or not reg.dims:
        return "", []
    binds = _host().bind_tokens_in_question(question, reg, type_hint=type_hint)
    return _host().format_dims_for_prompt(reg, binds=binds), binds


async def planning_dim_grounding(
    store: "GraphStore | None",
    *,
    tenant_id: str,
    kg: str,
    question: str,
    type_hint: str | None = None,
) -> str:
    """Ensure registry + format prompt block for /ask grounding spine."""
    text, _binds = await planning_dim_context(
        store,
        tenant_id=tenant_id,
        kg=kg,
        question=question,
        type_hint=type_hint,
    )
    return text

