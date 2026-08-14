"""Layered type resolve and summary-cache eviction.

``invalidate_summary_cache`` runs *first* on every recompute so a retired
SPARQL scan cannot leave stale Explorer ``fieldStats`` (ONTA-534).
"""

from __future__ import annotations

from typing import Any

from infona_client.api.routes.explore_state import _summary_cache
from infona_client.auth.api_keys import TenantContext
from infona_client.graph.entitlement import layer_stack_for
from infona_client.graph.layers import Layer, fetch_types_by_layer, layer_type_uri


async def _resolve_layered_type(
    client: Any, tenant: TenantContext, type_name: str
) -> tuple[str, str, Layer] | None:
    """Resolve ``type_name`` across the workspace LayerStack (ONTA-397).

    Returns ``(type_uri, owning_graph_uri, layer)`` for the winning definition
    under first-visible-layer-wins shadowing, or ``None`` if no visible layer
    declares the name. Used by Explorer ontology-touching reads so empty-tenant
    + populated Public still surfaces Public types.
    """
    stack = layer_stack_for(tenant)
    types_by_layer = await fetch_types_by_layer(client, stack)
    resolved = stack.resolve_type(type_name, types_by_layer)
    if resolved is None:
        return None
    layer, _ = resolved
    return (
        layer_type_uri(layer, type_name),
        stack.graph_uri_for(layer),
        layer,
    )


def invalidate_summary_cache(tenant_id: str, kg_name: str) -> int:
    """Drop every in-process type-summary for one (tenant, kg).

    Explorer Browse chips AND table columns are derived from this cache
    (``fieldStats``), not from the live records payload. Enrichment's
    ``refresh_after_write`` → ``schedule_recompute`` used to evict only at the
    *end* of a SPARQL whole-KG scan. Under Neo4j that scan raises
    ``SparqlClientRetired``, ``_safe_recompute`` swallowed it, and the 30-minute
    cache kept serving the pre-enrich schema — refresh showed the old table
    even though facts had landed (job c7c2c7d2). Evict *first*, always.
    """
    dropped = 0
    for key in [k for k in _summary_cache if k[0] == tenant_id and k[1] == kg_name]:
        _summary_cache.pop(key, None)
        dropped += 1
    return dropped
