"""Tests for the schema-on-write validator."""

import pytest

from cograph.resolver.models import RejectedValue, ValidatedTriple, ValidationOutcome
from cograph.resolver.validator import coerce_value, validate_triple, validate_value


class TestCoerceValue:
    def test_string_passthrough(self):
        assert coerce_value("hello", "string") == "hello"

    def test_integer_from_string(self):
        assert coerce_value("42", "integer") == "42"

    def test_integer_from_float_string(self):
        assert coerce_value("42.7", "integer") == "42"

    def test_float_from_string(self):
        assert coerce_value("3.14", "float") == "3.14"

    def test_boolean_true_variants(self):
        for val in ["true", "1", "yes", "on", "True", "YES"]:
            assert coerce_value(val, "boolean") == "true"

    def test_boolean_false_variants(self):
        for val in ["false", "0", "no", "off", "False", "NO"]:
            assert coerce_value(val, "boolean") == "false"

    def test_boolean_invalid(self):
        assert coerce_value("maybe", "boolean") is None

    def test_datetime_iso(self):
        result = coerce_value("2026-04-04", "datetime")
        assert result is not None
        assert "2026-04-04" in result

    def test_datetime_us_format(self):
        result = coerce_value("04/04/2026", "datetime")
        assert result is not None

    def test_datetime_invalid(self):
        assert coerce_value("not-a-date", "datetime") is None

    def test_uri_valid(self):
        assert coerce_value("https://example.com", "uri") == "https://example.com"

    def test_uri_invalid(self):
        assert coerce_value("not-a-uri", "uri") is None

    def test_integer_non_numeric(self):
        assert coerce_value("abc", "integer") is None


class TestValidateValue:
    def test_string_always_valid(self):
        assert validate_value("anything", "string") is True

    def test_integer_valid(self):
        assert validate_value("42", "integer") is True
        assert validate_value("-7", "integer") is True

    def test_integer_invalid(self):
        assert validate_value("42.5", "integer") is False
        assert validate_value("abc", "integer") is False

    def test_float_valid(self):
        assert validate_value("3.14", "float") is True
        assert validate_value("42", "float") is True

    def test_boolean_valid(self):
        assert validate_value("true", "boolean") is True
        assert validate_value("false", "boolean") is True

    def test_boolean_invalid(self):
        assert validate_value("yes", "boolean") is False


class TestValidateTriple:
    def test_valid_triple(self):
        result = validate_triple(
            "s", "p", "42", "integer", entity_id="e1", attribute_name="count",
        )
        assert isinstance(result, ValidatedTriple)
        assert result.outcome == ValidationOutcome.OK

    def test_coerced_triple(self):
        result = validate_triple(
            "s", "p", "42.7", "integer", entity_id="e1", attribute_name="count",
        )
        assert isinstance(result, ValidatedTriple)
        assert result.outcome == ValidationOutcome.COERCED
        assert result.object == "42^^http://www.w3.org/2001/XMLSchema#integer"
        assert result.original_value == "42.7"

    def test_rejected_triple(self):
        result = validate_triple(
            "s", "p", "not-a-number", "integer", entity_id="e1", attribute_name="count",
        )
        assert isinstance(result, RejectedValue)
        assert result.expected_datatype == "integer"
