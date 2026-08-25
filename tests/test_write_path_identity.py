"""Write-path identity: key columns, numeric literals, one-row one-entity, labels.

Generic Event/Person/Order fixtures — no founder-domain special cases.
"""

from __future__ import annotations

import pytest

from infona_client.graph.facts import entity_display_label
from infona_client.graph.ontology_catalog import classify_attr_range, list_attributes
from infona_client.graph.ontology_catalog_models import (
    LITERAL_DATATYPES,
    canonicalize_literal_datatype,
)
from infona_client.graph.queries import kg_graph_uri
from infona_client.graph.store import get_graph_store
from infona_client.resolver.attribute_resolver import (
    check_promotion,
    resolve_attribute,
)
from infona_client.resolver.csv_resolver import CSVResolver
from infona_client.resolver.models import (
    ColumnMapping,
    ColumnRole,
    CSVSchemaMapping,
    EntitySpec,
    ExtractedAttribute,
    ExtractedEntity,
)
from infona_client.resolver.schema_resolver import SchemaResolver
from infona_client.resolver.verdict_cache import JsonVerdictCache

TENANT = "write-path-id"
KG = "events"
GRAPH = f"https://graph.infona.ai/graphs/{TENANT}"
INSTANCE = kg_graph_uri(TENANT, KG)


def _resolver(mock_neptune) -> SchemaResolver:
    cache = JsonVerdictCache.__new__(JsonVerdictCache)
    cache._path = None
    cache._cache = {}
    resolver = SchemaResolver(mock_neptune, "fake-key", cache)
    resolver._er_enabled = False
    mock_neptune.batch_exists.return_value = set()
    return resolver


def test_number_is_literal_not_relationship_range():
    assert "number" in LITERAL_DATATYPES
    kind, dt, rng = classify_attr_range("number")
    assert kind == "literal"
    assert rng is None
    assert dt == "float"
    assert canonicalize_literal_datatype("number") == "float"
    assert canonicalize_literal_datatype("integer") == "integer"
    resolved = resolve_attribute(
        ExtractedAttribute(name="amount", value="12.5", datatype="number"),
        {},
    )
    assert resolved.datatype == "float"


def test_key_column_order_id_is_not_reserved_attr_id():
    mapping = CSVSchemaMapping(
        entity_type="Order",
        columns=[
            ColumnMapping(
                column_name="order_id",
                role=ColumnRole.TYPE_ID,
                datatype="string",
                attribute_name="id",
            ),
            ColumnMapping(
                column_name="amount",
                role=ColumnRole.ATTRIBUTE,
                datatype="number",
                attribute_name="amount",
            ),
        ],
    )
    applied = CSVResolver.apply_mapping(
        mapping, [{"order_id": "ORD-1", "amount": "9.50"}]
    )
    names = {a.name for e in applied.entities for a in e.attributes}
    assert "order_id" in names
    assert "id" not in names


def test_multi_entity_id_column_keeps_order_id_leaf():
    mapping = CSVSchemaMapping(
        entity_type="Order",
        entities=[EntitySpec(name="order", type_name="Order", id_column="order_id")],
        columns=[
            ColumnMapping(
                column_name="order_id",
                role=ColumnRole.ATTRIBUTE,
                datatype="string",
                attribute_name="id",
                entity="order",
            ),
            ColumnMapping(
                column_name="amount",
                role=ColumnRole.ATTRIBUTE,
                datatype="number",
                attribute_name="amount",
                entity="order",
            ),
        ],
    )
    applied = CSVResolver.apply_mapping(
        mapping, [{"order_id": "ORD-1", "amount": "4"}]
    )
    names = {a.name for e in applied.entities for a in e.attributes}
    assert "order_id" in names
    assert "id" not in names


def test_same_type_prefix_cluster_does_not_self_promote():
    entity = ExtractedEntity(
        type_name="Event",
        id="e1",
        attributes=[
            ExtractedAttribute(name="event_id", value="e1", datatype="string"),
            ExtractedAttribute(name="event_title", value="Kickoff", datatype="string"),
            ExtractedAttribute(name="event_date", value="2026-01-01", datatype="datetime"),
            ExtractedAttribute(name="event_type", value="sync", datatype="string"),
        ],
    )
    promotions = check_promotion(
        entity, {}, existing_types={"Event": "an event"},
    )
    assert promotions == []


def test_nested_address_cluster_still_promotes_without_reserved_id():
    entity = ExtractedEntity(
        type_name="Property",
        id="p1",
        attributes=[
            ExtractedAttribute(name="address_id", value="a-1", datatype="string"),
            ExtractedAttribute(name="address_street", value="1 Main", datatype="string"),
            ExtractedAttribute(name="address_city", value="Austin", datatype="string"),
            ExtractedAttribute(name="address_zip", value="78701", datatype="string"),
        ],
    )
    promotions = check_promotion(
        entity, {}, existing_types={"Address": "postal"},
    )
    assert promotions
    assert all(p.promoted_type == "Address" for p in promotions)
    assert {p.name for p in promotions} == {
        "address_id", "street", "city", "zip",
    }
    assert "id" not in {p.name for p in promotions}


def test_display_label_prefers_human_cell_over_slug():
    entity = ExtractedEntity(
        type_name="Person",
        id="Ada_Lovelace",
        attributes=[
            ExtractedAttribute(
                name="person_name", value="Ada Lovelace", datatype="string",
            ),
        ],
    )
    assert entity_display_label(entity.id, entity.attributes) == "Ada Lovelace"
    opaque = ExtractedEntity(
        type_name="Order",
        id="ORD-1",
        attributes=[
            ExtractedAttribute(name="order_id", value="ORD-1", datatype="string"),
        ],
    )
    assert entity_display_label(opaque.id, opaque.attributes) == "ORD-1"


@pytest.mark.asyncio
async def test_ingest_key_number_dedupe_and_human_name(mock_neptune):
    resolver = _resolver(mock_neptune)
    mapping = CSVSchemaMapping(
        entity_type="Event",
        columns=[
            ColumnMapping(
                column_name="event_id",
                role=ColumnRole.TYPE_ID,
                datatype="string",
                attribute_name="event_id",
            ),
            ColumnMapping(
                column_name="event_title",
                role=ColumnRole.ATTRIBUTE,
                datatype="string",
                attribute_name="event_title",
            ),
            ColumnMapping(
                column_name="event_date",
                role=ColumnRole.ATTRIBUTE,
                datatype="datetime",
                attribute_name="event_date",
            ),
            ColumnMapping(
                column_name="score",
                role=ColumnRole.ATTRIBUTE,
                datatype="number",
                attribute_name="score",
            ),
            ColumnMapping(
                column_name="person_name",
                role=ColumnRole.ATTRIBUTE,
                datatype="string",
                attribute_name="person_name",
            ),
        ],
    )
    rows = [
        {
            "event_id": "EVT-1",
            "event_title": "Q3 Offsite",
            "event_date": "2026-03-01",
            "score": "8.5",
            "person_name": "Ada Lovelace",
        },
        {
            "event_id": "EVT-1",
            "event_title": "Q3 Offsite",
            "event_date": "2026-03-01",
            "score": "8.5",
            "person_name": "Ada Lovelace",
        },
    ]
    result = await resolver._ingest_mapped(
        mapping,
        rows,
        GRAPH,
        {"Event": ""},
        {"Event": {}},
        "",
        instance_graph=INSTANCE,
    )
    assert result.rejections == [] or all(
        getattr(r, "reason", "") != "reserved" for r in result.rejections
    )

    store = get_graph_store()
    events = [
        e for e in store.snapshot_entities()
        if e.get("primary_type") == "Event"
    ]
    assert len(events) == 1
    event = events[0]
    assert event["name"] == "Q3 Offsite"
    props = event.get("props") or {}
    assert props.get("person_name") == "Ada Lovelace"
    assert "score" in props
    assert str(props["score"]).startswith("8.5")

    rels = store.snapshot_rels()
    assert not any(
        (r.get("rel_type") or "").upper() == "HAS_EVENT"
        or (r.get("attr") or "") == "has_event"
        for r in rels
    )
    assert not any(e["id"].endswith("-event") for e in events)

    attrs = await list_attributes(
        store=store, layer="tenant", tenant_id=TENANT, type_name="Event",
    )
    names = {a.name for a in attrs}
    assert "event_id" in names
    assert "id" not in names
    score = next(a for a in attrs if a.name == "score")
    assert score.kind == "literal"
    assert score.range_type is None
    assert score.datatype in {"float", "number", "integer"}
    types = {e.get("primary_type") for e in store.snapshot_entities()}
    assert "number" not in types
    assert "Number" not in types


@pytest.mark.asyncio
async def test_person_keyed_by_human_name_keeps_spaces(mock_neptune):
    resolver = _resolver(mock_neptune)
    mapping = CSVSchemaMapping(
        entity_type="Person",
        columns=[
            ColumnMapping(
                column_name="person_name",
                role=ColumnRole.TYPE_ID,
                datatype="string",
                attribute_name="person_name",
            ),
        ],
    )
    await resolver._ingest_mapped(
        mapping,
        [{"person_name": "Ada Lovelace"}],
        GRAPH,
        {"Person": ""},
        {"Person": {}},
        "",
        instance_graph=INSTANCE,
    )
    people = [
        e for e in get_graph_store().snapshot_entities()
        if e.get("primary_type") == "Person"
    ]
    assert len(people) == 1
    assert people[0]["name"] == "Ada Lovelace"
    assert "Ada_Lovelace" not in (people[0]["name"] or "")
