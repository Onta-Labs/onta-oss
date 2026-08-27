"""CSV mapped ingest: two types sharing a numeric id stay distinct.

CRM exports number Contact 17 and Purchase 17 independently. The write-path
id map used to be keyed by raw id alone (last writer wins). This captures
``insert_facts`` triples — no live store, no customer data.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.queries import kg_graph_uri
from infona_client.resolver.models import (
    ColumnMapping,
    ColumnRole,
    CSVSchemaMapping,
    EntityRelationSpec,
    EntitySpec,
)
from infona_client.resolver.schema_resolver import SchemaResolver
from infona_client.resolver.verdict_cache import JsonVerdictCache

TENANT, KG = "typedids", "crm"
GRAPH = f"https://graph.infona.ai/graphs/{TENANT}"
INSTANCE = kg_graph_uri(TENANT, KG)


def _purchase_mapping() -> CSVSchemaMapping:
    return CSVSchemaMapping(
        entity_type="Purchase",
        entities=[
            EntitySpec(name="purchase", type_name="Purchase", id_column="order_id",
                       key_strategy="column"),
            EntitySpec(name="contact", type_name="Contact", id_column="contact_id",
                       key_strategy="column"),
            EntitySpec(name="product", type_name="Product", id_column="product_id",
                       key_strategy="column"),
        ],
        relationships=[
            EntityRelationSpec(subject="purchase", predicate="bought_by", object="contact"),
            EntityRelationSpec(subject="purchase", predicate="has_product", object="product"),
        ],
        columns=[
            ColumnMapping(
                column_name="total", role=ColumnRole.ATTRIBUTE,
                datatype="float", attribute_name="total", entity="purchase",
            ),
        ],
    )


@pytest.mark.asyncio
async def test_colliding_numeric_ids_emit_distinct_uris_and_edges(monkeypatch, tmp_path):
    collected: list[tuple[str, str, str]] = []

    async def capture(_neptune, _graph, triples, **_kwargs):
        collected.extend(triples or [])

    monkeypatch.setattr(
        "infona_client.resolver.schema_resolver.insert_facts", capture,
    )

    n = MagicMock()
    n.query = AsyncMock(return_value={"results": {"bindings": []}})
    n.update = AsyncMock()
    n.batch_exists = AsyncMock(return_value=set())
    resolver = SchemaResolver(n, "fake-key", JsonVerdictCache(tmp_path / "v.json"))
    resolver._er_enabled = False

    async def _resolve_type(entity, *args, **kwargs):
        return entity.type_name

    resolver._resolve_type = _resolve_type  # type: ignore[method-assign]
    resolver._commit_ontology = AsyncMock()  # type: ignore[method-assign]
    resolver._apply_mapping_text_markers = AsyncMock()  # type: ignore[method-assign]
    resolver._refresh_ontology = AsyncMock()  # type: ignore[method-assign]

    rows = [{"order_id": "17", "contact_id": "17", "product_id": "17", "total": "40"}]
    existing = {"Contact": "", "Purchase": "", "Product": ""}
    result = await resolver._ingest_mapped(
        _purchase_mapping(), rows, GRAPH,
        existing, {t: {} for t in existing}, "",
        instance_graph=INSTANCE,
    )
    assert result.entities_resolved == 3

    contact_uri = entity_uri("Contact", "17")
    purchase_uri = entity_uri("Purchase", "17")
    product_uri = entity_uri("Product", "17")
    subjects = {s for s, _p, _o in collected}
    assert contact_uri in subjects
    assert purchase_uri in subjects
    assert product_uri in subjects

    bought = [
        (s, o) for s, p, o in collected
        if p.endswith("/onto/bought_by")
    ]
    assert bought == [(purchase_uri, contact_uri)]
    has_prod = [
        (s, o) for s, p, o in collected
        if p.endswith("/onto/has_product")
    ]
    assert has_prod == [(purchase_uri, product_uri)]

    leaked = [
        p for s, p, _o in collected
        if s == contact_uri and p.endswith("/total")
    ]
    assert leaked == []
