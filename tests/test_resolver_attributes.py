"""Tests for attribute resolution + ONTA-383 promotion gate / junk-type guard."""

import pytest

from cograph_client.resolver.attribute_resolver import (
    AttributeSchema,
    check_promotion,
    is_junk_type_name,
    resolve_attribute,
)
from cograph_client.resolver.models import AttrAction, ExtractedAttribute, ExtractedEntity


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


class TestJunkTypeName:
    """ONTA-383 junk-type guard — property/quality/state classes are not entities."""

    @pytest.mark.parametrize(
        "name",
        [
            "Colour",
            "Color",
            "Online",
            "Offline",
            "InstructionMode",
            "instruction_mode",
            "Mode",
            "Status",
            "ColorStatus",
            "OnlineFlag",
            "",
            "   ",
        ],
    )
    def test_junk_names_rejected(self, name):
        assert is_junk_type_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "Address",
            "Institution",
            "University",
            "College",
            "Asset",  # legitimate real-estate ancestor; not a property-class token
            "City",
            "State",
            "Physician",
            "Organization",
            "PublicInstitution",
        ],
    )
    def test_entity_names_allowed(self, name):
        assert is_junk_type_name(name) is False


class TestCheckPromotion:
    def test_cluster_detection_with_identity(self):
        """Well-evidenced Address cluster (identity leaf = street) still promotes."""
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

    def test_no_promotion_without_identity(self):
        """Cluster of 3+ without an identity leaf is STAGED (held flat), not minted.

        Uses a non-junk prefix (metrics_*) so only the identity gate fires —
        junk rejection is covered separately.
        """
        entity = ExtractedEntity(
            type_name="Product",
            id="sku-1",
            attributes=[
                ExtractedAttribute(name="metrics_score", value="12", datatype="integer"),
                ExtractedAttribute(name="metrics_count", value="34", datatype="integer"),
                ExtractedAttribute(name="metrics_rate", value="0.5", datatype="float"),
            ],
        )
        promotions = check_promotion(entity, {})
        assert promotions == []

    def test_junk_type_never_promoted(self):
        """Colour / Online / InstructionMode clusters never mint types (ONTA-383)."""
        for prefix, leaves in (
            ("colour", ("primary", "secondary", "accent", "hex")),
            ("online", ("status", "available", "since")),
            ("instruction", ("mode_primary", "mode_secondary", "mode_fallback")),
        ):
            attrs = [
                ExtractedAttribute(
                    name=f"{prefix}_{leaf}", value=str(i), datatype="string",
                )
                for i, leaf in enumerate(leaves)
            ]
            # Plant an identity leaf so only the junk guard (not identity) rejects.
            attrs.append(
                ExtractedAttribute(
                    name=f"{prefix}_name", value="x", datatype="string",
                )
            )
            entity = ExtractedEntity(type_name="Record", id="r1", attributes=attrs)
            promotions = check_promotion(entity, {})
            assert promotions == [], f"expected no promotion for prefix={prefix}"

    def test_existing_type_promotes_without_identity(self):
        """Reuse test: cluster into an ALREADY-EXISTING type needs no identity leaf."""
        entity = ExtractedEntity(
            type_name="Property",
            id="p1",
            attributes=[
                ExtractedAttribute(name="address_city", value="SF", datatype="string"),
                ExtractedAttribute(name="address_state", value="CA", datatype="string"),
                ExtractedAttribute(name="address_zip", value="94105", datatype="string"),
            ],
        )
        promotions = check_promotion(
            entity, {}, existing_types={"Address": "a postal address"},
        )
        assert len(promotions) == 3
        assert all(p.promoted_type == "Address" for p in promotions)

    def test_auto_promote_new_false_stages_new_types(self):
        """Staging switch: auto_promote_new=False holds even well-evidenced NEW types."""
        entity = ExtractedEntity(
            type_name="Property",
            id="123-main-st",
            attributes=[
                ExtractedAttribute(name="address_street", value="123 Main St", datatype="string"),
                ExtractedAttribute(name="address_city", value="SF", datatype="string"),
                ExtractedAttribute(name="address_state", value="CA", datatype="string"),
            ],
        )
        promotions = check_promotion(entity, {}, auto_promote_new=False)
        assert promotions == []
