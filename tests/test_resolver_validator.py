"""Tests for the schema-on-write validator."""

import re

import pytest

from cograph_client.resolver.models import RejectedValue, ValidatedTriple, ValidationOutcome
from cograph_client.resolver.validator import coerce_value, validate_triple, validate_value


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


GEO_WKT = "http://www.opengis.net/ont/geosparql#wktLiteral"


class TestGeoDatatype:
    """`geo` coerces a 'lat,lon' pair or a WKT POINT to a typed geo:wktLiteral."""

    def test_validate_value_wkt_conforms(self):
        # An already-canonical WKT POINT conforms (no coercion).
        assert validate_value("POINT(2.29 48.85)", "geo") is True

    def test_validate_value_latlon_not_conforming(self):
        # "lat,lon" is coercible, not conforming — so it gets normalized, not stored verbatim.
        assert validate_value("48.85,2.29", "geo") is False

    def test_validate_value_out_of_range(self):
        # lat 1920 is out of WGS84 range → not a coordinate.
        assert validate_value("1920,1080", "geo") is False

    def test_coerce_latlon_to_wkt(self):
        # Comma form is lat,lon; WKT is lon-then-lat.
        assert coerce_value("48.85,2.29", "geo") == "POINT(2.29 48.85)"

    def test_coerce_wkt_passthrough(self):
        assert coerce_value("POINT(2.29 48.85)", "geo") == "POINT(2.29 48.85)"

    def test_coerce_rejects_non_coord(self):
        assert coerce_value("Paris", "geo") is None
        assert coerce_value("1920,1080", "geo") is None  # out of range

    def test_triple_latlon_coerced_to_typed_wkt(self):
        result = validate_triple("s", "p", "48.85,2.29", "geo")
        assert isinstance(result, ValidatedTriple)
        assert result.outcome == ValidationOutcome.COERCED
        assert result.object == f"POINT(2.29 48.85)^^{GEO_WKT}"

    def test_triple_wkt_ok_and_typed(self):
        result = validate_triple("s", "p", "POINT(2.29 48.85)", "geo")
        assert isinstance(result, ValidatedTriple)
        assert result.outcome == ValidationOutcome.OK
        assert result.object == f"POINT(2.29 48.85)^^{GEO_WKT}"

    def test_triple_non_coord_rejected(self):
        result = validate_triple("s", "p", "somewhere", "geo")
        assert isinstance(result, RejectedValue)

    def test_precision_preserved(self):
        # No float re-formatting → exact lexical precision is kept.
        assert coerce_value("-33.8688197,151.2092955", "geo") == (
            "POINT(151.2092955 -33.8688197)"
        )


XSD_BOOLEAN = "http://www.w3.org/2001/XMLSchema#boolean"
XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"

# The complete lexical space of xsd:boolean per the XSD spec. Anything outside
# this set (e.g. Python's ``str(True)`` == ``"True"``) is NOT a boolean literal —
# a SPARQL engine treats it as an ill-typed literal that no ``= true`` / ``= false``
# comparison ever matches.
_VALID_XSD_BOOLEAN_LEXICAL = {"true", "false", "1", "0"}
# The canonical lexical forms (what a well-behaved writer should emit).
_CANONICAL_XSD_BOOLEAN_LEXICAL = {"true", "false"}


def _split_typed_literal(obj: str) -> tuple[str, str]:
    """Split ``"lexical^^datatype"`` into ``(lexical, datatype)``.

    Splits on the LAST ``^^`` so a lexical form that itself contains ``^^`` is
    handled (not relevant for booleans, but keeps the helper honest).
    """
    lexical, _, datatype = obj.rpartition("^^")
    return lexical, datatype


class TestBooleanLiteralCanonicalization:
    """Regression + mechanism tests for the boolean-typed-literal bug.

    Root cause: ``validate_value`` accepts booleans case-insensitively, so
    ``"True"`` (Python's ``str(True)``) reported as *conforming* and skipped
    coercion, then got stamped raw as ``"True"^^xsd:boolean``. But ``"True"`` is
    NOT a valid xsd:boolean lexical form, so no semantically-correct SPARQL
    boolean filter (``?o = true``) ever matched the data (verified live: 0 rows).
    The fix canonicalizes the lexical form in ``_typed_value`` before stamping
    the datatype. These tests assert the MECHANISM with invented predicates, not
    just the single ``"True"`` example.
    """

    # Every lexical form the writers can plausibly see for a truthy value, and
    # the canonical form each must be stored as.
    TRUE_FORMS = ["True", "TRUE", "true", "TrUe", "1", "yes", "YES", "on"]
    FALSE_FORMS = ["False", "FALSE", "false", "FaLsE", "0", "no", "NO", "off"]

    @pytest.mark.parametrize("raw", TRUE_FORMS)
    def test_true_forms_stored_canonically(self, raw):
        result = validate_triple("s", "onto/flag", raw, "boolean")
        assert isinstance(result, ValidatedTriple)
        lexical, datatype = _split_typed_literal(result.object)
        # Correct datatype …
        assert datatype == XSD_BOOLEAN
        # … a VALID xsd:boolean lexical form …
        assert lexical in _VALID_XSD_BOOLEAN_LEXICAL, (
            f"{raw!r} produced ill-typed boolean lexical {lexical!r}"
        )
        # … and specifically the canonical ``true``.
        assert lexical == "true"

    @pytest.mark.parametrize("raw", FALSE_FORMS)
    def test_false_forms_stored_canonically(self, raw):
        result = validate_triple("s", "onto/flag", raw, "boolean")
        assert isinstance(result, ValidatedTriple)
        lexical, datatype = _split_typed_literal(result.object)
        assert datatype == XSD_BOOLEAN
        assert lexical in _VALID_XSD_BOOLEAN_LEXICAL
        assert lexical == "false"

    def test_lexical_form_never_capitalized(self):
        # The exact bug: the stored lexical form must NEVER be the capitalized
        # Python ``str(True)`` / ``str(False)``. This assertion fails on the
        # pre-fix code (which stored ``"True"^^xsd:boolean`` verbatim).
        for raw in ("True", "TRUE", "False", "FALSE"):
            lexical, _ = _split_typed_literal(
                validate_triple("s", "onto/flag", raw, "boolean").object
            )
            assert lexical not in ("True", "False", "TRUE", "FALSE")
            assert lexical in _CANONICAL_XSD_BOOLEAN_LEXICAL

    def test_stored_form_matches_semantic_boolean_filter(self):
        # A produced literal must be one that an ``?o = true`` / ``?o = false``
        # SPARQL comparison would actually match — i.e. its lexical form is in
        # the xsd:boolean lexical space, so the engine coerces it to a real
        # xsd:boolean value rather than an ill-typed literal. We express that as:
        # the canonical lexical form equals the boolean's expected form.
        assert (
            _split_typed_literal(
                validate_triple("s", "onto/streaming", "True", "boolean").object
            )[0]
            == "true"
        )
        assert (
            _split_typed_literal(
                validate_triple("s", "onto/streaming", "False", "boolean").object
            )[0]
            == "false"
        )

    def test_all_boolean_outcomes_stay_the_same(self):
        # Canonicalizing the STORED form must not change the accept/reject or
        # OK/COERCED semantics: a plain-conforming form is still OK, a
        # normalizable-but-non-canonical form ("1") is still COERCED, and a
        # non-boolean is still rejected.
        assert validate_triple("s", "p", "true", "boolean").outcome is ValidationOutcome.OK
        assert validate_triple("s", "p", "True", "boolean").outcome is ValidationOutcome.OK
        assert validate_triple("s", "p", "1", "boolean").outcome is ValidationOutcome.COERCED
        assert isinstance(
            validate_triple("s", "p", "maybe", "boolean"), RejectedValue
        )


class TestNumericLiteralCanonicalization:
    """A numeric-declared attribute gets a valid typed literal.

    (Scoped to the SAME ``_typed_value`` path as the boolean fix. The separate
    "value stored as a plain string with no xsd:integer" symptom the RCA noted
    is a DIFFERENT root cause — the attribute was declared ``string``, not
    ``integer`` — and is intentionally out of scope for this fix.)

    Note: unlike ``boolean``/``datetime``/``geo``, a *conforming* integer/float
    is left on the RAW path — it is already a valid xsd numeric lexical form,
    compared numerically. It is deliberately NOT round-tripped through
    ``coerce_value`` (``str(int(float(value)))``), which would corrupt large
    integers and crash on very long ones — see ``TestLargeIntegerNotCorrupted``.
    """

    def test_integer_conforming_is_typed_and_canonical(self):
        lexical, datatype = _split_typed_literal(
            validate_triple("s", "onto/latency", "30", "integer").object
        )
        assert datatype == XSD_INTEGER
        assert lexical == "30"

    def test_integer_leading_zeros_is_a_valid_literal(self):
        # "030" is a valid xsd:integer lexical form that ``= 30`` matches (numeric
        # comparison), so we only require a VALID typed literal here — we do NOT
        # canonicalize integers (that path would corrupt large ints; see
        # TestLargeIntegerNotCorrupted). The value stays exactly as-declared.
        result = validate_triple("s", "onto/latency", "030", "integer")
        assert isinstance(result, ValidatedTriple)
        assert result.outcome is ValidationOutcome.OK
        lexical, datatype = _split_typed_literal(result.object)
        assert datatype == XSD_INTEGER
        # A valid xsd:integer lexical form (matches ``= 30`` numerically). We
        # assert validity, not a specific canonical spelling.
        assert re.fullmatch(r"-?\d+", lexical)
        assert int(lexical) == 30

    def test_integer_coerced_from_float_string(self):
        # This value does NOT conform (has a decimal point) → it goes through the
        # COERCED branch, which already coerced pre-fix and is unchanged.
        result = validate_triple("s", "onto/count", "42.7", "integer")
        assert result.outcome is ValidationOutcome.COERCED
        lexical, datatype = _split_typed_literal(result.object)
        assert datatype == XSD_INTEGER
        assert lexical == "42"


class TestLargeIntegerNotCorrupted:
    """Regression: conforming integers must be stored EXACTLY (unbounded xsd:integer).

    Locks in the reviewer finding on PR #166: an earlier form of the boolean fix
    routed EVERY datatype through ``coerce_value`` in ``_typed_value``, whose
    integer branch is ``str(int(float(value)))``. That round-trips through a
    64-bit float, so:
      * integers ≥ 2**53 silently CHANGE value (a different fact lands in the KG),
      * integers longer than ~309 digits overflow ``float()`` to ``inf`` and
        ``int(inf)`` raises ``OverflowError`` — aborting the write.
    Reachable on the CSV path (``_datatype_from_profile`` types large-int ID
    columns as ``integer``). The fix leaves conforming integers on the raw path.
    """

    def test_int64_max_stored_byte_for_byte(self):
        raw = "9223372036854775807"  # 2**63 - 1; not float-exact
        lexical, datatype = _split_typed_literal(
            validate_triple("s", "onto/external_id", raw, "integer").object
        )
        assert datatype == XSD_INTEGER
        assert lexical == raw  # exact, un-corrupted

    def test_twenty_digit_id_stored_byte_for_byte(self):
        raw = "12345678901234567890"  # 20 digits, > 2**53
        lexical, _ = _split_typed_literal(
            validate_triple("s", "onto/external_id", raw, "integer").object
        )
        assert lexical == raw

    def test_very_long_integer_does_not_crash(self):
        # 400-digit integer: overflows float() to inf on the coerce path. The raw
        # path must store it as a valid literal without raising.
        raw = "9" * 400
        result = validate_triple("s", "onto/external_id", raw, "integer")
        assert isinstance(result, ValidatedTriple)
        lexical, datatype = _split_typed_literal(result.object)
        assert datatype == XSD_INTEGER
        assert lexical == raw  # stored exactly, no OverflowError

    def test_booleans_still_canonicalize(self):
        # Sanity: narrowing canonicalization to boolean/datetime/geo did NOT
        # regress the actual bug this PR fixes.
        lexical, _ = _split_typed_literal(
            validate_triple("s", "onto/flag", "True", "boolean").object
        )
        assert lexical == "true"
