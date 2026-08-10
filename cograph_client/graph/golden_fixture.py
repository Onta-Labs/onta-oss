"""Hermetic mini_people fixture for Neo4j RDF-semantic golden queries.

Builds Assertion-centric structure on :class:`AssertionMemoryStore` (optionally
mirroring Entity MERGEs into :class:`MemoryGraphStore`). No AWS / Neptune.

Fixture content (neo4j-golden-queries.md §4.1):

* Classes: ``Agent`` ⊐ ``Person`` ⊐ ``Employee``; ``Organization``
* Properties: name, birth_year, email (multi), works_at (object), rdf_type
* Entities:
  - Alice — Person; birth_year 1991; emails (2); works_at Acme (+ provenance)
  - Bob — Person + Employee (multi-type)
  - Dana — Employee only (type via subclass of Person)
  - Acme — Organization
* Sibling empty kg for isolation (GQ-11)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cograph_client.graph.assertion_memory import AssertionMemoryStore, mint_entity
from cograph_client.graph.assertion_model import (
    AssertionFact,
    MiniPeopleIds,
    property_uri,
    type_membership_property_id,
)
from cograph_client.graph.memory_store import MemoryGraphStore
from cograph_client.graph.ontology_queries import type_uri


DEFAULT_TENANT = "golden-tenant"
DEFAULT_KG = "mini_people"
DEFAULT_SIBLING_KG = "mini_people_other"


@dataclass
class MiniPeopleFixture:
    """Loaded mini_people graph + resolved ids for gold expansion."""

    store: AssertionMemoryStore
    mirror: MemoryGraphStore | None
    ids: MiniPeopleIds

    @property
    def tenant_id(self) -> str:
        return self.ids.tenant_id

    @property
    def kg(self) -> str:
        return self.ids.kg

    @property
    def sibling_kg(self) -> str:
        return self.ids.sibling_kg


def build_mini_people(
    *,
    tenant_id: str = DEFAULT_TENANT,
    kg: str = DEFAULT_KG,
    sibling_kg: str = DEFAULT_SIBLING_KG,
    mirror: MemoryGraphStore | None = None,
) -> MiniPeopleFixture:
    """Populate catalog + instance Assertions for the golden suite."""
    if mirror is None:
        mirror = MemoryGraphStore()
    store = AssertionMemoryStore(mirror=mirror)

    # --- catalog (tenant ontology scope could be __ontology__; use kg for hermetic simplicity)
    store.ensure_type_membership_property(tenant_id=tenant_id, kg=kg)

    agent = store.upsert_class(tenant_id=tenant_id, kg=kg, name="Agent")
    person = store.upsert_class(
        tenant_id=tenant_id, kg=kg, name="Person", parent_name="Agent"
    )
    employee = store.upsert_class(
        tenant_id=tenant_id, kg=kg, name="Employee", parent_name="Person"
    )
    org = store.upsert_class(tenant_id=tenant_id, kg=kg, name="Organization")

    store.upsert_property(
        tenant_id=tenant_id,
        kg=kg,
        name="name",
        kind="datatype",
        datatype="string",
        domain_class_name="Agent",
    )
    store.upsert_property(
        tenant_id=tenant_id,
        kg=kg,
        name="birth_year",
        kind="datatype",
        datatype="long",
        domain_class_name="Person",
    )
    store.upsert_property(
        tenant_id=tenant_id,
        kg=kg,
        name="email",
        kind="datatype",
        datatype="string",
        cardinality="1:N",
        domain_class_name="Person",
    )
    store.upsert_property(
        tenant_id=tenant_id,
        kg=kg,
        name="works_at",
        kind="object",
        range_class_name="Organization",
        domain_class_name="Person",
    )

    # --- entities
    alice_id = mint_entity("Person", "Alice")
    bob_id = mint_entity("Person", "Bob")
    dana_id = mint_entity("Employee", "Dana")
    acme_id = mint_entity("Organization", "Acme")

    store.merge_entity(
        tenant_id=tenant_id, kg=kg, entity_id=alice_id, name="Alice", primary_type="Person"
    )
    store.merge_entity(
        tenant_id=tenant_id, kg=kg, entity_id=bob_id, name="Bob", primary_type="Person"
    )
    store.merge_entity(
        tenant_id=tenant_id, kg=kg, entity_id=dana_id, name="Dana", primary_type="Employee"
    )
    store.merge_entity(
        tenant_id=tenant_id, kg=kg, entity_id=acme_id, name="Acme", primary_type="Organization"
    )

    facts: list[AssertionFact] = [
        # Types
        AssertionFact(subject_id=alice_id, kind="type", value="Person"),
        AssertionFact(subject_id=bob_id, kind="type", value="Person"),
        AssertionFact(subject_id=bob_id, kind="type", value="Employee"),
        AssertionFact(subject_id=dana_id, kind="type", value="Employee"),
        AssertionFact(subject_id=acme_id, kind="type", value="Organization"),
        # Literals
        AssertionFact(
            subject_id=alice_id, kind="literal", property_leaf="name", value="Alice"
        ),
        AssertionFact(
            subject_id=alice_id, kind="literal", property_leaf="birth_year", value=1991
        ),
        AssertionFact(
            subject_id=alice_id,
            kind="literal",
            property_leaf="email",
            value="alice@example.com",
            source_url="https://example.com/people/alice",
            verified_at="2026-01-15T12:00:00Z",
            run_id="enrich-run-1",
            confidence=0.95,
        ),
        AssertionFact(
            subject_id=alice_id,
            kind="literal",
            property_leaf="email",
            value="a.work@acme.com",
            source_url="https://example.com/people/alice#work",
            verified_at="2026-01-16T08:00:00Z",
            run_id="enrich-run-2",
            confidence=0.9,
        ),
        AssertionFact(
            subject_id=bob_id, kind="literal", property_leaf="name", value="Bob"
        ),
        AssertionFact(
            subject_id=bob_id, kind="literal", property_leaf="birth_year", value=1985
        ),
        AssertionFact(
            subject_id=dana_id, kind="literal", property_leaf="name", value="Dana"
        ),
        AssertionFact(
            subject_id=dana_id, kind="literal", property_leaf="birth_year", value=1995
        ),
        AssertionFact(
            subject_id=acme_id, kind="literal", property_leaf="name", value="Acme"
        ),
        # Object property
        AssertionFact(
            subject_id=alice_id,
            kind="object",
            property_leaf="works_at",
            value=acme_id,
            source_url="https://example.com/org/acme/employees",
            verified_at="2026-02-01T00:00:00Z",
            run_id="ingest-run-1",
            confidence=1.0,
        ),
    ]
    store.insert_facts(facts, tenant_id=tenant_id, kg=kg)

    # Sibling scope exists as empty catalog stub (no entities) for isolation.
    store.ensure_type_membership_property(tenant_id=tenant_id, kg=sibling_kg)
    store.upsert_class(tenant_id=tenant_id, kg=sibling_kg, name="Person")

    ids = MiniPeopleIds(
        tenant_id=tenant_id,
        kg=kg,
        sibling_kg=sibling_kg,
        classes={
            "Agent": agent.id,
            "Person": person.id,
            "Employee": employee.id,
            "Organization": org.id,
        },
        properties={
            "name": property_uri("name"),
            "birth_year": property_uri("birth_year"),
            "email": property_uri("email"),
            "works_at": property_uri("works_at"),
            "rdf_type": type_membership_property_id(),
        },
        entities={
            "Alice": alice_id,
            "Bob": bob_id,
            "Dana": dana_id,
            "Acme": acme_id,
        },
    )
    return MiniPeopleFixture(store=store, mirror=mirror, ids=ids)


def expand_symbol(value: Any, ids: MiniPeopleIds) -> Any:
    """Expand gold placeholders like ``$entity:Alice`` / ``$class:Person``."""
    if isinstance(value, str) and value.startswith("$"):
        if value == "$sibling_kg":
            return ids.sibling_kg
        if value == "$kg":
            return ids.kg
        if value == "$tenant":
            return ids.tenant_id
        if value.startswith("$entity:"):
            key = value[len("$entity:") :]
            # allow Type:raw or bare name
            if ":" in key and key.split(":", 1)[0] in (
                "Person",
                "Employee",
                "Organization",
                "Agent",
            ):
                # $entity:Person:Alice — prefer entities map by raw id leaf
                leaf = key.split(":", 1)[1]
                return ids.entities.get(leaf, mint_entity(key.split(":", 1)[0], leaf))
            return ids.entities[key]
        if value.startswith("$class:"):
            return ids.classes[value[len("$class:") :]]
        if value.startswith("$prop:"):
            return ids.properties[value[len("$prop:") :]]
        if value.startswith("$type_uri:"):
            return type_uri(value[len("$type_uri:") :])
    if isinstance(value, list):
        return [expand_symbol(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: expand_symbol(v, ids) for k, v in value.items()}
    return value


__all__ = [
    "DEFAULT_KG",
    "DEFAULT_SIBLING_KG",
    "DEFAULT_TENANT",
    "MiniPeopleFixture",
    "build_mini_people",
    "expand_symbol",
]
