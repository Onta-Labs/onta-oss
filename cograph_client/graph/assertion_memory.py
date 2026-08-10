"""In-memory Assertion-centric graph (ADR 0013) for hermetic golden tests.

Companion to :class:`~cograph_client.graph.memory_store.MemoryGraphStore`.
Stores Class / Property / Entity / Assertion / SUBCLASS_OF / INSTANCE_OF
(cache) with structural ``tenant_id`` + ``kg`` isolation.

When the full assertion write path lands in MemoryGraphStore / Neo4jGraphStore,
this store remains the golden-query substrate until those backends implement the
same helper surface in :mod:`cograph_client.graph.rdfs_helpers`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from cograph_client.graph.assertion_model import (
    AssertionFact,
    AssertionNode,
    ClassNode,
    EntityNode,
    PropertyKind,
    PropertyNode,
    canonical_literal,
    make_assertion_id,
    property_uri,
    type_membership_property_id,
)
from cograph_client.graph.ontology_queries import entity_uri, type_uri
from cograph_client.graph.scope import GraphScope, GraphScopeError

if TYPE_CHECKING:
    from cograph_client.graph.memory_store import MemoryGraphStore


class AssertionMemoryStore:
    """Process-local assertion graph. Not multi-process safe."""

    def __init__(self, *, mirror: "MemoryGraphStore | None" = None) -> None:
        # Optional dual-write of Entity MERGE into Wave-1 MemoryGraphStore so
        # explore/list paths can see the same subjects while assertions remain SoT.
        # Typed as Any at runtime to avoid import cycle with memory_store →
        # rdfs_helpers → assertion_memory.
        self._mirror = mirror
        # Class: (tenant_id, kg, id) → ClassNode
        self._classes: dict[tuple[str, str, str], ClassNode] = {}
        # name index: (tenant_id, kg, name) → class id
        self._class_by_name: dict[tuple[str, str, str], str] = {}
        # Property: (tenant_id, kg, id)
        self._properties: dict[tuple[str, str, str], PropertyNode] = {}
        self._property_by_name: dict[tuple[str, str, str], str] = {}
        # Entity: (tenant_id, kg, id)
        self._entities: dict[tuple[str, str, str], EntityNode] = {}
        # Assertion: (tenant_id, kg, id)
        self._assertions: dict[tuple[str, str, str], AssertionNode] = {}
        # SUBCLASS_OF: (tenant_id, kg, child_id) → parent_id
        self._subclass_of: dict[tuple[str, str, str], str] = {}
        # SUBPROPERTY_OF: (tenant_id, kg, child_id) → parent_id
        self._subproperty_of: dict[tuple[str, str, str], str] = {}
        # INSTANCE_OF cache: (tenant_id, kg, entity_id) → set of class_ids
        self._instance_of: dict[tuple[str, str, str], set[str]] = {}
        # DECLARES: (tenant_id, kg, class_id) → set of property_ids
        self._declares: dict[tuple[str, str, str], set[str]] = {}

    # --- catalog ------------------------------------------------------------

    def upsert_class(
        self,
        *,
        tenant_id: str,
        kg: str,
        name: str,
        class_id: str | None = None,
        layer: str = "tenant",
        description: str = "",
        parent_name: str | None = None,
        parent_id: str | None = None,
    ) -> ClassNode:
        cid = class_id or type_uri(name)
        node = ClassNode(
            id=cid,
            name=name,
            tenant_id=tenant_id,
            kg=kg,
            layer=layer,
            description=description,
        )
        self._classes[(tenant_id, kg, cid)] = node
        self._class_by_name[(tenant_id, kg, name)] = cid
        if parent_name or parent_id:
            pid = parent_id or self.resolve_class_id(tenant_id, kg, parent_name or "")
            if not pid:
                raise GraphScopeError(
                    f"parent class not found for {name!r}: {parent_name!r}"
                )
            self._subclass_of[(tenant_id, kg, cid)] = pid
        return node

    def upsert_property(
        self,
        *,
        tenant_id: str,
        kg: str,
        name: str,
        kind: PropertyKind,
        property_id: str | None = None,
        layer: str = "tenant",
        datatype: str | None = None,
        range_class_id: str | None = None,
        range_class_name: str | None = None,
        cardinality: str = "1:1",
        domain_class_name: str | None = None,
        description: str = "",
    ) -> PropertyNode:
        pid = property_id or property_uri(name)
        range_id = range_class_id
        if range_id is None and range_class_name:
            range_id = self.resolve_class_id(tenant_id, kg, range_class_name)
        node = PropertyNode(
            id=pid,
            name=name,
            kind=kind,
            tenant_id=tenant_id,
            kg=kg,
            layer=layer,
            datatype=datatype,
            range_class_id=range_id,
            cardinality=cardinality,
            description=description,
        )
        self._properties[(tenant_id, kg, pid)] = node
        self._property_by_name[(tenant_id, kg, name)] = pid
        if domain_class_name:
            domain_id = self.resolve_class_id(tenant_id, kg, domain_class_name)
            if domain_id:
                self._declares.setdefault((tenant_id, kg, domain_id), set()).add(pid)
        return node

    def ensure_type_membership_property(
        self, *, tenant_id: str, kg: str, layer: str = "tenant"
    ) -> PropertyNode:
        return self.upsert_property(
            tenant_id=tenant_id,
            kg=kg,
            name="rdf_type",
            kind="type",
            property_id=type_membership_property_id(),
            layer=layer,
            description="Type membership (rdf:type equivalent)",
        )

    # --- instance -----------------------------------------------------------

    def merge_entity(
        self,
        *,
        tenant_id: str,
        kg: str,
        entity_id: str,
        name: str | None = None,
        primary_type: str | None = None,
    ) -> EntityNode:
        if not entity_id:
            raise GraphScopeError("entity_id required")
        key = (tenant_id, kg, entity_id)
        existing = self._entities.get(key)
        if existing is None:
            node = EntityNode(
                id=entity_id,
                tenant_id=tenant_id,
                kg=kg,
                name=name,
                primary_type=primary_type,
            )
            self._entities[key] = node
        else:
            if name is not None:
                existing.name = name
            if primary_type is not None:
                existing.primary_type = primary_type
            node = existing
        if self._mirror is not None:
            self._mirror._merge_entity(
                tenant_id,
                kg,
                entity_id,
                primary_type=primary_type,
                name=name,
            )
        return node

    def insert_assertion(
        self,
        fact: AssertionFact,
        *,
        tenant_id: str,
        kg: str,
    ) -> AssertionNode:
        """Insert one Assertion (MERGE by stable id). Dual-writes INSTANCE_OF for types."""
        prop_id = fact.resolved_property_id()
        # Ensure subject exists.
        if (tenant_id, kg, fact.subject_id) not in self._entities:
            self.merge_entity(
                tenant_id=tenant_id, kg=kg, entity_id=fact.subject_id
            )

        object_id: str | None = None
        object_class_id: str | None = None
        literal_value: Any = None
        object_key: str

        if fact.kind == "literal":
            literal_value = fact.value
            object_key = canonical_literal(fact.value)
        elif fact.kind == "object":
            if not isinstance(fact.value, str) or not fact.value:
                raise GraphScopeError("object Assertion needs entity id value")
            object_id = fact.value
            object_key = object_id
            if (tenant_id, kg, object_id) not in self._entities:
                self.merge_entity(tenant_id=tenant_id, kg=kg, entity_id=object_id)
        elif fact.kind == "type":
            # value is class leaf or class IRI
            if isinstance(fact.value, str) and fact.value.startswith("http"):
                object_class_id = fact.value
            else:
                leaf = str(fact.value)
                object_class_id = self.resolve_class_id(tenant_id, kg, leaf) or type_uri(
                    leaf
                )
            object_key = object_class_id
        else:
            raise GraphScopeError(f"unknown AssertionFact.kind {fact.kind!r}")

        aid = make_assertion_id(
            fact.subject_id,
            prop_id,
            object_key,
            source_discriminator=fact.source_url or fact.run_id,
        )
        node = AssertionNode(
            id=aid,
            tenant_id=tenant_id,
            kg=kg,
            subject_id=fact.subject_id,
            property_id=prop_id,
            literal_value=literal_value,
            object_id=object_id,
            object_class_id=object_class_id,
            source_url=fact.source_url,
            verified_at=fact.verified_at,
            run_id=fact.run_id,
            confidence=fact.confidence,
            provenance=fact.provenance,
        )
        self._assertions[(tenant_id, kg, aid)] = node

        if fact.kind == "type" and object_class_id:
            self._instance_of.setdefault(
                (tenant_id, kg, fact.subject_id), set()
            ).add(object_class_id)
            # primary_type hint = last asserted leaf name if known
            cls = self._classes.get((tenant_id, kg, object_class_id))
            if cls is not None:
                ent = self._entities[(tenant_id, kg, fact.subject_id)]
                ent.primary_type = cls.name
                if self._mirror is not None:
                    self._mirror._merge_entity(
                        tenant_id,
                        kg,
                        fact.subject_id,
                        primary_type=cls.name,
                        name=ent.name,
                    )
        return node

    def insert_facts(
        self,
        facts: Sequence[AssertionFact],
        *,
        tenant_id: str,
        kg: str,
    ) -> list[AssertionNode]:
        return [self.insert_assertion(f, tenant_id=tenant_id, kg=kg) for f in facts]

    # --- resolve / list -----------------------------------------------------

    def resolve_class_id(
        self, tenant_id: str, kg: str, name_or_id: str
    ) -> str | None:
        if not name_or_id:
            return None
        if (tenant_id, kg, name_or_id) in self._classes:
            return name_or_id
        return self._class_by_name.get((tenant_id, kg, name_or_id))

    def resolve_property_id(
        self, tenant_id: str, kg: str, name_or_id: str
    ) -> str | None:
        if not name_or_id:
            return None
        if (tenant_id, kg, name_or_id) in self._properties:
            return name_or_id
        return self._property_by_name.get((tenant_id, kg, name_or_id))

    def resolve_entity_by_name(
        self, tenant_id: str, kg: str, name: str
    ) -> str | None:
        for (t, k, eid), ent in self._entities.items():
            if t == tenant_id and k == kg and ent.name == name:
                return eid
        return None

    def list_assertions(
        self, *, tenant_id: str, kg: str
    ) -> list[AssertionNode]:
        return [
            a
            for (t, k, _), a in self._assertions.items()
            if t == tenant_id and k == kg
        ]

    def list_entities(self, *, tenant_id: str, kg: str) -> list[EntityNode]:
        return [
            e
            for (t, k, _), e in self._entities.items()
            if t == tenant_id and k == kg
        ]

    def get_entity(self, tenant_id: str, kg: str, entity_id: str) -> EntityNode | None:
        return self._entities.get((tenant_id, kg, entity_id))

    def get_assertion(
        self, tenant_id: str, kg: str, assertion_id: str
    ) -> AssertionNode | None:
        return self._assertions.get((tenant_id, kg, assertion_id))

    def subclass_parent(
        self, tenant_id: str, kg: str, class_id: str
    ) -> str | None:
        return self._subclass_of.get((tenant_id, kg, class_id))

    def instance_of_classes(
        self, tenant_id: str, kg: str, entity_id: str
    ) -> set[str]:
        return set(self._instance_of.get((tenant_id, kg, entity_id), set()))

    def all_class_ids(self, tenant_id: str, kg: str) -> list[str]:
        return [cid for (t, k, cid) in self._classes if t == tenant_id and k == kg]

    def class_name(self, tenant_id: str, kg: str, class_id: str) -> str | None:
        node = self._classes.get((tenant_id, kg, class_id))
        return node.name if node else None

    def property_name(self, tenant_id: str, kg: str, property_id: str) -> str | None:
        node = self._properties.get((tenant_id, kg, property_id))
        return node.name if node else None

    def clear(self) -> None:
        self._classes.clear()
        self._class_by_name.clear()
        self._properties.clear()
        self._property_by_name.clear()
        self._entities.clear()
        self._assertions.clear()
        self._subclass_of.clear()
        self._subproperty_of.clear()
        self._instance_of.clear()
        self._declares.clear()


def mint_entity(type_name: str, raw_id: str) -> str:
    """Thin alias — always go through the shared entity_uri sanitizer."""
    return entity_uri(type_name, raw_id)


def scope_key(scope: GraphScope) -> tuple[str, str]:
    return scope.tenant_id, scope.kg


__all__ = [
    "AssertionMemoryStore",
    "mint_entity",
    "scope_key",
]
