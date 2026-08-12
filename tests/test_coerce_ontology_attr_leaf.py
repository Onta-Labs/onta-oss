"""Reserved Entity property keys must coerce to ontology-safe leaves."""

from infona_client.graph.facts import (
    RESERVED_ENTITY_PROPERTY_KEYS,
    coerce_ontology_attr_leaf,
)
from infona_client.resolver.attribute_resolver import _normalize_attr_name


def test_coerce_name_to_display_name():
    assert coerce_ontology_attr_leaf("name") == "display_name"
    assert coerce_ontology_attr_leaf("Name") == "display_name"
    assert coerce_ontology_attr_leaf("  name  ") == "display_name"


def test_coerce_other_reserved():
    assert coerce_ontology_attr_leaf("id") == "external_id"
    assert coerce_ontology_attr_leaf("label") == "display_label"
    assert coerce_ontology_attr_leaf("tenant_id") == "external_tenant_id"


def test_coerce_passes_through_safe_leaves():
    assert coerce_ontology_attr_leaf("title") == "title"
    assert coerce_ontology_attr_leaf("author") == "author"
    assert coerce_ontology_attr_leaf("rating") == "rating"


def test_reserved_set_all_have_renames():
    for key in RESERVED_ENTITY_PROPERTY_KEYS:
        out = coerce_ontology_attr_leaf(key)
        assert out not in RESERVED_ENTITY_PROPERTY_KEYS
        assert out  # non-empty


def test_normalize_attr_name_coerces_reserved():
    assert _normalize_attr_name("name") == "display_name"
    # manufacturedBy stays non-reserved snake_case
    assert _normalize_attr_name("manufacturedBy") == "manufactured_by"

