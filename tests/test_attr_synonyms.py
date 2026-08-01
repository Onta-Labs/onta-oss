"""Free-text attr synonym alignment — multi-domain, anti-overfit."""

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


def test_notes_maps_to_content_incident_domain():
    """Variant domain: Incident runbook, not Decision."""
    existing = {
        "content": AttributeSchema(name="content", datatype="string"),
        "severity": AttributeSchema(name="severity", datatype="string"),
    }
    hit = _find_existing_attr("notes", existing)
    assert hit is not None and hit.name == "content"


def test_domain_maps_to_category_clinic():
    existing = {
        "category": AttributeSchema(name="category", datatype="string"),
    }
    hit = _find_existing_attr("topic", existing)
    assert hit is not None and hit.name == "category"


def test_title_does_not_collapse_to_name():
    existing = {"name": AttributeSchema(name="name", datatype="string")}
    assert _find_existing_attr("title", existing) is None


def test_ambiguous_synonyms_do_not_collapse():
    existing = {
        "description": AttributeSchema(name="description", datatype="string"),
        "summary": AttributeSchema(name="summary", datatype="string"),
    }
    assert _find_existing_attr("statement", existing) is None
