"""Regression: ``ExtractedAttribute.value`` coerces non-string scalars.

The LLM / Firecrawl JSON extraction legitimately returns a bare boolean or
number for a boolean- / numeric-valued attribute (e.g. ``streaming_support:
true``, ``context_window: 8192``). ``value`` is typed ``str``; before the
``_coerce_scalar`` before-validator, Pydantic v2 raised ``ValidationError`` on
those, and because the extraction handler in ``schema_resolver`` only caught
``(JSONDecodeError, KeyError, TypeError)`` — and ``ValidationError`` subclasses
``ValueError``, not ``TypeError`` — the error propagated out and failed the
WHOLE discovery job with 0 records ("4 matches produced 0 filled records").

Found by persona-eval Speko pf8 (an STT-provider discovery run whose extraction
returned boolean ``streaming_support`` / ``diarization_support``). Tests run
fully offline — no network / LLM.
"""

from __future__ import annotations

from cograph_client.resolver.models import ExtractedAttribute, ExtractedEntity


def test_bool_true_coerced_to_lowercase_string():
    # Lowercase "true"/"false" == the canonical xsd:boolean lexical form the
    # downstream validator (#166 _typed_value) expects.
    assert ExtractedAttribute(name="streaming_support", value=True).value == "true"


def test_bool_false_coerced_to_lowercase_string():
    assert ExtractedAttribute(name="streaming_support", value=False).value == "false"


def test_int_coerced_to_string():
    assert ExtractedAttribute(name="context_window", value=8192).value == "8192"


def test_float_coerced_to_string():
    assert ExtractedAttribute(name="rtf", value=0.25).value == "0.25"


def test_plain_string_passes_through_unchanged():
    assert ExtractedAttribute(name="name", value="Whisper").value == "Whisper"


def test_datatype_default_and_name_preserved():
    attr = ExtractedAttribute(name="context_window", value=8192)
    assert attr.name == "context_window"
    assert attr.datatype == "string"


def test_entity_with_scalar_attributes_constructs_and_coerces():
    """The end-to-end symptom shape: an entity whose attribute list carries
    boolean scalars constructs without raising and yields coerced strings."""
    entity = ExtractedEntity(
        type_name="SpeechToTextProvider",
        id="whisper",
        attributes=[
            {"name": "streaming_support", "value": True},
            {"name": "diarization_support", "value": False},
            {"name": "context_window", "value": 8192},
            {"name": "name", "value": "Whisper"},
        ],
    )
    by_name = {a.name: a.value for a in entity.attributes}
    assert by_name["streaming_support"] == "true"
    assert by_name["diarization_support"] == "false"
    assert by_name["context_window"] == "8192"
    assert by_name["name"] == "Whisper"
    # Every coerced value is a str (the field's declared type).
    assert all(isinstance(a.value, str) for a in entity.attributes)
