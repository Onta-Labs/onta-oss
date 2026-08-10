from infona_client.graph.iri import ENHANCED_GRAPH_URI, IRI_BASE, PUBLIC_GRAPH_URI, TYPE_URI_PREFIX
"""Ontology layers + precedence resolver (ADR 0002 §1).

Three layers, each a named graph with its own type-URI namespace:

  Tenant > Global-Enhanced > Global-Public

Precedence is resolved by SHADOWING: the highest visible layer that defines a
type name wins for that tenant's queries; lower layers are never mutated.
Non-entitled tenants simply do not see the Enhanced layer — resolution
degrades gracefully to ``Tenant > Public``, never errors.

Namespaces (one per layer, so shadowing is explicit and collisions impossible):

  Tenant   — https://graph.onta.sh/types/          (the EXISTING namespace,
             unchanged — existing data keeps resolving via type_uri())
  Enhanced — https://graph.onta.sh/types/x/
  Public   — https://graph.onta.sh/types/public/

**Reads are layered; writes are not (ONTA-397).** A workspace *read* sees the
merged stack via :class:`LayerStack` / :func:`~infona_client.graph.global_ontology.fetch_ontology`.
A workspace *write* (ordinary ontology mutation: create type, add attribute,
ingest schema mint, …) **always** goes to the tenant named graph. A workspace
must never write into a global layer through an ordinary mutation — only the
governed promotion path may, and only with consent (ONTA-402a). Isolation is
by **named graph**, not by type URI: two tenants' ``Person`` share the same
tenant-namespace URI; unioning graphs without scoping by the caller's stack
leaks tenant A into tenant B.

Everything here is additive and opt-in: default single-tenant, single-graph
behavior is the write path; layered reads are wired through
:func:`~infona_client.graph.entitlement.layer_stack_for`.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

from .ontology_queries import TYPE_URI_PREFIX, list_types_query, type_uri
from .parser import parse_sparql_results
from .queries import require_valid_type_name

logger = structlog.stdlib.get_logger("cograph.graph.layers")


class Layer(str, Enum):
    """Ontology layers, see module docstring for precedence."""

    TENANT = "tenant"
    ENHANCED = "enhanced"
    PUBLIC = "public"


# Shared named graphs for the two Global layers. The tenant layerf's graph URI
# stays whatever callers pass today (per-tenant, e.g. tenant_graph_uri()).
_PUBLIC_GRAPH_URI = PUBLIC_GRAPH_URI
_ENHANCED_GRAPH_URI = ENHANCED_GRAPH_URI

# Per-layer type-URI namespaces. TENANT is the existing namespace — do not
# change it, or every URI already written to Neptune stops resolving.
_TYPE_NAMESPACES = {
    Layer.TENANT: TYPE_URI_PREFIX,
    Layer.ENHANCED: f"{TYPE_URI_PREFIX}x/",
    Layer.PUBLIC: f"{TYPE_URI_PREFIX}public/",
}


def public_graph_uri() -> str:
    """Named graph holding the Global-Public ontology layer."""
    return _PUBLIC_GRAPH_URI


def enhanced_graph_uri() -> str:
    """Named graph holding the Global-Enhanced (premium delta) layer."""
    return _ENHANCED_GRAPH_URI


def type_namespace(layer: Layer) -> str:
    """Type-URI namespace prefix for a layer."""
    return _TYPE_NAMESPACES[layer]


def type_name_from_uri(uri: str) -> str | None:
    """Extract the bare type name from a type URI in ANY layer's namespace.

    Tries namespaces longest-first (the tenant namespace is a prefix of the
    other two, so order matters): `types/public/Person`, `types/x/Person`, and
    `types/Person` all yield "Person". Attribute URIs reduce to their type name
    (`types/Person/attrs/email` -> "Person"), matching the existing parent-map
    parsing. Returns None for URIs outside every layer namespace (e.g.
    rdfs:Resource), which callers skip.
    """
    for ns in sorted(_TYPE_NAMESPACES.values(), key=len, reverse=True):
        if uri.startswith(ns):
            name = uri[len(ns):].rstrip("/").split("/")[0]
            return name or None
    return None


def layer_from_uri(uri: str) -> Layer | None:
    """Which layer's namespace does `uri` live in? None if outside all of them.

    The layer half of :func:`type_name_from_uri` — same longest-prefix-first
    scan, so `types/public/Person` resolves to PUBLIC (not TENANT, whose
    namespace is a prefix of it). Callers that key anything on a type IDENTITY
    need this: a bare name is NOT an identity across layers, since Public and
    Enhanced may both declare `Person` and they are different types.
    """
    for ns, layer in sorted(
        ((ns, layer) for layer, ns in _TYPE_NAMESPACES.items()),
        key=lambda pair: len(pair[0]),
        reverse=True,
    ):
        if uri.startswith(ns):
            return layer
    return None


def layer_type_uri(layer: Layer, type_name: str) -> str:
    """Type URI for `type_name` in `layer`'s namespace.

    For TENANT this delegates to the existing type_uri() so the two can never
    drift — tenant URIs are exactly what they have always been.

    ONTA-425: the non-TENANT branch validates the name the same way ``type_uri``
    does. Without it this f-string would be the one way a caller-supplied name
    still reached a generated IRI unchecked, since every Explorer read resolves
    through here and a Public/Enhanced declaration is exactly as interpolatable
    as a tenant one.
    """
    if layer is Layer.TENANT:
        return type_uri(type_name)
    return f"{_TYPE_NAMESPACES[layer]}{require_valid_type_name(type_name)}"


@dataclass(frozen=True)
class LayerStack:
    """The ordered set of ontology layers visible to one tenant.

    Built from (tenant_graph_uri, entitled). Entitled tenants see
    [TENANT, ENHANCED, PUBLIC]; non-entitled see [TENANT, PUBLIC] — the
    Enhanced layer is silently excluded, never an error.

    **Version pin (ONTA-405):** optional ``public_version`` / ``enhanced_version``
    pin the stack to a published release graph (``…/v{N}``). ``None`` means the
    live global graph for that layer. When a pin points at a missing / empty
    snapshot, loaders degrade to an **empty** layer (not silent fall-through to
    live) so a pinned workspace's effective ontology cannot jump to latest when
    the snapshot is unavailable — pin stability is the core property.
    """

    tenant_graph_uri: str
    entitled: bool = False
    # None => live global public / enhanced graph (pre-pin or "track live until
    # first release"). Set to a positive int to pin at release graph …/v{N}.
    public_version: int | None = None
    enhanced_version: int | None = None

    @property
    def layers(self) -> tuple[Layer, ...]:
        """Visible layers in precedence order (highest first)."""
        if self.entitled:
            return (Layer.TENANT, Layer.ENHANCED, Layer.PUBLIC)
        return (Layer.TENANT, Layer.PUBLIC)

    def graph_uri_for(self, layer: Layer) -> str:
        """Named graph URI for ``layer``.

        Tenant is always the caller's tenant graph. Public / Enhanced resolve to
        a release snapshot URI when the corresponding version field is set;
        otherwise the live global graph. Missing snapshot content is **not**
        substituted with live here — callers that load the URI treat empty /
        missing as an empty layer (see module docstring).
        """
        if layer is Layer.TENANT:
            return self.tenant_graph_uri
        if layer is Layer.ENHANCED:
            if self.enhanced_version is not None:
                from infona_client.graph.ontology_commit import release_graph_uri

                return release_graph_uri(_ENHANCED_GRAPH_URI, self.enhanced_version)
            return _ENHANCED_GRAPH_URI
        # PUBLIC
        if self.public_version is not None:
            from infona_client.graph.ontology_commit import release_graph_uri

            return release_graph_uri(_PUBLIC_GRAPH_URI, self.public_version)
        return _PUBLIC_GRAPH_URI

    def visible_graph_uris(self) -> list[str]:
        """Graph URIs of the visible layers, in precedence order."""
        return [self.graph_uri_for(layer) for layer in self.layers]

    def layer_pairs(self) -> list[tuple[Layer, str]]:
        """``(Layer, graph_uri)`` pairs for :func:`~infona_client.graph.global_ontology.fetch_ontology`.

        Precedence order matches :attr:`layers` (first wins under shadowing).
        """
        return [(layer, self.graph_uri_for(layer)) for layer in self.layers]

    def resolve_type(
        self, name: str, types_by_layer: dict[Layer, dict[str, Any]]
    ) -> tuple[Layer, Any] | None:
        """Resolve `name` across the stack by shadowing.

        The first VISIBLE layer (in precedence order) that defines `name`
        wins; definitions in lower layers — or in layers not visible to this
        stack, e.g. ENHANCED for a non-entitled tenant — are ignored.
        Returns (layer, definition) or None if no visible layer defines it.
        """
        for layer in self.layers:
            defs = types_by_layer.get(layer)
            if defs is not None and name in defs:
                return layer, defs[name]
        return None


async def fetch_types_by_layer(neptune, stack: LayerStack) -> dict[Layer, dict[str, str]]:
    """Fetch existing types per visible layer (one list_types_query per graph).

    Returns {layer: {type_name: description}} for every layer in the stack —
    shaped to feed LayerStack.resolve_type. A layer whose graph is missing,
    empty, or errors yields {} (graceful degradation, mirroring
    SchemaResolver._fetch_ontology); other layers are unaffected.
    """
    types_by_layer: dict[Layer, dict[str, str]] = {}
    for layer in stack.layers:
        types: dict[str, str] = {}
        try:
            raw = await neptune.query(list_types_query(stack.graph_uri_for(layer)))
            _, bindings = parse_sparql_results(raw)
            for row in bindings:
                label = row.get("label", "")
                if label:
                    types[label] = row.get("comment", "")
        except Exception:
            # Degrade to an empty layer, never error (ADR 0002 §1).
            logger.warning("layer_types_fetch_failed", layer=layer.value, exc_info=True)
        types_by_layer[layer] = types
    return types_by_layer
