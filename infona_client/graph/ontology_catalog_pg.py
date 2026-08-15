"""Property-graph template writers / readers for the ontology catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from infona_client.graph.ontology_catalog_models import (
    VALID_CARDINALITIES,
    LayerName,
    OntoAttrRecord,
    OntoTypeRecord,
    _label_token_for,
    _record_from_attr_row,
    _record_from_type_row,
    _type_uri,
    _validate_attr_leaf,
    _validate_type_leaf,
    classify_attr_range,
    layer_from_scope,
)
from infona_client.graph.ontology_catalog_session import resolve_catalog_session
from infona_client.graph.queries import require_valid_type_name
from infona_client.graph.scope import GraphScopeError

if TYPE_CHECKING:
    from infona_client.graph.store import GraphSession, GraphStore


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
    from infona_client.graph.ontology_descriptions import ensure_description

    leaf = _validate_type_leaf(name)
    layer = layer_from_scope(session.scope)
    label_token = _label_token_for(leaf)
    uri = _type_uri(leaf)
    provided = bool((description or "").strip())
    desc, desc_at = ensure_description(leaf, description, kind="type")
    rows = await session.execute_template(
        "onto_type_upsert",
        {
            "layer": layer,
            "name": leaf,
            "description": desc,
            "description_updated_at": desc_at,
            "description_provided": provided,
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
        description=desc,
        description_updated_at=desc_at,
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
    from infona_client.graph.ontology_descriptions import ensure_description

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

    attr_kind: str = "relationship" if kind == "relationship" else "literal"
    provided = bool((description or "").strip())
    desc, desc_at = ensure_description(
        leaf,
        description,
        kind=attr_kind,  # type: ignore[arg-type]
        domain=domain,
        datatype=lit_dt,
        range_type=range_type,
    )
    # Stub domain OntoType on CREATE of DECLARES must also carry a description.
    domain_desc, domain_desc_at = ensure_description(domain, "", kind="type")

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
            "description": desc,
            "description_updated_at": desc_at,
            "description_provided": provided,
            "prop_key": prop_key,
            "domain_label_token": _label_token_for(domain),
            "domain_description": domain_desc,
            "domain_description_updated_at": domain_desc_at,
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
        # Range type stub should also be described when first created.
        try:
            await upsert_type_pg(
                session,
                range_type,
                description="",
                clear_parent=False,
            )
        except Exception:
            pass  # best-effort; range node may already exist with desc
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
        description=desc,
        description_updated_at=desc_at,
        prop_key=prop_key,
    )


async def set_attribute_text_kind_pg(
    session: "GraphSession",
    type_name: str,
    attr_name: str,
    text_kind: str = "",
) -> OntoAttrRecord:
    """Idempotently set (or clear) an attribute's free-text candidacy marker.

    Mirrors the SPARQL-era :func:`upsert_attribute_text_kind`: empty
    ``text_kind`` clears any existing marker (candidacy becomes undecided).
    MERGEs a stub ``:OntoAttr`` when the attribute has not been declared yet so
    reconciler-side heuristics can still land a durable verdict before a full
    schema pass (ONTA-533).

    Reserved Entity property keys (``name``, ``id``, …) are allowed here: the
    marker is catalog metadata OF an attribute leaf, not an Entity property
    write (B2 only gates real schema declarations / dual-written props).
    """
    domain = _validate_type_leaf(type_name)
    # Validate shape only — do NOT reject reserved Entity keys (see docstring).
    leaf = require_valid_type_name(attr_name, "attribute name")
    layer = layer_from_scope(session.scope)
    kind_val = (text_kind or "").strip()
    rows = await session.execute_template(
        "onto_attr_set_text_kind",
        {
            "layer": layer,
            "domain": domain,
            "name": leaf,
            "text_kind": kind_val,
            "domain_label_token": _label_token_for(domain),
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
        kind="literal",
        datatype="string",
        text_kind=kind_val or None,
    )


async def set_attribute_text_kind(
    neptune: Any = None,
    graph_uri: str | None = None,
    type_name: str = "",
    attr_name: str = "",
    text_kind: str = "",
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    layer: LayerName | str = "tenant",
    tenant_id: str | None = None,
    privileged: bool = False,
) -> OntoAttrRecord:
    """Public dual-backend entry for textKind markers (ONTA-533)."""
    if not type_name or not attr_name:
        raise GraphScopeError(
            "set_attribute_text_kind requires type_name and attr_name"
        )
    # Shape-only validation (no B2 reserved-key gate — see set_attribute_text_kind_pg).
    require_valid_type_name(type_name, "type name")
    require_valid_type_name(attr_name, "attribute name")
    gs = resolve_catalog_session(
        store=store,
        session=session,
        layer=layer,
        tenant_id=tenant_id,
        privileged=privileged,
    )
    return await set_attribute_text_kind_pg(
        gs, type_name, attr_name, text_kind=text_kind
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


async def delete_attribute_pg(
    session: "GraphSession", type_name: str, attr_name: str
) -> bool:
    """Property-graph DELETE for one attribute declaration."""
    domain = _validate_type_leaf(type_name)
    leaf, _ = _validate_attr_leaf(attr_name)
    layer = layer_from_scope(session.scope)
    rows = await session.execute_template(
        "onto_attr_delete",
        {"layer": layer, "domain": domain, "name": leaf},
    )
    if rows:
        return True
    # Template may return empty; fall back to store-native helper when present.
    delete_fn = getattr(session, "write_delete_onto_attr", None)
    if callable(delete_fn):
        return bool(await delete_fn(domain, leaf))
    # Memory / Neo4j templates that return deleted count.
    return False


async def delete_type(
    name: str,
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    layer: LayerName | str = "tenant",
    tenant_id: str | None = None,
    privileged: bool = False,
) -> bool:
    """Drop an ``:OntoType`` (attributes not cascaded — matches SPARQL path)."""
    leaf = _validate_type_leaf(name)
    gs = resolve_catalog_session(
        store=store,
        session=session,
        layer=layer,
        tenant_id=tenant_id,
        privileged=privileged,
    )
    return await delete_type_pg(gs, leaf)


async def delete_type_pg(session: "GraphSession", name: str) -> bool:
    leaf = _validate_type_leaf(name)
    layer = layer_from_scope(session.scope)
    rows = await session.execute_template(
        "onto_type_delete",
        {"layer": layer, "name": leaf},
    )
    if rows:
        return True
    delete_fn = getattr(session, "write_delete_onto_type", None)
    if callable(delete_fn):
        return bool(await delete_fn(leaf))
    return False
