"""E4 — Ontology catalog (OntoType / OntoAttr) on Memory GraphStore.

Hermetic: no Neo4j process. Covers create/upsert/list, reserved-key rejection
(B2), dual-backend resolution, and the KG schema retrieval stub.
"""

from __future__ import annotations

import asyncio

import pytest

from cograph_client.graph.facts import RESERVED_ENTITY_PROPERTY_KEYS
from cograph_client.graph.labels import RESERVED_SYSTEM_LABELS
from cograph_client.graph.memory_store import MemoryGraphStore
from cograph_client.graph.ontology_catalog import (
    LITERAL_DATATYPES,
    OntoAttrRecord,
    OntoTypeRecord,
    classify_attr_range,
    get_type_pg,
    graph_backend,
    list_attributes,
    list_types,
    resolve_catalog_session,
    schema_types_for_kg,
    upsert_attribute,
    upsert_type,
)
from cograph_client.graph.pg_ops import merge_entity
from cograph_client.graph.schema_bootstrap import TEMPLATES
from cograph_client.graph.scope import (
    GLOBAL_TENANT_ID,
    ONTOLOGY_KG,
    PUBLIC_KG,
    GraphScope,
    GraphScopeError,
)
from cograph_client.graph.store import configure_graph_store, reset_graph_store_for_tests


@pytest.fixture
def store():
    s = MemoryGraphStore()
    yield s
    asyncio.run(s.close())
    reset_graph_store_for_tests()


def test_catalog_templates_registered():
    for name in (
        "onto_type_upsert",
        "onto_subclass_set",
        "onto_subclass_clear",
        "onto_type_list",
        "onto_type_get",
        "onto_attr_upsert",
        "onto_attr_range_type",
        "onto_attr_list",
        "entity_count_by_primary_type",
    ):
        assert name in TEMPLATES
        assert "$tenant_id" in TEMPLATES[name].cypher
        assert "$kg" in TEMPLATES[name].cypher


def test_classify_attr_range_literal_vs_rel():
    assert classify_attr_range("string") == ("literal", "string", None)
    assert classify_attr_range("integer") == ("literal", "integer", None)
    kind, dt, rng = classify_attr_range("Organization")
    assert kind == "relationship" and dt is None and rng == "Organization"
    assert "string" in LITERAL_DATATYPES


def test_upsert_type_and_list(store):
    async def run():
        rec = await upsert_type(
            store=store,
            layer="tenant",
            tenant_id="demo-tenant",
            name="Person",
            description="A person",
        )
        assert isinstance(rec, OntoTypeRecord)
        assert rec.name == "Person"
        assert rec.layer == "tenant"
        assert rec.tenant_id == "demo-tenant"
        assert rec.kg == ONTOLOGY_KG
        assert rec.description == "A person"
        assert rec.label_token == "Person"
        assert rec.parent_type is None

        await upsert_type(
            store=store,
            layer="tenant",
            tenant_id="demo-tenant",
            name="Employee",
            description="Works somewhere",
            parent_type="Person",
        )
        types = await list_types(
            store=store, layer="tenant", tenant_id="demo-tenant"
        )
        by_name = {t.name: t for t in types}
        assert set(by_name) == {"Person", "Employee"}
        assert by_name["Employee"].parent_type == "Person"
        assert by_name["Person"].parent_type is None

        # Idempotent upsert with same description
        again = await upsert_type(
            store=store,
            layer="tenant",
            tenant_id="demo-tenant",
            name="Person",
            description="A person",
        )
        assert again.name == "Person"
        types2 = await list_types(
            store=store, layer="tenant", tenant_id="demo-tenant"
        )
        assert len(types2) == 2

    asyncio.run(run())


def test_upsert_type_clears_parent_when_none(store):
    async def run():
        await upsert_type(
            store=store,
            layer="tenant",
            tenant_id="t1",
            name="Child",
            parent_type="Parent",
        )
        t = await get_type_pg(
            store.session(GraphScope.for_catalog(layer="tenant", tenant_id="t1")),
            "Child",
        )
        assert t is not None and t.parent_type == "Parent"

        await upsert_type(
            store=store,
            layer="tenant",
            tenant_id="t1",
            name="Child",
            parent_type=None,
            clear_parent=True,
        )
        t2 = await get_type_pg(
            store.session(GraphScope.for_catalog(layer="tenant", tenant_id="t1")),
            "Child",
        )
        assert t2 is not None and t2.parent_type is None

    asyncio.run(run())


def test_upsert_attribute_literal_and_relationship(store):
    async def run():
        await upsert_type(
            store=store, layer="tenant", tenant_id="demo", name="Person"
        )
        await upsert_type(
            store=store, layer="tenant", tenant_id="demo", name="Organization"
        )

        email = await upsert_attribute(
            store=store,
            layer="tenant",
            tenant_id="demo",
            type_name="Person",
            attr_name="email",
            description="Email address",
            datatype="string",
        )
        assert isinstance(email, OntoAttrRecord)
        assert email.kind == "literal"
        assert email.datatype == "string"
        assert email.range_type is None
        assert email.domain == "Person"
        assert email.cardinality == "1:1"
        assert email.prop_key is None  # safe leaf

        works = await upsert_attribute(
            store=store,
            layer="tenant",
            tenant_id="demo",
            type_name="Person",
            attr_name="works_at",
            datatype="Organization",
        )
        assert works.kind == "relationship"
        assert works.range_type == "Organization"
        assert works.datatype is None
        assert works.cardinality == "N:N"

        # Unsafe leaf gets prop_key
        town = await upsert_attribute(
            store=store,
            layer="tenant",
            tenant_id="demo",
            type_name="Person",
            attr_name="city/town",
            datatype="string",
        )
        assert town.prop_key == "city_town"

        attrs = await list_attributes(
            store=store,
            layer="tenant",
            tenant_id="demo",
            type_name="Person",
        )
        names = {a.name for a in attrs}
        assert names == {"email", "works_at", "city/town"}

        # Upsert flips range literal → relationship (idempotent replace)
        flipped = await upsert_attribute(
            store=store,
            layer="tenant",
            tenant_id="demo",
            type_name="Person",
            attr_name="email",
            datatype="Organization",
        )
        assert flipped.kind == "relationship"
        assert flipped.range_type == "Organization"

    asyncio.run(run())


def test_reject_reserved_entity_property_keys_b2(store):
    async def run():
        await upsert_type(
            store=store, layer="tenant", tenant_id="demo", name="Person"
        )
        for key in ("id", "tenant_id", "kg", "primary_type", "name", "label", "source"):
            assert key in RESERVED_ENTITY_PROPERTY_KEYS
            with pytest.raises(GraphScopeError, match="reserved"):
                await upsert_attribute(
                    store=store,
                    layer="tenant",
                    tenant_id="demo",
                    type_name="Person",
                    attr_name=key,
                    datatype="string",
                )

    asyncio.run(run())


def test_reject_reserved_system_label_as_type(store):
    async def run():
        for lab in ("Entity", "OntoType", "OntoAttr", "ProvEvent"):
            assert lab in RESERVED_SYSTEM_LABELS
            with pytest.raises(GraphScopeError, match="reserved"):
                await upsert_type(
                    store=store,
                    layer="tenant",
                    tenant_id="demo",
                    name=lab,
                )

    asyncio.run(run())


def test_catalog_scope_isolation(store):
    async def run():
        await upsert_type(
            store=store, layer="tenant", tenant_id="acme", name="Person"
        )
        await upsert_type(
            store=store, layer="tenant", tenant_id="beta", name="Hotel"
        )
        acme = await list_types(store=store, layer="tenant", tenant_id="acme")
        beta = await list_types(store=store, layer="tenant", tenant_id="beta")
        assert [t.name for t in acme] == ["Person"]
        assert [t.name for t in beta] == ["Hotel"]

    asyncio.run(run())


def test_public_layer_requires_privileged_for_write(store):
    async def run():
        # Non-privileged global write must fail closed.
        with pytest.raises(GraphScopeError, match="privileged|global"):
            await upsert_type(
                store=store,
                layer="public",
                name="Thing",
                privileged=False,
            )
        # Privileged write succeeds.
        rec = await upsert_type(
            store=store,
            layer="public",
            name="Thing",
            privileged=True,
        )
        assert rec.tenant_id == GLOBAL_TENANT_ID
        assert rec.kg == PUBLIC_KG
        assert rec.layer == "public"

        types = await list_types(store=store, layer="public")
        assert any(t.name == "Thing" for t in types)

    asyncio.run(run())


def test_schema_types_for_kg_with_counts(store):
    async def run():
        await upsert_type(
            store=store, layer="tenant", tenant_id="demo", name="Person"
        )
        await upsert_type(
            store=store, layer="tenant", tenant_id="demo", name="Org"
        )
        await upsert_attribute(
            store=store,
            layer="tenant",
            tenant_id="demo",
            type_name="Person",
            attr_name="email",
            datatype="string",
        )
        # Instance entities in bookstore kg
        sess = store.session(GraphScope.for_instance("demo", "bookstore"))
        await merge_entity(sess, "e1", primary_type="Person", name="Alice")
        await merge_entity(sess, "e2", primary_type="Person", name="Bob")
        await merge_entity(sess, "e3", primary_type="Org", name="Acme")

        summary = await schema_types_for_kg(
            store, tenant_id="demo", kg="bookstore"
        )
        by_name = {s.name: s for s in summary}
        assert by_name["Person"].entity_count == 2
        assert by_name["Org"].entity_count == 1
        assert len(by_name["Person"].attributes) == 1
        assert by_name["Person"].attributes[0].name == "email"

    asyncio.run(run())


def test_resolve_catalog_session_env_backend(store, monkeypatch):
    monkeypatch.setenv("COGRAPH_GRAPH_BACKEND", "neo4j")
    configure_graph_store(store)
    try:
        assert graph_backend() == "neo4j"
        sess = resolve_catalog_session(layer="tenant", tenant_id="demo")
        assert sess is not None
        assert sess.scope.kg == ONTOLOGY_KG
    finally:
        reset_graph_store_for_tests()
        monkeypatch.delenv("COGRAPH_GRAPH_BACKEND", raising=False)


def test_resolve_catalog_session_none_when_neptune(monkeypatch):
    monkeypatch.delenv("COGRAPH_GRAPH_BACKEND", raising=False)
    reset_graph_store_for_tests()
    assert graph_backend() == "neptune"
    assert resolve_catalog_session(layer="tenant", tenant_id="demo") is None


def test_sparql_path_requires_client_without_store(monkeypatch):
    monkeypatch.delenv("COGRAPH_GRAPH_BACKEND", raising=False)
    reset_graph_store_for_tests()

    async def run():
        with pytest.raises(GraphScopeError, match="SPARQL|neptune"):
            await upsert_type(name="Person", layer="tenant", tenant_id="demo")
        with pytest.raises(GraphScopeError, match="SPARQL|neptune"):
            await list_types(layer="tenant", tenant_id="demo")

    asyncio.run(run())


def test_session_passthrough(store):
    async def run():
        sess = store.session(
            GraphScope.for_catalog(layer="tenant", tenant_id="x")
        )
        rec = await upsert_type(session=sess, name="Widget")
        assert rec.name == "Widget"
        types = await list_types(session=sess)
        assert [t.name for t in types] == ["Widget"]

    asyncio.run(run())
