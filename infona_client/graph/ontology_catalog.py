"""Ontology catalog on the property-graph store (E4 / model §5).

Minimal CRUD for ``:OntoType`` / ``:OntoAttr`` (+ ``SUBCLASS_OF`` /
``DECLARES`` / ``RANGE_TYPE``). Full ``ontology_commit`` / changelog /
snapshots stay deferred (model §10.1 B7) — see module TODO at bottom.

**Dual-backend:** when an explicit ``store`` / ``session`` is passed, or
``INFONA_GRAPH_BACKEND=neo4j``, mutations run through GraphStore templates.
Otherwise the legacy SPARQL builders in :mod:`ontology_queries` are executed
via the Neptune client (unchanged until cutover).

Scope sentinels (model §3.1):

* public  → ``tenant_id=__global__``, ``kg=public``,  ``layer=public``
* enhanced → ``tenant_id=__global__``, ``kg=enhanced``, ``layer=enhanced``
* tenant  → real ``tenant_id``, ``kg=__ontology__``, ``layer=tenant``

B2: attribute leaves that collide with reserved Entity property keys, and type
leaves that sanitize to reserved system labels, are rejected at schema time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Mapping, Optional, Sequence

from infona_client.graph.facts import (
    RESERVED_ENTITY_PROPERTY_KEYS,
    sanitize_prop_key,
)
from infona_client.graph.iri import TYPE_URI_PREFIX
from infona_client.graph.labels import (
    RESERVED_SYSTEM_LABELS,
    sanitize_domain_label,
)
from infona_client.graph.queries import (
    require_valid_type_name,
)
from infona_client.graph.scope import (
    ENHANCED_KG,
    GLOBAL_TENANT_ID,
    ONTOLOGY_KG,
    PUBLIC_KG,
    GraphScope,
    GraphScopeError,
)

if TYPE_CHECKING:
    from infona_client.graph.store import GraphSession, GraphStore

LayerName = Literal["public", "enhanced", "tenant"]
AttrKind = Literal["literal", "relationship"]

# Primitive datatypes accepted by the RDF-era upsert_attribute (literal range).
LITERAL_DATATYPES: frozenset[str] = frozenset(
    {"string", "integer", "float", "boolean", "datetime", "uri", "geo"}
)

VALID_CARDINALITIES: frozenset[str] = frozenset({"1:1", "1:N", "N:1", "N:N"})

# TODO(E5 / B7): ontology_commit, ontology_changelog, ontology_snapshots,
# ontology_base_pin, and versioned catalog history are **not** ported here.
# Callers that need governance history still use the SPARQL modules until a
# dedicated epic ports them onto ProvEvent / catalog events.


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OntoTypeRecord:
    """One ``:OntoType`` row (model §5.1)."""

    name: str
    layer: str
    tenant_id: str
    kg: str
    description: str = ""
    parent_type: str | None = None
    label_token: str | None = None
    uri: str | None = None


@dataclass(frozen=True, slots=True)
class OntoAttrRecord:
    """One ``:OntoAttr`` row (model §5.1)."""

    name: str
    domain: str
    layer: str
    tenant_id: str
    kg: str
    kind: AttrKind
    datatype: str | None = None
    range_type: str | None = None
    cardinality: str = "1:1"
    description: str = ""
    prop_key: str | None = None


@dataclass(frozen=True, slots=True)
class SchemaTypeSummary:
    """KG-scoped type row for future NL schema retrieval (minimal stub)."""

    name: str
    layer: str
    description: str = ""
    parent_type: str | None = None
    entity_count: int = 0
    attributes: tuple[OntoAttrRecord, ...] = ()


# ---------------------------------------------------------------------------
# Validation (B1 / B2)
# ---------------------------------------------------------------------------


def _validate_type_leaf(name: str) -> str:
    """Validate ontology type leaf; reject reserved system labels after sanitize."""
    leaf = require_valid_type_name(name, "type name")
    # Sanitizer raises on empty/unsafe/reserved labels — that is the B2 gate.
    sanitize_domain_label(leaf)
    return leaf


def _validate_attr_leaf(name: str) -> tuple[str, str | None]:
    """Validate attribute leaf; reject reserved Entity property keys (B2).

    Returns ``(original_leaf, prop_key_or_None)`` where ``prop_key`` is set only
    when the sanitized Entity property key differs from the original leaf.
    """
    leaf = require_valid_type_name(name, "attribute name")
    if leaf in RESERVED_ENTITY_PROPERTY_KEYS:
        raise GraphScopeError(
            f"Attribute name {leaf!r} collides with a reserved Entity property "
            "key (model B2); rename at schema time"
        )
    # sanitize_prop_key also rejects reserved after sanitization (e.g. weird
    # leaves that collapse onto ``id`` / ``tenant_id``).
    prop_key = sanitize_prop_key(leaf)
    return leaf, (None if prop_key == leaf else prop_key)


def _label_token_for(name: str) -> str:
    return sanitize_domain_label(name)


def _type_uri(name: str) -> str:
    # Mint via shared prefix — never hardcode host (model B5 spirit for catalog).
    return f"{TYPE_URI_PREFIX}{name}"


def classify_attr_range(
    datatype: str,
) -> tuple[AttrKind, str | None, str | None]:
    """Map RDF-era ``datatype`` arg to ``(kind, datatype, range_type)``.

    Primitives (``string``, ``integer``, …) → literal.
    Any other name is treated as a target type leaf → relationship.
    """
    if not isinstance(datatype, str) or not datatype.strip():
        raise GraphScopeError("Attribute datatype / range must be a non-empty string")
    dt = datatype.strip()
    if dt in LITERAL_DATATYPES:
        return "literal", dt, None
    # Relationship range — validate as a type leaf (also rejects reserved labels).
    target = _validate_type_leaf(dt)
    return "relationship", None, target


def layer_from_scope(scope: GraphScope) -> str:
    """Infer catalog layer name from a :class:`GraphScope`."""
    if scope.tenant_id == GLOBAL_TENANT_ID and scope.kg == PUBLIC_KG:
        return "public"
    if scope.tenant_id == GLOBAL_TENANT_ID and scope.kg == ENHANCED_KG:
        return "enhanced"
    if scope.kg == ONTOLOGY_KG:
        return "tenant"
    raise GraphScopeError(
        f"Scope tenant_id={scope.tenant_id!r} kg={scope.kg!r} is not a catalog "
        "scope; use GraphScope.for_catalog(...)"
    )


# ---------------------------------------------------------------------------
# Session resolution (dual-backend)
# ---------------------------------------------------------------------------


def graph_backend() -> str:
    """Same switch as :func:`infona_client.graph.kg_writer.graph_backend`."""
    return (os.environ.get("INFONA_GRAPH_BACKEND") or "neo4j").strip().lower()


def resolve_catalog_session(
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    layer: LayerName | str = "tenant",
    tenant_id: str | None = None,
    privileged: bool = False,
) -> Optional["GraphSession"]:
    """Return a catalog-scoped session when the Neo4j path should run.

    Priority: explicit ``session`` → explicit ``store`` → env ``neo4j`` backend.
    Returns ``None`` when the SPARQL path should be used instead.

    Global-catalog **writes** require ``privileged=True`` (model §3.3 T7);
    reads of public/enhanced may use a non-privileged session.
    """
    if session is not None:
        return session
    if store is None and graph_backend() != "neo4j":
        return None
    if store is None:
        from infona_client.graph.store import get_graph_store

        store = get_graph_store()
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


def _record_from_type_row(
    row: Mapping[str, Any], *, default_layer: str | None = None
) -> OntoTypeRecord:
    return OntoTypeRecord(
        name=str(row["name"]),
        layer=str(row.get("layer") or default_layer or ""),
        tenant_id=str(row.get("tenant_id") or ""),
        kg=str(row.get("kg") or ""),
        description=str(row.get("description") or ""),
        parent_type=row.get("parent_type"),
        label_token=row.get("label_token"),
        uri=row.get("uri"),
    )


def _record_from_attr_row(
    row: Mapping[str, Any], *, default_layer: str | None = None
) -> OntoAttrRecord:
    kind = row.get("kind") or "literal"
    if kind not in ("literal", "relationship"):
        kind = "literal"
    return OntoAttrRecord(
        name=str(row["name"]),
        domain=str(row["domain"]),
        layer=str(row.get("layer") or default_layer or ""),
        tenant_id=str(row.get("tenant_id") or ""),
        kg=str(row.get("kg") or ""),
        kind=kind,  # type: ignore[arg-type]
        datatype=row.get("datatype"),
        range_type=row.get("range_type"),
        cardinality=str(row.get("cardinality") or "1:1"),
        description=str(row.get("description") or ""),
        prop_key=row.get("prop_key"),
    )


# ---------------------------------------------------------------------------
# Property-graph path (templates)
# ---------------------------------------------------------------------------


async def upsert_type_pg(
    session: "GraphSession",
    name: str,
    *,
    description: str = "",
    parent_type: str | None = None,
    clear_parent: bool = True,
) -> OntoTypeRecord:
    """MERGE ``:OntoType`` in the session's catalog scope.

    When ``parent_type`` is set, replaces ``SUBCLASS_OF``. When ``parent_type``
    is ``None`` and ``clear_parent`` is True (default, mirrors SPARQL
    ``upsert_type``), clears any existing parent edge. Pass ``clear_parent=False``
    to leave hierarchy alone (comment-only style updates).

    **ADR 0013 dual-write:** also MERGEs a ``:Class`` node whose ``id`` is the
    type IRI (``uri``) and, when ``parent_type`` is set, a Class-level
    ``SUBCLASS_OF`` edge. OntoType remains the catalog reader surface until
    cutover; Class hierarchy is the preferred path for NL / explore subclass
    helpers.
    """
    leaf = _validate_type_leaf(name)
    layer = layer_from_scope(session.scope)
    label_token = _label_token_for(leaf)
    uri = _type_uri(leaf)
    rows = await session.execute_template(
        "onto_type_upsert",
        {
            "layer": layer,
            "name": leaf,
            "description": description or "",
            "label_token": label_token,
            "uri": uri,
        },
    )
    if parent_type is not None:
        parent_leaf = _validate_type_leaf(parent_type)
        await session.execute_template(
            "onto_subclass_set",
            {
                "layer": layer,
                "name": leaf,
                "parent_name": parent_leaf,
                "parent_label_token": _label_token_for(parent_leaf),
            },
        )
    elif clear_parent:
        await session.execute_template(
            "onto_subclass_clear",
            {"layer": layer, "name": leaf},
        )

    # Dual-write Class node (id = type IRI) + Class SUBCLASS_OF.
    await _dual_write_class_for_type(
        session,
        leaf,
        uri=uri,
        layer=layer,
        parent_type=parent_type,
        clear_parent=clear_parent and parent_type is None,
    )

    # Re-read for parent_type accuracy.
    got = await session.execute_template(
        "onto_type_get",
        {"layer": layer, "name": leaf},
    )
    if got:
        return _record_from_type_row(got[0].to_dict(), default_layer=layer)
    if rows:
        return _record_from_type_row(rows[0].to_dict(), default_layer=layer)
    return OntoTypeRecord(
        name=leaf,
        layer=layer,
        tenant_id=session.scope.tenant_id,
        kg=session.scope.kg,
        description=description or "",
        parent_type=parent_type,
        label_token=label_token,
        uri=uri,
    )


async def _dual_write_class_for_type(
    session: "GraphSession",
    leaf: str,
    *,
    uri: str,
    layer: str,
    parent_type: str | None,
    clear_parent: bool,
) -> None:
    """Ensure Class node + optional SUBCLASS_OF for an OntoType upsert."""
    from infona_client.graph.rdf_model import merge_class, set_subclass_of

    await merge_class(session, uri, name=leaf, layer=layer)
    if parent_type is not None:
        parent_leaf = _validate_type_leaf(parent_type)
        parent_uri = _type_uri(parent_leaf)
        await merge_class(session, parent_uri, name=parent_leaf, layer=layer)
        await set_subclass_of(session, uri, parent_uri)
    elif clear_parent:
        clear = getattr(session, "write_clear_class_subclass", None)
        if callable(clear):
            await clear(uri)


async def upsert_attribute_pg(
    session: "GraphSession",
    type_name: str,
    attr_name: str,
    *,
    description: str = "",
    datatype: str = "string",
    cardinality: str | None = None,
) -> OntoAttrRecord:
    """MERGE ``:OntoAttr`` + ``DECLARES`` (+ ``RANGE_TYPE`` for relationships).

    ``datatype`` follows the RDF-era convention: a primitive name for literals,
    or a target type leaf for relationships (see :func:`classify_attr_range`).
    """
    domain = _validate_type_leaf(type_name)
    leaf, prop_key = _validate_attr_leaf(attr_name)
    kind, lit_dt, range_type = classify_attr_range(datatype)
    layer = layer_from_scope(session.scope)

    if cardinality is None:
        card = "1:1" if kind == "literal" else "N:N"
    else:
        if cardinality not in VALID_CARDINALITIES:
            raise GraphScopeError(
                f"Invalid cardinality {cardinality!r}; expected one of "
                f"{sorted(VALID_CARDINALITIES)}"
            )
        card = cardinality

    rows = await session.execute_template(
        "onto_attr_upsert",
        {
            "layer": layer,
            "domain": domain,
            "name": leaf,
            "kind": kind,
            "datatype": lit_dt,
            "range_type": range_type,
            "cardinality": card,
            "description": description or "",
            "prop_key": prop_key,
            "domain_label_token": _label_token_for(domain),
        },
    )
    if kind == "relationship" and range_type:
        await session.execute_template(
            "onto_attr_range_type",
            {
                "layer": layer,
                "domain": domain,
                "name": leaf,
                "range_type": range_type,
                "range_label_token": _label_token_for(range_type),
            },
        )
    if rows:
        return _record_from_attr_row(rows[0].to_dict(), default_layer=layer)
    return OntoAttrRecord(
        name=leaf,
        domain=domain,
        layer=layer,
        tenant_id=session.scope.tenant_id,
        kg=session.scope.kg,
        kind=kind,
        datatype=lit_dt,
        range_type=range_type,
        cardinality=card,
        description=description or "",
        prop_key=prop_key,
    )


async def list_types_pg(
    session: "GraphSession",
    *,
    layer: str | None = None,
) -> list[OntoTypeRecord]:
    """List ``:OntoType`` nodes in the session scope (optional layer filter)."""
    default_layer = layer_from_scope(session.scope)
    rows = await session.execute_template(
        "onto_type_list",
        {"layer": layer},  # None = all layers in this tenant_id+kg
    )
    return [
        _record_from_type_row(r.to_dict(), default_layer=default_layer) for r in rows
    ]


async def list_attributes_pg(
    session: "GraphSession",
    *,
    domain: str | None = None,
    layer: str | None = None,
) -> list[OntoAttrRecord]:
    """List ``:OntoAttr`` nodes; optional domain / layer filters."""
    default_layer = layer_from_scope(session.scope)
    if domain is not None:
        domain = _validate_type_leaf(domain)
    rows = await session.execute_template(
        "onto_attr_list",
        {"domain": domain, "layer": layer},
    )
    return [
        _record_from_attr_row(r.to_dict(), default_layer=default_layer) for r in rows
    ]


async def get_type_pg(
    session: "GraphSession", name: str
) -> OntoTypeRecord | None:
    leaf = _validate_type_leaf(name)
    layer = layer_from_scope(session.scope)
    rows = await session.execute_template(
        "onto_type_get", {"layer": layer, "name": leaf}
    )
    if not rows:
        return None
    return _record_from_type_row(rows[0].to_dict(), default_layer=layer)


# ---------------------------------------------------------------------------
# Dual-backend public API (writer-facing)
# ---------------------------------------------------------------------------


async def upsert_type(
    neptune: Any = None,
    graph_uri: str | None = None,
    name: str = "",
    description: str = "",
    parent_type: str | None = None,
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    layer: LayerName | str = "tenant",
    tenant_id: str | None = None,
    privileged: bool = False,
    clear_parent: bool = True,
) -> OntoTypeRecord:
    """Upsert a type declaration (PG catalog or SPARQL).

    Prefer ``store`` / ``session`` for Neo4j. With ``INFONA_GRAPH_BACKEND=neo4j``
    and no store, uses the process GraphStore. Otherwise requires ``neptune`` +
    ``graph_uri`` and runs :func:`ontology_queries.upsert_type`.
    """
    if not name:
        raise GraphScopeError("upsert_type requires a non-empty name")
    gs = resolve_catalog_session(
        store=store,
        session=session,
        layer=layer,
        tenant_id=tenant_id,
        privileged=privileged,
    )
    if gs is not None:
        return await upsert_type_pg(
            gs,
            name,
            description=description,
            parent_type=parent_type,
            clear_parent=clear_parent,
        )
    if neptune is None or not graph_uri:
        raise GraphScopeError(
            "SPARQL ontology path requires neptune client and graph_uri "
            "(or pass store=/session=/INFONA_GRAPH_BACKEND=neo4j)"
        )
    from infona_client.graph import ontology_queries as oq

    leaf = _validate_type_leaf(name)
    if parent_type is not None:
        parent_type = _validate_type_leaf(parent_type)
    sparql = oq.upsert_type(
        graph_uri, leaf, description=description, parent_type=parent_type
    )
    await neptune.update(sparql)
    return OntoTypeRecord(
        name=leaf,
        layer=str(layer),
        tenant_id=tenant_id or "",
        kg=ONTOLOGY_KG if str(layer) == "tenant" else str(layer),
        description=description or "",
        parent_type=parent_type,
        label_token=_label_token_for(leaf),
        uri=_type_uri(leaf),
    )


async def upsert_attribute(
    neptune: Any = None,
    graph_uri: str | None = None,
    type_name: str = "",
    attr_name: str = "",
    description: str = "",
    datatype: str = "string",
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    layer: LayerName | str = "tenant",
    tenant_id: str | None = None,
    privileged: bool = False,
    cardinality: str | None = None,
) -> OntoAttrRecord:
    """Upsert an attribute declaration (PG catalog or SPARQL).

    Rejects reserved Entity property keys (B2) on both backends before write.
    """
    if not type_name or not attr_name:
        raise GraphScopeError("upsert_attribute requires type_name and attr_name")
    # Fail closed on B2 before either backend writes.
    domain = _validate_type_leaf(type_name)
    leaf, prop_key = _validate_attr_leaf(attr_name)
    kind, lit_dt, range_type = classify_attr_range(datatype)

    gs = resolve_catalog_session(
        store=store,
        session=session,
        layer=layer,
        tenant_id=tenant_id,
        privileged=privileged,
    )
    if gs is not None:
        return await upsert_attribute_pg(
            gs,
            domain,
            leaf,
            description=description,
            datatype=datatype,
            cardinality=cardinality,
        )
    if neptune is None or not graph_uri:
        raise GraphScopeError(
            "SPARQL ontology path requires neptune client and graph_uri "
            "(or pass store=/session=/INFONA_GRAPH_BACKEND=neo4j)"
        )
    from infona_client.graph import ontology_queries as oq

    if kind == "relationship" and range_type:
        # Declare as property then re-point range at the type URI (matches
        # writer upgrade path: upsert_attribute + set_object_property_range).
        sparql = oq.upsert_attribute(
            graph_uri, domain, leaf, description=description, datatype="string"
        )
        await neptune.update(sparql)
        await neptune.update(
            oq.set_object_property_range(graph_uri, domain, leaf, range_type)
        )
    else:
        sparql = oq.upsert_attribute(
            graph_uri,
            domain,
            leaf,
            description=description,
            datatype=lit_dt or "string",
        )
        await neptune.update(sparql)
    card = cardinality or ("1:1" if kind == "literal" else "N:N")
    return OntoAttrRecord(
        name=leaf,
        domain=domain,
        layer=str(layer),
        tenant_id=tenant_id or "",
        kg=ONTOLOGY_KG if str(layer) == "tenant" else str(layer),
        kind=kind,
        datatype=lit_dt,
        range_type=range_type,
        cardinality=card,
        description=description or "",
        prop_key=prop_key,
    )


async def list_types(
    neptune: Any = None,
    graph_uri: str | None = None,
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    layer: LayerName | str = "tenant",
    tenant_id: str | None = None,
) -> list[OntoTypeRecord]:
    """List types in a catalog scope (PG) or SPARQL graph."""
    gs = resolve_catalog_session(
        store=store, session=session, layer=layer, tenant_id=tenant_id
    )
    if gs is not None:
        return await list_types_pg(gs)
    if neptune is None or not graph_uri:
        raise GraphScopeError(
            "SPARQL list_types requires neptune + graph_uri "
            "(or pass store=/session=/INFONA_GRAPH_BACKEND=neo4j)"
        )
    from infona_client.graph import ontology_queries as oq
    from infona_client.graph.parser import parse_sparql_results

    _, rows = parse_sparql_results(await neptune.query(oq.list_types_query(graph_uri)))
    out: list[OntoTypeRecord] = []
    for r in rows:
        label = r.get("label") or ""
        if not label:
            continue
        parent = r.get("parent")
        parent_leaf = None
        if parent and isinstance(parent, str) and parent.startswith(TYPE_URI_PREFIX):
            parent_leaf = parent[len(TYPE_URI_PREFIX) :].rstrip("/")
        out.append(
            OntoTypeRecord(
                name=label,
                layer=str(layer),
                tenant_id=tenant_id or "",
                kg=ONTOLOGY_KG if str(layer) == "tenant" else str(layer),
                description=r.get("comment") or "",
                parent_type=parent_leaf,
                label_token=_label_token_for(label) if label else None,
                uri=r.get("type"),
            )
        )
    return out


async def list_attributes(
    neptune: Any = None,
    graph_uri: str | None = None,
    type_name: str | None = None,
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    layer: LayerName | str = "tenant",
    tenant_id: str | None = None,
) -> list[OntoAttrRecord]:
    """List attributes for a type (or all types when ``type_name`` is None on PG).

    SPARQL path requires ``type_name`` (matches ``get_type_attributes_query``).
    """
    gs = resolve_catalog_session(
        store=store, session=session, layer=layer, tenant_id=tenant_id
    )
    if gs is not None:
        return await list_attributes_pg(gs, domain=type_name)
    if neptune is None or not graph_uri or not type_name:
        raise GraphScopeError(
            "SPARQL list_attributes requires neptune, graph_uri, and type_name "
            "(or pass store=/session=/INFONA_GRAPH_BACKEND=neo4j)"
        )
    from infona_client.graph import ontology_queries as oq
    from infona_client.graph.parser import parse_sparql_results

    domain = _validate_type_leaf(type_name)
    _, rows = parse_sparql_results(
        await neptune.query(oq.get_type_attributes_query(graph_uri, domain))
    )
    out: list[OntoAttrRecord] = []
    for r in rows:
        attr_label = r.get("attrLabel") or ""
        if not attr_label:
            continue
        range_uri = r.get("range") or ""
        kind: AttrKind = "literal"
        lit_dt: str | None = "string"
        range_type: str | None = None
        if range_uri.startswith(TYPE_URI_PREFIX):
            kind = "relationship"
            lit_dt = None
            range_type = range_uri[len(TYPE_URI_PREFIX) :].rstrip("/")
        else:
            lit_dt = oq.xsd_to_datatype(range_uri) if range_uri else "string"
        try:
            _, prop_key = _validate_attr_leaf(attr_label)
        except GraphScopeError:
            prop_key = None
        out.append(
            OntoAttrRecord(
                name=attr_label,
                domain=domain,
                layer=str(layer),
                tenant_id=tenant_id or "",
                kg=ONTOLOGY_KG if str(layer) == "tenant" else str(layer),
                kind=kind,
                datatype=lit_dt,
                range_type=range_type,
                description=r.get("attrComment") or "",
                prop_key=prop_key,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Schema retrieval helper (future NL — minimal stub)
# ---------------------------------------------------------------------------


async def schema_types_for_kg(
    store: "GraphStore",
    *,
    tenant_id: str,
    kg: str,
    include_attrs: bool = True,
) -> list[SchemaTypeSummary]:
    """List tenant-catalog types with optional instance entity counts for ``kg``.

    Active types = tenant ``:OntoType`` rows (dual-written Class nodes land on
    the same upsert). Counts come from ``INSTANCE_OF`` → Class in the instance
    scope (ADR 0013), not ``Entity.primary_type`` alone. Does not yet implement
    layer precedence (public/enhanced shadowing).
    """
    cat = store.session(
        GraphScope.for_catalog(layer="tenant", tenant_id=tenant_id)
    )
    types = await list_types_pg(cat)
    counts: dict[str, int] = {}
    inst = store.session(GraphScope.for_instance(tenant_id, kg))
    try:
        # Template groups by Class.name via INSTANCE_OF (row key still
        # ``primary_type`` for template compat).
        count_rows = await inst.execute_template("entity_count_by_primary_type", {})
        for r in count_rows:
            pt = r.get("primary_type")
            if pt:
                counts[str(pt)] = int(r.get("n") or 0)
    except Exception:
        # Counts are best-effort for the stub.
        counts = {}

    attrs_by_domain: dict[str, list[OntoAttrRecord]] = {}
    if include_attrs:
        all_attrs = await list_attributes_pg(cat)
        for a in all_attrs:
            attrs_by_domain.setdefault(a.domain, []).append(a)

    out: list[SchemaTypeSummary] = []
    for t in types:
        out.append(
            SchemaTypeSummary(
                name=t.name,
                layer=t.layer,
                description=t.description,
                parent_type=t.parent_type,
                entity_count=counts.get(t.name, 0),
                attributes=tuple(attrs_by_domain.get(t.name, ())),
            )
        )
    return out


__all__ = [
    "LITERAL_DATATYPES",
    "RESERVED_ENTITY_PROPERTY_KEYS",
    "OntoAttrRecord",
    "OntoTypeRecord",
    "SchemaTypeSummary",
    "VALID_CARDINALITIES",
    "classify_attr_range",
    "get_type_pg",
    "graph_backend",
    "layer_from_scope",
    "list_attributes",
    "list_attributes_pg",
    "list_types",
    "list_types_pg",
    "resolve_catalog_session",
    "schema_types_for_kg",
    "upsert_attribute",
    "upsert_attribute_pg",
    "upsert_type",
    "upsert_type_pg",
]
