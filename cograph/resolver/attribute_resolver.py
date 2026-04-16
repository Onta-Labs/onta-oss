"""Attribute resolution — matches proposed attributes against existing schema.

Rules:
1. Attribute exists, same datatype → REUSE
2. Attribute exists, different datatype → COERCE the value, keep ontology
3. New attribute → EXTEND the type
4. Never remove, rename, or change attribute datatypes
5. Option D: when structured data arrives for a flat field → PROMOTE (coexist)
"""

from __future__ import annotations

from difflib import SequenceMatcher

import structlog

from cograph.resolver.models import (
    AttrAction,
    ExtractedAttribute,
    ExtractedEntity,
    ResolvedAttribute,
)
from cograph.resolver.validator import coerce_value

logger = structlog.stdlib.get_logger("cograph.resolver.attribute")

_ATTR_SIMILARITY_THRESHOLD = 0.85
_STRIP_ATTR_PREFIXES = (
    "listing_", "property_", "total_", "current_", "primary_",
    "default_", "original_", "actual_", "base_",
)


class AttributeSchema:
    """Snapshot of an existing attribute in the ontology."""

    __slots__ = ("name", "datatype", "description")

    def __init__(self, name: str, datatype: str = "string", description: str = ""):
        self.name = name
        self.datatype = datatype
        self.description = description


def _normalize_attr_name(name: str) -> str:
    """Normalize attribute names for comparison."""
    return name.lower().strip().replace(" ", "_").replace("-", "_")


def _strip_attr_prefixes(name: str) -> str:
    """Strip common domain prefixes for fuzzy comparison."""
    for prefix in _STRIP_ATTR_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix):]
    return name


def _find_existing_attr(
    attr_name: str,
    existing_attrs: dict[str, AttributeSchema],
) -> AttributeSchema | None:
    """Find an existing attribute by normalized name, with fuzzy fallback."""
    normalized = _normalize_attr_name(attr_name)

    # 1. Exact normalized match
    for name, schema in existing_attrs.items():
        if _normalize_attr_name(name) == normalized:
            return schema

    if not existing_attrs:
        return None

    # 2. Fuzzy match with prefix stripping
    stripped = _strip_attr_prefixes(normalized)
    best_match: AttributeSchema | None = None
    best_ratio = 0.0
    for name, schema in existing_attrs.items():
        existing_stripped = _strip_attr_prefixes(_normalize_attr_name(name))
        ratio = SequenceMatcher(None, stripped, existing_stripped).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = schema

    if best_ratio >= _ATTR_SIMILARITY_THRESHOLD and best_match is not None:
        logger.info(
            "attr_fuzzy_match",
            proposed=attr_name,
            matched=best_match.name,
            ratio=round(best_ratio, 3),
        )
        return best_match

    return None


def resolve_attribute(
    attr: ExtractedAttribute,
    existing_attrs: dict[str, AttributeSchema],
) -> ResolvedAttribute:
    """Resolve a single attribute against the existing schema.

    Args:
        attr: The proposed attribute from LLM extraction.
        existing_attrs: Map of existing attribute name → schema.

    Returns:
        ResolvedAttribute with the resolution action.
    """
    existing = _find_existing_attr(attr.name, existing_attrs)

    if existing is None:
        # New attribute → extend the type
        return ResolvedAttribute(
            name=_normalize_attr_name(attr.name),
            value=attr.value,
            datatype=attr.datatype,
            action=AttrAction.EXTEND,
        )

    if existing.datatype == attr.datatype:
        # Same datatype → reuse
        return ResolvedAttribute(
            name=existing.name,
            value=attr.value,
            datatype=existing.datatype,
            action=AttrAction.REUSE,
        )

    # Different datatype → try to coerce the value to the existing datatype
    coerced = coerce_value(attr.value, existing.datatype)
    if coerced is not None:
        return ResolvedAttribute(
            name=existing.name,
            value=coerced,
            datatype=existing.datatype,
            action=AttrAction.COERCE,
            original_value=attr.value,
        )

    # Cannot coerce — still reuse the attribute name but log the type mismatch
    logger.warning(
        "attr_type_mismatch",
        attr=attr.name,
        expected=existing.datatype,
        got=attr.datatype,
        value=attr.value,
    )
    return ResolvedAttribute(
        name=existing.name,
        value=attr.value,
        datatype=existing.datatype,
        action=AttrAction.COERCE,
        original_value=attr.value,
    )


def check_promotion(
    entity: ExtractedEntity,
    existing_attrs: dict[str, AttributeSchema],
) -> list[ResolvedAttribute]:
    """Check if any attributes should be promoted to entities (Option D).

    The three tests for promotion:
    1. Identity: Does the sub-concept have a name? Can you point at it?
    2. Reuse: Would multiple entities reference the same instance?
    3. Cluster: Do 3+ attributes describe the same sub-concept?

    For Phase 1, we detect the cluster test heuristically: if a group of
    attributes share a common prefix (e.g., address_street, address_city,
    address_zip), they form a cluster that should be promoted.
    """
    # Group attributes by prefix
    prefix_groups: dict[str, list[ExtractedAttribute]] = {}
    for attr in entity.attributes:
        normalized = _normalize_attr_name(attr.name)
        if "_" in normalized:
            prefix = normalized.split("_")[0]
            prefix_groups.setdefault(prefix, []).append(attr)

    promotions: list[ResolvedAttribute] = []
    for prefix, attrs in prefix_groups.items():
        if len(attrs) >= 3:
            # Cluster test passes — this group should be an entity
            promoted_type = prefix.title()
            logger.info(
                "attr_promotion_detected",
                entity=entity.type_name,
                prefix=prefix,
                attr_count=len(attrs),
                promoted_type=promoted_type,
            )
            for attr in attrs:
                # Strip the prefix from the attribute name for the promoted entity
                short_name = _normalize_attr_name(attr.name)
                if short_name.startswith(prefix + "_"):
                    short_name = short_name[len(prefix) + 1:]

                promotions.append(ResolvedAttribute(
                    name=short_name,
                    value=attr.value,
                    datatype=attr.datatype,
                    action=AttrAction.PROMOTE,
                    promoted_type=promoted_type,
                ))

    return promotions
