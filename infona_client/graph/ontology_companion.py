"""GraphStore companion state for ontology governance (ONTA-531).

Neo4j product path no longer has SPARQL named graphs for aliases, changelog,
revision counters, or frozen snapshot shapes. Those live here as structured
state on the process GraphStore:

* **aliases** — old_attr_uri → new_attr_uri (flattened by the reader)
* **changelog** — append-only commit entries (ONTA-401/403)
* **revisions** — monotonic workspaceRevision counter
* **frozen_shapes** — immutable OntologyShape JSON for ``…/v{N}`` / ``…/r{N}``
* **snapshots** — release/revision metadata lists per live graph

MemoryGraphStore tests and single-process Neo4j both hang the companion on
the store instance. Hermetic fixtures create a fresh store per test, so state
cannot leak. Callers must obtain the companion via :func:`get_ontology_companion`
so a missing bag is created once and reused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from infona_client.graph.iri import GRAPH_URI_PREFIX, IRI_BASE
from infona_client.graph.scope import (
    ENHANCED_KG,
    GLOBAL_TENANT_ID,
    PUBLIC_KG,
    GraphScope,
)

if TYPE_CHECKING:
    from infona_client.graph.store import GraphStore


@dataclass
class OntologyCompanion:
    """Mutable governance bag attached to a GraphStore instance."""

    # live_or_any graph_uri → {old_attr_uri: new_attr_uri}
    aliases: dict[str, dict[str, str]] = field(default_factory=dict)
    # live graph_uri → newest-first list of entry dicts
    changelog: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # live graph_uri → int counter
    revisions: dict[str, int] = field(default_factory=dict)
    # snapshot graph_uri → shape.as_dict()
    frozen_shapes: dict[str, dict[str, Any]] = field(default_factory=dict)
    # live graph_uri → list of release/revision metadata dicts (newest last)
    snapshots: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def clear(self) -> None:
        self.aliases.clear()
        self.changelog.clear()
        self.revisions.clear()
        self.frozen_shapes.clear()
        self.snapshots.clear()


def get_ontology_companion(store: "GraphStore | None" = None) -> OntologyCompanion:
    """Return the OntologyCompanion for ``store`` (process store if omitted)."""
    if store is None:
        from infona_client.graph.store import get_graph_store

        store = get_graph_store()
    bag = getattr(store, "_ontology_companion", None)
    if bag is None:
        bag = OntologyCompanion()
        try:
            store._ontology_companion = bag  # type: ignore[attr-defined]
        except Exception:
            # Immutable/proxy stores — still return a usable bag for this call.
            pass
    return bag


@dataclass(frozen=True, slots=True)
class CatalogTarget:
    """Resolved catalog write/read target for an ontology graph URI."""

    layer: str  # public | enhanced | tenant
    tenant_id: str | None  # None for global layers
    privileged: bool
    live_graph_uri: str  # stripped of trailing slash


_GLOBAL_PUBLIC_RE = re.compile(
    rf"^{re.escape(IRI_BASE)}/graphs/global/public(?:/.*)?$"
)
_GLOBAL_ENHANCED_RE = re.compile(
    rf"^{re.escape(IRI_BASE)}/graphs/global/enhanced(?:/.*)?$"
)
_TENANT_RE = re.compile(
    rf"^{re.escape(GRAPH_URI_PREFIX)}([^/]+)(?:/.*)?$"
)


def catalog_target_from_graph_uri(graph_uri: str) -> CatalogTarget:
    """Map a live (or any) ontology graph URI to a catalog layer/tenant.

    * ``…/graphs/global/public[…]`` → public layer (privileged)
    * ``…/graphs/global/enhanced[…]`` → enhanced layer (privileged)
    * ``…/graphs/{tenant}[…]`` → tenant catalog for that workspace

    Snapshot suffixes (``/vN``, ``/revisions/rN``, ``/changelog``, ``/versions``)
    are ignored for scope resolution — callers that need immutability checks
    must use :func:`~infona_client.graph.ontology_commit.is_immutable_version_graph`
    separately.
    """
    if not isinstance(graph_uri, str) or not graph_uri.strip():
        raise ValueError(f"graph_uri must be a non-empty string, got {graph_uri!r}")
    g = graph_uri.rstrip("/")

    if _GLOBAL_PUBLIC_RE.match(g) or g.endswith("/global/public"):
        # Live public graph (strip version/revision/companion suffixes).
        live = f"{IRI_BASE}/graphs/global/public"
        if "/v" in g[len(live):] or "/revisions/" in g or g.endswith("/changelog") or g.endswith("/versions"):
            # Keep live as the base public URI.
            pass
        return CatalogTarget(
            layer="public", tenant_id=None, privileged=True, live_graph_uri=live
        )
    if _GLOBAL_ENHANCED_RE.match(g) or g.endswith("/global/enhanced"):
        live = f"{IRI_BASE}/graphs/global/enhanced"
        return CatalogTarget(
            layer="enhanced", tenant_id=None, privileged=True, live_graph_uri=live
        )

    m = _TENANT_RE.match(g)
    if not m:
        raise ValueError(f"unrecognized ontology graph URI: {graph_uri!r}")
    tenant = m.group(1)
    if tenant == "global":
        # …/graphs/global without /public|/enhanced — refuse ambiguous scope.
        raise ValueError(
            f"ambiguous global ontology graph URI {graph_uri!r}; "
            "use …/graphs/global/public or …/graphs/global/enhanced"
        )
    live = f"{GRAPH_URI_PREFIX}{tenant}"
    return CatalogTarget(
        layer="tenant", tenant_id=tenant, privileged=False, live_graph_uri=live
    )


def catalog_session_kwargs(
    target: CatalogTarget, *, for_write: bool = True
) -> dict[str, Any]:
    """Keyword args for ontology_catalog public helpers / resolve_catalog_session.

    ``list_types`` / ``list_attributes`` do not accept ``privileged`` — pass
    ``for_write=False`` for those. Write helpers (``upsert_*``, ``delete_*``,
    marker setters) need ``privileged=True`` for global layers.
    """
    if target.layer in ("public", "enhanced"):
        kw: dict[str, Any] = {"layer": target.layer}
        if for_write:
            kw["privileged"] = True
        return kw
    kw = {"layer": "tenant", "tenant_id": target.tenant_id}
    if for_write:
        kw["privileged"] = target.privileged
    return kw


def live_graph_uri(graph_uri: str) -> str:
    """Strip companion/version suffixes to the live ontology graph URI."""
    return catalog_target_from_graph_uri(graph_uri).live_graph_uri


__all__ = [
    "CatalogTarget",
    "OntologyCompanion",
    "catalog_session_kwargs",
    "catalog_target_from_graph_uri",
    "get_ontology_companion",
    "live_graph_uri",
    "ENHANCED_KG",
    "GLOBAL_TENANT_ID",
    "PUBLIC_KG",
    "GraphScope",
]
