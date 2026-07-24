"""Read the ENTIRE Global ontology (Public + Enhanced layers) in one pass.

Backs the operator-only browser ``GET /operator/ontology/global``. One batched
:func:`~cograph_client.graph.ontology_queries.full_ontology_detail_query` per
layer graph — no N+1 per-type round trips — assembled into the flat, sorted,
search-friendly payload the Explorer renders.

Reads exactly what the premium ``GlobalShapeWriter`` writes (``cograph/
governance/writer.py``, read-only reference — never imported here, OSS
boundary): a type as ``rdf:type rdfs:Class`` + ``rdfs:label`` + optional
``rdfs:comment``, and per slot a property under ``<type>/attrs/<slot>`` with
``rdfs:label`` / ``rdfs:domain`` / ``rdfs:range`` / ``onto/coreSlot
"true"^^xsd:boolean`` / optional ``rdfs:comment`` (the slot rationale).

Degradation (mirrors :func:`~cograph_client.graph.layers.fetch_types_by_layer`,
ADR 0002 §1): a layer whose graph is missing or whose query raises is reported
``available=False`` with ``type_count=0`` and contributes no types; the other
layer is unaffected and the request still returns 200. An EMPTY Global ontology
— today's expected state — is likewise a normal 200 with ``types: []``, never
an error.
"""

from __future__ import annotations

from typing import Any

import structlog

from cograph_client.graph.layers import (
    Layer,
    enhanced_graph_uri,
    layer_from_uri,
    public_graph_uri,
    type_name_from_uri,
)
from cograph_client.graph.ontology_queries import (
    full_ontology_detail_query,
    xsd_to_datatype,
)
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.models.ontology import (
    GlobalOntologyAttribute,
    GlobalOntologyLayer,
    GlobalOntologyRelationship,
    GlobalOntologyResponse,
    GlobalOntologyType,
)

logger = structlog.stdlib.get_logger("cograph.graph.global_ontology")

#: The two GLOBAL layers, in the order they are reported. Deliberately excludes
#: ``Layer.TENANT`` — this browser is the cross-tenant canon, not one tenant's
#: ontology (which the tenant-scoped ``/graphs/{tenant}/ontology`` routes serve).
GLOBAL_LAYERS: tuple[tuple[Layer, str], ...] = (
    (Layer.PUBLIC, public_graph_uri()),
    (Layer.ENHANCED, enhanced_graph_uri()),
)

#: Lexical forms a SPARQL boolean/marker literal can arrive as. The writer emits
#: ``"true"^^xsd:boolean`` (parsed to the bare string ``"true"``), but a marker
#: hand-written as a plain literal must not silently read as False.
_TRUTHY = {"true", "1", "yes"}


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _name_key(name: str) -> str:
    """Case-insensitive alphabetical sort key (the contract's ordering)."""
    return name.lower()


def _pick(values: set[str]) -> str | None:
    """Deterministically choose ONE value for a predicate that arrived with
    several — ``min()`` over the candidate set.

    Everything folded here is SINGLE-VALUED by the ontology's own upsert
    contract (``upsert_type`` / ``upsert_attribute`` DELETE-then-INSERT exactly
    for that reason), but a graph written by a blind ``INSERT DATA``, a partial
    migration, or a hand edit can still carry two. Taking "the first row that
    bound it" would then make the RESPONSE depend on Neptune's row order, which
    SPARQL leaves unspecified: two identical requests could flip a slot between
    ``attributes`` and ``relationships`` (one XSD range, one ``types/…`` range).
    That is the same intermittent-by-row-order failure class the QC fuzzer
    caught in ER lineage, so it is closed by construction here rather than left
    merely unlikely. ``min()`` is arbitrary but total and stable; the query also
    carries an ``ORDER BY`` so the engine's own output is reproducible.
    """
    return min(values) if values else None


class _TypeAccumulator:
    """Mutable per-(layer, type) scratch built up across the query's rows.

    A batched query returns one row per (type × parent × slot) combination, so
    the same type name recurs; this folds those rows back into one record.
    Every folded field is collected as a SET and resolved by :func:`_pick` at
    build time — never "first row wins", which would inherit the engine's
    unspecified row order.
    """

    __slots__ = ("name", "layer", "descriptions", "parent_uris", "slots")

    def __init__(self, name: str, layer: str) -> None:
        self.name = name
        self.layer = layer
        self.descriptions: set[str] = set()
        #: Raw rdfs:subClassOf object URIs — kept as URIs, not names, because a
        #: bare name is not an identity across layers (see :meth:`parent`).
        self.parent_uris: set[str] = set()
        #: slot name -> {"descriptions": set, "ranges": set, "core": bool}
        self.slots: dict[str, dict[str, Any]] = {}

    def absorb(self, row: dict[str, str]) -> None:
        if row.get("typeComment"):
            self.descriptions.add(row["typeComment"])
        if row.get("parent"):
            self.parent_uris.add(row["parent"])

        attr_name = row.get("attrLabel")
        if not attr_name:
            return
        slot = self.slots.setdefault(
            attr_name, {"descriptions": set(), "ranges": set(), "core": False}
        )
        if row.get("attrComment"):
            slot["descriptions"].add(row["attrComment"])
        if row.get("range"):
            slot["ranges"].add(row["range"])
        # coreSlot is a MARKER, not a value: any row asserting it wins, so the
        # fold is order-independent without needing _pick.
        if _truthy(row.get("core")):
            slot["core"] = True

    def parent(self) -> tuple[Layer, str] | None:
        """The parent's LAYER-QUALIFIED identity, or None.

        ``rdfs:subClassOf`` may point outside every layer namespace (e.g.
        ``rdfs:Resource``), in which case the type is left un-parented rather
        than given an invented name.
        """
        uri = _pick(self.parent_uris)
        if not uri:
            return None
        layer = layer_from_uri(uri)
        name = type_name_from_uri(uri)
        if layer is None or not name:
            return None
        return layer, name

    def build(self, subtypes: list[str]) -> GlobalOntologyType:
        attributes: list[GlobalOntologyAttribute] = []
        relationships: list[GlobalOntologyRelationship] = []
        parent = self.parent()
        for slot_name, slot in self.slots.items():
            range_uri = _pick(slot["ranges"]) or ""
            # A slot is a RELATIONSHIP iff its range resolves to a type in ANY
            # layer namespace (tenant / enhanced / public). Everything else —
            # an XSD primitive, rdfs:Resource, a geo WKT literal, or no range
            # at all — is a literal attribute. Check the type namespaces FIRST:
            # xsd_to_datatype would otherwise happily reduce `types/X` to "X"
            # and mis-file a relationship as a primitive datatype.
            target = type_name_from_uri(range_uri) if range_uri else None
            if target:
                relationships.append(
                    GlobalOntologyRelationship(
                        name=slot_name,
                        target_type=target,
                        description=_pick(slot["descriptions"]),
                        core_slot=slot["core"],
                    )
                )
            else:
                attributes.append(
                    GlobalOntologyAttribute(
                        name=slot_name,
                        datatype=xsd_to_datatype(range_uri) if range_uri else "string",
                        description=_pick(slot["descriptions"]),
                        core_slot=slot["core"],
                    )
                )
        attributes.sort(key=lambda a: _name_key(a.name))
        relationships.sort(key=lambda r: _name_key(r.name))
        return GlobalOntologyType(
            name=self.name,
            layer=self.layer,
            description=_pick(self.descriptions),
            # The CONTRACT carries a bare name; the layer qualification is an
            # internal identity concern (see the children map below).
            parent_type=parent[1] if parent else None,
            subtypes=subtypes,
            attributes=attributes,
            relationships=relationships,
        )


async def fetch_global_ontology(neptune) -> GlobalOntologyResponse:
    """Assemble the full Global ontology payload — one query per layer graph.

    Never raises for an unreachable/erroring layer; see the module docstring.
    """
    layer_infos: list[GlobalOntologyLayer] = []
    accumulators: dict[tuple[str, str], _TypeAccumulator] = {}

    for layer, graph_uri in GLOBAL_LAYERS:
        available = True
        bindings: list[dict[str, str]] = []
        try:
            raw = await neptune.query(full_ontology_detail_query(graph_uri))
            _, bindings = parse_sparql_results(raw)
        except Exception:
            available = False
            logger.warning(
                "global_ontology_layer_unavailable",
                layer=layer.value,
                graph_uri=graph_uri,
                exc_info=True,
            )

        layer_types = 0
        for row in bindings:
            label = row.get("typeLabel", "")
            if not label:
                continue
            key = (layer.value, label)
            acc = accumulators.get(key)
            if acc is None:
                acc = _TypeAccumulator(label, layer.value)
                accumulators[key] = acc
                layer_types += 1
            acc.absorb(row)

        layer_infos.append(
            GlobalOntologyLayer(
                layer=layer.value,
                graph_uri=graph_uri,
                type_count=layer_types,
                available=available,
            )
        )

    # Invert rdfs:subClassOf across BOTH layers at once — an Enhanced type may
    # subclass a Public one, and the Public parent should still list it — but
    # key on the parent's LAYER-QUALIFIED identity, never its bare name. The
    # parent's layer comes from its URI namespace, so `types/x/Doctor
    # subClassOf types/x/Person` attaches to the ENHANCED Person only, and an
    # unrelated Public `Person` homonym is left alone. Name-keying would list
    # Doctor under both — the exact shadowing confusion this payload exists to
    # make visible.
    children: dict[tuple[str, str], set[str]] = {}
    for acc in accumulators.values():
        parent = acc.parent()
        if parent:
            parent_layer, parent_name = parent
            children.setdefault((parent_layer.value, parent_name), set()).add(acc.name)

    types = [
        # `children` values are SETS, so a key that folds two distinct names to
        # the same value (case) would leave their relative order to set
        # iteration — i.e. PYTHONHASHSEED-dependent, differing between API
        # workers for the same graph. Tie-break on the raw name for a total
        # order, same reason `_pick` uses min() rather than next(iter(...)).
        acc.build(
            sorted(
                children.get((acc.layer, acc.name), set()),
                key=lambda n: (_name_key(n), n),
            )
        )
        for acc in accumulators.values()
    ]
    # Alphabetical by name (case-insensitive); layer breaks ties so a name
    # declared in both layers has a stable order.
    types.sort(key=lambda t: (_name_key(t.name), t.layer))

    return GlobalOntologyResponse(layers=layer_infos, types=types)
