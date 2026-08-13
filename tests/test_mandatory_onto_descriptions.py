"""Mandatory short descriptions + description_updated_at on ontology expand."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.ontology_catalog import (
    list_attributes,
    list_types,
    upsert_attribute,
    upsert_type,
)
from infona_client.graph.ontology_descriptions import (
    default_short_description,
    ensure_description,
    humanize_leaf,
    utc_description_date,
)
from infona_client.graph.store import configure_graph_store, reset_graph_store_for_tests

TENANT = "desc-tenant"


@pytest.fixture
def store():
    s = MemoryGraphStore()
    configure_graph_store(s)
    yield s
    asyncio.run(s.close())
    reset_graph_store_for_tests()


def test_humanize_and_defaults():
    assert "unit cost" in humanize_leaf("unit_cost")
    assert "Entity type" in default_short_description("SynthWidget", kind="type")
    rel = default_short_description(
        "stored_in", kind="relationship", domain="Widget", range_type="Site"
    )
    assert "stored_in" in rel and "widget" in rel.lower()
    lit = default_short_description(
        "unit_cost", kind="literal", domain="Widget", datatype="float"
    )
    assert "float" in lit and "widget" in lit.lower()


def test_ensure_description_never_empty_and_dates():
    d, at = ensure_description("Sku", "", kind="type")
    assert d.strip()
    assert at == utc_description_date()
    # ISO date
    date.fromisoformat(at)
    d2, at2 = ensure_description("Sku", "  Custom blurb.  ", kind="type")
    assert d2 == "Custom blurb."
    assert at2 == at


def test_upsert_type_fills_description_and_date(store):
    async def run():
        rec = await upsert_type(
            store=store,
            name="SynthWidget",
            description="",
            layer="tenant",
            tenant_id=TENANT,
        )
        assert rec.description.strip()
        assert rec.description_updated_at
        date.fromisoformat(rec.description_updated_at)
        types = await list_types(store=store, tenant_id=TENANT, layer="tenant")
        by = {t.name: t for t in types}
        assert by["SynthWidget"].description.strip()
        assert by["SynthWidget"].description_updated_at

    asyncio.run(run())


def test_upsert_attribute_literal_and_relationship(store):
    async def run():
        await upsert_type(
            store=store, name="Widget", description="A widget.", layer="tenant", tenant_id=TENANT
        )
        lit = await upsert_attribute(
            store=store,
            type_name="Widget",
            attr_name="unit_cost",
            description="",
            datatype="float",
            layer="tenant",
            tenant_id=TENANT,
        )
        assert lit.description.strip()
        assert lit.description_updated_at
        assert lit.kind == "literal"

        rel = await upsert_attribute(
            store=store,
            type_name="Widget",
            attr_name="stored_in",
            description="",
            datatype="Site",
            layer="tenant",
            tenant_id=TENANT,
        )
        assert rel.kind == "relationship"
        assert rel.description.strip()
        assert rel.description_updated_at
        # Range type stub also described
        types = await list_types(store=store, tenant_id=TENANT, layer="tenant")
        by = {t.name: t for t in types}
        assert "Site" in by
        assert by["Site"].description.strip()

        attrs = await list_attributes(
            store=store, tenant_id=TENANT, layer="tenant", type_name="Widget"
        )
        names = {a.name: a for a in attrs}
        assert names["unit_cost"].description_updated_at
        assert names["stored_in"].description_updated_at

    asyncio.run(run())


def test_explicit_description_preserved_on_reupsert(store):
    async def run():
        await upsert_type(
            store=store,
            name="Widget",
            description="Custom type description.",
            layer="tenant",
            tenant_id=TENANT,
        )
        again = await upsert_type(
            store=store,
            name="Widget",
            description="",  # empty must NOT wipe custom
            layer="tenant",
            tenant_id=TENANT,
            clear_parent=False,
        )
        assert again.description == "Custom type description."

    asyncio.run(run())
