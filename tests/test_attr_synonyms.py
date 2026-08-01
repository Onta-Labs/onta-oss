"""Free-text attr synonym alignment (dogfood S1)."""

from cograph_client.resolver.attribute_resolver import (
    AttributeSchema,
    _find_existing_attr,
)


def test_statement_maps_to_description():
    existing = {
        "description": AttributeSchema(name="description", datatype="string"),
        "area": AttributeSchema(name="area", datatype="string"),
    }
    hit = _find_existing_attr("statement", existing)
    assert hit is not None and hit.name == "description"


def test_domain_maps_to_area():
    existing = {
        "area": AttributeSchema(name="area", datatype="string"),
    }
    hit = _find_existing_attr("domain", existing)
    assert hit is not None and hit.name == "area"


def test_ambiguous_synonyms_do_not_collapse():
    existing = {
        "description": AttributeSchema(name="description", datatype="string"),
        "summary": AttributeSchema(name="summary", datatype="string"),
    }
    assert _find_existing_attr("statement", existing) is None
