"""Schema-on-write validation — every value checked before Neptune insertion.

Conforms → insert. Coercible → coerce + insert. Invalid → reject + log.
"""

from __future__ import annotations

import re
from datetime import datetime

import structlog

from cograph_client.resolver.models import RejectedValue, ValidatedTriple, ValidationOutcome

logger = structlog.stdlib.get_logger("cograph.resolver.validator")


def coerce_value(value: str, target_datatype: str) -> str | None:
    """Try to coerce a value to the target datatype.

    Returns the coerced string representation, or None if not possible.
    """
    try:
        match target_datatype:
            case "string":
                return str(value)
            case "integer":
                return str(int(float(value)))
            case "float":
                return str(float(value))
            case "boolean":
                lower = value.lower().strip()
                if lower in ("true", "1", "yes", "on"):
                    return "true"
                if lower in ("false", "0", "no", "off"):
                    return "false"
                return None
            case "datetime":
                return _parse_datetime(value)
            case "uri":
                if value.startswith("http://") or value.startswith("https://"):
                    return value
                return None
            case _:
                return str(value)
    except (ValueError, TypeError):
        return None


def _parse_datetime(value: str) -> str | None:
    """Try common datetime formats, return ISO-8601 or None."""
    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y-%m",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%m/%d/%Y %H:%M:%S",
        "%d/%m/%Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(value.strip(), fmt)
            return dt.isoformat()
        except ValueError:
            continue

    # Try ISO parse as last resort
    try:
        dt = datetime.fromisoformat(value.strip())
        return dt.isoformat()
    except ValueError:
        return None


def validate_value(value: str, datatype: str) -> bool:
    """Check if a value conforms to the expected datatype without coercion."""
    match datatype:
        case "string":
            return True
        case "integer":
            return bool(re.match(r"^-?\d+$", value.strip()))
        case "float":
            return bool(re.match(r"^-?\d+(\.\d+)?$", value.strip()))
        case "boolean":
            return value.lower().strip() in ("true", "false")
        case "datetime":
            return _parse_datetime(value) is not None
        case "uri":
            return value.startswith("http://") or value.startswith("https://")
        case _:
            return True


XSD = "http://www.w3.org/2001/XMLSchema"

_DATATYPE_TO_XSD = {
    "integer": f"{XSD}#integer",
    "float": f"{XSD}#float",
    "boolean": f"{XSD}#boolean",
    "datetime": f"{XSD}#dateTime",
}


def _typed_value(value: str, datatype: str) -> str:
    """Append XSD type annotation for non-string datatypes.

    Returns "500000^^http://www.w3.org/2001/XMLSchema#integer" for integers, etc.
    Plain strings return as-is (no annotation needed).
    Datetime values are normalized to full ISO-8601 with time component so that
    Neptune xsd:dateTime comparisons work correctly.
    """
    xsd = _DATATYPE_TO_XSD.get(datatype)
    if xsd:
        if datatype == "datetime":
            # Normalize to full ISO-8601 so Neptune dateTime comparisons work
            normalized = _parse_datetime(value)
            if normalized:
                value = normalized
        return f"{value}^^{xsd}"
    return value


def validate_triple(
    subject: str,
    predicate: str,
    value: str,
    expected_datatype: str,
    entity_id: str = "",
    attribute_name: str = "",
) -> ValidatedTriple | RejectedValue:
    """Validate a single triple value against the expected datatype.

    Returns ValidatedTriple (OK or COERCED) or RejectedValue.
    Values are annotated with XSD types for non-string datatypes.
    """
    # Check if value conforms as-is
    if validate_value(value, expected_datatype):
        return ValidatedTriple(
            subject=subject,
            predicate=predicate,
            object=_typed_value(value, expected_datatype),
            outcome=ValidationOutcome.OK,
        )

    # Try coercion
    coerced = coerce_value(value, expected_datatype)
    if coerced is not None:
        logger.info(
            "value_coerced",
            entity=entity_id,
            attr=attribute_name,
            original=value,
            coerced=coerced,
            datatype=expected_datatype,
        )
        return ValidatedTriple(
            subject=subject,
            predicate=predicate,
            object=_typed_value(coerced, expected_datatype),
            outcome=ValidationOutcome.COERCED,
            original_value=value,
        )

    # Reject
    logger.warning(
        "value_rejected",
        entity=entity_id,
        attr=attribute_name,
        value=value,
        expected=expected_datatype,
    )
    return RejectedValue(
        entity_id=entity_id,
        attribute=attribute_name,
        value=value,
        expected_datatype=expected_datatype,
        reason=f"Cannot coerce '{value}' to {expected_datatype}",
    )
