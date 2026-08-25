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

from infona_client.resolver.models import (
    AttrAction,
    ExtractedAttribute,
    ExtractedEntity,
    ResolvedAttribute,
)
from infona_client.resolver.validator import coerce_value

logger = structlog.stdlib.get_logger("infona.resolver.attribute")

_ATTR_SIMILARITY_THRESHOLD = 0.85
_STRIP_ATTR_PREFIXES = (
    "listing_", "property_", "total_", "current_", "primary_",
    "default_", "original_", "actual_", "base_",
)
# Relationship/role affixes that do not change the underlying property concept
# (manufacturer ≈ manufactured_by; source ≈ sourced_from). Shared spirit with
# predicate_normalizer — used so CSV expansion cannot invent a parallel
# relationship for a fact that already exists as a literal attribute (or vice
# versa).
_STRIP_ROLE_PREFIXES = ("is_", "has_", "was_", "does_", "can_", "get_")
_STRIP_ROLE_SUFFIXES = ("_of", "_by", "_for", "_to", "_from", "_in", "_at")

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
    """Normalize attribute names for comparison.

    Handles spaces/hyphens, camelCase boundaries (``manufacturedBy`` →
    ``manufactured_by``, ``drugClass`` → ``drug_class``), and collapses
    repeated underscores. Underscore-free equality is handled separately in
    :func:`_find_existing_attr` via compact form.

    Reserved Entity property keys (model B2: ``name``, ``label``, …) are
    rewritten via :func:`coerce_ontology_attr_leaf` so schema inference cannot
    mint ontology attrs that fail closed at Neo4j commit.
    """
    from infona_client.graph.facts import coerce_ontology_attr_leaf

    s = (name or "").strip()
    # camelCase / PascalCase → snake_case before lowercasing, otherwise
    # ``ManufacturedBy`` collapses to the opaque ``manufacturedby``.
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", s)
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_").lower()
    return coerce_ontology_attr_leaf(s)


def _strip_attr_prefixes(name: str) -> str:
    """Strip common domain prefixes for fuzzy comparison."""
    for prefix in _STRIP_ATTR_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix):]
    return name


def _strip_role_affixes(name: str) -> str:
    """Strip relationship-style role affixes for synonym matching.

    ``manufactured_by`` → ``manufactured``, ``has_companion_diagnostic`` →
    ``companion_diagnostic``. One prefix and one suffix at most (same contract
    as :mod:`predicate_normalizer`).
    """
    stripped = name
    for prefix in _STRIP_ROLE_PREFIXES:
        if stripped.startswith(prefix) and len(stripped) > len(prefix):
            stripped = stripped[len(prefix):]
            break
    for suffix in _STRIP_ROLE_SUFFIXES:
        if stripped.endswith(suffix) and len(stripped) > len(suffix):
            stripped = stripped[: -len(suffix)]
            break
    return stripped


def _compact_attr_name(name: str) -> str:
    """Underscore-free form so ``drug_class`` equals ``drugclass``."""
    return name.replace("_", "")


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


# Free-text synonym families (dogfood S1): successive free-text ingests invent
# statement / summary / description for the same slot. Prefer the EXISTING
# attribute when the proposed name shares a family and exactly one family
# member is already on the type (unambiguous).
_ATTR_SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
    # Tight free-text prose family (dogfood S1). Deliberately exclude title/name
    # and decision/outcome — those are often distinct slots.
    frozenset({
        "description", "summary", "statement", "rationale", "reason",
        "text", "body", "content", "notes", "note", "details", "detail",
    }),
    frozenset({"area", "domain", "category", "topic", "scope"}),
    frozenset({"date", "dated", "timestamp", "day"}),
    frozenset({"email", "e_mail", "mail"}),
    frozenset({"phone", "telephone", "mobile", "tel"}),
)


def _synonym_group(name: str) -> frozenset[str] | None:
    n = _normalize_attr_name(name)
    for g in _ATTR_SYNONYM_GROUPS:
        if n in g:
            return g
    return None


def _find_existing_attr(
    attr_name: str,
    existing_attrs: dict[str, AttributeSchema],
) -> AttributeSchema | None:
    """Find an existing attribute by normalized name, synonym, or fuzzy fallback.

    Match ladder (first hit wins for exact/compact/synonym; fuzzy takes best ≥ threshold):
      1. Exact snake_case equality (``manufacturer`` ↔ ``manufacturer``)
      2. Compact equality (``drug_class`` ↔ ``drugclass``)
      3. Synonym family — only when exactly one existing attr is in the family
      4. Fuzzy over domain-prefix + role-affix stripped forms
         (``manufactured_by`` ↔ ``manufacturer`` at ≥ 0.85)
    """
    normalized = _normalize_attr_name(attr_name)
    if not normalized:
        return None

    # 1. Exact normalized match
    for name, schema in existing_attrs.items():
        if _normalize_attr_name(name) == normalized:
            return schema

    if not existing_attrs:
        return None

    # 2. Compact (underscore-insensitive) exact match
    compact = _compact_attr_name(normalized)
    for name, schema in existing_attrs.items():
        if _compact_attr_name(_normalize_attr_name(name)) == compact:
            logger.info(
                "attr_compact_match",
                proposed=attr_name,
                matched=schema.name,
            )
            return schema

    # 3. Synonym family — only when exactly one existing attr is in the family
    # (avoids collapsing distinct slots like "summary" + "rationale" both present).
    group = _synonym_group(normalized)
    if group is not None:
        hits = [
            schema
            for name, schema in existing_attrs.items()
            if _normalize_attr_name(name) in group
        ]
        if len(hits) == 1:
            logger.info(
                "attr_synonym_match",
                proposed=attr_name,
                matched=hits[0].name,
            )
            return hits[0]

    # 4. Fuzzy match with domain-prefix + role-affix stripping.
    # Guard: never collapse two names that only differ by *different* role
    # suffixes (created_by ↔ created_at both strip to "created" at ratio 1.0).
    # manufacturer ↔ manufactured_by still matches: cores differ after strip
    # (manufacturer vs manufactured) at ≥ 0.85.
    stripped = _strip_role_affixes(_strip_attr_prefixes(normalized))
    best_match: AttributeSchema | None = None
    best_ratio = 0.0
    for name, schema in existing_attrs.items():
        existing_norm = _normalize_attr_name(name)
        existing_stripped = _strip_role_affixes(
            _strip_attr_prefixes(existing_norm)
        )
        if not _affix_fuzzy_pair_allowed(normalized, stripped, existing_norm, existing_stripped):
            continue
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


def _affix_fuzzy_pair_allowed(
    proposed: str,
    proposed_stripped: str,
    existing: str,
    existing_stripped: str,
) -> bool:
    """Reject role-affix collisions that would equate distinct slots.

    ``created_by`` and ``created_at`` both strip to ``created`` — that is a
    different *role*, not a synonym. Allow when cores differ after strip
    (``manufactured`` vs ``manufacturer``) or only one side lost an affix
    (``has_manufacturer`` → ``manufacturer``).
    """
    if proposed_stripped != existing_stripped:
        return True
    # Same core after strip. Identical originals already handled by exact match.
    if proposed == existing:
        return True
    proposed_lost_affix = proposed != proposed_stripped
    existing_lost_affix = existing != existing_stripped
    # Both sides lost an affix to land on the same core → different roles.
    if proposed_lost_affix and existing_lost_affix:
        return False
    # One side is bare core, the other is core+affix (has_X / X_by vs X).
    return True


def is_primitive_datatype(datatype: str | None) -> bool:
    """True when ``datatype`` is a literal range, not a type-ranged relationship.

    ``_fetch_ontology`` stores type-ranged attributes with ``datatype`` equal to
    the target type name (e.g. ``Indication``); primitives keep the usual
    ``string`` / ``integer`` / … labels. Used by CSV mapping reconcile to decide
    whether an existing property should force ATTRIBUTE vs RELATIONSHIP.
    """
    if not datatype:
        return True
    return datatype.lower() in {
        "string", "integer", "float", "boolean", "datetime", "uri", "geo", "date",
        "number", "int", "double", "long", "text",
    }


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

    from infona_client.graph.ontology_catalog_models import canonicalize_literal_datatype

    datatype = canonicalize_literal_datatype(attr.datatype)

    if existing is None:
        # New attribute → extend the type
        return ResolvedAttribute(
            name=_normalize_attr_name(attr.name),
            value=attr.value,
            datatype=datatype,
            action=AttrAction.EXTEND,
        )

    if existing.datatype == datatype:
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
        got=datatype,
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

        # Same-type cluster (event_id / event_title / …) is the mapped type
        # itself, not a nested Address-style concept. Promoting it mints a
        # shadow ``{key}-{type}`` node and ``has_{type}`` self-rel.
        if (entity.type_name or "").lower() == promoted_type.lower():
            continue

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
        from infona_client.graph.facts import RESERVED_ENTITY_PROPERTY_KEYS

        for attr in attrs:
            # Strip the prefix from the attribute name for the promoted entity
            short_name = _normalize_attr_name(attr.name)
            if short_name.startswith(prefix + "_"):
                short_name = short_name[len(prefix) + 1 :]
            # ``order_id`` / ``address_id`` must not collapse onto reserved ``id``.
            if short_name in RESERVED_ENTITY_PROPERTY_KEYS:
                short_name = _normalize_attr_name(attr.name)

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
