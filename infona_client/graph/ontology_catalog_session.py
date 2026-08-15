"""Catalog-scoped GraphSession resolution (the one backend switch)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from infona_client.graph.ontology_catalog_models import LayerName
from infona_client.graph.scope import GraphScope, GraphScopeError

if TYPE_CHECKING:
    from infona_client.graph.store import GraphSession, GraphStore


def resolve_catalog_session(
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    layer: LayerName | str = "tenant",
    tenant_id: str | None = None,
    privileged: bool = False,
) -> "GraphSession":
    """Return a catalog-scoped session for ontology-catalog work.

    Priority: explicit ``session`` → explicit ``store`` → the process store.
    Never returns ``None``: Neo4j is the only backend (ONTA-527), so there is
    no SPARQL path to hand back to. Raises :class:`GraphConfigError` when no
    store is configured.

    Global-catalog **writes** require ``privileged=True`` (model §3.3 T7);
    reads of public/enhanced may use a non-privileged session.
    """
    if session is not None:
        return session
    if store is None:
        from infona_client.graph.store import get_optional_graph_store

        store = get_optional_graph_store()
    layer_norm = (layer or "tenant").strip().lower()
    if layer_norm in ("public", "enhanced"):
        scope = GraphScope.for_catalog(layer=layer_norm, privileged=privileged)
    elif layer_norm == "tenant":
        scope = GraphScope.for_catalog(
            layer="tenant", tenant_id=tenant_id, privileged=privileged
        )
    else:
        raise GraphScopeError(
            f"Unknown catalog layer {layer!r}; expected public|enhanced|tenant"
        )
    return store.session(scope)
