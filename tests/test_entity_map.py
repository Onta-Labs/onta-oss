"""Unit tests for type-qualified ingest id keys (CRM integer collisions)."""

from infona_client.resolver.entity_map import (
    lookup_type,
    lookup_uri,
    qualified_count,
    qualified_id,
    register_entity,
)
from infona_client.resolver.models_extract import ExtractedRelationship
from infona_client.resolver.entity_map import rel_source_key, rel_target_key


def test_qualified_id_joins_type_and_raw():
    assert qualified_id("Contact", "17") != qualified_id("Purchase", "17")


def test_register_keeps_both_uris_when_raw_ids_collide():
    uri_map: dict[str, str] = {}
    type_map: dict[str, str] = {}
    resolved: dict[str, str] = {}
    owner: dict[str, str] = {}
    collided: set[str] = set()
    register_entity(
        declared_type="Contact", entity_id="17", resolved_type="Contact",
        uri="https://graph.infona.ai/entities/Contact/17",
        uri_map=uri_map, type_map=type_map, resolved_types=resolved,
        unqualified_owner=owner, collided=collided,
    )
    register_entity(
        declared_type="Purchase", entity_id="17", resolved_type="Purchase",
        uri="https://graph.infona.ai/entities/Purchase/17",
        uri_map=uri_map, type_map=type_map, resolved_types=resolved,
        unqualified_owner=owner, collided=collided,
    )
    assert lookup_uri(uri_map, "17", "Contact").endswith("/Contact/17")
    assert lookup_uri(uri_map, "17", "Purchase").endswith("/Purchase/17")
    # Unqualified lookup must not silently pick a winner.
    assert lookup_uri(uri_map, "17") is None
    assert "17" in collided
    assert qualified_count(uri_map) == 2


def test_unique_id_still_resolves_unqualified():
    uri_map: dict[str, str] = {}
    type_map: dict[str, str] = {}
    resolved: dict[str, str] = {}
    owner: dict[str, str] = {}
    collided: set[str] = set()
    register_entity(
        declared_type="Product", entity_id="sku-1", resolved_type="Product",
        uri="https://graph.infona.ai/entities/Product/sku-1",
        uri_map=uri_map, type_map=type_map, resolved_types=resolved,
        unqualified_owner=owner, collided=collided,
    )
    assert lookup_uri(uri_map, "sku-1") is not None
    assert lookup_type(type_map, "sku-1") == "Product"
    assert not collided


def test_rel_keys_use_declared_types_when_present():
    rel = ExtractedRelationship(
        source_id="17", predicate="bought_by", target_id="17",
        source_type="Purchase", target_type="Contact",
    )
    assert rel_source_key(rel) == qualified_id("Purchase", "17")
    assert rel_target_key(rel) == qualified_id("Contact", "17")
    legacy = ExtractedRelationship(source_id="a", predicate="x", target_id="b")
    assert rel_source_key(legacy) == "a"
    assert rel_target_key(legacy) == "b"
