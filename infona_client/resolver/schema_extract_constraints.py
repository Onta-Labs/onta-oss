from __future__ import annotations

"""Post-extraction constraint / ceiling / plausibility guards.

Job: deterministic filters that run AFTER the LLM proposes entities
(attribute ceiling, implausible node labels, compound-plan-attr drop,
hard-constraint cage). Do not reimplement these in ingest or discovery —
call the functions here. Soft focus-floor lives in ``schema_focus``.
"""

import re

from infona_client.resolver.attribute_resolver import _normalize_attr_name
from infona_client.resolver.models import (
    CleanFact,
    CleanOutcome,
    ExtractedEntity,
    ExtractionResult,
)
from infona_client.resolver.schema_extract_prompts import (
    EXTRACTION_CONSTRAINT_USER_TEMPLATE,
    EXTRACTION_TARGET_USER_CEILING_TEMPLATE,
    EXTRACTION_TARGET_USER_TEMPLATE,
)
# Call-time host lookups so tests that patch schema_resolver.logger /
# insert_facts / _entity_uri / env flags keep working after this extract.
from infona_client.resolver import schema_resolver as _sr

def _build_constraint_user_block(constraint) -> str:
    """Render the per-type allowed-attribute lines appended to the user prompt.

    ``constraint`` is an :class:`ExtractionConstraint`. Returns an empty string
    when the constraint is inactive so the caller can no-op cleanly.
    """
    if constraint is None or not getattr(constraint, "is_active", False):
        return ""
    lines = []
    for t in constraint.types:
        attrs = constraint.attributes.get(t) or []
        if attrs:
            lines.append(f"- {t}: {', '.join(attrs)}")
        else:
            lines.append(f"- {t}: (all confirmed attributes)")
    if getattr(constraint, "soft", False):
        template = (
            EXTRACTION_TARGET_USER_CEILING_TEMPLATE
            if getattr(constraint, "attributes_exhaustive", False)
            else EXTRACTION_TARGET_USER_TEMPLATE
        )
    else:
        template = EXTRACTION_CONSTRAINT_USER_TEMPLATE
    return template.format(constraint_lines="\n".join(lines))


# Identity attributes always retained under an attribute ceiling so a trimmed
# record stays resolvable/displayable (mirrors the hard-constraint guard).
_CEILING_IDENTITY_ATTRS = frozenset({"name", "label", "title"})


def _apply_attribute_ceiling(result, constraint):
    """ONTA-382 — allowlist focus-type attributes when the set is exhaustive.

    Soft type decomposition (off-type nodes, relationships, lineage) is preserved;
    only unlisted attributes on focus-related entities are dropped. Each drop is
    recorded as a :class:`CleanFact` on ``result.ceiling_drops`` (folded into the
    A3 CleanReport ledger by ``ingest``) and logged — never silent.
    """
    if constraint is None or not getattr(constraint, "is_active", False):
        return result
    if not getattr(constraint, "attributes_exhaustive", False):
        return result

    new_entities = []
    drops: list[CleanFact] = list(getattr(result, "ceiling_drops", None) or [])
    dropped_count = 0
    for e in result.entities:
        allowed = constraint.ceiling_attributes_for(
            e.type_name, parent_chain=list(e.parent_chain or [])
        )
        if allowed is None:
            new_entities.append(e)
            continue
        allowed = allowed | _CEILING_IDENTITY_ATTRS
        kept = []
        for a in e.attributes:
            if a.name in allowed:
                kept.append(a)
                continue
            dropped_count += 1
            drops.append(
                CleanFact(
                    datatype=getattr(a, "datatype", None) or "string",
                    raw_value=str(a.value) if a.value is not None else "",
                    clean_value=None,
                    outcome=CleanOutcome.DROPPED,
                    conformed=False,
                    reason="attribute_ceiling",
                    entity_id=e.id,
                    attribute=a.name,
                )
            )
        if len(kept) != len(e.attributes):
            new_entities.append(e.model_copy(update={"attributes": kept}))
        else:
            new_entities.append(e)

    if dropped_count:
        _sr.logger.info(
            "extraction_attribute_ceiling_applied",
            dropped_attributes=dropped_count,
            focus_types=list(constraint.types),
            kept_entities=len(new_entities),
        )
    return ExtractionResult(
        entities=new_entities,
        relationships=list(result.relationships),
        source_text=result.source_text,
        ceiling_drops=drops,
    )

# ONTA-394: a value is a real numeric range like "2020-2021" / "1998–99" (a bare
# span, not a proper name). Kept separate from the pure-digit check below.
_YEAR_RANGE_RE = re.compile(r"^\s*\d{3,4}\s*[-–/]\s*\d{2,4}\s*$")
# URL / navigation / calendar fragments that leak in from scraped pages. A value
# carrying one of these is chrome, not an entity label.
_NAV_JUNK_RE = re.compile(
    r"(?i)(?:https?://|www\.|\.(?:com|org|net|edu|gov)\b|academic\s+calendar"
    r"|\bcalendar\b|\bsitemap\b|\bnavigation\b|\bbreadcrumb\b|\bmenu\b"
    r"|read\s+more|click\s+here|view\s+all|home\s*page)"
)


def _is_implausible_node_label(value: str | None) -> bool:
    """True when ``value`` cannot be a real-world entity label (ONTA-394).

    The VALUE-side companion to :func:`is_junk_type_name` (which guards the target
    TYPE name). Soft extract promotes an attribute value to a first-class NODE
    reached by a relationship edge only when the value actually names a real-world
    thing. A skewed cell — a bare year or number, a URL / navigation fragment, a
    slug, or truncated text — is NOT an entity label; minting it as a node
    fabricates junk (the dogfood's ``city -> UBC_Academic_Calendar`` / years-as-
    City edges). When this returns True the caller keeps the value as a LITERAL
    instead of minting a node.

    Deliberately CONSERVATIVE: it flags only shapes that are never a proper-noun
    label, so real places / orgs ("San Francisco", "AcmeCorp", "New York City")
    pass untouched.
    """
    if value is None:
        return True
    v = str(value).strip()
    if not v:
        return True
    # Bare number / year: the alphanumeric content is all digits ("2020", "42").
    alnum = re.sub(r"[^0-9A-Za-z]", "", v)
    if alnum and alnum.isdigit():
        return True
    if _YEAR_RANGE_RE.match(v):
        return True
    # Truncated navigation text ("WCC_-_Western_Community_Colle…").
    if "…" in v or v.endswith(".."):
        return True
    # URL / navigation / calendar fragments.
    if _NAV_JUNK_RE.search(v):
        return True
    # Slug shape: the SURFACE form of a real label has spaces — underscore-joined
    # tokens are a URL-derived slug (``_safe_id`` underscores belong in the URI,
    # not the value). Two-or-more underscores, or the "_-_" separator, is a strong
    # signal it is a scraped slug rather than a name.
    if v.count("_") >= 2 or "_-_" in v:
        return True
    # Absurdly long for a proper-noun label (a paragraph of scraped nav text).
    if len(v) > 80:
        return True
    return False


def _drop_offplan_compound_attributes(result, constraint):
    """ONTA-394 — drop compound attribute names fabricated from ≥2 plan attrs.

    Soft extract sometimes invents a name by CONCATENATING two separately-
    requested plan attributes (the dogfood's ``website_city`` from ``website`` +
    ``city``). Soft mode is meant to SPLIT composites, never merge two requested
    fields into a new name; such a compound is a fabrication that also spawns a
    junk column. Runs for any ACTIVE SOFT constraint — INDEPENDENT of
    ``attributes_exhaustive`` (ONTA-382's ceiling only fires on a user-declared
    closed set, but a merged-plan-attr compound is wrong even when the plan attrs
    are illustrative). Drops are ledgered on ``ceiling_drops`` (folded into the A3
    CleanReport by ``ingest``), never silent.

    Only fires when ≥2 DISTINCT tokens of the name are THEMSELVES plan attributes,
    so a real multi-word attribute whose components are not separate plan attrs
    (``postal_code``, ``address_line_1`` under a plan of {name, address, …}) is
    untouched. An exact plan attribute is never treated as a compound.
    """
    if constraint is None or not getattr(constraint, "is_active", False):
        return result
    if not getattr(constraint, "soft", False):
        return result  # hard mode already ceilings to the plan attrs
    plan_attrs: set[str] = set()
    for names in (getattr(constraint, "attributes", None) or {}).values():
        for n in names or ():
            plan_attrs.add(_normalize_attr_name(n))
    if len(plan_attrs) < 2:
        return result  # need ≥2 plan attrs to form a compound
    focus_types = set(constraint.types)
    # A PRIMARY record (a relationship source, or an orphan with no edges) is a
    # subject the plan names — it either IS the focus type or will be collapsed /
    # anchored under it (ONTA-394 AC#4 / ONTA-383). The dogfood's compound-bearing
    # records are EVIDENCE-FREE near-synonym subtypes (``College`` with an EMPTY
    # parent_chain), so a pure ``type_name in focus_types`` check would miss them
    # here — this drop runs BEFORE type resolution/collapse. Guarding every primary
    # (not just already-focus-typed ones) is what makes the backstop actually fire
    # on the shape that produced ``website_city``. Dimension-only nodes (relationship
    # TARGETS the decomposer lifts out — City, …) are NOT primary and stay
    # unrestricted, so a lifted node keeps its own attributes.
    from infona_client.resolver.schema_focus import _primary_entity_ids
    primary_ids = _primary_entity_ids(result)

    def _is_plan_compound(attr_name: str) -> bool:
        norm = _normalize_attr_name(attr_name)
        if norm in plan_attrs:
            return False  # an exact plan attr is never a compound
        tokens = [t for t in norm.split("_") if t]
        distinct_plan = {t for t in tokens if t in plan_attrs}
        return len(distinct_plan) >= 2

    new_entities: list[ExtractedEntity] = []
    drops: list[CleanFact] = list(getattr(result, "ceiling_drops", None) or [])
    dropped = 0
    for e in result.entities:
        # Guard the plan's SUBJECT records: focus-typed, focus-lineaged, OR any
        # primary record (a brand-new near-synonym subtype the collapse will fold
        # into the focus). Off-type dimension nodes are unrestricted.
        chain = list(getattr(e, "parent_chain", None) or [])
        is_focus_related = (
            e.type_name in focus_types
            or any(p in focus_types for p in chain)
            or e.id in primary_ids
        )
        if not is_focus_related or not e.attributes:
            new_entities.append(e)
            continue
        kept = []
        for a in e.attributes:
            if _is_plan_compound(a.name):
                dropped += 1
                drops.append(
                    CleanFact(
                        datatype=getattr(a, "datatype", None) or "string",
                        raw_value=str(a.value) if a.value is not None else "",
                        clean_value=None,
                        outcome=CleanOutcome.DROPPED,
                        conformed=False,
                        reason="compound_plan_attribute",
                        entity_id=e.id,
                        attribute=a.name,
                    )
                )
                _sr.logger.info(
                    "discovery_compound_plan_attribute_dropped",
                    entity_id=e.id, type_name=e.type_name, attribute=a.name,
                )
            else:
                kept.append(a)
        if len(kept) != len(e.attributes):
            new_entities.append(e.model_copy(update={"attributes": kept}))
        else:
            new_entities.append(e)
    if not dropped:
        return result
    _sr.logger.info(
        "discovery_compound_plan_attributes_filtered",
        dropped_attributes=dropped, focus_types=list(constraint.types),
    )
    return ExtractionResult(
        entities=new_entities,
        relationships=list(result.relationships),
        source_text=result.source_text,
        ceiling_drops=drops,
    )


def _apply_extraction_constraint(result, constraint):
    """Light post-extraction guard for constrained (discovery) extraction.

    Prompt-level constraints are the primary mechanism; this is a cheap,
    deterministic backstop that drops:
      * entities whose ``type_name`` is not among the allowed types, and
      * attributes not in a type's confirmed set (the entity's key/name-like
        attribute is always kept so the record stays identifiable).
    Relationships between surviving entities are preserved. A ``None`` /
    inactive constraint returns ``result`` unchanged (document path no-op).

    SOFT mode keeps off-type entities / lineage / relationships (decomposition
    is the desired output). When ``attributes_exhaustive`` is also set
    (ONTA-382), soft mode STILL enforces the attribute allowlist on focus-type
    records — type decomposition stays free, attribute set is a ceiling.

    ``result`` is an :class:`ExtractionResult`; ``constraint`` an
    :class:`ExtractionConstraint`.
    """
    if constraint is None or not getattr(constraint, "is_active", False):
        return result
    if getattr(constraint, "soft", False):
        # SOFT (seed) mode: the type/attributes were a PRIOR in the prompt, not a
        # cage. The extractor's decomposition (subtypes, real-world nodes,
        # multi-valued splits, relationships) is the desired output — never drop
        # off-type entities, strip lineage, or delete edges here. The ONE thing
        # this backstop still asserts (ONTA-255) is that a subject's cost/latency
        # metric must not sit on an off-brief standards/compliance concept. This
        # is the PER-CHUNK view, so it runs RE-HOME-ONLY (``allow_strip=False``):
        # it re-homes a misattached metric onto a focus subject visible in THIS
        # chunk, but never strips-and-declares-starved on a partial view — the
        # subject may live in another chunk. The merged full-batch pass in
        # ``ingest`` (allow_strip=True) is the only one trusted to strip / judge
        # starvation, so a cross-chunk metric survives to be re-homed there.
        # ONTA-382 attribute ceiling is applied once over the FULLY-MERGED
        # extraction in ``ingest`` (after the authoritative soft focus floor),
        # not per-chunk — so a soft-floor re-home that lands a metric on the
        # focus subject is still ceiling-checked, and drop ledgering is not
        # doubled across chunk merges.
        from infona_client.resolver.schema_focus import _apply_soft_focus_floor
        return _apply_soft_focus_floor(result, constraint, allow_strip=False)
    allowed_types = set(constraint.types)
    kept_entities = []
    kept_ids: set[str] = set()
    dropped_off_type = 0
    dropped_attrs = 0
    stripped_lineage = 0
    for e in result.entities:
        if e.type_name not in allowed_types:
            dropped_off_type += 1
            continue
        update: dict = {}
        allowed_attrs = constraint.allowed_attributes(e.type_name)
        if allowed_attrs is not None:
            # Always keep an identifying attribute (name/label/id-like) so a
            # record the guard trims can still be resolved/displayed.
            allowed_attrs = allowed_attrs | {"name", "label", "title"}
            filtered = [a for a in e.attributes if a.name in allowed_attrs]
            dropped_attrs += len(e.attributes) - len(filtered)
            if len(filtered) != len(e.attributes):
                update["attributes"] = filtered
        # Strip lineage fields that could STILL mint extra types during the
        # resolve step even though the entity's own type_name is allowed: a
        # constrained record that carries also_types=["Organization"] or a
        # parent_chain into off-list ancestors would create exactly the sub-types
        # ONTA-199 is trying to prevent. The confirmed target type already exists,
        # so a constrained record needs no new subclass/co-type edge.
        if e.also_types or e.parent_chain or e.parent_type or e.subtype_description:
            update.update(
                also_types=[],
                parent_chain=[],
                parent_type=None,
                subtype_description=None,
            )
            stripped_lineage += 1
        if update:
            e = e.model_copy(update=update)
        kept_entities.append(e)
        kept_ids.add(e.id)
    kept_rels = [
        r
        for r in result.relationships
        if r.source_id in kept_ids and r.target_id in kept_ids
    ]
    if (
        dropped_off_type
        or dropped_attrs
        or stripped_lineage
        or len(kept_rels) != len(result.relationships)
    ):
        _sr.logger.info(
            "extraction_constraint_applied",
            allowed_types=sorted(allowed_types),
            dropped_off_type=dropped_off_type,
            dropped_attributes=dropped_attrs,
            stripped_lineage=stripped_lineage,
            dropped_relationships=len(result.relationships) - len(kept_rels),
            kept_entities=len(kept_entities),
        )
    return ExtractionResult(
        entities=kept_entities,
        relationships=kept_rels,
        source_text=result.source_text,
    )
