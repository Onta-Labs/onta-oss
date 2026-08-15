"""Ontology-catalog records, validation helpers, and row adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from infona_client.graph.facts import (
    RESERVED_ENTITY_PROPERTY_KEYS,
    sanitize_prop_key,
)
from infona_client.graph.iri import TYPE_URI_PREFIX
from infona_client.graph.labels import sanitize_domain_label
from infona_client.graph.queries import require_valid_type_name
from infona_client.graph.scope import (
    ENHANCED_KG,
    GLOBAL_TENANT_ID,
    ONTOLOGY_KG,
    PUBLIC_KG,
    GraphScope,
    GraphScopeError,
)

LayerName = Literal["public", "enhanced", "tenant"]
AttrKind = Literal["literal", "relationship"]

LITERAL_DATATYPES: frozenset[str] = frozenset(
    {"string", "integer", "float", "boolean", "datetime", "uri", "geo"}
)

VALID_CARDINALITIES: frozenset[str] = frozenset({"1:1", "1:N", "N:1", "N:N"})

# ONTA-531 ports the remaining ontology_commit surface onto this catalog
# (deletes, markers, aliases, changelog, revision, load_ontology_shape).
# Snapshots/base-pin still use the companion bag + frozen shapes.


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
    #: UTC date ``YYYY-MM-DD`` when ``description`` was last set (mandatory).
    description_updated_at: str | None = None
    parent_type: str | None = None
    label_token: str | None = None
    uri: str | None = None
    # ONTA-531 / ONTA-404 — deprecation markers (schema identity).
    deprecated_at: str | None = None
    superseded_by: str | None = None


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
    #: UTC date ``YYYY-MM-DD`` when ``description`` was last set (mandatory).
    description_updated_at: str | None = None
    prop_key: str | None = None
    # ONTA-531 — markers that used to be SPARQL triples on the attr subject.
    core_slot: bool = False
    #: Free-text candidacy marker (ONTA-177 / ONTA-533). ``"free_text"`` /
    #: ``"not_text"`` when decided; ``None`` when candidacy was never adjudicated.
    text_kind: str | None = None
    deprecated_at: str | None = None
    superseded_by: str | None = None


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


def _record_from_type_row(
    row: Mapping[str, Any], *, default_layer: str | None = None
) -> OntoTypeRecord:
    dep = row.get("deprecated_at")
    sup = row.get("superseded_by")
    dua = row.get("description_updated_at")
    return OntoTypeRecord(
        name=str(row["name"]),
        layer=str(row.get("layer") or default_layer or ""),
        tenant_id=str(row.get("tenant_id") or ""),
        kg=str(row.get("kg") or ""),
        description=str(row.get("description") or ""),
        description_updated_at=str(dua) if dua else None,
        parent_type=row.get("parent_type"),
        label_token=row.get("label_token"),
        uri=row.get("uri"),
        deprecated_at=str(dep) if dep else None,
        superseded_by=str(sup) if sup else None,
    )


def _record_from_attr_row(
    row: Mapping[str, Any], *, default_layer: str | None = None
) -> OntoAttrRecord:
    kind = row.get("kind") or "literal"
    if kind not in ("literal", "relationship"):
        kind = "literal"
    core = row.get("core_slot")
    core_bool = bool(core) if core not in (None, "", False, "false", "False", 0) else False
    if isinstance(core, str) and core.lower() in ("true", "1"):
        core_bool = True
    dep = row.get("deprecated_at")
    sup = row.get("superseded_by")
    raw_tk = row.get("text_kind")
    text_kind = str(raw_tk).strip() if raw_tk else None
    if text_kind == "":
        text_kind = None
    dua = row.get("description_updated_at")
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
        description_updated_at=str(dua) if dua else None,
        prop_key=row.get("prop_key"),
        core_slot=core_bool,
        text_kind=text_kind,
        deprecated_at=str(dep) if dep else None,
        superseded_by=str(sup) if sup else None,
    )
