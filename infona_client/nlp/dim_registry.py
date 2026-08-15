"""Low-cardinality dimension registry for NL planning / filter binding.

Tracks **data-driven** dims (literal enums and entity-range relationships)
per tenant+KG so NL can bind filter tokens ("Fall", "NorthFleet") to the
right type/attr/rel. Complements ontology ``[values: …]`` annotations and
constraint-coverage work — never hardcodes domain strings.

Thresholds (documented, tunable via module constants):

* Absolute distinct cap: ``MAX_DIM_CARDINALITY`` (default 50)
* Dynamic cap: ``max(MIN_DIM_FLOOR, ceil(RATIO * type_entity_count))``
* A leaf is a dim only when ``0 < distinct <= min(absolute, dynamic)``
* High uniqueness (``distinct / type_n > HIGH_UNIQUENESS`` with
  ``distinct > HIGH_UNIQUENESS_MIN_DISTINCT``) is treated as id-like and
  rejected even under the cap (small-type free-text trap)

Refresh: process-scoped cache keyed by ``(tenant_id, kg)``. Invalidated
best-effort from :func:`refresh_after_write`; rebuilt lazily on first
``/ask`` (or explicit :func:`refresh_dim_registry`).
"""

from __future__ import annotations

from infona_client.nlp.dim_registry_models import (  # noqa: F401
    BIND_AMBIGUITY_MARGIN,
    BIND_MIN_SCORE,
    DIM_CARDINALITY_RATIO,
    EDIT_DISTANCE_MAX,
    EDIT_DISTANCE_MAX_LEN,
    HIGH_UNIQUENESS,
    HIGH_UNIQUENESS_MIN_DISTINCT,
    MAX_DIM_CARDINALITY,
    MAX_VALUES_IN_PROMPT,
    MAX_VALUES_STORED,
    MIN_DIM_FLOOR,
    DimBind,
    DimBindResult,
    DimEntry,
    DimInventorySlot,
    DimRegistry,
    DimValue,
    _iso_now,
    build_registry_from_inventory,
    dim_cardinality_threshold,
    is_dim_eligible_leaf,
    make_dim_value,
    normalize_dim_token,
    passes_cardinality_gates,
)
from infona_client.nlp.dim_registry_bind import (  # noqa: F401
    _edit_distance,
    _score_value_match,
    _token_overlap,
    bind_filter_token,
    bind_filter_token_result,
    bind_tokens_in_question,
    extract_filter_tokens,
    format_dims_for_prompt,
    rank_filter_token_dims,
)
from infona_client.nlp.dim_registry_refresh import (  # noqa: F401
    collect_dim_inventory_from_store,
    ensure_dim_registry,
    get_cached_dim_registry,
    invalidate_dim_registry,
    planning_dim_binds,
    planning_dim_context,
    planning_dim_grounding,
    put_cached_dim_registry,
    refresh_dim_registry,
    reset_dim_registry_for_tests,
)


def _host():
    """Call-time lookup of this module (monkeypatch surface)."""
    from infona_client.nlp import dim_registry as _mod

    return _mod


__all__ = [
    "BIND_AMBIGUITY_MARGIN",
    "DIM_CARDINALITY_RATIO",
    "DimBind",
    "DimBindResult",
    "DimEntry",
    "DimInventorySlot",
    "DimRegistry",
    "DimValue",
    "MAX_DIM_CARDINALITY",
    "MIN_DIM_FLOOR",
    "bind_filter_token",
    "bind_filter_token_result",
    "bind_tokens_in_question",
    "build_registry_from_inventory",
    "collect_dim_inventory_from_store",
    "dim_cardinality_threshold",
    "ensure_dim_registry",
    "extract_filter_tokens",
    "format_dims_for_prompt",
    "get_cached_dim_registry",
    "invalidate_dim_registry",
    "is_dim_eligible_leaf",
    "normalize_dim_token",
    "passes_cardinality_gates",
    "planning_dim_binds",
    "planning_dim_context",
    "planning_dim_grounding",
    "put_cached_dim_registry",
    "rank_filter_token_dims",
    "refresh_dim_registry",
    "reset_dim_registry_for_tests",
]
