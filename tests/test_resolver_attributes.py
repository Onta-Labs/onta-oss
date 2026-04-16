"""Tests for attribute resolution."""

import pytest

from cograph.resolver.attribute_resolver import (
    AttributeSchema,
    check_promotion,
    resolve_attribute,
)
from cograph.resolver.models import AttrAction, ExtractedAttribute, ExtractedEntity


class TestResolveAttribute:
    def test_reuse_same_datatype(self):
        attr = ExtractedAttribute(name="price", value="500000", datatype="integer")
        existing = {"price": AttributeSchema("price", "integer")}
        result = resolve_attribute(attr, existing)
        assert result.action == AttrAction.REUSE
        assert result.name == "price"

    def test_extend_new_attribute(self):
        attr = ExtractedAttribute(name="bedrooms", value="3", datatype="integer")
        existing = {"price": AttributeSchema("price", "integer")}
        result = resolve_attribute(attr, existing)
        assert result.action == AttrAction.EXTEND
        assert result.name == "bedrooms"

    def test_coerce_different_datatype(self):
        attr = ExtractedAttribute(name="price", value="500000.0", datatype="float")
        existing = {"price": AttributeSchema("price", "integer")}
        result = resolve_attribute(attr, existing)
        assert result.action == AttrAction.COERCE
        assert result.value == "500000"
        assert result.original_value == "500000.0"

    def test_name_normalization(self):
        attr = ExtractedAttribute(name="Bed Rooms", value="3", datatype="integer")
        existing = {"bed_rooms": AttributeSchema("bed_rooms", "integer")}
        result = resolve_attribute(attr, existing)
        assert result.action == AttrAction.REUSE
        assert result.name == "bed_rooms"


class TestCheckPromotion:
    def test_cluster_detection(self):
        entity = ExtractedEntity(
            type_name="Property",
            id="123-main-st",
            attributes=[
                ExtractedAttribute(name="address_street", value="123 Main St", datatype="string"),
                ExtractedAttribute(name="address_city", value="San Francisco", datatype="string"),
                ExtractedAttribute(name="address_state", value="CA", datatype="string"),
                ExtractedAttribute(name="address_zip", value="94105", datatype="string"),
                ExtractedAttribute(name="price", value="500000", datatype="integer"),
            ],
        )
        promotions = check_promotion(entity, {})
        assert len(promotions) == 4
        assert all(p.action == AttrAction.PROMOTE for p in promotions)
        assert all(p.promoted_type == "Address" for p in promotions)

    def test_no_promotion_below_threshold(self):
        entity = ExtractedEntity(
            type_name="Property",
            id="123-main-st",
            attributes=[
                ExtractedAttribute(name="address_street", value="123 Main St", datatype="string"),
                ExtractedAttribute(name="address_city", value="San Francisco", datatype="string"),
                ExtractedAttribute(name="price", value="500000", datatype="integer"),
            ],
        )
        promotions = check_promotion(entity, {})
        assert len(promotions) == 0

    def test_no_promotion_without_prefix(self):
        entity = ExtractedEntity(
            type_name="Property",
            id="123-main-st",
            attributes=[
                ExtractedAttribute(name="price", value="500000", datatype="integer"),
                ExtractedAttribute(name="bedrooms", value="3", datatype="integer"),
                ExtractedAttribute(name="bathrooms", value="2", datatype="integer"),
            ],
        )
        promotions = check_promotion(entity, {})
        assert len(promotions) == 0
