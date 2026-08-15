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

from typing import TYPE_CHECKING, Any, Optional

from infona_client.graph.facts import RESERVED_ENTITY_PROPERTY_KEYS  # noqa: F401
from infona_client.graph.ontology_catalog_models import (  # noqa: F401
    LITERAL_DATATYPES,
    VALID_CARDINALITIES,
    AttrKind,
    LayerName,
    OntoAttrRecord,
    OntoTypeRecord,
    SchemaTypeSummary,
    _label_token_for,
    _record_from_attr_row,
    _record_from_type_row,
    _type_uri,
    _validate_attr_leaf,
    _validate_type_leaf,
    classify_attr_range,
    layer_from_scope,
)
from infona_client.graph.ontology_catalog_session import (  # noqa: F401
    resolve_catalog_session,
)
from infona_client.graph.ontology_catalog_pg import (  # noqa: F401
    _dual_write_class_for_type,
    delete_attribute_pg,
    delete_type,
    delete_type_pg,
    get_type_pg,
    list_attributes_pg,
    list_types_pg,
    set_attribute_text_kind,
    set_attribute_text_kind_pg,
    upsert_attribute_pg,
    upsert_type_pg,
)
from infona_client.graph.queries import require_valid_type_name
from infona_client.graph.scope import GraphScope, GraphScopeError

if TYPE_CHECKING:
    from infona_client.graph.store import GraphSession, GraphStore


def _host():
    """Call-time lookup of this module (monkeypatch surface)."""
    from infona_client.graph import ontology_catalog as _mod

    return _mod


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
    """Upsert a type declaration in the property-graph catalog.

    Prefer an explicit ``store`` / ``session``; otherwise the process GraphStore
    is used. ``neptune`` / ``graph_uri`` are vestigial (ONTA-527 deleted the
    SPARQL ontology path) and ignored.
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
    return await upsert_type_pg(
        gs,
        name,
        description=description,
        parent_type=parent_type,
        clear_parent=clear_parent,
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
    return await upsert_attribute_pg(
        gs,
        domain,
        leaf,
        description=description,
        datatype=datatype,
        cardinality=cardinality,
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
    """List types in a catalog scope."""
    gs = resolve_catalog_session(
        store=store, session=session, layer=layer, tenant_id=tenant_id
    )
    return await list_types_pg(gs)

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
    """List attributes for a type, or all types when ``type_name`` is None."""
    gs = resolve_catalog_session(
        store=store, session=session, layer=layer, tenant_id=tenant_id
    )
    return await list_attributes_pg(gs, domain=type_name)


# ---------------------------------------------------------------------------
# Deletes + marker writers (ONTA-531 — previously SPARQL-only)
# ---------------------------------------------------------------------------


async def delete_attribute(
    type_name: str,
    attr_name: str,
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    layer: LayerName | str = "tenant",
    tenant_id: str | None = None,
    privileged: bool = False,
) -> bool:
    """Drop an ``:OntoAttr`` declaration (instance data untouched)."""
    domain = _validate_type_leaf(type_name)
    leaf, _ = _validate_attr_leaf(attr_name)
    gs = resolve_catalog_session(
        store=store,
        session=session,
        layer=layer,
        tenant_id=tenant_id,
        privileged=privileged,
    )
    return await delete_attribute_pg(gs, domain, leaf)


async def set_attr_markers(
    type_name: str,
    attr_name: str,
    *,
    core_slot: bool | None = None,
    text_kind: str | None = None,
    clear_text_kind: bool = False,
    deprecated_at: str | None = None,
    superseded_by: str | None = None,
    clear_deprecation: bool = False,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    layer: LayerName | str = "tenant",
    tenant_id: str | None = None,
    privileged: bool = False,
) -> None:
    """Set core-slot / text-kind / deprecation markers on an attribute.

    Does **not** re-validate B2 reserved property keys: markers attach to an
    already-declared row (or no-op if missing). Callers that mint attributes
    still go through :func:`upsert_attribute` which enforces B2.
    """
    domain = require_valid_type_name(type_name, "type name")
    leaf = require_valid_type_name(attr_name, "attribute name")
    gs = resolve_catalog_session(
        store=store,
        session=session,
        layer=layer,
        tenant_id=tenant_id,
        privileged=privileged,
    )
    lyr = layer_from_scope(gs.scope)
    await gs.execute_template(
        "onto_attr_set_markers",
        {
            "layer": lyr,
            "domain": domain,
            "name": leaf,
            "core_slot": core_slot,
            "text_kind": text_kind,
            "clear_text_kind": clear_text_kind,
            "deprecated_at": deprecated_at,
            "superseded_by": superseded_by,
            "clear_deprecation": clear_deprecation,
        },
    )


async def set_type_markers(
    name: str,
    *,
    description: str | None = None,
    deprecated_at: str | None = None,
    superseded_by: str | None = None,
    clear_deprecation: bool = False,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    layer: LayerName | str = "tenant",
    tenant_id: str | None = None,
    privileged: bool = False,
) -> None:
    """Set type-level description and/or deprecation markers."""
    leaf = _validate_type_leaf(name)
    gs = resolve_catalog_session(
        store=store,
        session=session,
        layer=layer,
        tenant_id=tenant_id,
        privileged=privileged,
    )
    lyr = layer_from_scope(gs.scope)
    await gs.execute_template(
        "onto_type_set_markers",
        {
            "layer": lyr,
            "name": leaf,
            "description": description,
            "deprecated_at": deprecated_at,
            "superseded_by": superseded_by,
            "clear_deprecation": clear_deprecation,
        },
    )


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
    "delete_attribute",
    "delete_attribute_pg",
    "delete_type",
    "delete_type_pg",
    "get_type_pg",
    "layer_from_scope",
    "list_attributes",
    "list_attributes_pg",
    "list_types",
    "list_types_pg",
    "resolve_catalog_session",
    "schema_types_for_kg",
    "set_attribute_text_kind",
    "set_attribute_text_kind_pg",
    "set_attr_markers",
    "set_type_markers",
    "upsert_attribute",
    "upsert_attribute_pg",
    "upsert_type",
    "upsert_type_pg",
]
