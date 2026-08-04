"""Regression tests: CSV ontology expansion must REUSE existing properties.

Oliver Genentech label-compliance DP (2026-08-04): uploading a one-row
Opdualag CSV against a curated Drug type that already had string
``manufacturer`` minted a parallel relationship ``manufacturedby`` →
Organization (plus ``hascompaniondiagnostic`` / ``sourcedfrom`` /
``studiedin`` with camelCase-collapsed leaves). Root causes covered here:

1. Schema inference discarded existing attrs (only type names went to the LLM).
2. No deterministic post-reconcile onto existing properties.
3. ``_snake_case`` / attr normalize dropped camelCase boundaries
   (``manufacturedBy`` → ``manufacturedby``).
4. Attribute fuzzy match did not strip relationship affixes
   (``manufactured_by`` failed to hit ``manufacturer`` at the 0.85 threshold).

These tests are pure / hermetic — no LLM, no Neptune.
"""

from __future__ import annotations

import pytest

from cograph_client.resolver.attribute_resolver import (
    AttributeSchema,
    _find_existing_attr,
    _normalize_attr_name,
    is_primitive_datatype,
    resolve_attribute,
)
from cograph_client.resolver.csv_resolver import (
    _snake_case,
    format_existing_ontology_for_prompt,
    reconcile_mapping_to_existing,
)
from cograph_client.resolver.models import (
    AttrAction,
    ColumnMapping,
    ColumnRole,
    CSVSchemaMapping,
    ExtractedAttribute,
    OntologyExtensions,
    TypeExtension,
)
from cograph_client.resolver.predicate_normalizer import normalize_predicate


# ---------------------------------------------------------------------------
# Fixtures mirroring the curated Oliver Drug ontology (pre-Opdualag)
# ---------------------------------------------------------------------------

def _drug_existing_attrs() -> dict[str, dict[str, AttributeSchema]]:
    return {
        "Drug": {
            "brand_name": AttributeSchema("brand_name", "string"),
            "generic_name": AttributeSchema("generic_name", "string"),
            "manufacturer": AttributeSchema("manufacturer", "string"),
            "drug_class": AttributeSchema("drug_class", "string"),
            "label_status": AttributeSchema("label_status", "string"),
            "as_of_date": AttributeSchema("as_of_date", "datetime"),
            "has_indication": AttributeSchema("has_indication", "Indication"),
        },
        "Indication": {
            "disease": AttributeSchema("disease", "string"),
            "supported_by_trial": AttributeSchema("supported_by_trial", "ClinicalTrial"),
        },
        "ClinicalTrial": {
            "nct_id": AttributeSchema("nct_id", "string"),
            "name": AttributeSchema("name", "string"),
        },
    }


def _drug_existing_types() -> dict[str, str]:
    return {
        "Drug": "",
        "Indication": "",
        "ClinicalTrial": "",
        "CompanionDiagnostic": "",
    }


# ---------------------------------------------------------------------------
# snake_case / name normalization (camelCase collapse bug)
# ---------------------------------------------------------------------------

class TestSnakeCaseCamelBoundary:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("manufactured_by", "manufactured_by"),
            ("manufacturedBy", "manufactured_by"),
            ("ManufacturedBy", "manufactured_by"),
            ("hasCompanionDiagnostic", "has_companion_diagnostic"),
            ("sourcedFrom", "sourced_from"),
            ("studiedIn", "studied_in"),
            ("drugClass", "drug_class"),
            ("drug_class", "drug_class"),
            ("XMLParser", "xml_parser"),
            ("already_snake", "already_snake"),
        ],
    )
    def test_snake_case_splits_camel(self, raw, expected):
        assert _snake_case(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("manufacturedBy", "manufactured_by"),
            ("drugClass", "drug_class"),
            ("Drug Class", "drug_class"),
            ("manufacturer", "manufacturer"),
        ],
    )
    def test_normalize_attr_name_splits_camel(self, raw, expected):
        assert _normalize_attr_name(raw) == expected


# ---------------------------------------------------------------------------
# Attribute / predicate matching (manufacturer ↔ manufactured_by)
# ---------------------------------------------------------------------------

class TestAttrSynonymMatch:
    def test_manufactured_by_reuses_manufacturer(self):
        existing = {"manufacturer": AttributeSchema("manufacturer", "string")}
        hit = _find_existing_attr("manufactured_by", existing)
        assert hit is not None
        assert hit.name == "manufacturer"

    def test_manufacturedBy_camel_reuses_manufacturer(self):
        existing = {"manufacturer": AttributeSchema("manufacturer", "string")}
        hit = _find_existing_attr("manufacturedBy", existing)
        assert hit is not None
        assert hit.name == "manufacturer"

    def test_drugclass_compact_reuses_drug_class(self):
        existing = {"drug_class": AttributeSchema("drug_class", "string")}
        hit = _find_existing_attr("drugclass", existing)
        assert hit is not None
        assert hit.name == "drug_class"

    def test_resolve_attribute_manufactured_by_reuses(self):
        existing = {"manufacturer": AttributeSchema("manufacturer", "string")}
        result = resolve_attribute(
            ExtractedAttribute(name="manufactured_by", value="BMS", datatype="string"),
            existing,
        )
        assert result.action == AttrAction.REUSE
        assert result.name == "manufacturer"

    def test_unrelated_names_do_not_collide(self):
        existing = {"manufacturer": AttributeSchema("manufacturer", "string")}
        assert _find_existing_attr("indication_summary", existing) is None
        assert _find_existing_attr("brand_name", existing) is None

    @pytest.mark.parametrize(
        "proposed,existing_name",
        [
            ("created_by", "created_at"),
            ("created_at", "created_by"),
            ("updated_by", "updated_at"),
            ("published_by", "published_at"),
            ("located_in", "located_at"),
            ("owned_by", "owned_at"),
        ],
    )
    def test_different_role_suffixes_do_not_collapse(self, proposed, existing_name):
        """Affix strip must NOT equate created_by ↔ created_at (review gate)."""
        existing = {existing_name: AttributeSchema(existing_name, "string")}
        assert _find_existing_attr(proposed, existing) is None

    def test_exact_wins_when_both_role_suffixes_present(self):
        existing = {
            "created_by": AttributeSchema("created_by", "string"),
            "created_at": AttributeSchema("created_at", "datetime"),
        }
        assert _find_existing_attr("created_by", existing).name == "created_by"
        assert _find_existing_attr("created_at", existing).name == "created_at"

    def test_has_prefix_still_reuses_bare_core(self):
        existing = {"manufacturer": AttributeSchema("manufacturer", "string")}
        hit = _find_existing_attr("has_manufacturer", existing)
        assert hit is not None
        assert hit.name == "manufacturer"


class TestPredicateNormalizeCamel:
    def test_camel_case_normalizes_before_match(self):
        existing = {"manufactured_by"}
        assert normalize_predicate("manufacturedBy", existing) == "manufactured_by"

    def test_manufactured_by_fuzzy_matches_manufacturer_predicate(self):
        # When manufacturer IS a relationship predicate, reuse it.
        existing = {"manufacturer", "has_indication"}
        assert normalize_predicate("manufactured_by", existing) == "manufacturer"


# ---------------------------------------------------------------------------
# Prompt formatting includes existing properties
# ---------------------------------------------------------------------------

class TestFormatExistingOntology:
    def test_includes_attrs_and_rels(self):
        text = format_existing_ontology_for_prompt(
            _drug_existing_types(), _drug_existing_attrs(),
        )
        assert "Drug:" in text
        assert "manufacturer:string" in text
        assert "drug_class:string" in text
        assert "has_indication→Indication" in text
        # Literals and relationships are labelled distinctly.
        assert "attrs[" in text
        assert "rels[" in text

    def test_empty_ontology(self):
        assert format_existing_ontology_for_prompt({}, None) == "(none)"

    def test_types_without_attrs_still_listed(self):
        text = format_existing_ontology_for_prompt({"Orphan": ""}, {})
        assert text == "- Orphan"

    def test_without_attrs_falls_back_to_type_names(self):
        text = format_existing_ontology_for_prompt({"Drug": "", "Trial": ""}, None)
        assert "- Drug" in text
        assert "- Trial" in text


# ---------------------------------------------------------------------------
# Deterministic reconcile (the Oliver regression itself)
# ---------------------------------------------------------------------------

class TestReconcileMappingToExisting:
    def _oliver_broken_mapping(self) -> CSVSchemaMapping:
        """What v2 inference produced on the live Opdualag upload (simplified)."""
        return CSVSchemaMapping(
            entity_type="Drug",
            columns=[
                ColumnMapping(
                    column_name="brand_name",
                    role=ColumnRole.TYPE_ID,
                    datatype="string",
                    attribute_name="brand_name",
                ),
                ColumnMapping(
                    column_name="manufacturer",
                    role=ColumnRole.RELATIONSHIP,  # wrong: should be attr
                    datatype="string",
                    attribute_name="manufactured_by",
                    target_type="Organization",
                ),
                ColumnMapping(
                    column_name="drug_class",
                    role=ColumnRole.ATTRIBUTE,
                    datatype="string",
                    attribute_name="drugClass",  # camelCase collapse risk
                ),
                ColumnMapping(
                    column_name="generic_name",
                    role=ColumnRole.ATTRIBUTE,
                    datatype="string",
                    attribute_name="generic_name",
                ),
                ColumnMapping(
                    column_name="demo_priority",
                    role=ColumnRole.ATTRIBUTE,
                    datatype="string",
                    attribute_name="demo_priority",
                ),
            ],
        )

    def test_manufactured_by_collapses_to_manufacturer_attr(self):
        mapping = reconcile_mapping_to_existing(
            self._oliver_broken_mapping(),
            _drug_existing_types(),
            _drug_existing_attrs(),
        )
        mfr = next(c for c in mapping.columns if c.column_name == "manufacturer")
        assert mfr.role == ColumnRole.ATTRIBUTE
        assert mfr.attribute_name == "manufacturer"
        assert mfr.target_type is None
        assert mfr.datatype == "string"

    def test_drug_class_keeps_existing_snake_name(self):
        mapping = reconcile_mapping_to_existing(
            self._oliver_broken_mapping(),
            _drug_existing_types(),
            _drug_existing_attrs(),
        )
        dc = next(c for c in mapping.columns if c.column_name == "drug_class")
        assert dc.role == ColumnRole.ATTRIBUTE
        assert dc.attribute_name == "drug_class"

    def test_new_attr_demo_priority_is_kept(self):
        mapping = reconcile_mapping_to_existing(
            self._oliver_broken_mapping(),
            _drug_existing_types(),
            _drug_existing_attrs(),
        )
        dp = next(c for c in mapping.columns if c.column_name == "demo_priority")
        assert dp.role == ColumnRole.ATTRIBUTE
        assert dp.attribute_name == "demo_priority"

    def test_existing_relationship_is_reused_not_literalized(self):
        """If Drug already has has_indication→Indication, a column mapped as a
        string attr with a synonym name must flip to the relationship."""
        mapping = CSVSchemaMapping(
            entity_type="Drug",
            columns=[
                ColumnMapping(
                    column_name="indication",
                    role=ColumnRole.ATTRIBUTE,
                    datatype="string",
                    attribute_name="has_indication",
                ),
            ],
        )
        out = reconcile_mapping_to_existing(
            mapping, _drug_existing_types(), _drug_existing_attrs(),
        )
        col = out.columns[0]
        assert col.role == ColumnRole.RELATIONSHIP
        assert col.attribute_name == "has_indication"
        assert col.target_type == "Indication"

    def test_greenfield_resnakes_camel_case(self):
        mapping = CSVSchemaMapping(
            entity_type="Widget",
            columns=[
                ColumnMapping(
                    column_name="madeBy",
                    role=ColumnRole.RELATIONSHIP,
                    datatype="string",
                    attribute_name="manufacturedBy",
                    target_type="Org",
                ),
            ],
        )
        out = reconcile_mapping_to_existing(mapping, {}, None)
        assert out.columns[0].attribute_name == "manufactured_by"

    def test_drops_redundant_pass_d_promotion(self):
        mapping = CSVSchemaMapping(
            entity_type="Drug",
            columns=[
                ColumnMapping(
                    column_name="manufacturer",
                    role=ColumnRole.ATTRIBUTE,
                    datatype="string",
                    attribute_name="manufacturer",
                ),
            ],
            ontology_extensions=OntologyExtensions(
                types=[
                    TypeExtension(
                        type_name="Manufacturer",
                        promoted_from_attribute="manufacturer",
                        core_slots=[],
                    ),
                ],
            ),
        )
        out = reconcile_mapping_to_existing(
            mapping, _drug_existing_types(), _drug_existing_attrs(),
        )
        assert out.ontology_extensions is not None
        assert out.ontology_extensions.types == []

    def test_multi_entity_owner_resolves_type(self):
        from cograph_client.resolver.models import EntitySpec

        mapping = CSVSchemaMapping(
            entity_type="Drug",
            entities=[
                EntitySpec(name="drug", type_name="Drug", id_column="brand_name"),
            ],
            columns=[
                ColumnMapping(
                    column_name="manufacturer",
                    role=ColumnRole.RELATIONSHIP,
                    datatype="string",
                    attribute_name="manufactured_by",
                    target_type="Organization",
                    entity="drug",
                ),
            ],
        )
        out = reconcile_mapping_to_existing(
            mapping, _drug_existing_types(), _drug_existing_attrs(),
        )
        col = out.columns[0]
        assert col.role == ColumnRole.ATTRIBUTE
        assert col.attribute_name == "manufacturer"

    def test_type_id_role_not_flipped_to_relationship(self):
        """A key column that shares a name with a type-ranged property stays TYPE_ID."""
        mapping = CSVSchemaMapping(
            entity_type="Drug",
            columns=[
                ColumnMapping(
                    column_name="has_indication",
                    role=ColumnRole.TYPE_ID,
                    datatype="string",
                    attribute_name="has_indication",
                ),
            ],
        )
        out = reconcile_mapping_to_existing(
            mapping, _drug_existing_types(), _drug_existing_attrs(),
        )
        assert out.columns[0].role == ColumnRole.TYPE_ID
        assert out.columns[0].attribute_name == "has_indication"

    def test_unknown_owner_does_not_false_reuse(self):
        from cograph_client.resolver.models import EntitySpec

        mapping = CSVSchemaMapping(
            entity_type="Drug",
            entities=[
                EntitySpec(name="drug", type_name="Drug", id_column="brand_name"),
            ],
            columns=[
                ColumnMapping(
                    column_name="manufacturer",
                    role=ColumnRole.RELATIONSHIP,
                    datatype="string",
                    attribute_name="manufactured_by",
                    target_type="Organization",
                    entity="ghost",  # not a declared entity handle
                ),
            ],
        )
        out = reconcile_mapping_to_existing(
            mapping, _drug_existing_types(), _drug_existing_attrs(),
        )
        # Unknown owner → no type props → only resnake, keep relationship.
        assert out.columns[0].role == ColumnRole.RELATIONSHIP
        assert out.columns[0].attribute_name == "manufactured_by"


class TestPrimitiveDatatypeHelper:
    def test_primitives(self):
        for dt in ("string", "integer", "float", "boolean", "datetime", "uri", "geo"):
            assert is_primitive_datatype(dt) is True

    def test_type_ranged(self):
        assert is_primitive_datatype("Indication") is False
        assert is_primitive_datatype("Organization") is False
        assert is_primitive_datatype("ClinicalTrial") is False
