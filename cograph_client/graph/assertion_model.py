"""Assertion-centric graph model types (ADR 0013 / neo4j-rdf-semantic-model).

This module is the **minimal SoT API** for Entity / Class / Property / Assertion
nodes used by golden-query helpers and the hermetic fixture builder. Full Neo4j
templates and ``kg_writer`` dual-write will absorb these shapes; until then
:class:`cograph_client.graph.assertion_memory.AssertionMemoryStore` is the
in-memory implementation.

Identity rules (model contract):
* Entity ``id`` = :func:`cograph_client.graph.ontology_queries.entity_uri`
* Class ``id`` = :func:`cograph_client.graph.ontology_queries.type_uri`
* Property ``id`` = :func:`property_uri` (stable under ``IRI_BASE``)
* Assertion ``id`` = :func:`make_assertion_id` within ``(tenant_id, kg)``
* Type membership uses the well-known :func:`type_membership_property_id`
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from cograph_client.graph.iri import IRI_BASE
from cograph_client.graph.scope import GraphScopeError

PropertyKind = Literal["datatype", "object", "type"]
ObjectKeyKind = Literal["literal", "entity", "class"]

# Well-known type-membership Property leaf (model §5.3).
TYPE_MEMBERSHIP_LEAF = "rdf_type"


def property_uri(leaf: str) -> str:
    """Stable Property IRI under ``IRI_BASE`` (catalog-independent leaf)."""
    if not isinstance(leaf, str) or not leaf.strip():
        raise GraphScopeError("property leaf must be a non-empty string")
    return f"{IRI_BASE}/properties/{leaf.strip()}"


def type_membership_property_id() -> str:
    """Well-known Property IRI used by type Assertions (rdf:type equivalent)."""
    return property_uri(TYPE_MEMBERSHIP_LEAF)


def canonical_literal(value: Any) -> str:
    """Serialize a literal for Assertion identity / multiset compare keys."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        # Decimal-normalize: 1.0 and 1.00 compare equal as numbers, not strings.
        text = f"{value:.15g}"
        return text
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, sort_keys=True, default=str)


def make_assertion_id(
    subject_id: str,
    property_id: str,
    object_key: str,
    *,
    source_discriminator: str | None = None,
) -> str:
    """Stable Assertion id within a scope (model §5.2).

    ``object_key`` is the object Entity id, Class id, or
    :func:`canonical_literal` for datatype values. Optional
    ``source_discriminator`` (source_url / run_id) keeps multi-source facts
    distinct when the product retains both.
    """
    if not subject_id or not property_id:
        raise GraphScopeError("assertion id needs subject_id and property_id")
    parts = [subject_id, property_id, object_key or ""]
    if source_discriminator:
        parts.append(source_discriminator)
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


@dataclass(frozen=True, slots=True)
class ClassNode:
    """Catalog Class (entity type) with IRI identity."""

    id: str
    name: str
    tenant_id: str
    kg: str
    layer: str = "tenant"
    description: str = ""


@dataclass(frozen=True, slots=True)
class PropertyNode:
    """Catalog Property (predicate) with IRI identity."""

    id: str
    name: str
    kind: PropertyKind
    tenant_id: str
    kg: str
    layer: str = "tenant"
    datatype: str | None = None
    range_class_id: str | None = None
    cardinality: str = "1:1"
    description: str = ""


@dataclass
class EntityNode:
    """Instance Entity with IRI identity (not SoT for attribute values)."""

    id: str
    tenant_id: str
    kg: str
    name: str | None = None
    primary_type: str | None = None


@dataclass
class AssertionNode:
    """Unit of truth for one fact (model §5).

    Direction (locked): conceptual ``(Assertion)-[:SUBJECT]->(Entity)`` etc.
    Denormalized ``subject_id`` / ``property_id`` / ``object_id`` mirror the
    links for index-friendly helpers without a live graph engine.
    """

    id: str
    tenant_id: str
    kg: str
    subject_id: str
    property_id: str
    literal_value: Any = None
    object_id: str | None = None  # Entity object (object property)
    object_class_id: str | None = None  # Class object (type membership)
    source_url: str | None = None
    verified_at: str | None = None
    run_id: str | None = None
    confidence: float | None = None
    provenance: str | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "kg": self.kg,
            "subject_id": self.subject_id,
            "property_id": self.property_id,
            "literal_value": self.literal_value,
            "object_id": self.object_id,
            "object_class_id": self.object_class_id,
            "source_url": self.source_url,
            "verified_at": self.verified_at,
            "run_id": self.run_id,
            "confidence": self.confidence,
            "provenance": self.provenance,
        }


@dataclass
class AssertionFact:
    """Writer input IR (model §10) — maps to one Assertion + endpoints."""

    subject_id: str
    property_id: str | None = None
    property_leaf: str | None = None
    kind: Literal["literal", "object", "type"] = "literal"
    value: Any = None
    source_url: str | None = None
    verified_at: str | None = None
    run_id: str | None = None
    confidence: float | None = None
    provenance: str | None = None

    def resolved_property_id(self) -> str:
        if self.kind == "type":
            return type_membership_property_id()
        if self.property_id:
            return self.property_id
        if self.property_leaf:
            return property_uri(self.property_leaf)
        raise GraphScopeError("AssertionFact needs property_id or property_leaf")


@dataclass
class MiniPeopleIds:
    """Resolved IRIs for the hermetic mini_people fixture (no hard-coded hosts)."""

    tenant_id: str
    kg: str
    sibling_kg: str
    classes: Mapping[str, str] = field(default_factory=dict)
    properties: Mapping[str, str] = field(default_factory=dict)
    entities: Mapping[str, str] = field(default_factory=dict)


__all__ = [
    "TYPE_MEMBERSHIP_LEAF",
    "AssertionFact",
    "AssertionNode",
    "ClassNode",
    "EntityNode",
    "MiniPeopleIds",
    "ObjectKeyKind",
    "PropertyKind",
    "PropertyNode",
    "canonical_literal",
    "make_assertion_id",
    "property_uri",
    "type_membership_property_id",
]
