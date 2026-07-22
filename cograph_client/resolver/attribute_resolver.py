"""Attribute resolution — matches proposed attributes against existing schema.

Rules:
1. Attribute exists, same datatype → REUSE
2. Attribute exists, different datatype → COERCE the value, keep ontology
3. New attribute → EXTEND the type
4. Never remove, rename, or change attribute datatypes
5. Option D: when structured data arrives for a flat field → PROMOTE (coexist)

ONTA-383 gates Option D auto-promotion: a prefix cluster alone is NOT enough.
Promotion requires evidence (identity + cluster), rejects property-class junk
types (Colour / Online / InstructionMode), and stages weak clusters as flat
attributes rather than minting fabricated types.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

import structlog

from cograph_client.resolver.models import (
    AttrAction,
    ExtractedAttribute,
    ExtractedEntity,
    ResolvedAttribute,
)
from cograph_client.resolver.validator import coerce_value

logger = structlog.stdlib.get_logger("cograph.resolver.attribute")

_ATTR_SIMILARITY_THRESHOLD = 0.85
_STRIP_ATTR_PREFIXES = (
    "listing_", "property_", "total_", "current_", "primary_",
    "default_", "original_", "actual_", "base_",
)

# Minimum attributes sharing a prefix to pass the cluster test.
_PROMOTION_CLUSTER_MIN = 3

# Identity leaves (the short name after the shared prefix is stripped) that
# satisfy the "can you point at this sub-concept?" test. Domain-free: these are
# structural identity markers, not domain nouns.
_IDENTITY_LEAVES = frozenset(
    {
        "name",
        "id",
        "label",
        "title",
        "street",
        "code",
        "number",
        "key",
        "identifier",
        "uri",
        "url",
        "address",
        "value",
        "line1",
        "line_1",
    }
)

# Property / quality / state class tokens (domain-free). A type whose whole name
# tokenizes into ONLY these, or that ends with a property-class suffix, is a
# non-entity class and must never be minted from attribute promotion (or cold-
# start relationship targets). Colour / Online / InstructionMode are the
# canonical failures this catches; Asset is intentionally NOT listed — it is a
# legitimate ancestor in real-estate lineages (Condo < Property < Asset) and is
# blocked for promotion only by the evidence gate (weak asset_* clusters).
_PROPERTY_CLASS_TOKENS = frozenset(
    {
        "colour",
        "color",
        "online",
        "offline",
        "mode",
        "status",
        "format",
        "style",
        "size",
        "kind",
        "type",
        "flag",
        "option",
        "instruction",
        "level",
        "rank",
        "grade",
        # Note: "state" is intentionally NOT listed — geo State is a real entity
        # type; OnlineState / ColorState are caught by the compound-suffix rule.
    }
)

# Suffixes that mark a compound as a property-class (InstructionMode, ColorStatus).
_PROPERTY_CLASS_SUFFIXES = (
    "mode",
    "status",
    "format",
    "style",
    "kind",
    "type",
    "flag",
    "option",
    "level",
    "rank",
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


def _split_type_tokens(name: str) -> set[str]:
    """Lowercased word tokens of a type name, splitting camelCase and separators."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
    tokens: set[str] = set()
    for part in re.split(r"[^A-Za-z0-9]+", spaced):
        if not part:
            continue
        low = part.lower()
        tokens.add(low)
        if low.endswith("s") and len(low) > 1:
            tokens.add(low[:-1])
    return tokens


def _raw_type_tokens(name: str) -> set[str]:
    """Lowercased word tokens WITHOUT de-pluralization (exact surface tokens)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
    return {
        part.lower()
        for part in re.split(r"[^A-Za-z0-9]+", spaced)
        if part
    }


def is_junk_type_name(name: str) -> bool:
    """True when ``name`` reads as a property/quality/state class, not an entity type.

    Domain-free structural heuristic (ONTA-383 junk-type guard). Used by
    attribute→type auto-promotion and cold-start relationship-target minting so
    Colour / Online / InstructionMode never become ontology types. Legitimate
    entity types (Address, Institution, University, Asset, City, State) pass.

    Rules (any match ⇒ junk):
      * empty / whitespace-only name
      * whole-name tokens ⊆ property-class vocabulary (Colour, Online, Mode, Status)
      * compound whose non-suffix tokens are all property-class
        (InstructionMode, ColorStatus, OnlineFlag)
    """
    if not name or not str(name).strip():
        return True
    raw = str(name).strip()
    # Use exact surface tokens (no de-pluralization) so "Status" stays {status}
    # rather than gaining a phantom "statu" token that escapes the property set.
    tokens = _raw_type_tokens(raw)
    if not tokens:
        return True
    # Whole name is property-class tokens only (Colour, Online, Mode, Status).
    if tokens <= _PROPERTY_CLASS_TOKENS:
        return True
    # Also accept when every token is a property class after light de-pluralization
    # ("Statuses" → status).
    normalized = {
        (t[:-1] if t.endswith("s") and len(t) > 1 and t[:-1] in _PROPERTY_CLASS_TOKENS else t)
        for t in tokens
    }
    if normalized <= _PROPERTY_CLASS_TOKENS:
        return True
    # Compound ending in a property-class suffix whose remaining tokens are
    # themselves property-class (InstructionMode → {instruction} ⊆ property set).
    compact = re.sub(r"[^a-z0-9]", "", raw.lower())
    for suf in _PROPERTY_CLASS_SUFFIXES:
        if not (compact.endswith(suf) and len(compact) > len(suf)):
            continue
        suffix_tokens = {suf}
        if suf.endswith("s") and len(suf) > 1:
            suffix_tokens.add(suf[:-1])
        non_suffix = tokens - suffix_tokens
        if non_suffix and non_suffix <= _PROPERTY_CLASS_TOKENS:
            return True
    return False




def _cluster_has_identity(prefix: str, attrs: list[ExtractedAttribute]) -> bool:
    """True when the cluster has an identity leaf (name/id/street/…) — test 1."""
    for attr in attrs:
        short = _normalize_attr_name(attr.name)
        if short.startswith(prefix + "_"):
            short = short[len(prefix) + 1 :]
        # Multi-segment leaf: take the last segment (address_line_1 → line_1 already
        # handled as full short; also check final token).
        leaf = short
        last = short.rsplit("_", 1)[-1]
        if leaf in _IDENTITY_LEAVES or last in _IDENTITY_LEAVES:
            return True
        # Bare prefix used as the name itself is rare; a value that is empty fails.
    return False


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
    *,
    existing_types: dict[str, str] | None = None,
    auto_promote_new: bool = True,
) -> list[ResolvedAttribute]:
    """Check if any attributes should be promoted to entities (Option D).

    The three tests for promotion (ALL required for a NEW type — ONTA-383):
    1. Identity: Does the sub-concept have a name / id / street? Can you point at it?
    2. Reuse: Would multiple entities reference the same instance?
       (Approximated: an existing type of that name is already reusable; for new
       types the identity leaf stands in as the reuse key.)
    3. Cluster: Do 3+ attributes describe the same sub-concept?

    Gate (ONTA-383):
      * Cluster alone is NOT enough — fabricated clusters (colour_r/g/b,
        online_*/asset_* without identity) stay flat attributes.
      * Junk / property-class type names (Colour, Online, InstructionMode) are
        never promoted, even with a cluster.
      * NEW types: require identity + cluster + non-junk. Weak evidence is
        *staged* (held as flat attrs; logged ``attr_promotion_held``) rather than
        auto-minted — confirmation/escape hatch is ``promote_to_node`` or an
        already-existing target type.
      * EXISTING types: cluster is enough to promote into a type that already
        lives in the ontology (reuse test passes structurally).

    ``auto_promote_new=False`` forces the staged path for every NEW type
    (cluster may still promote into existing types). Default ``True`` keeps
    well-evidenced Address-style promotions working.
    """
    existing_types = existing_types or {}

    # Group attributes by prefix
    prefix_groups: dict[str, list[ExtractedAttribute]] = {}
    for attr in entity.attributes:
        normalized = _normalize_attr_name(attr.name)
        if "_" in normalized:
            prefix = normalized.split("_")[0]
            prefix_groups.setdefault(prefix, []).append(attr)

    promotions: list[ResolvedAttribute] = []
    for prefix, attrs in prefix_groups.items():
        if len(attrs) < _PROMOTION_CLUSTER_MIN:
            continue

        # CamelCase the prefix: "address" → "Address", "instruction" → "Instruction"
        promoted_type = prefix[:1].upper() + prefix[1:] if prefix else prefix

        # --- Junk-type guard -------------------------------------------------
        if is_junk_type_name(promoted_type):
            logger.info(
                "attr_promotion_rejected_junk_type",
                entity=entity.type_name,
                prefix=prefix,
                attr_count=len(attrs),
                promoted_type=promoted_type,
            )
            continue

        type_already_exists = any(
            t.lower() == promoted_type.lower() for t in existing_types
        )
        has_identity = _cluster_has_identity(prefix, attrs)

        # --- Evidence gate / staging ----------------------------------------
        # NEW type: require identity (and auto_promote_new). Without identity the
        # cluster is held as flat attributes (staged — not auto-minted).
        if not type_already_exists:
            if not auto_promote_new or not has_identity:
                logger.info(
                    "attr_promotion_held",
                    entity=entity.type_name,
                    prefix=prefix,
                    attr_count=len(attrs),
                    promoted_type=promoted_type,
                    has_identity=has_identity,
                    auto_promote_new=auto_promote_new,
                    reason=(
                        "auto_promote_new_disabled"
                        if not auto_promote_new
                        else "missing_identity"
                    ),
                )
                continue

        # Resolve to the canonical casing of an existing type when present.
        if type_already_exists:
            for t in existing_types:
                if t.lower() == promoted_type.lower():
                    promoted_type = t
                    break

        logger.info(
            "attr_promotion_detected",
            entity=entity.type_name,
            prefix=prefix,
            attr_count=len(attrs),
            promoted_type=promoted_type,
            has_identity=has_identity,
            type_already_exists=type_already_exists,
        )
        for attr in attrs:
            # Strip the prefix from the attribute name for the promoted entity
            short_name = _normalize_attr_name(attr.name)
            if short_name.startswith(prefix + "_"):
                short_name = short_name[len(prefix) + 1 :]

            promotions.append(
                ResolvedAttribute(
                    name=short_name,
                    value=attr.value,
                    datatype=attr.datatype,
                    action=AttrAction.PROMOTE,
                    promoted_type=promoted_type,
                )
            )

    return promotions
