"""Schema Resolver — deterministic layer between LLM extraction and Neptune.

Pipeline:
  Raw data → LLM extraction (non-deterministic) → Schema Resolver → Neptune

The resolver enforces ontology consistency: type matching, attribute resolution,
schema-on-write validation, and Option D coexistence for structure promotion.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

import os

import anthropic
import httpx
import structlog

from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.ontology_queries import (
    PRIMITIVE_TYPES,
    TEXT_KIND_FREE_TEXT,
    TEXT_KIND_NOT_TEXT,
    batch_entity_exists_query,
    entities_by_key_value_query,
    entity_exists_query,
    entity_uri as _entity_uri,
    get_full_ontology_query,
    insert_attribute,
    insert_subtype,
    insert_type,
    ontology_version,
    parent_map_query,
    set_object_property_range,
    type_uri,
    upsert_attribute_text_kind,
    upsert_type,
    upsert_type_comment,
    attr_uri,
)
from cograph_client.graph.layers import LayerStack, type_name_from_uri
from cograph_client.graph.text_markers import (
    TextCandidacy,
    classify_text_candidacy,
    invalidate_for_graph as invalidate_text_marker_cache,
)
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.kg_writer import build_graph_delta, insert_facts
from cograph_client.pipeline.envelope import derive_fact_id
from cograph_client.graph.provenance import (
    build_attribute_provenance_companions,
    build_provenance_triples,
    build_truth_verdict_companion,
    provenance_graph_uri,
)
from cograph_client.graph.queries import BATCH_PREDICATE, batched_insert_triples, delete_batch_query, insert_triples, tenant_graph_uri
from cograph_client.resolver.attribute_resolver import (
    AttributeSchema,
    _normalize_attr_name,
    check_promotion,
    resolve_attribute,
)
from cograph_client.resolver.models import (
    AttrAction,
    ColumnMapping,
    ColumnRole,
    CSVSchemaMapping,
    ExtractionConstraint,
    ExtractionResult,
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    IngestResult,
    KeyJoin,
    MatchVerdict,
    RejectedValue,
    ValidatedTriple,
    ValidationOutcome,
    assert_soft_a2,
    soft_a2_from_structured_rows,
    validate_soft_a2,
)
from cograph_client.resolver.llm_router import PRIMARY_MODEL, openrouter_chat
from cograph_client.resolver.predicate_normalizer import normalize_predicate
from cograph_client.resolver.type_matcher import TypeMatcher
from cograph_client.resolver.validator import validate_triple
from cograph_client.normalization.clean import clean_value
from cograph_client.resolver.verdict_cache import JsonVerdictCache
# ONTA-370: A4 Verify seam. Reuse the Wave-6 orchestrator + its policy-enabled
# gate AS-IS — never reimplemented. `_policy_enabled` is the SAME duck-typed
# on/off check `verify_clean_facts` uses internally, so the seam-level gate and
# the orchestrator can never disagree on whether verification is on.
from cograph_client.verification.verifier import _policy_enabled, verify_clean_facts

logger = structlog.stdlib.get_logger("cograph.resolver")

EXTRACTION_SYSTEM = """\
You are a knowledge graph extraction engine. Given raw text and the current \
ontology, extract structured entities, their attributes, and relationships.

Rules:
- Each entity must have a type_name (PascalCase, singular noun, e.g. "Property" not "properties")
- Each entity must have an id — a stable handle, NOT a display label. Use a genuine human identifier (name, title, address) when the entity has one; when it has no natural name (a reified measurement, event, or other dependent entity) derive a compact STRUCTURAL id from its defining fields. Never invent a descriptive phrase just to serve as an id (see "Names are optional" below).
- Attributes have a name (snake_case), value (string), and datatype (string, integer, float, boolean, datetime, uri, geo)
- Use datatype "geo" only for a SINGLE coordinate value (a WKT "POINT(lon lat)" or a "lat,lon" pair); keep separate latitude/longitude columns as float
- Relationships connect two entities by their id with a predicate (snake_case)

Type placement:
You will be given the existing ontology types. For each entity you extract:
- Always pick the MOST SPECIFIC type the data justifies (HotelGuest over Guest \
over Person; Condo over Property) — granularity is recovered later, coarseness \
is not.
- If its type already exists in the ontology, use that exact type name and set \
same_as to that name.
- If its type is new but is a subtype of an existing type (is-a relationship), \
set parent_type to the EXISTING type name. Prefer connecting to the hierarchy \
over creating orphaned types. A Broker is a Person. A City is a Place. A Condo \
is a Property. But geographic containment is NOT a subtype: State is NOT a \
subtype of City, City is NOT a subtype of State. Use relationships for containment.
- parent_chain: list the FULL is-a lineage of type_name, most-specific first, up \
to the most general type — e.g. type_name "HotelGuest" -> parent_chain \
["Guest", "Person"]; "Condo" -> ["Property", "Asset"]. Include ancestors even if \
they are NOT yet in the ontology (they will be created). This closes a brand-new \
multi-level hierarchy in one shot. Omit or leave empty only for a top-level type.
- also_types: ONLY for genuine, independent multi-classification — when the entity \
truly IS two unrelated things at once (a hotel employee who is also a guest: \
type_name "Employee", also_types ["Guest"]). These are NOT ancestors. Leave empty \
in the common case.
- If its type is genuinely unrelated to anything in the ontology, leave same_as \
and parent_type null and parent_chain empty.

Entity-first principle:
When unsure whether a value should be a literal attribute or a separate entity \
with a relationship, ALWAYS prefer creating a separate entity. Entities can have \
attributes and relationships added later; literals are dead ends. Only use literal \
attributes for truly atomic values: numbers, dates, booleans, short enums, or \
identifiers.

Reify measurements:
When a value is a MEASUREMENT, METRIC, or other observation that can CHANGE OVER \
TIME or carries PROVENANCE (a score, rating, price, ranking, benchmark result), \
model it as its OWN entity (e.g. type_name "Score", "Rating", "Price") with \
attributes "value" and, when available, "timestamp"/"as_of" — plus relationships \
linking it to the thing measured and to the provider/publisher that produced it. \
Name that producer relationship "measured_by" / "reported_by" / "published_by" / \
"produced_by" (NEVER the bare predicate "source" — that collides with internal \
housekeeping). Reify INSTEAD of a bare scalar attribute on the parent: a bare \
number loses its history and its provenance the moment a newer reading arrives. \
Reify only genuine observations; do NOT reify a fixed intrinsic property (a \
person's birth_year, a product's sku).

Names are optional:
Not every entity has a name. Emit a "name" (or other name-like label) attribute \
ONLY when the entity has a real, human-identifying proper name (a person, place, \
organization, product, titled work). Do NOT fabricate one for an entity that is \
identified structurally or by its links — a reified measurement/observation \
(score, rating, price, ranking), an untitled event or transaction, or a \
dependent/association entity has \
NO proper name. Identify those by their "value", timestamp, and relationships; a \
descriptive label stitched together from those fields (the measured thing + the \
number) is redundant — omit it. Forcing a name onto a nameless entity is a \
modeling error, not a default.

Never fabricate values:
Extract only values the source actually STATES. When the text does not give a \
value for an attribute, that attribute is UNKNOWN — OMIT it entirely (leave it \
out; never emit it with a made-up value and never null-pad it). NEVER invent an \
identifier, code, NPI, SKU, price, date, phone number, or any other value to \
fill a field: a value you cannot find is omitted, not guessed. Do NOT emit \
placeholder filler such as "1234567890", "0000000000", "123-45-6789", "N/A", \
"unknown", or "TBD" — a fabricated identifier silently corrupts every join \
keyed on it, so a missing value is correct and a made-up one is a bug.

Lift providers / organizations:
When records carry a recurring CATEGORICAL naming a provider, vendor, publisher, \
manufacturer, organization, or brand (a value that repeats across records and \
names a real-world actor), create an "Organization" entity per distinct value and \
relate to it (e.g. provided_by / published_by / made_by) instead of leaving it a \
string. Do NOT lift free-form descriptive text or a one-off label that names no \
actor. Also do NOT mint as an Organization: (a) the data SOURCE, benchmark, \
leaderboard, dataset, index, or publication name ITSELF — that names the artifact, \
not an actor; the publisher is the company that OPERATES it, so attribute \
publication to that operating company, never to the dataset's own name; or (b) \
baseline, placeholder, or null-like values ("Human", "Unknown", "N/A", "None", \
"-", "other", "self", "none"). When the only provider/source string available is \
the dataset's own name or such a placeholder, OMIT the organization rather than \
inventing one.

Subtypes with a description:
When a measurement or entity is a SPECIALIZED KIND of a more general type (e.g. a \
"Humanness Index" is a kind of Score; a "Condo" is a kind of Property), emit it \
as a subtype via parent_chain AND set subtype_description to a brief sentence \
explaining what it is / what it measures. The description becomes the new type's \
definition in the ontology. Set subtype_description ONLY for a new specialized \
type you are minting — leave it null otherwise.

Respond with valid JSON only. No markdown."""

EXTRACTION_USER_TEMPLATE = """\
Existing ontology types:
{existing_types}

Extract entities, attributes, and relationships from this content:

---
{content}
---

Return JSON:
{{
  "entities": [
    {{
      "type_name": "MostSpecificTypeName",
      "id": "identifier",
      "same_as": "<existing type name if this is the same concept, else null>",
      "parent_type": "<existing type name if this is a subtype, else null>",
      "parent_chain": ["<immediate parent>", "<grandparent>", "..."],
      "also_types": ["<independent co-type, rare>"],
      "subtype_description": "<brief definition when minting a NEW specialized subtype, else null>",
      "attributes": [
        {{"name": "attr_name", "value": "attr_value", "datatype": "string"}}
      ]
    }}
  ],
  "relationships": [
    {{
      "source_id": "entity_id",
      "predicate": "relationship_name",
      "target_id": "entity_id"
    }}
  ]
}}"""


# --- ONTA-199: DISCOVERY-ONLY extraction constraint -------------------------
# Web discovery has already CONFIRMED the single target type + exact attribute
# set with the user, so it must NOT re-run the open-ended multi-type reifier
# (which mints Address/Taxonomy/Organization/… sub-entities and ~3x the output
# tokens, blowing the extraction watchdog). When an ExtractionConstraint is
# present, this block is APPENDED to the system + user prompt to pin extraction
# to that one type + those attributes. Absent (None) → the prompt is byte-for-
# byte the open-ended default, so document/CSV/text ingestion is unchanged.

EXTRACTION_CONSTRAINT_SYSTEM = """\

CONSTRAINED EXTRACTION MODE (overrides the type-placement and reification rules \
above):
This source has a SINGLE confirmed target. Extract ONLY entities of the \
type(s) listed below, each carrying ONLY the confirmed attributes for that type \
(plus its key/identifier attribute). Specifically:
- Do NOT create any entity whose type is not in the allowed list — do NOT lift \
Address, Taxonomy, Organization, HealthcareOrganization, or any other \
sub-entity out of the record. Fold what would have been a sub-entity into a \
plain literal attribute of the target entity when (and only when) it is one of \
the confirmed attributes; otherwise omit it.
- Do NOT reify measurements/scores/prices into their own entities here — the \
target type + its confirmed attributes are the whole schema.
- Do NOT emit attributes that are not in the confirmed list for that type \
(besides the entity's key/identifier). Ignore extra fields the source happens \
to carry.
- Leave "also_types", "parent_type", "parent_chain", and "subtype_description" \
EMPTY/null — the target type is confirmed and already exists, so do not classify \
records into additional or ancestor types.
- Emit an empty "relationships" list — this mode collects flat records of one \
type, not a relationship graph.
Everything else (id rules, snake_case attribute names, datatypes, JSON-only \
output) still applies."""

EXTRACTION_CONSTRAINT_USER_TEMPLATE = """\

CONSTRAINT — extract ONLY these type(s), with ONLY these attributes (plus each \
type's key/identifier):
{constraint_lines}
Emit no other entity types, no sub-entities, and no other attributes."""


# --- SOFT / SEED extraction mode (the discovery fix) ------------------------
# The HARD constraint above (ONTA-199) fixed speed + over-fragmentation by
# FLATTENING discovery to one literal-only type — which mis-typed subtypes
# (a nurse practitioner became a "Physician"), demoted real-world values
# (city, specialty) to literals, and dropped relationships. The SOFT mode
# fixes the ORIGINAL problem the right way: keep the confirmed focus type +
# attributes as a PRIOR that orients extraction (so it stays focused and
# compact — the cost/fragmentation win) while letting the extractor decompose
# faithfully (subtypes, real-world nodes, multi-valued splits, reuse-first —
# the correctness win). Appended in place of EXTRACTION_CONSTRAINT_SYSTEM when
# ExtractionConstraint.soft is True; the post-extraction guard becomes a no-op.

EXTRACTION_TARGET_SYSTEM = """\

TARGET-SCHEMA MODE (a FOCUS HINT, not a restriction — it overrides nothing above):
This source was gathered to collect records of a CONFIRMED focus type (named in \
the user block), and those records usually carry a known set of attributes. Treat \
that as a PRIOR that orients you — NOT a cage. Model the data faithfully, exactly \
as you would for open ingestion:
- TYPE TO THE TRUTH. Give each record its most specific correct type. When records \
are specialized KINDS of the focus (e.g. a nurse practitioner or physician \
assistant alongside physicians), mint them as distinct SUBTYPES under a shared \
parent — never force every record into the single focus type, and never leave the \
distinguishing role as a bare string attribute.
- REAL-WORLD THINGS BECOME NODES. When an attribute value is itself a reusable \
real-world entity — a place (city, state, country), an organization, a person, a \
category / specialty / sector — model it as its OWN entity reached by a \
relationship, so rows sharing that value share ONE node. Split a composite like \
"City, State" into the two nodes it names.
- KEEP MEASUREMENTS LITERAL. Pure identifiers, numbers, prices, counts, ratings, \
dates, booleans, phone numbers, and street addresses stay LITERAL attributes with \
the right datatype. Do NOT reify a measurement / score / price / rating into its \
own entity.
- SPLIT MULTI-VALUED FIELDS. A field holding several values (comma- or \
pipe-separated) becomes SEVERAL assertions / edges — one per value — never one \
glued string.
- REUSE, DON'T FRAGMENT. Prefer an existing ontology type over minting a new one; \
create a new type only for a genuinely new real-world KIND. Aim for a COMPACT, \
reusable ontology — not a type per column or per value.
- THE FOCUS TYPE NAMES THE SUBJECT — a requested attribute is a FACT ABOUT the \
subject, NEVER a rival type to mint records under. The focus type names WHAT each \
record IS. When the brief also asks for something like a certification, standard, \
compliance regime, accreditation, or regulation, that is a FACT/EDGE about the \
subject — model it as its own node the subject LINKS to (e.g. `certified_for` / \
`complies_with` / `conforms_to`), not as the type the record collapses into. Never \
let a certification / standard / regulation / compliance concept become the \
dominant type that the subject's own records get reclassified as; the number of \
subject records must not shrink to zero while such a concept absorbs them.
- MEASUREMENTS BELONG TO THE SUBJECT. A cost, price, fee, latency, throughput, \
rate, or other measurement is a property of the SUBJECT record — attach it to the \
focus subject (as a literal, per KEEP MEASUREMENTS LITERAL above), NEVER to a \
certification / standard / regulatory / compliance entity. A standards body or \
certificate is never the bearer of the subject's cost or latency.
- EXAMPLE (neutral, non-domain): you are collecting Widget records that carry \
`cost_per_unit`, `latency_ms`, and a "SprocketSafe" certification. Mint each row \
as a Widget (the subject); keep `cost_per_unit` and `latency_ms` as LITERALS on \
the Widget; model "SprocketSafe" as a Certification NODE the Widget links to via \
`certified_for`. Do NOT mint a "SprocketSafe" / Certification record and hang \
`cost_per_unit` / `latency_ms` on it — that misfiles the subject and its metrics \
under a mere fact about it.
- NEVER FABRICATE A VALUE. Extract only what the source STATES. If a requested \
attribute (an identifier, code, NPI, price, date, phone, …) is not given for a \
record, OMIT it — never invent one, never null-pad, and never emit placeholder \
filler like "1234567890", "0000000000", "N/A", "unknown", or "TBD". A made-up \
identifier silently breaks every join keyed on it, so a missing value is \
correct and a fabricated one is a bug.
The focus type + expected attributes below say what to look for; add exactly the \
structure the data justifies and keep it tight."""

EXTRACTION_TARGET_USER_TEMPLATE = """\

FOCUS — you are collecting records of:
{constraint_lines}
Model each record with its most specific type (subtypes encouraged), lift \
real-world values (places, orgs, people, categories) into their own nodes via \
relationships, split multi-valued fields into separate assertions, and keep pure \
measurements / identifiers as literals. The focus type + attributes are a guide, \
not a limit — add the structure the data justifies, reuse types, stay compact."""


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
    template = (
        EXTRACTION_TARGET_USER_TEMPLATE
        if getattr(constraint, "soft", False)
        else EXTRACTION_CONSTRAINT_USER_TEMPLATE
    )
    return template.format(constraint_lines="\n".join(lines))


def _apply_extraction_constraint(result, constraint):
    """Light post-extraction guard for constrained (discovery) extraction.

    Prompt-level constraints are the primary mechanism; this is a cheap,
    deterministic backstop that drops:
      * entities whose ``type_name`` is not among the allowed types, and
      * attributes not in a type's confirmed set (the entity's key/name-like
        attribute is always kept so the record stays identifiable).
    Relationships between surviving entities are preserved. A ``None`` /
    inactive constraint returns ``result`` unchanged (document path no-op).

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
        logger.info(
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


# --- ONTA-255: SOFT-mode focus-type floor + metric-misattachment guard -------
# SOFT extraction (the seed prior) deliberately lets the model decompose freely
# — that faithful decomposition IS the desired output, so the soft post-guard
# stays a no-op for everything EXCEPT one drift failure it must still assert
# against. When a multi-type brief ("<subject> records … with pricing, latency,
# AND compliance") is collapsed to a single focus type + a flat attribute list,
# and a source page interleaves subject rows with certification / standard rows,
# the extractor can latch onto a fact-ABOUT-the-subject (a Compliance / Standard
# concept) as the DOMINANT type and mint the subject's records under it — folding
# the subject's own cost / latency / price metrics onto a standards-body node.
# The confirmed focus type is a CONTRACT about what the records ARE, so a numeric
# metric-shaped attribute (cost / price / fee / latency / throughput …) must not
# sit on an entity whose TYPE reads as a standards / certification / regulation
# concept — the metric belongs to the SUBJECT. What the guard actually does with
# a misattached metric, honestly (not "always re-homed"):
#   * RE-HOME when a subject can be identified — the concept entity is linked to a
#     focus subject by a surviving edge, OR there is exactly ONE focus subject in
#     the batch (see the single-subject caveat at the `sole_focus` attach below).
#   * Otherwise (no identifiable subject) STRIP the metric off the concept node so
#     a cost/latency triple can never persist on a standards entity, and COUNT +
#     LOG every removed value so nothing is silent. When the focus type minted
#     ~zero entities at all, that strip is the FOCUS-TYPE FLOOR breach and is
#     logged as `discovery_focus_type_starved`.
# `allow_strip` gates the destructive half: the PER-CHUNK backstop
# (`_apply_extraction_constraint`) runs with allow_strip=False so a partial view
# can RE-HOME within its own chunk but NEVER strip-and-declare-starved (the
# subject may live in another chunk); only the MERGED full-batch pass in `ingest`
# runs with allow_strip=True and is trusted to strip / judge starvation.
# A compliance-FOCUSED KG is safe: its confirmed focus IS the cert/standard, so
# those entities are focus-lineage and never treated as a misattachment target.

# Whole-token allowlist for a type whose NAME reads as a genuine standards / cert
# / regulation concept. Deliberately NARROW and matched as WHOLE tokens (never as
# a loose stem): loose stems like "standard"/"license"/"audit"/"governance" occur
# inside ordinary SUBJECT types (StandardRoom, SoftwareLicense, AuditLog,
# GovernanceBoard) and would mis-yank their legitimate metrics. The tokenizer
# de-pluralizes ("Certifications" -> "certification", "Certs" -> "cert").
_STANDARDS_CONCEPT_TOKENS = frozenset(
    {
        "compliance",
        "certification",
        "certificate",
        "cert",
        "regulation",
        "regulatory",
        "accreditation",
        "attestation",
    }
)
# "standard" is compound-prone, so it signals a standards concept ONLY as a BARE
# type (Standard / Standards) or when combined with a token above (which already
# matches). Any Standard-COMPOUND without a concept token (StandardRoom,
# StandardPlan, StandardEdition) is a subject and keeps its metrics.
_BARE_STANDARD_TOKENS = frozenset({"standard", "standards"})

# Substrings that mark an attribute NAME as a cost / price / latency-shaped
# metric. Combined with a numeric-value check so a non-numeric attribute whose
# name merely contains one of these (e.g. `pricing_model: "usage-based"`) is
# never touched.
_METRIC_NAME_SUBSTRINGS = (
    "cost",
    "price",
    "pricing",
    "fee",
    "latency",
    "throughput",
    "bandwidth",
    "per_minute",
    "per_second",
    "per_hour",
    "per_token",
    "per_unit",
    "_ms",
)

_NUMERIC_DATATYPES = frozenset(
    {"integer", "int", "float", "number", "double", "decimal", "long"}
)
# A leading number (optionally signed / currency-prefixed), so "0.30", "$0.30",
# "200", and "200ms" read as numeric while "SprocketSafe" / "GDPR" / "yes" do not.
_LEADING_NUMBER_RE = re.compile(r"^\s*[-+]?\s*\$?\s*\d[\d,]*(?:\.\d+)?")


def _split_type_tokens(name: str) -> set[str]:
    """Lowercased word tokens of a type name, splitting camelCase and separators,
    with a crude de-pluralization so "Standards" matches "standard"."""
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


def _is_standards_concept_type(type_name: str) -> bool:
    """True when ``type_name`` reads as a genuine standards / certification /
    regulation concept (a fact ABOUT a subject, not a subject itself).

    Whole-token match against a narrow allowlist, so compound SUBJECT types that
    merely CONTAIN a loose stem — StandardRoom, SoftwareLicense, License,
    AuditLog / AuditTrail, LicensePlate, GovernanceBoard, DataGovernance — are
    correctly classified as NON-concept and keep their own metrics. "standard"
    counts only as a bare type (Standard / Standards) or paired with a concept
    token (ComplianceStandard / RegulatoryStandard, matched by the token above).
    """
    tokens = _split_type_tokens(type_name)
    if tokens & _STANDARDS_CONCEPT_TOKENS:
        return True
    return bool(tokens) and tokens <= _BARE_STANDARD_TOKENS


def _is_metric_attribute(attr) -> bool:
    """True when ``attr`` is a numeric metric (cost / price / latency-shaped NAME
    AND a numeric value/datatype). Requires BOTH so non-numeric look-alikes stay."""
    name = (getattr(attr, "name", "") or "").lower()
    if not any(sub in name for sub in _METRIC_NAME_SUBSTRINGS):
        return False
    if (getattr(attr, "datatype", "") or "").lower() in _NUMERIC_DATATYPES:
        return True
    return bool(_LEADING_NUMBER_RE.match(str(getattr(attr, "value", "") or "")))


def _apply_soft_focus_floor(result, constraint, *, allow_strip: bool = True):
    """SOFT-mode focus-type floor + metric-misattachment guard (ONTA-255).

    Runs only for an ACTIVE, SOFT constraint. Returns ``result`` UNCHANGED unless
    a numeric metric-shaped attribute has landed on an off-brief standards /
    certification / regulation-typed entity — the drift signature. When it has,
    each such metric is handled by identifiability, NOT unconditionally re-homed:

      * RE-HOMED onto a focus-lineage subject when one can be identified — the
        concept entity is linked to a focus subject by a surviving edge, or there
        is exactly ONE focus subject in the batch.
      * Otherwise STRIPPED off the concept node (so a cost/latency triple can never
        persist on a standards entity) and COUNTED. When no focus subject survives
        at all, that strip is the floor breach: ``discovery_focus_type_starved``.

    ``allow_strip`` gates only the destructive half. The PER-CHUNK backstop passes
    ``allow_strip=False``: on a partial view it re-homes within its own chunk but
    NEVER strips or declares starvation (the subject may be in another chunk), so
    the metric survives for the merged full-batch pass to re-home. The merged pass
    (``allow_strip=True``, the default) is the only one trusted to strip / judge
    starvation. A same-named metric that collides on the subject is COUNTED and
    logged (`discovery_metric_collision`), never silently dropped.

    Never drops an entity, a relationship, or a non-metric attribute. Idempotent:
    a second pass finds no misattached metric and returns the input untouched.
    """
    if constraint is None or not getattr(constraint, "is_active", False):
        return result
    if not getattr(constraint, "soft", False):
        return result

    focus_types = set(constraint.types)

    def _is_focus_lineage(e) -> bool:
        if e.type_name in focus_types:
            return True
        lineage = set(e.parent_chain or []) | set(e.also_types or [])
        if e.parent_type:
            lineage.add(e.parent_type)
        return bool(lineage & focus_types)

    focus_entities = [e for e in result.entities if _is_focus_lineage(e)]
    # Concept entities: standards/cert/regulation-typed AND not themselves the
    # confirmed focus (a compliance-focused KG's own records are never targets).
    concept_entities = [
        e
        for e in result.entities
        if not _is_focus_lineage(e) and _is_standards_concept_type(e.type_name)
    ]

    misattached = [
        (e, [a for a in e.attributes if _is_metric_attribute(a)])
        for e in concept_entities
    ]
    misattached = [(e, metrics) for e, metrics in misattached if metrics]
    if not misattached:
        return result  # no drift — soft decomposition passes through untouched

    # Map a concept entity to a focus subject it is directly linked to, so a
    # re-homed metric lands on the RIGHT subject when the edge survived extraction.
    focus_by_id = {e.id: e for e in focus_entities}
    linked_focus_of: dict[str, object] = {}
    for r in result.relationships:
        if r.source_id in focus_by_id and r.target_id not in focus_by_id:
            linked_focus_of.setdefault(r.target_id, focus_by_id[r.source_id])
        if r.target_id in focus_by_id and r.source_id not in focus_by_id:
            linked_focus_of.setdefault(r.source_id, focus_by_id[r.target_id])
    # Single-subject fallback: when exactly one focus subject exists and the
    # concept carries no surviving link, attach to that subject. CAVEAT: a metric
    # that truly belonged to an ABSENT subject would attach to the present one —
    # accepted because a discovery micro-batch is homogeneous per source_url, so
    # the single surviving subject is almost always the right owner, and the
    # alternative (dropping the metric) is worse.
    sole_focus = focus_entities[0] if len(focus_entities) == 1 else None

    # Only the full-batch pass (allow_strip=True) may declare the floor breached;
    # a per-chunk partial view must never call starvation.
    starved = allow_strip and len(focus_entities) == 0
    stripped_attrs: dict[str, list] = {}   # concept id -> surviving (non-metric) attrs
    add_to_focus: dict[str, list] = {}     # focus id -> metrics moved onto it
    reattributed = 0
    stripped = 0
    collisions = 0
    for concept_entity, metrics in misattached:
        dest = linked_focus_of.get(concept_entity.id) or sole_focus
        if dest is None:
            if not allow_strip:
                # PER-CHUNK partial view: the subject may live in another chunk.
                # Leave the metric in place — the merged pass re-homes it. Never
                # strip here (would destroy it) and never declare starvation.
                continue
            # MERGED pass, no subject anywhere → assert the floor. Count the loss.
            stripped_attrs[concept_entity.id] = [
                a for a in concept_entity.attributes if not _is_metric_attribute(a)
            ]
            stripped += len(metrics)
            continue
        # A subject was identified → move the metrics off the concept onto it.
        stripped_attrs[concept_entity.id] = [
            a for a in concept_entity.attributes if not _is_metric_attribute(a)
        ]
        existing = {a.name for a in dest.attributes} | {
            a.name for a in add_to_focus.get(dest.id, [])
        }
        for m in metrics:
            if m.name in existing:
                # The subject already holds this metric slot (its own value, or an
                # earlier re-home). Do NOT silently drop the second value — count
                # and log it so the collision is visible.
                collisions += 1
                logger.warning(
                    "discovery_metric_collision",
                    focus_types=sorted(focus_types),
                    concept_type=concept_entity.type_name,
                    subject_id=dest.id,
                    attribute=m.name,
                    dropped_value=str(getattr(m, "value", "")),
                )
                continue
            add_to_focus.setdefault(dest.id, []).append(m)
            existing.add(m.name)
            reattributed += 1

    if not stripped_attrs and not add_to_focus:
        # PER-CHUNK partial view could not identify any subject → nothing acted
        # on; leave the batch for the merged pass. (Cannot happen when
        # allow_strip=True, which always strips an un-re-homable metric.)
        return result

    new_entities = []
    for e in result.entities:
        update: dict = {}
        if e.id in stripped_attrs:
            update["attributes"] = stripped_attrs[e.id]
        if e.id in add_to_focus:
            base = update.get("attributes", list(e.attributes))
            update["attributes"] = base + add_to_focus[e.id]
        if update:
            e = e.model_copy(update=update)
        new_entities.append(e)

    concept_type_names = sorted({e.type_name for e, _ in misattached})
    if starved:
        logger.error(
            "discovery_focus_type_starved",
            focus_types=sorted(focus_types),
            concept_types=concept_type_names,
            metrics_reattributed=reattributed,
            metrics_stripped=stripped,
            metrics_collision=collisions,
            focus_entities=len(focus_entities),
        )
    else:
        logger.warning(
            "discovery_metric_reattributed",
            focus_types=sorted(focus_types),
            concept_types=concept_type_names,
            metrics_reattributed=reattributed,
            metrics_stripped=stripped,
            metrics_collision=collisions,
            focus_entities=len(focus_entities),
            partial_view=not allow_strip,
        )

    return ExtractionResult(
        entities=new_entities,
        relationships=result.relationships,
        source_text=result.source_text,
    )


# --- ONTA-177: free-text candidacy adjudication (the REASON layer) ----------
# The name-blind classifier (graph/text_markers.classify_text_candidacy —
# profiler ValueShape.TEXT proposes, ADR 0003 litmus) hands the AMBIGUOUS band
# to this prompt: text-shaped attributes whose values could equally be prose
# or structured strings (addresses, org names, composite titles). This is the
# ONE layer where the attribute NAME may be consulted. Verdicts become
# `<attr> <onto/textKind> "free_text"` ontology markers for the semantic
# instance index (ONTA-173) and its query-side filter (ONTA-176).

TEXT_CANDIDACY_SYSTEM = """\
You adjudicate FREE-TEXT candidacy for knowledge-graph attributes feeding a semantic \
(meaning-based) search index. Every candidate below is text-SHAPED (multi-word string \
values) but not obviously prose. Using each attribute's NAME plus its sample values, \
decide whether its values are free-running PROSE — descriptions, reviews, speeches, \
notes, transcripts, summaries, commentary — worth semantic indexing. Structured strings \
are NOT free text: postal addresses, person or organization names, titles used as \
identifiers or labels, delimited value lists, codes or paths containing spaces.

Respond with strict JSON only:
{"attributes":[{"type":"<TypeName>","attribute":"<attr_name>","free_text":true|false,"why":"<brief>"}]}
Include EVERY candidate exactly once. JSON only."""

TEXT_CANDIDACY_USER = """\
Candidate attributes (each with up to {n_samples} sample values):
{candidates}

Return the adjudication JSON now."""

#: Per-attribute cap on collected sample values for candidacy evidence — keeps
#: memory bounded on large batches; the shape statistics stabilize long before
#: this many samples.
_TEXT_EVIDENCE_MAX_VALUES = 50
#: How many sample values (truncated) each ambiguous attribute contributes to
#: the adjudication prompt.
_TEXT_ADJUDICATION_SAMPLES = 5
_TEXT_ADJUDICATION_SAMPLE_MAX_LEN = 140


def _looks_like_url(value: str) -> bool:
    """Whether a record ``source`` is a fetch URL (web discovery) vs a bare label
    (e.g. a CSV filename). Only a URL source becomes an attribute's `_source_url`
    citation; a non-URL source is still recorded as `_provenance`."""
    return isinstance(value, str) and (
        value.startswith("http://") or value.startswith("https://")
    )


# --- ONTA-259: deterministic anti-fabrication backstop ----------------------
# Discovery / text extraction runs an LLM over a source and PROPOSES attribute
# VALUES. When the model has no real value but the prompt nudges it to fill the
# field anyway, it emits a placeholder — in one UCI-health run the NPI
# "1234567890" landed on 92 distinct physicians, silently breaking every
# ID-keyed join. The extraction prompts now forbid this (see EXTRACTION_SYSTEM /
# EXTRACTION_TARGET_SYSTEM), but a prompt is not a guarantee: this
# deterministic, model-agnostic filter is the defense-in-depth backstop. A value
# it flags is treated as UNSTATED — the attribute is omitted (never written),
# exactly as if the source gave no value.
#
# Conservative BY DESIGN. It fires ONLY on values that are placeholder-shaped in
# FULL (whole-value match, never a substring), so a legitimate price "1000", a
# year "2024", or a real short code ("AAA", "XYZ") is KEPT. It is a fabrication
# guard, not a data cleaner — when unsure it keeps the value.

#: Whole-value filler tokens (case-folded) an extractor emits in place of a real
#: value. Matched only when the ENTIRE trimmed value equals one of these.
#: DELIBERATELY excludes ambiguous tokens that carry a real reading in some
#: domains — "None"/"nil" is a clinical CONFIRMED-none (allergies="None",
#: medications="None"), and "NA"/"nan" is a real code (Namibia's ISO code, a
#: North-America region code, or a person's name). Dropping those would turn a
#: STATED "none" into indistinguishable-from-unknown = information loss, so only
#: UNAMBIGUOUS non-values live here. "N/A" (with the slash) stays: it reads only
#: as "not applicable / available", never as a value.
_PLACEHOLDER_FILLER_TOKENS = frozenset({
    "n/a", "n.a.", "null", "unknown", "unspecified", "undefined",
    "not available", "not applicable", "no data", "no value",
    "tbd", "tba", "test", "placeholder",
})

#: Glyphs that, repeated as the WHOLE value (length ≥ 3), read as "unknown"
#: filler — "xxx", "xxxx", "----", "????", "....".
_PLACEHOLDER_RUN_CHARS = frozenset("x-_.?*#")

#: Canonical monotonic digit rings. A digit-only value is a sequential
#: placeholder when it is a SUBSTRING of one of these — so "1234567890"
#: (phone-keypad order, wraps 9→0), "0123456789", "123456", and their reverses
#: all match, while a real NPI like "1023011178" (not a contiguous run) does not.
_SEQ_DIGITS_ASC = "01234567890"
_SEQ_DIGITS_DESC = "09876543210"

#: A value must reduce to at least this many bare digits before the digit-run /
#: all-same-digit rules can flag it — so a real year ("2024"), a small price
#: ("1000"), or a short code is never caught. NPIs / phones / SSNs are 9–10 long.
_MIN_PLACEHOLDER_DIGITS = 6


def _is_fabricated_placeholder(value: str | None) -> bool:
    """True when ``value`` is an OBVIOUS fabricated placeholder (ONTA-259).

    Two families, both WHOLE-value (never a substring match) so the check stays
    conservative:
      * a filler token / filler-glyph run ("N/A", "unknown", "TBD", "xxx", …); and
      * a digit-shaped identifier placeholder — all-same-digit ("0000000000") or
        a monotonic run ("1234567890", "0123456789") — of at least
        ``_MIN_PLACEHOLDER_DIGITS`` digits, after stripping separators so
        "000-00-0000" / "(000) 000-0000" normalize.

    A legitimate price ("1000"), year ("2024"), or short code is NOT flagged.
    """
    if not value:
        return False
    v = value.strip()
    if not v:
        return False
    low = v.casefold()
    if low in _PLACEHOLDER_FILLER_TOKENS:
        return True
    # A run of a single filler glyph as the whole value: "xxx", "----", "????".
    if len(v) >= 3 and len(set(low)) == 1 and low[0] in _PLACEHOLDER_RUN_CHARS:
        return True
    # Digit-shaped identifier placeholders. Only judge values that are
    # essentially all digits (digits + separators) so a real alphanumeric code
    # is never touched.
    if re.fullmatch(r"[0-9\s().+\-/]+", v):
        digits = re.sub(r"[^0-9]", "", v)
        if len(digits) >= _MIN_PLACEHOLDER_DIGITS:
            if len(set(digits)) == 1:  # 0000000000, 1111111111, …
                return True
            if digits in _SEQ_DIGITS_ASC or digits in _SEQ_DIGITS_DESC:
                return True
    return False


def _structured_rows_mapping(
    rows: list[dict], type_name: str, key_field: str
) -> CSVSchemaMapping:
    """Build the fixed :class:`CSVSchemaMapping` for PRE-STRUCTURED rows (ONTA-272).

    Every distinct field (first-seen order across the rows) becomes a literal
    ATTRIBUTE column of ``type_name`` except the key field, which is the TYPE_ID
    (URI + label + key-as-attribute, per ADR 0003 §2). ``source_url`` is typed
    ``uri`` so its per-record citation renders as a link; every other field is a
    plain ``string`` literal — pre-structured sources deliver clean scalar cells,
    so there is no LLM datatype guessing. A degenerate ``key_field`` that never
    appears in the rows falls back to the first field so ``apply_mapping`` always
    has a TYPE_ID (an all-empty key still mints via its synthetic-key path)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for k in row:
            if k not in seen:
                seen.add(k)
                ordered.append(k)
    if key_field not in seen and ordered:
        key_field = ordered[0]
    columns = [
        ColumnMapping(
            column_name=k,
            role=ColumnRole.TYPE_ID if k == key_field else ColumnRole.ATTRIBUTE,
            target_type=type_name,
            datatype="uri" if k == "source_url" else "string",
        )
        for k in ordered
    ]
    return CSVSchemaMapping(entity_type=type_name, columns=columns)


class SchemaResolver:
    # Primary extraction model, routed through OpenRouter with the configured
    # fallback. Defaults to the shared primary.
    EXTRACT_MODEL = os.environ.get("OMNIX_EXTRACT_MODEL", PRIMARY_MODEL)
    EXTRACT_PROVIDER = os.environ.get("OMNIX_EXTRACT_PROVIDER", "openrouter")
    # Anthropic-SDK offline fallback (used only when no OpenRouter key is set) —
    # must be a NATIVE Anthropic model id. Env-overridable.
    INFER_MODEL = os.environ.get("OMNIX_INFER_MODEL", "claude-opus-4-8")
    ONTOLOGY_REFRESH_INTERVAL = int(os.environ.get("OMNIX_ONTOLOGY_REFRESH_INTERVAL", "50"))
    # Output ceiling for one extraction call. Raised 4096 → 8192 (ONTA-196) →
    # 16384 (ONTA-381): the reification/lift prompt makes each record emit MANY
    # more entities + relationships, and a dense multi-attribute page routinely
    # expands past 8192 even at 5 records (``finish_reason=length`` mid-JSON →
    # parse error → reactive split). Env-overridable.
    EXTRACT_MAX_TOKENS = int(os.environ.get("OMNIX_EXTRACT_MAX_TOKENS", "16384"))
    # Absolute hard ceiling adaptive completion may stretch to for a single
    # multi-record call (ONTA-381). Beyond this we shrink the chunk instead of
    # unbounded cost. Must be ≥ EXTRACT_MAX_TOKENS.
    EXTRACT_MAX_TOKENS_HARD = int(
        os.environ.get("OMNIX_EXTRACT_MAX_TOKENS_HARD", "32768")
    )
    # Bounded concurrency for the JSON/text chunk-extraction fan-out (ONTA-197
    # item 3). Independent chunks each take ~70s sequentially; running them under
    # a semaphore overlaps the independent LLM calls while capping how many are
    # in flight at once (avoid hammering the provider / exhausting rate limits).
    # Env-overridable so ops can widen/narrow without a deploy.
    EXTRACT_CONCURRENCY = int(os.environ.get("OMNIX_EXTRACT_CONCURRENCY", "5"))

    def __init__(
        self,
        neptune: NeptuneClient,
        anthropic_key: str,
        verdict_cache: JsonVerdictCache,
        embedding_service: object | None = None,
        ontology_lock: asyncio.Lock | None = None,
        verify_policy: object | None = None,
    ):
        self._neptune = neptune
        self._anthropic = anthropic.AsyncAnthropic(api_key=anthropic_key)
        self._embedding_service = embedding_service
        # ONTA-268: ontology-write lock. Serializes the read-decide-write of
        # ontology EXISTENCE (type/subtype/attribute/range creation) so several
        # per-sub-query resolvers ingesting concurrently can't race on
        # type-creation (which fragments the ontology). SHAREABLE: pass ONE lock
        # to every per-sub-query resolver in a discovery job (web_ingest_cap) and
        # their ontology mutations serialize against each other; default is a
        # private lock so a standalone resolver still guards its own critical
        # sections. Only the ontology existence read-decide-write is guarded —
        # NOT the LLM extraction (`_extract`), and NOT the instance-data write
        # (which is per-sub-query by construction). asyncio.Lock is NOT reentrant,
        # so the guarded methods never nest a second acquisition (see `_resolve_type`
        # / `_locked_ontology_update`).
        self._ontology_lock = ontology_lock or asyncio.Lock()
        from cograph_client.config import settings
        self._openrouter_key = settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._type_matcher = TypeMatcher(self._openrouter_key, verdict_cache, embedding_service)
        # Cross-file entity resolution. Best-effort: failures never block ingest.
        from cograph_client.resolver.er import ERPipeline
        self._er = ERPipeline(neptune)
        self._er_enabled = os.environ.get("COGRAPH_ER_ENABLED", "1") != "0"
        # Per-fact provenance (ADR 0002 §4): statement-metadata nodes in the
        # companion provenance graph. Default OFF so default triple output and
        # Neptune call pattern stay byte-identical.
        self._provenance_enabled = os.environ.get("COGRAPH_PROVENANCE_ENABLED", "0") == "1"
        # Per-attribute DISPLAY provenance companions (ONTA-245 F1): the same
        # attr_meta `source_url` / `verified_at` instance companions enrichment
        # always writes (metadata namespace, never ontology attributes — ONTA-262),
        # emitted by discovery too so a DISCOVERED fact and an
        # ENRICHED fact are provenance-symmetric (attribute-level, not just the
        # per-record `onto/source`). Default OFF so bulk CSV ingest stays byte-stable
        # (it would otherwise add up to 3 companions PER attribute fact); web
        # discovery flips it on to give the personas the per-fact citation + freshness
        # signal. Flows through the SAME shared write path (insert_facts) as every
        # other fact — the companions ride in the instance-triple collector.
        self._attr_provenance_enabled = (
            os.environ.get("COGRAPH_DISCOVERY_ATTR_PROVENANCE", "0") == "1"
        )
        # Governance seam (ADR 0002 §2): when ON, a brand-new type is ALSO
        # proposed to an LLM judge panel; on majority approval it is written
        # to the Global-Public layer with governance provenance. The tenant
        # write stays today's behavior either way — governance never blocks
        # or gates ingest. Default OFF (matching COGRAPH_PROVENANCE_ENABLED).
        self._governance_enabled = os.environ.get("COGRAPH_GOVERNANCE_ENABLED", "0") == "1"
        if self._governance_enabled:
            from cograph_client.resolver.governance import GovernanceEngine, LLMJudgePanel
            self._governance = GovernanceEngine(neptune)
            self._judge_panel = LLMJudgePanel(self._openrouter_key)
        # Background governance tasks (COG-46): the judge panel + Public-layer
        # write are scheduled off the ingest path; references are retained
        # here so drain_governance() can await them deterministically.
        self._governance_tasks: list[asyncio.Task] = []
        # child->parent (type-name) map for subclass-chain walks. Built once per
        # ingest from parent_map_query and mutated in-place as new subtypes are
        # created so later entities in the same batch can climb the chain.
        # ONTA-268: on the reentrant ingest path this is threaded call-locally
        # (see `ingest`/`_resolve_and_insert` `parent_of=` params); the instance
        # attribute remains the fallback for legacy direct-call sites (the
        # `/ingest/csv/rows` route and unit tests that seed it directly).
        self._parent_of: dict[str, str] = {}
        # ONTA-370: A4 Verify policy — the OPT-IN gate for the verify seam wedged
        # between the A3 clean ledger and the write. DEFAULT None => verification
        # is OFF: the seam short-circuits with ZERO cost (no verifier, no
        # iteration, no LLM/network) and the write stays byte-identical. A caller
        # that resolves a `VerifyPolicy` for the tenant/type hands it in here to
        # turn the seam on; duck-typed (`object | None`) so this module never
        # imports the policy type — the shared `_policy_enabled` reads its
        # `mode`/`enabled`. Mirrors the other DEFAULT-OFF opt-ins above
        # (`_provenance_enabled` / `_attr_provenance_enabled`).
        self._verify_policy = verify_policy

    async def _locked_ontology_update(self, sparql: str) -> None:
        """Run a single ontology-mutating SPARQL update under the ontology-write
        lock (ONTA-268).

        Used for the scattered pass-2 ontology writes (attribute/range/promotion
        type creation) that are NOT already inside a lock-guarded region. MUST NOT
        be called from within `_resolve_type` (which already holds the lock —
        `asyncio.Lock` is not reentrant, so that would deadlock)."""
        async with self._ontology_lock:
            await self._neptune.update(sparql)

    def _verify_clean_facts(
        self,
        result: IngestResult,
        *,
        workspace_id: str | None,
        run_id: str | None,
    ) -> None:
        """A4 Verify seam (ONTA-370) — the OPT-IN wedge between the A3 clean
        ledger and the write.

        **DEFAULT-OFF is load-bearing.** With no ``VerifyPolicy`` configured (the
        default, ``self._verify_policy is None``) the very FIRST check
        short-circuits and returns: no verifier is constructed, ``result.clean_report``
        is not iterated, no LLM / network / cost / latency is incurred, and
        ``result.verified_facts`` stays its empty default. The written graph and
        the rest of the returned :class:`IngestResult` are therefore byte-identical
        to a build without this seam. Even the offline
        :class:`~cograph_client.verification.verifier.DefaultOfflineVerifier` is NOT
        run on the default path — "off" means the seam does nothing at all.
        Verification is strictly OPT-IN, exactly like the ``_provenance_enabled`` /
        ``_attr_provenance_enabled`` seams above.

        When a policy turns it ON, the A3 :class:`CleanFact`\\ s the ONTA-373 ledger
        already collected (passed + transformed + dropped) are handed to the shared
        Wave-6 orchestrator :func:`verify_clean_facts` under the run envelope's
        ``workspace_id`` / ``run_id`` (ONTA-372); the resulting
        :class:`~cograph_client.verification.types.VerifiedFact`\\ s (verdict +
        independent evidence + confidence + A4 lineage) are stamped on the result.
        Verification is READ-ONLY and sits BEFORE the write — it never forks the
        converged writer (:func:`insert_facts`); the facts still flow through the
        shared write path below unchanged.
        """
        policy = self._verify_policy
        # FIRST and ONLY thing evaluated on the default path. `_policy_enabled` is
        # the SAME gate the orchestrator uses, so the seam can't drift from it.
        if not _policy_enabled(policy):
            return
        # --- opt-in path only past this point ---
        report = result.clean_report
        a3_facts = [*report.passed, *report.transformed, *report.dropped]
        if not a3_facts:
            return
        # Thread the real run scope when we have it; fall back to the
        # orchestrator's own "local" defaults (its ArtifactEnvelope rejects an
        # empty workspace_id/run_id) when a direct caller threaded none.
        scope: dict[str, str] = {}
        if workspace_id:
            scope["workspace_id"] = workspace_id
        if run_id:
            scope["run_id"] = run_id
        try:
            result.verified_facts = verify_clean_facts(a3_facts, policy, **scope)
        except Exception:
            # A misbehaving verifier must never fail an otherwise-successful write:
            # the seam sits before the write but degrades to "no verdicts", never a
            # rollback (mirrors the best-effort embedding / free-text seams). The
            # FactVerifier contract already requires fail-closed; this is defense.
            logger.warning("verify_seam_failed", exc_info=True)

    def _verdict_companion_triples(
        self,
        result: IngestResult,
        entity_uri_map: dict[str, str],
        entity_type_map: dict[str, str],
    ) -> list[tuple[str, str, str]]:
        """A4 verdict PERSIST (ONTA-375) — stamp each verified fact's epistemic
        ``TruthVerdict`` as a per-attribute ``attr_meta/`` companion.

        DEFAULT-OFF passthrough: with no ``VerifyPolicy`` enabled the A4 seam left
        ``result.verified_facts`` empty (the common path), so this returns ``[]`` and
        the write is byte-identical — no companion is minted. When the seam produced
        verdicts, each written fact's ``TruthVerdict`` is minted via the SHARED
        companion minter (:func:`build_truth_verdict_companion`) onto an INTERNAL
        ``attr_meta/`` predicate (``is_internal_predicate`` True), so it is invisible
        to Explorer/type-stats/NL dumps yet stays queryable by the P7 answer layer.
        The triples ride the SAME shared write path (they are appended to the
        instance-triple collector) — never a bespoke insert.

        Skips DROPPED facts (``value is None`` — no domain triple was written, so
        there is nothing to attach a verdict to) and any fact whose entity did not
        resolve to a URI/type in this batch. The verdict is a per-attribute signal
        keyed by ``(subject, Type, attribute)`` — matching how the surface-form /
        display companions are keyed."""
        verified = getattr(result, "verified_facts", None)
        if not verified:
            return []
        out: list[tuple[str, str, str]] = []
        for vf in verified:
            if vf.value is None:  # DROPPED — no domain fact to annotate.
                continue
            entity_uri = entity_uri_map.get(vf.entity_id)
            type_name = entity_type_map.get(vf.entity_id)
            if not entity_uri or not type_name:
                continue
            verdict = vf.verdict.value if hasattr(vf.verdict, "value") else str(vf.verdict)
            out.extend(
                build_truth_verdict_companion(entity_uri, type_name, vf.attribute, verdict)
            )
        return out

    async def ingest(
        self,
        content: str,
        tenant_id: str,
        content_type: str = "text",
        source: str = "",
        instance_graph: str | None = None,
        constrain_types: list[str] | None = None,
        constrain_attributes: dict[str, list[str]] | None = None,
        constrain_soft: bool = False,
        run_id: str | None = None,
        observed_at: datetime | None = None,
        fact_ids: list[str] | None = None,
        tier: str | None = None,
    ) -> IngestResult:
        """Full ingestion pipeline: extract → resolve → validate → insert.

        Args:
            instance_graph: If set, instance data goes into this graph while
                ontology updates go into the tenant's base graph. This enables
                multiple KGs sharing one ontology.
            run_id: STABLE identity of this logical ingest run (ONTA-271). Every
                fact_id + the batch_id + the A6 Graph Delta derive from it, so a
                retry/replay that PRESERVES the run_id reproduces a byte-identical
                graph (P6 dedupes the replay instead of duplicating). Defaults to
                a fresh ``uuid4`` per call — today's behavior, and the "no stable
                id" control (each call is a distinct run, so nothing dedupes).
            observed_at: The run's ``onto/ingested_at`` timestamp (ONTA-271). A
                nonce if left to wall-clock, so it is threaded from the envelope
                and a replay REUSES it to stay byte-identical. Defaults to now.
            constrain_types: OPT-IN, DISCOVERY-ONLY (ONTA-199). When set, extraction
                is constrained to emit ONLY entities of these confirmed type(s).
                ``None`` (the default, and every document/CSV/text caller) keeps
                the fully open-ended multi-type extractor unchanged.
            constrain_attributes: OPT-IN, DISCOVERY-ONLY. Per-type allowed
                attribute names (snake_case) paired with ``constrain_types``. A
                type absent from this map is unrestricted on attributes. ``None``
                = no attribute restriction. Only meaningful alongside
                ``constrain_types``.
            fact_ids: OPT-IN A1→A2 lineage handoff (ONTA-371). The per-row A1
                ``fact_id`` of each row in this micro-batch, in row order, forwarded
                from the discovery capability's A1 Source Bundle. Recorded for
                lineage observability; the emitted graph is byte-identical (the A6
                delta still keys off ``run_id``) — a PASS-THROUGH of provenance, not
                a change to WHAT is written. ``None`` for every non-discovery
                caller — unchanged.
            tier: OPT-IN A1→A2 lineage (ONTA-371). The source authority tier the
                bundle rows came from (``authoritative`` / ``web``). Pass-through
                provenance only. ``None`` for non-discovery callers.
        """
        # Build the opt-in extraction constraint (ONTA-199). None / empty types →
        # inactive → every _extract prompt is byte-for-byte the open-ended default,
        # so document/CSV/text ingestion is provably unchanged.
        constraint: ExtractionConstraint | None = None
        if constrain_types:
            constraint = ExtractionConstraint(
                types=list(constrain_types),
                attributes={k: list(v) for k, v in (constrain_attributes or {}).items()},
                soft=constrain_soft,
            )
        # ONTA-371: record the A1→A2 lineage handoff (the discovery capability now
        # drives extraction from the A1 Source Bundle and forwards each row's A1
        # fact_id + source tier). Observability only — the emitted graph is
        # byte-identical (the A6 delta keys off run_id). Fires only when a
        # discovery run threads lineage; every other caller passes None → silent.
        if fact_ids or tier is not None:
            logger.debug(
                "a1_a2_lineage_handoff",
                path="ingest",
                run_id=run_id,
                source_fact_ids=len(fact_ids or ()),
                source_tier=tier,
            )
        graph_uri = tenant_graph_uri(tenant_id)
        # Ontology always goes to the base tenant graph
        # Instance data goes to instance_graph if specified, otherwise base graph.
        # ONTA-268 (reentrancy): the target instance graph is CALL-LOCAL and
        # threaded down the write path so two ingest() calls interleaving on ONE
        # shared resolver can't clobber each other's target (the leak
        # `qc/isolation.py::check_isolation` catches). The `self.` attribute is
        # written too, but only as the fallback legacy direct-call sites read; the
        # reentrant path below never reads it.
        target_instance_graph = instance_graph or graph_uri
        self._instance_graph = target_instance_graph
        # Set graph URI on type matcher so embedding pre-filter can find the right
        # store — threaded per-call to `match(graph_uri=...)` below; the attribute
        # stays a fallback only.
        self._type_matcher._graph_uri = graph_uri

        # Step 1: Fetch existing ontology (needed for extraction context)
        existing_types, existing_attrs = await self._fetch_ontology(graph_uri)
        # Build the child->parent subclass map once per ingest. Used to climb the
        # hierarchy for ER config selection and ancestor synthesis. Mutated
        # in-place as new subtypes are created during this ingest. CALL-LOCAL and
        # threaded (ONTA-268): a fresh dict per ingest, so concurrent ingests each
        # mutate their own map; `self._parent_of` remains the legacy fallback.
        parent_of = await self._fetch_parent_map(graph_uri)
        self._parent_of = parent_of

        # ONTA-270: fingerprint the ontology snapshot THIS run (P5) planned
        # against. Stamped onto the A5 placement plan and threaded into the apply
        # (`_resolve_and_insert`), where P6 rejects/recomputes it if a concurrent
        # run advances the ontology during the (long, async) extraction below.
        # Computed here, right after the snapshot read, so it captures exactly the
        # state every downstream placement decision is made against.
        plan_ontology_version = ontology_version(existing_types, existing_attrs, parent_of)

        # Stage timing (ONTA-198 follow-up): time the two heavy halves of an
        # ingest — LLM EXTRACTION vs type-RESOLUTION+insert — so a slow run reveals
        # which half dominates without hand-reconstructing it from request gaps.
        _t_extract = time.monotonic()

        # CSV: use schema-inference pipeline (1 LLM call for schema, deterministic for rows)
        if content_type == "csv":
            return await self._ingest_csv(
                content, graph_uri, existing_types, existing_attrs, source,
                instance_graph=target_instance_graph, parent_of=parent_of,  # ONTA-268
            )

        # Text/JSON: chunk and process
        from cograph_client.resolver.chunker import (
            chunk_text,
            chunk_json_array,
            json_array_len,
        )
        is_json = content_type in ("json", "jsonl")
        if is_json:
            # Token-budget batching (ONTA-196): size each batch so its predicted
            # reified output stays under a fraction of THIS resolver's extraction
            # cap, so the common dense-record case extracts first-try instead of
            # overflowing max_tokens and dropping into the slow split-and-retry
            # recovery (which remains the safety net below).
            chunks = chunk_json_array(content, max_tokens=self.EXTRACT_MAX_TOKENS)
        else:
            chunks = chunk_text(content)

        # Row-conservation accounting for the JSON path (ADR 0003 §2): a chunk
        # whose extraction yields nothing (e.g. truncated output) must not vanish
        # silently. We count records IN and records DROPPED so the run can never
        # be presented as complete while a whole batch was lost.
        rows_in = 0
        rows_dropped = 0

        # ONTA-199: forward the constraint kwarg to ``_extract`` ONLY when it's
        # active. The default document path then calls ``_extract`` with the EXACT
        # argument shape it had before this change, so existing tests that patch
        # ``_extract`` with a mock lacking a ``constraint`` parameter still pass
        # (the no-op path never sends the kwarg). Real methods below
        # (``_extract_json_chunk_with_recovery`` / ``_extract_json_chunks_calibrated``)
        # always accept ``constraint`` so they take it directly.
        _extract_c = {"constraint": constraint} if constraint is not None else {}

        if len(chunks) <= 1:
            # Small content — single extraction. JSON STILL routes through the
            # truncation-recovery helper (FIX 1): even one chunk's reified output
            # (each row → Model + reified Score + Organization + relationships) can
            # exceed max_tokens and get truncated, and bare _extract would then
            # silently return ZERO entities for the whole pull. Recovery splits +
            # retries down to the floor so a single chunk can't vanish.
            if is_json:
                rows_in = json_array_len(content)
                extraction, dropped = await self._extract_json_chunk_with_recovery(
                    content, existing_types, constraint=constraint,
                )
                rows_dropped += dropped
            else:
                extraction = await self._extract(
                    content, content_type, existing_types, **_extract_c,
                )
        elif is_json:
            # Multiple JSON chunks: first-batch CALIBRATION (ONTA-197 item 2) +
            # bounded CONCURRENCY (item 3), composed. The two features compose
            # naturally because calibration NEEDS chunk 1's result before it can
            # re-size the rest:
            #   1. Extract chunk 1 sequentially (with recovery).
            #   2. Measure its REAL output-tokens-per-record and RE-CHUNK the
            #      not-yet-processed remainder ONCE with the observed ratio — the
            #      conservative ONTA-196 default only ever sized the FIRST batch,
            #      so sparse records get ~4-7x bigger (still cap-safe) batches now.
            #   3. Extract the re-chunked remainder CONCURRENTLY under a semaphore,
            #      preserving order and per-chunk recovery + drop accounting.
            extraction, chunk_rows_in, chunk_dropped = (
                await self._extract_json_chunks_calibrated(
                    chunks, content, existing_types, constraint=constraint,
                )
            )
            rows_in += chunk_rows_in
            rows_dropped += chunk_dropped
        else:
            # Multiple TEXT chunks — independent, no token-budget calibration
            # (calibration is a JSON-record concept). Extract concurrently under
            # the same semaphore, then merge in deterministic chunk order.
            results = await self._extract_chunks_concurrently(
                [
                    lambda c=chunk: self._extract(
                        c, content_type, existing_types, **_extract_c,
                    )
                    for chunk in chunks
                ]
            )
            merged_entities = []
            merged_relationships = []
            seen_ids: set[str] = set()
            for extraction in results:
                for e in extraction.entities:
                    if e.id not in seen_ids:
                        merged_entities.append(e)
                        seen_ids.add(e.id)
                merged_relationships.extend(extraction.relationships)
            extraction = ExtractionResult(
                entities=merged_entities,
                relationships=merged_relationships,
                source_text=content[:500],
            )

        # ONTA-255: SOFT-mode focus-type floor over the FULLY-MERGED extraction.
        # This is the AUTHORITATIVE pass (allow_strip=True): the per-chunk backstop
        # in `_apply_extraction_constraint` only RE-HOMES within a chunk and never
        # strips, so a metric whose subject sits in a different chunk survives to
        # here. With the whole batch in view this pass re-homes such a metric onto
        # the right subject (or, if no subject exists anywhere, strips it off the
        # concept node and logs the loss / starvation — nothing silent). It also
        # covers callers/tests that stub `_extract` wholesale. Idempotent: once the
        # metrics are off the concept nodes, this is a no-op.
        if constraint is not None and constraint.is_active and constraint.soft:
            extraction = _apply_soft_focus_floor(extraction, constraint)
            # A2 zero-ontology-commitment contract (ONTA-272): the soft-typed
            # candidate facts must carry NO committed ontology reference in any type
            # slot (soft lineage is fine — it is P5's suggestion, not a commitment).
            # OBSERVE-ONLY here: imperfect LLM output must never HARD-fail a run, so
            # a violation is logged, not raised (the deterministic pre-structured
            # fast path asserts the same contract FATALLY, where it can only be a bug).
            _a2_violations = validate_soft_a2(extraction)
            if _a2_violations:
                logger.warning(
                    "soft_a2_contract_violation",
                    count=len(_a2_violations),
                    sample=_a2_violations[:3],
                )

        logger.info(
            "extraction_complete",
            entities=len(extraction.entities),
            relationships=len(extraction.relationships),
            rows_in=rows_in,
            rows_dropped=rows_dropped,
        )
        logger.info(
            "stage_timing",
            stage="extract",
            duration_ms=round((time.monotonic() - _t_extract) * 1000, 1),
            entities=len(extraction.entities),
            rows_in=rows_in,
        )

        if not extraction.entities:
            return IngestResult(
                entities_extracted=0, rows_in=rows_in, rows_dropped=rows_dropped,
            )

        # Step 3: Resolve types and attributes, validate, insert
        # ONTA-271: STABLE run identity. run_id defaults to a fresh uuid4 (a
        # distinct run per call — today's behavior), but a caller that preserves
        # it across a retry makes the whole write replay-deterministic. batch_id
        # is DERIVED from run_id (was a bare uuid4) so a replay reuses the same
        # batch token → the BATCH_PREDICATE triple is idempotent instead of a
        # per-call nonce; rollback-by-batch is unchanged (still a unique token
        # per distinct run). observed_at feeds onto/ingested_at (see
        # _resolve_and_insert_entity), threaded so a replay reuses it.
        run_id = run_id or str(uuid4())
        observed_at = observed_at or datetime.now(timezone.utc)
        batch_id = derive_fact_id(run_id=run_id, stage="A6-batch")
        result = IngestResult(
            entities_extracted=len(extraction.entities),
            batch_id=batch_id,
            rows_in=rows_in,
            rows_dropped=rows_dropped,
        )
        entity_uri_map: dict[str, str] = {}  # entity id → URI
        entity_type_map: dict[str, str] = {}  # entity id → resolved type name

        _t_resolve = time.monotonic()
        try:
            final = await self._resolve_and_insert(
                extraction, graph_uri, existing_types, existing_attrs,
                source, result, entity_uri_map, entity_type_map, batch_id,
                # ONTA-177: text/JSON/web-discovery ingest IS the schema pass
                # for these modalities (extract + apply happen in one call),
                # so free-text candidacy is decided here.
                decide_text_candidacy=True,
                # ONTA-268: thread the call-local target graph + parent map so
                # the write path never reads shared `self.` state.
                instance_graph=target_instance_graph,
                parent_of=parent_of,
                # ONTA-270: the version P5 stamped the plan at, so P6 (the apply
                # inside `_resolve_and_insert`) can reject/recompute a stale plan.
                ontology_version_stamp=plan_ontology_version,
                # ONTA-271: stable run identity + the run's ingested_at stamp,
                # threaded call-local (like instance_graph/parent_of) so the A6
                # Graph Delta and every fact_id are replay-deterministic.
                run_id=run_id,
                observed_at=observed_at,
                # ONTA-370/372: the workspace scope for the A4 Verify seam's run
                # envelope. `tenant_id` IS the product-facing `workspace_id`
                # (ADR 0011 §3 — pipeline code says workspace_id). Only consumed
                # when a VerifyPolicy turns the seam on; ignored on the default path.
                workspace_id=tenant_id,
            )
            logger.info(
                "stage_timing",
                stage="resolve_insert",
                duration_ms=round((time.monotonic() - _t_resolve) * 1000, 1),
                entities=final.entities_resolved,
                types_created=len(final.types_created),
            )
            # Never present a run as complete while a whole chunk was lost to
            # truncation (FIX 1): a non-zero drop count after recovery is an
            # ERROR-level signal carried back on the result for the caller.
            if final.rows_dropped:
                logger.error(
                    "ingest_rows_dropped",
                    batch_id=batch_id,
                    rows_in=final.rows_in,
                    rows_dropped=final.rows_dropped,
                )
            return final
        except Exception:
            logger.error(
                "ingest_failed_rolling_back",
                batch_id=batch_id,
                entities_so_far=result.entities_resolved,
                exc_info=True,
            )
            instance_graph = target_instance_graph  # ONTA-268: call-local, not self
            try:
                sparql = delete_batch_query(instance_graph, batch_id)
                await self._neptune.update(sparql)
                logger.info("batch_rollback_complete", batch_id=batch_id)
            except Exception:
                logger.error("batch_rollback_failed", batch_id=batch_id, exc_info=True)
            raise

    async def _resolve_and_insert(
        self,
        extraction: ExtractionResult,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        source: str,
        result: IngestResult,
        entity_uri_map: dict[str, str],
        entity_type_map: dict[str, str],
        batch_id: str,
        decide_text_candidacy: bool = False,
        key_join: KeyJoin | None = None,
        *,
        instance_graph: str | None = None,
        parent_of: dict[str, str] | None = None,
        ontology_version_stamp: str | None = None,
        run_id: str | None = None,
        observed_at: datetime | None = None,
        workspace_id: str | None = None,
    ) -> IngestResult:
        """Inner pipeline: resolve entities, insert triples. Separated for rollback.

        Two-pass architecture for I/O efficiency:
          Pass 1: Resolve types for all entities, compute URIs
          Batch check: Which URIs already exist in Neptune (one query per 500)
          Pass 2: Resolve attributes, validate, insert triples

        ``decide_text_candidacy`` (ONTA-177): when True, string-attribute
        values are sampled during pass 2 and free-text candidacy is decided +
        persisted as ``textKind`` ontology markers after the write — set by
        :meth:`ingest` (text/JSON/web-discovery), where this call IS the
        schema pass. Deliberately OFF by default: ``/ingest/csv/rows`` calls
        this method with a client-supplied mapping and never runs a schema
        pass — its contract is "no LLM call", and its candidacy is covered
        later by a reconciler-side default heuristic (ONTA-181).

        ``instance_graph`` / ``parent_of`` (ONTA-268, reentrancy): CALL-LOCAL
        overrides threaded from :meth:`ingest`. When ``None`` (legacy direct
        callers — the ``/ingest/csv/rows`` route sets ``self._instance_graph``
        and unit tests seed ``self._parent_of``) they fall back to the instance
        attributes. On the reentrant ``ingest`` path they carry per-call state so
        two interleaved ingests never read each other's target graph / parent map.

        ``ontology_version_stamp`` (ONTA-270): the ontology fingerprint
        :meth:`ingest` computed for the A5 placement plan. When set, the apply is
        an optimistic-concurrency P6: before pass 1 computes any placement we
        reconcile the stamp against the CURRENT ontology and reject-and-recompute
        a stale plan (see :meth:`_reconcile_ontology_version`). ``None`` (legacy
        direct callers) skips the guard, preserving today's behavior exactly.

        ``run_id`` / ``observed_at`` (ONTA-271): stable run identity + the run's
        ingested_at stamp, threaded call-local (alongside ``instance_graph`` /
        ``parent_of``). ``observed_at`` is passed to each per-entity write so the
        ``onto/ingested_at`` triple is replay-stable; ``run_id`` keys the A6
        :class:`GraphDelta` receipt built on ``result.graph_delta`` at the end,
        so a preserved-run_id replay reproduces a byte-identical delta. Both
        ``None`` (legacy direct callers) → no delta, wall-clock ingested_at.
        """
        instance_graph = (
            instance_graph if instance_graph is not None
            else getattr(self, "_instance_graph", graph_uri)
        )
        parent_of = self._parent_of if parent_of is None else parent_of
        # ONTA-270: P6 optimistic-concurrency guard. If a concurrent run advanced
        # the ontology while we were extracting, the snapshot this plan was
        # computed against is STALE and applying it verbatim mints duplicate
        # terms; reconcile brings the in-place snapshot current so pass 1 resolves
        # against the new version. No-op (single cheap read) when nothing raced.
        if ontology_version_stamp is not None:
            await self._reconcile_ontology_version(
                graph_uri, ontology_version_stamp,
                existing_types, existing_attrs, parent_of,
            )
        # ONTA-177: (resolved_type, attr_name) -> sampled string values,
        # filled by _resolve_and_insert_entity during pass 2.
        text_values: dict[tuple[str, str], list[str]] | None = (
            {} if decide_text_candidacy else None
        )

        # Pass 1: Resolve types and compute entity URIs
        resolved_types: dict[str, str] = {}  # entity.id → resolved_type
        pending_uris: list[str] = []
        # ER index triples (block keys + denormalized signals) for newly minted
        # entities. Empty for merged/dedup'd entities.
        er_index_triples: list[tuple[str, str, str]] = []
        # Genuine independent co-classifications per entity id (ADR rule 1).
        # Empty for the common single-type case.
        entity_also_types: dict[str, list[str]] = {}
        # Track which entity IDs were merged into existing URIs (for telemetry)
        er_merged_count = 0
        for i, entity in enumerate(extraction.entities):
            if i > 0 and i % self.ONTOLOGY_REFRESH_INTERVAL == 0:
                await self._refresh_ontology(graph_uri, existing_types, existing_attrs)

            resolved_type = await self._resolve_type(
                entity, graph_uri, existing_types, existing_attrs, result,
                parent_of=parent_of,
            )
            if resolved_type:
                resolved_types[entity.id] = resolved_type
                # Resolve genuine co-types so they exist in the ontology; record
                # them for the multi-type write in pass 2. The declared primary
                # type (resolved_type) still owns URI minting + ER.
                also = await self._resolve_also_types(
                    entity, resolved_type, graph_uri, existing_types, existing_attrs, result,
                    parent_of=parent_of,
                )
                if also:
                    entity_also_types[entity.id] = also
                entity_uri = _entity_uri(resolved_type, entity.id)

                # Cross-file ER: see if this entity matches an existing one.
                # Failures here MUST never block ingest — log and fall through.
                if self._er_enabled:
                    try:
                        from cograph_client.resolver.er import MergeAction, config_for_with_hierarchy
                        # Climb the subclass chain so a granular leaf (HotelGuest)
                        # inherits a configured ancestor's (Guest) ER config and
                        # ER fires on the subtype.
                        er_config = config_for_with_hierarchy(resolved_type, parent_of)
                        er_applies = er_config is not None
                        type_uri = f"https://cograph.tech/types/{resolved_type}"
                        decision = await self._er.find_match(
                            entity, resolved_type, type_uri, instance_graph,
                            config=er_config, parent_of=parent_of,
                        )
                        if decision.action == MergeAction.AUTO_MERGE and decision.canonical_uri:
                            entity_uri = decision.canonical_uri
                            er_merged_count += 1
                            # Merge expansion: write the incoming entity's
                            # ER signals onto the CANONICAL URI so future
                            # ingests can find this same person via the new
                            # signals (e.g. a CRM merge adds the secondary
                            # email as an alias of the canonical Guest,
                            # letting a Loyalty ingest match later via that
                            # email). Triples are idempotent on Neptune.
                            normalized, keys = self._er.signals_and_keys(entity)
                            if normalized and keys:
                                er_index_triples.extend(
                                    self._er._blocker.index_triples(entity_uri, normalized, keys)
                                )
                        else:
                            # No match — mint a new URI. For ER-enabled types
                            # we add a short signal-hash suffix so two unrelated
                            # humans sharing a name (e.g. two distinct John
                            # Smiths) get distinct URIs and don't quietly
                            # contaminate each other's signal store.
                            if er_applies:
                                import hashlib
                                normalized, keys = self._er.signals_and_keys(entity)
                                if normalized is not None:
                                    fingerprint_parts = [
                                        normalized.email or "",
                                        normalized.phone_e164 or "",
                                        normalized.dob_iso or "",
                                        "|".join(normalized.email_aliases),
                                    ]
                                    fp = hashlib.sha1("|".join(fingerprint_parts).encode("utf-8")).hexdigest()[:8]
                                    entity_uri = f"{entity_uri}-{fp}"
                                if normalized and keys:
                                    er_index_triples.extend(
                                        self._er._blocker.index_triples(entity_uri, normalized, keys)
                                    )
                            else:
                                normalized, keys = self._er.signals_and_keys(entity)
                                if normalized and keys:
                                    er_index_triples.extend(
                                        self._er._blocker.index_triples(entity_uri, normalized, keys)
                                    )
                    except Exception as e:
                        logger.warning("er_pipeline_failed", error=str(e), entity_id=entity.id)

                entity_uri_map[entity.id] = entity_uri
                entity_type_map[entity.id] = resolved_type
                pending_uris.append(entity_uri)
        if er_merged_count:
            logger.info("er_merged_entities", count=er_merged_count, total=len(extraction.entities))

        # ONTA-250 join-by-exact-key: rebind each row-entity whose key value
        # matches an EXISTING entity onto that node's URI, so the existence check
        # below sees a duplicate (Pass 2 merges attributes, skips a second
        # rdf:type/label) instead of minting a parallel node. Runs AFTER ER so a
        # caller-declared exact key wins over signal-based minting. Returns the ids
        # to SKIP (unmatched with mint_unmatched=false).
        skip_ids: set[str] = set()
        if key_join is not None:
            skip_ids = await self._resolve_key_join(
                extraction.entities, resolved_types, entity_uri_map,
                instance_graph, key_join, result,
            )
            # Only URIs we will actually write get the existence check.
            pending_uris = [
                entity_uri_map[e.id]
                for e in extraction.entities
                if e.id in resolved_types and e.id not in skip_ids
            ]

        # Batch existence check: one SPARQL query per 500 URIs instead of N individual ASKs
        existing_uris: set[str] = set()
        BATCH_CHECK_SIZE = 500
        for i in range(0, len(pending_uris), BATCH_CHECK_SIZE):
            batch = pending_uris[i : i + BATCH_CHECK_SIZE]
            sparql = batch_entity_exists_query(instance_graph, batch)
            found = await self._neptune.batch_exists(sparql)
            existing_uris.update(found)
        if existing_uris:
            logger.info("batch_dedup_found", existing=len(existing_uris), total=len(pending_uris))

        # Pass 2: Resolve attributes, validate, collect triples
        # All entity triples are collected into one list, then batch-inserted
        # in a single call. This is ~10-50x faster than per-entity INSERT.
        all_entity_triples: list[tuple[str, str, str]] = []
        # Provenance collector (COG-46): statement-metadata triples for the
        # COMPANION provenance graph accumulate here during entity processing
        # and flush in one batched INSERT below, instead of one awaited
        # Neptune update per entity. Stays empty unless the flag is on.
        all_provenance_triples: list[tuple[str, str, str]] = []
        for entity in extraction.entities:
            if entity.id not in resolved_types:
                continue
            if entity.id in skip_ids:
                continue  # key-join unmatched with mint_unmatched=false
            resolved_type = resolved_types[entity.id]
            entity_uri = entity_uri_map[entity.id]
            is_duplicate = entity_uri in existing_uris

            if is_duplicate:
                result.entities_deduplicated += 1

            await self._resolve_and_insert_entity(
                entity, resolved_type, entity_uri, is_duplicate,
                graph_uri, existing_types, existing_attrs, source, result, batch_id,
                _collect_triples=all_entity_triples,
                _collect_provenance=all_provenance_triples,
                also_types=entity_also_types.get(entity.id),
                _collect_text_values=text_values,
                # ONTA-259: this is the model-proposed extraction path (text /
                # JSON / web-discovery), the only rail where an LLM can invent an
                # identifier value — enable the anti-fabrication backstop here.
                # The CSV path (`_ingest_mapped`) leaves it off: cells are
                # authoritative and written verbatim.
                drop_placeholder_values=True,
                instance_graph=instance_graph,  # ONTA-268: call-local target
                observed_at=observed_at,  # ONTA-271: replay-stable ingested_at
            )

        # Append ER index triples (block keys + denormalized signals) to the
        # same batch so future ingests can find these entities in O(1).
        if er_index_triples:
            all_entity_triples.extend(er_index_triples)

        # ONTA-370: A4 Verify seam — the OPT-IN wedge between the A3 clean ledger
        # (`result.clean_report`, complete now that the per-entity loop above has
        # run) and the write below. DEFAULT-OFF: with no VerifyPolicy configured
        # this short-circuits BEFORE constructing a verifier or iterating facts, so
        # the `insert_facts` write and the returned result are byte-identical. When
        # a policy turns it on, it stamps VerifiedFacts on the result. It sits
        # before the write and is read-only — it never forks the converged writer.
        self._verify_clean_facts(result, workspace_id=workspace_id, run_id=run_id)

        # ONTA-375: PERSIST each A4 verdict as an attr_meta/ companion. DEFAULT-OFF
        # no-op (empty verified_facts ⇒ [] ⇒ byte-identical write). When the seam ran,
        # the verdict companions are appended to the SAME instance-triple collector,
        # so they flow through the shared insert_facts write below (never a bespoke
        # insert) onto an internal predicate, invisible to every user surface but
        # queryable by the P7 answer layer.
        verdict_companions = self._verdict_companion_triples(
            result, entity_uri_map, entity_type_map,
        )
        if verdict_companions:
            all_entity_triples.extend(verdict_companions)
            result.triples_inserted += len(verdict_companions)

        # Single shared write path (graph/kg_writer.py) — the SAME function the
        # enrichment writer uses: batched instance-triple insert + the companion
        # provenance graph, in one place, so ingestion and enrichment can never
        # drift on HOW facts are written. (Per-fact provenance is flushed in one
        # batched INSERT per ingest, COG-46 — the exact triples a per-entity
        # write would produce; only the write pattern is batched.)
        if all_entity_triples or all_provenance_triples:
            # instance_graph resolved once at method top (ONTA-268 call-local).
            await insert_facts(
                self._neptune,
                instance_graph,
                all_entity_triples,
                provenance_triples=all_provenance_triples or None,
            )

        # ONTA-177: decide + persist free-text candidacy for the attributes this
        # schema pass touched — written alongside the other attribute upserts of
        # pass 2, best-effort (never blocks or fails ingest).
        if text_values:
            await self._mark_free_text_attributes(graph_uri, text_values, result)

        # Incrementally embed newly created types for future embedding pre-filter matches
        if result.types_created and self._embedding_service is not None:
            try:
                await self._embedding_service.embed_types(
                    graph_uri, result.types_created, self._neptune,
                )
                logger.info("embedded_new_types", count=len(result.types_created))
            except Exception:
                logger.warning("embed_new_types_failed", exc_info=True)

        # Step 4: Insert relationships (instance triples to instance graph, ontology to base graph)
        # instance_graph resolved once at method top (ONTA-268 call-local).
        rel_triples: list[tuple[str, str, str]] = []
        for rel in extraction.relationships:
            # An edge whose source or target was skipped (key-join unmatched with
            # mint_unmatched=false) has no node to hang off — drop it.
            if rel.source_id in skip_ids or rel.target_id in skip_ids:
                continue
            source_uri = entity_uri_map.get(rel.source_id)
            target_uri = entity_uri_map.get(rel.target_id)
            if source_uri and target_uri:
                # Normalize predicate against existing predicates on this type
                source_type = entity_type_map.get(rel.source_id)
                existing_preds = set()
                if source_type:
                    for attr_name, schema in existing_attrs.get(source_type, {}).items():
                        if schema.datatype not in PRIMITIVE_TYPES:
                            existing_preds.add(attr_name)
                canonical_pred = normalize_predicate(rel.predicate, existing_preds)

                predicate = f"https://cograph.tech/onto/{canonical_pred}"
                rel_triples.append((source_uri, predicate, target_uri))

                # Register relationship as object property in ontology
                target_type = entity_type_map.get(rel.target_id)
                if source_type and target_type:
                    type_attrs = existing_attrs.get(source_type, {})
                    existing = type_attrs.get(canonical_pred)
                    if existing is None:
                        sparql = insert_attribute(
                            graph_uri, source_type, canonical_pred, "", target_type,
                        )
                        await self._locked_ontology_update(sparql)  # ONTA-268
                        result.attributes_added.append(f"{source_type}.{canonical_pred}")
                        existing_attrs.setdefault(source_type, {})[canonical_pred] = AttributeSchema(
                            name=canonical_pred, datatype=target_type,
                        )
                    elif existing.datatype in PRIMITIVE_TYPES:
                        # First seen as a primitive attribute, now carrying an
                        # entity object: upgrade its ontology range to the target
                        # type so the schema-only Explorer overview draws the edge
                        # (the detail view already shows it from instance data).
                        await self._locked_ontology_update(  # ONTA-268
                            set_object_property_range(
                                graph_uri, source_type, canonical_pred, target_type,
                            )
                        )
                        existing_attrs[source_type][canonical_pred] = AttributeSchema(
                            name=canonical_pred, datatype=target_type,
                        )

        # Batch insert relationship triples
        if rel_triples:
            for sparql in batched_insert_triples(instance_graph, rel_triples):
                await self._neptune.update(sparql)
            result.triples_inserted += len(rel_triples)

        result.entities_resolved = len(entity_uri_map)

        # ONTA-271: emit the run's deterministic A6 Graph Delta receipt. Built
        # over the COMPLETE set of instance facts this run wrote (entity triples
        # + relationship triples), via the shared `build_graph_delta` — the same
        # projection `insert_facts` returns for its own portion. We assemble it
        # HERE rather than take `insert_facts`'s return because relationship
        # triples are written after it (through `batched_insert_triples`, not a
        # second `insert_facts` call), so only the run owner sees every fact.
        # Nonces (ingested_at/batch_id) are projected out and each fact is keyed
        # by its stable fact_id, so a preserved-run_id replay reproduces byte-
        # identical bytes and P6 dedupes it. `fan_in` records source facts that
        # merged onto one node (ER auto-merge, key-join, in-run same-key dedup):
        # >1 source entity id resolving to the SAME final URI is a merge, so the
        # non-canonical sources' natural URIs map to the shared node.
        if run_id is not None:
            ids_by_uri: dict[str, list[str]] = {}
            for eid, uri in entity_uri_map.items():
                ids_by_uri.setdefault(uri, []).append(eid)
            fan_in: dict[str, str] = {}
            for uri, eids in ids_by_uri.items():
                if len(eids) > 1:
                    for eid in eids:
                        natural = _entity_uri(entity_type_map.get(eid, ""), eid)
                        if natural != uri:
                            fan_in[natural] = uri
            result.graph_delta = build_graph_delta(
                instance_graph,
                all_entity_triples + rel_triples,
                run_id=run_id,
                fan_in=fan_in,
            ).to_dict()

        logger.info(
            "ingest_complete",
            entities_resolved=result.entities_resolved,
            triples_inserted=result.triples_inserted,
            types_created=result.types_created,
            rejections=len(result.rejections),
        )
        return result

    async def _ingest_csv(
        self,
        content: str,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        source: str,
        *,
        instance_graph: str | None = None,
        parent_of: dict[str, str] | None = None,
    ) -> IngestResult:
        """CSV ingestion: 1 LLM call for schema inference, deterministic mapping for all rows.

        ``instance_graph`` / ``parent_of`` (ONTA-268): CALL-LOCAL overrides
        threaded from :meth:`ingest` down through :meth:`_ingest_mapped`."""
        import csv
        import io
        from cograph_client.resolver.csv_resolver import CSVResolver

        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
        if not rows:
            return IngestResult(entities_extracted=0)

        headers = list(rows[0].keys())
        logger.info("csv_ingest_start", rows=len(rows), columns=len(headers))

        # Step 1: Infer schema from sample (1 LLM call)
        csv_resolver = CSVResolver(self._anthropic, self._openrouter_key)
        mapping = await csv_resolver.infer_schema(headers, rows[:10], existing_types, total_rows=len(rows))

        # Step 2+: apply the mapping and run the shared resolve→dedup→insert
        # tail (also reused by web-discovery ingest via ingest_mapped_records).
        return await self._ingest_mapped(
            mapping, rows, graph_uri, existing_types, existing_attrs, source,
            instance_graph=instance_graph, parent_of=parent_of,
        )

    async def ingest_mapped_records(
        self,
        rows: list[dict[str, str]],
        mapping: CSVSchemaMapping,
        tenant_id: str,
        source: str = "",
        instance_graph: str | None = None,
        key_join: KeyJoin | None = None,
        run_id: str | None = None,
    ) -> IngestResult:
        """Ingest pre-mapped records (no schema inference) — the fixed-mapping seam.

        A caller infers a :class:`CSVSchemaMapping` once (e.g. from a sample at
        plan time) and applies that SAME mapping to the full record set here. The
        mapping is applied DETERMINISTICALLY (no LLM, no re-inference), so the
        schema previewed to the user is exactly the schema committed
        (preview == commit). This is the CSV path's guarantee; the web-DISCOVERY
        path instead routes through :meth:`ingest` (the non-deterministic
        ``_extract``), where the previewed shape is only a sample-based estimate,
        not an exact match. Records flow through the identical type-resolution,
        batch existence-dedup, ER and batch-insert path CSV ingest uses.

        Mirrors :meth:`ingest`'s per-call setup (instance graph, type-matcher
        graph URI, ontology + parent-map fetch) so it can be called standalone,
        not only inside the CSV pipeline.

        ``run_id`` (ONTA-372): the run-scoped lineage id, forwarded to
        :meth:`_ingest_mapped`. When set (the discovery structured fast-path), the
        batch_id is derived from it and an A6 Graph Delta keyed to it is emitted on
        the result — the SAME run identity the A1 Source Bundle carries. ``None``
        (the CSV route) keeps the fresh-uuid4-per-call behavior, unchanged.
        """
        graph_uri = tenant_graph_uri(tenant_id)
        # Ontology always goes to the base tenant graph; instance data goes to
        # instance_graph when a specific KG is targeted, else the base graph.
        # ONTA-268: CALL-LOCAL target + parent map threaded down the write path;
        # the `self.` attributes stay as the legacy fallback only.
        target_instance_graph = instance_graph or graph_uri
        self._instance_graph = target_instance_graph
        self._type_matcher._graph_uri = graph_uri
        existing_types, existing_attrs = await self._fetch_ontology(graph_uri)
        parent_of = await self._fetch_parent_map(graph_uri)
        self._parent_of = parent_of
        return await self._ingest_mapped(
            mapping, rows, graph_uri, existing_types, existing_attrs, source,
            key_join=key_join,
            instance_graph=target_instance_graph, parent_of=parent_of,
            run_id=run_id,
        )

    async def ingest_structured_rows(
        self,
        rows: list[dict],
        tenant_id: str,
        type_name: str,
        attributes: list[str] | None = None,
        source: str = "",
        instance_graph: str | None = None,
        key_attribute: str | None = None,
        key_join: KeyJoin | None = None,
        run_id: str | None = None,
        fact_ids: list[str] | None = None,
        tier: str | None = None,
    ) -> IngestResult:
        """FAST-PATH for PRE-STRUCTURED rows (ONTA-272) — no unstructured LLM ``_extract``.

        Pre-structured payloads (an API-registry pull with a known field mapping, a
        structured / extension capture) already arrive as clean rows keyed by the
        confirmed attribute set, so running the open-ended LLM extractor over them
        is a nonsensical, non-deterministic detour. This commits them through the
        SAME deterministic mapping seam CSV ingest uses (:meth:`ingest_mapped_records`
        → ``apply_mapping``, NO LLM): a fixed :class:`CSVSchemaMapping` with one
        column per field and the key attribute as the type-id.

        Before committing it materializes the SOFT-TYPED A2 witness for the rows
        (:func:`soft_a2_from_structured_rows`) and ASSERTS the zero-ontology-
        commitment contract (:func:`assert_soft_a2`) — pre-structured rows are
        inherently soft (candidate type, literal attributes, evidence = the
        per-record ``source_url``), so this can only fire on a genuine bug (fail
        fast). ``require_evidence`` is asserted only when the rows actually carry a
        ``source_url``, so a provenance-less structured source is not force-failed.
        Returns the SAME :class:`IngestResult` the deterministic path produces.

        ``run_id`` (ONTA-372): the run-scoped lineage id threaded from the discovery
        P1 entry (``web_ingest_cap``). Forwarded through
        :meth:`ingest_mapped_records` so the structured fast-path keys its batch_id
        and A6 Graph Delta off the SAME run as the A1 Source Bundle instead of a
        fresh uuid4. ``None`` (the default) preserves today's per-call behavior.

        ``fact_ids`` / ``tier`` (ONTA-371): the OPT-IN A1→A2 lineage handoff — the
        per-row A1 ``fact_id`` (row order) + source authority tier forwarded from
        the discovery capability's A1 Source Bundle. Recorded for lineage
        observability; the committed graph is byte-identical (the deterministic
        mapping seam is untouched). ``None`` for the CSV / non-discovery route.
        """
        if not rows:
            return IngestResult(rows_in=0)
        # ONTA-371: record the A1→A2 lineage handoff for the structured fast-path.
        # Observability only — the deterministic mapping write below is unchanged.
        if fact_ids or tier is not None:
            logger.debug(
                "a1_a2_lineage_handoff",
                path="ingest_structured_rows",
                run_id=run_id,
                source_fact_ids=len(fact_ids or ()),
                source_tier=tier,
            )
        # The key field is the join/identity column: an explicit key_attribute, else
        # the first confirmed attribute, else the row's natural "name".
        key_field = key_attribute or (attributes[0] if attributes else None) or "name"
        # A2 CONTRACT (zero ontology commitment): render the pre-structured rows as
        # candidate facts and assert soft-typed-only (+ evidence-linked where
        # provenance exists) at the point A2 is emitted.
        witness = soft_a2_from_structured_rows(rows, type_name, key_field=key_field)
        # Require evidence only when EVERY row carries a source_url — a
        # provenance-less (or mixed) structured source must never be force-failed by
        # the fatal assert; it still asserts soft-typed-only. Discovery micro-batches
        # are partitioned by source_url upstream, so the common case is all-or-none.
        require_evidence = bool(rows) and all(
            isinstance(r, dict) and str(r.get("source_url") or "").strip()
            for r in rows
        )
        assert_soft_a2(witness, require_evidence=require_evidence)
        mapping = _structured_rows_mapping(rows, type_name, key_field)
        return await self.ingest_mapped_records(
            rows, mapping, tenant_id, source=source,
            instance_graph=instance_graph, key_join=key_join,
            run_id=run_id,
        )

    async def _resolve_key_join(
        self,
        entities: list[ExtractedEntity],
        resolved_types: dict[str, str],
        entity_uri_map: dict[str, str],
        instance_graph: str,
        key_join: KeyJoin,
        result: IngestResult,
    ) -> set[str]:
        """Join-by-exact-key (ONTA-250): rebind each row-entity whose key value
        matches an EXISTING entity onto that existing node's URI, so Pass 2 merges
        the row's attributes onto it (via the shared write path) instead of
        minting a duplicate.

        The key value is the resolved value of ``key_join.key_attribute`` carried
        on the entity (the CSV key column lands the key as a regular attribute —
        ADR 0003 §2 "key-as-attribute"). For every entity of a type that has that
        attribute, we look up the existing entity(ies) whose
        ``attrs/<key_attribute>`` equals it (one batched SPARQL per type), and:

        - exactly one match → rebind ``entity_uri_map[id]`` to that URI (MERGE),
        - no match → leave the freshly-minted URI in place; the caller mints it
          only if ``mint_unmatched`` (else the id is returned as *skip*),
        - several matches → the key is not unique; leave as-is + log (treated as
          unmatched so we never silently merge onto an arbitrary one).

        Returns the set of entity ids to SKIP (unmatched + ``mint_unmatched`` is
        false). Mutates ``entity_uri_map`` in place for merged rows and records the
        merged/minted/unmatched counts on ``result``. Fully general over any
        (type, key-attribute); best-effort — a lookup failure degrades to
        ordinary minting (never blocks ingest)."""
        key_attr = _normalize_attr_name(key_join.key_attribute)

        # Group the incoming key value per entity id, bucketed by resolved type
        # (the lookup query is per-type). An entity with no value for the key
        # attribute cannot be joined — it is treated as unmatched.
        by_type: dict[str, dict[str, str]] = {}  # type -> {entity.id: key_value}
        no_key: set[str] = set()
        for entity in entities:
            if entity.id not in resolved_types:
                continue
            rtype = resolved_types[entity.id]
            val = next(
                (a.value for a in entity.attributes
                 if _normalize_attr_name(a.name) == key_attr and (a.value or "").strip()),
                None,
            )
            if val is None:
                no_key.add(entity.id)
                continue
            by_type.setdefault(rtype, {})[entity.id] = val.strip()

        # value -> existing URI(s), resolved per type via one batched query.
        matched_uri: dict[str, str] = {}   # entity.id -> existing URI
        ambiguous: set[str] = set()
        BATCH = 300
        for rtype, id_to_val in by_type.items():
            # Distinct values to look up (many rows may share a key value).
            distinct_vals = sorted({v for v in id_to_val.values()})
            val_to_uris: dict[str, list[str]] = {}
            for i in range(0, len(distinct_vals), BATCH):
                chunk = distinct_vals[i : i + BATCH]
                try:
                    sparql = entities_by_key_value_query(
                        instance_graph, rtype, key_attr, chunk,
                    )
                    res = await self._neptune.query(sparql)
                except Exception as e:  # best-effort — degrade to ordinary mint
                    logger.warning("key_join_lookup_failed", type=rtype, error=str(e))
                    continue
                for b in res.get("results", {}).get("bindings", []):
                    v = b.get("v", {}).get("value")
                    ent = b.get("entity", {}).get("value")
                    if v is not None and ent:
                        val_to_uris.setdefault(v, []).append(ent)
            for eid, val in id_to_val.items():
                uris = val_to_uris.get(val, [])
                if len(uris) == 1:
                    matched_uri[eid] = uris[0]
                elif len(uris) > 1:
                    ambiguous.add(eid)

        if ambiguous:
            logger.warning(
                "key_join_ambiguous",
                key_attribute=key_attr,
                count=len(ambiguous),
            )

        # Rebind merged rows onto the existing node; tally outcomes. Entities with
        # NO key value (``no_key`` — e.g. the mapping's relationship-target stubs,
        # or a row missing the key column) were never join CANDIDATES, so they
        # always mint and are NEVER force-skipped by mint_unmatched=false — that
        # flag governs only rows that HAD a key value but matched nothing (or
        # matched ambiguously). Otherwise strict mode would silently drop
        # relationship targets.
        skip: set[str] = set()
        for entity in entities:
            if entity.id not in resolved_types:
                continue
            if entity.id in matched_uri:
                entity_uri_map[entity.id] = matched_uri[entity.id]
                result.rows_key_merged += 1
            elif entity.id in no_key:
                # No key to join on → ordinary mint, unaffected by mint_unmatched.
                pass
            else:
                # Had a key value but no unique match (missed or ambiguous).
                if key_join.mint_unmatched:
                    result.rows_key_minted += 1
                else:
                    result.rows_key_unmatched += 1
                    skip.add(entity.id)

        if result.rows_key_unmatched:
            logger.warning(
                "key_join_unmatched_skipped",
                key_attribute=key_attr,
                skipped=result.rows_key_unmatched,
            )
        logger.info(
            "key_join_resolved",
            key_attribute=key_attr,
            merged=result.rows_key_merged,
            minted=result.rows_key_minted,
            unmatched=result.rows_key_unmatched,
        )
        return skip

    async def _ingest_mapped(
        self,
        mapping: CSVSchemaMapping,
        rows: list[dict[str, str]],
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        source: str,
        key_join: KeyJoin | None = None,
        *,
        instance_graph: str | None = None,
        parent_of: dict[str, str] | None = None,
        run_id: str | None = None,
    ) -> IngestResult:
        """Apply a pre-inferred mapping to rows and run the resolve→insert tail.

        Extracted verbatim from the former ``_ingest_csv`` body (Step 2 onward)
        so CSV ingest and web-discovery ingest commit through one code path.

        ``run_id`` (ONTA-372): STABLE run identity threaded from the discovery
        structured fast-path (``web_ingest_cap`` → :meth:`ingest_structured_rows` →
        :meth:`ingest_mapped_records`). When set, the ``batch_id`` is DERIVED from
        it (replay-stable, mirroring :meth:`ingest`) and an A6 :class:`GraphDelta`
        keyed to it is emitted on ``result.graph_delta`` — the SAME run the A1
        Source Bundle carries, so discovery lineage no longer diverges. To project
        that delta the run's instance triples are collected and flushed in ONE
        batched write (the same ``batched_insert_triples`` primitive the per-entity
        path uses). ``None`` (the CSV route) keeps the fresh-uuid4 batch_id, the
        byte-for-byte per-entity insert, and no delta — unchanged.

        ``key_join`` (ONTA-250): when set, each row is matched to an EXISTING
        entity by an exact key attribute and its attributes are merged ONTO that
        node instead of minting a duplicate — a first-class, deterministic
        complement to signal-ER. The merge rides the SAME resolve→insert tail (the
        row's minted URI is simply rebound to the existing node's URI before the
        write), so it flows through the shared write path untouched.

        ``instance_graph`` / ``parent_of`` (ONTA-268): CALL-LOCAL overrides; fall
        back to the ``self.`` attributes for legacy direct callers.
        """
        parent_of = self._parent_of if parent_of is None else parent_of
        # Resolve the call-local target graph ONCE up front so it is bound for the
        # rollback except-block too (ONTA-268).
        instance_graph = (
            instance_graph if instance_graph is not None
            else getattr(self, "_instance_graph", graph_uri)
        )
        from cograph_client.resolver.csv_resolver import CSVResolver

        # Step 2: Apply mapping deterministically to ALL rows (no LLM)
        applied = CSVResolver.apply_mapping(mapping, rows)
        entities, relationships = applied.entities, applied.relationships

        # Step 3: Resolve entities + insert in batches. ONTA-372: when a run_id is
        # threaded (discovery structured fast-path), DERIVE the batch_id from it so
        # a preserved-run_id replay reuses the same batch token (idempotent
        # BATCH_PREDICATE triple), mirroring the LLM-extract `ingest` path; the CSV
        # route (run_id=None) keeps a fresh uuid4 per call — unchanged.
        # ``collected_entity_triples`` accumulates the run's instance triples ONLY
        # when a run_id is threaded, so the A6 Graph Delta can be projected over
        # them below; run_id=None leaves it None → the per-entity insert path.
        batch_id = (
            derive_fact_id(run_id=run_id, stage="A6-batch") if run_id else str(uuid4())
        )
        collected_entity_triples: list[tuple[str, str, str]] | None = (
            [] if run_id is not None else None
        )
        result = IngestResult(
            entities_extracted=len(entities),
            chunks_processed=1,
            batch_id=batch_id,
            # Row-conservation accounting (ADR 0003 §2).
            rows_in=applied.rows_in,
            rows_dropped=applied.rows_dropped,
            drops_by_entity=applied.drops_by_entity,
        )
        entity_uri_map: dict[str, str] = {}
        entity_type_map: dict[str, str] = {}

        try:
            # Pass 1: Resolve types and compute URIs
            pending_uris: list[str] = []
            resolved_types: dict[str, str] = {}
            # Mapping-declared type name -> resolved ontology type name, so the
            # schema-time text_kind verdicts (keyed by the mapping's types) can
            # target the attr URIs actually written (ONTA-177). setdefault: the
            # first resolution wins, matching how attributes are declared.
            resolved_by_decl_type: dict[str, str] = {}
            for i, entity in enumerate(entities):
                if i > 0 and i % self.ONTOLOGY_REFRESH_INTERVAL == 0:
                    await self._refresh_ontology(graph_uri, existing_types, existing_attrs)

                resolved_type = await self._resolve_type(
                    entity, graph_uri, existing_types, existing_attrs, result,
                    parent_of=parent_of,
                )
                if resolved_type:
                    resolved_types[entity.id] = resolved_type
                    resolved_by_decl_type.setdefault(entity.type_name, resolved_type)
                    entity_uri = _entity_uri(resolved_type, entity.id)
                    entity_uri_map[entity.id] = entity_uri
                    entity_type_map[entity.id] = resolved_type

            # instance_graph resolved once at method top (ONTA-268 call-local).

            # ONTA-250 join-by-exact-key: rebind matched rows onto the EXISTING
            # node's URI BEFORE the existence check, so a merged row's URI is seen
            # as a duplicate (Pass 2 merges attributes, skips a second rdf:type)
            # and never mints a parallel node. Runs on the mapping's stub
            # relationship-target entities too, but those carry no key value so
            # they fall through as unmatched-minted (unchanged). Returns the ids
            # to SKIP entirely (unmatched + mint_unmatched=false).
            skip_ids: set[str] = set()
            if key_join is not None:
                skip_ids = await self._resolve_key_join(
                    entities, resolved_types, entity_uri_map,
                    instance_graph, key_join, result,
                )

            # Only URIs we will actually write get the existence check.
            pending_uris = [
                entity_uri_map[e.id]
                for e in entities
                if e.id in resolved_types and e.id not in skip_ids
            ]

            # Batch existence check
            existing_uris: set[str] = set()
            BATCH_CHECK_SIZE = 500
            for i in range(0, len(pending_uris), BATCH_CHECK_SIZE):
                batch = pending_uris[i : i + BATCH_CHECK_SIZE]
                sparql = batch_entity_exists_query(instance_graph, batch)
                found = await self._neptune.batch_exists(sparql)
                existing_uris.update(found)
            if existing_uris:
                logger.info("csv_batch_dedup_found", existing=len(existing_uris), total=len(pending_uris))

            # Pass 2: Resolve attributes and insert
            for entity in entities:
                if entity.id not in resolved_types:
                    continue
                if entity.id in skip_ids:
                    continue  # key-join unmatched with mint_unmatched=false
                resolved_type = resolved_types[entity.id]
                entity_uri = entity_uri_map[entity.id]
                is_duplicate = entity_uri in existing_uris
                if is_duplicate:
                    result.entities_deduplicated += 1
                await self._resolve_and_insert_entity(
                    entity, resolved_type, entity_uri, is_duplicate,
                    graph_uri, existing_types, existing_attrs, source, result, batch_id,
                    # ONTA-372: collect the instance triples for the A6 delta ONLY
                    # when a run_id is threaded; None → unchanged per-entity insert.
                    _collect_triples=collected_entity_triples,
                    instance_graph=instance_graph,  # ONTA-268: call-local target
                )

            # ONTA-372: when collecting (run_id threaded), the per-entity method
            # appended rather than inserted — flush the run's instance triples in
            # ONE batched write via the SAME primitive the per-entity path uses, so
            # the written facts are byte-identical (only the batch_id keying
            # differs). Ordering matches the per-entity path: entities land before
            # the text markers + relationships below. The triple COUNT was already
            # tallied inside `_resolve_and_insert_entity`, so do NOT re-increment.
            if collected_entity_triples:
                for sparql in batched_insert_triples(instance_graph, collected_entity_triples):
                    await self._neptune.update(sparql)

            # ONTA-177: persist the schema pass's free-text verdicts (the
            # mapping's per-column text_kind, decided ONCE at schema-inference
            # time by the REASON pass + name-blind auto tier) as textKind
            # ontology markers on the resolved attribute URIs. No re-decision
            # here — a legacy/hand-written mapping without text_kind writes no
            # markers (candidacy undecided; ONTA-181's reconciler-side
            # heuristic covers those attributes later).
            await self._apply_mapping_text_markers(
                mapping, resolved_by_decl_type, graph_uri, result,
            )

            # Step 4: Batch-insert relationships
            rel_triples: list[tuple[str, str, str]] = []
            for rel in relationships:
                # An edge whose source or target was skipped (key-join unmatched
                # with mint_unmatched=false) has no node to hang off — drop it.
                if rel.source_id in skip_ids or rel.target_id in skip_ids:
                    continue
                source_uri = entity_uri_map.get(rel.source_id)
                target_uri = entity_uri_map.get(rel.target_id)
                if source_uri and target_uri:
                    # Normalize predicate against existing predicates on this type
                    source_type = entity_type_map.get(rel.source_id)
                    existing_preds = set()
                    if source_type:
                        for attr_name, schema in existing_attrs.get(source_type, {}).items():
                            if schema.datatype not in PRIMITIVE_TYPES:
                                existing_preds.add(attr_name)
                    canonical_pred = normalize_predicate(rel.predicate, existing_preds)

                    predicate = f"https://cograph.tech/onto/{canonical_pred}"
                    rel_triples.append((source_uri, predicate, target_uri))

                    # Register relationship as object property in ontology
                    target_type = entity_type_map.get(rel.target_id)
                    if source_type and target_type:
                        type_attrs = existing_attrs.get(source_type, {})
                        existing = type_attrs.get(canonical_pred)
                        if existing is None:
                            sparql = insert_attribute(graph_uri, source_type, canonical_pred, "", target_type)
                            await self._locked_ontology_update(sparql)  # ONTA-268
                            result.attributes_added.append(f"{source_type}.{canonical_pred}")
                            existing_attrs.setdefault(source_type, {})[canonical_pred] = AttributeSchema(
                                name=canonical_pred, datatype=target_type,
                            )
                        elif existing.datatype in PRIMITIVE_TYPES:
                            # Upgrade a primitive attribute to a relationship range
                            # so the Explorer overview draws the edge (see entity
                            # ingest path above for the full rationale).
                            await self._locked_ontology_update(  # ONTA-268
                                set_object_property_range(
                                    graph_uri, source_type, canonical_pred, target_type,
                                )
                            )
                            existing_attrs[source_type][canonical_pred] = AttributeSchema(
                                name=canonical_pred, datatype=target_type,
                            )

            for sparql in batched_insert_triples(graph_uri, rel_triples):
                await self._neptune.update(sparql)
            result.triples_inserted += len(rel_triples)

            result.entities_resolved = len(entity_uri_map)
            logger.info(
                "csv_ingest_complete",
                rows=len(rows),
                entities=result.entities_resolved,
                triples=result.triples_inserted,
                types=result.types_created,
            )
            # ONTA-372: emit the run's deterministic A6 Graph Delta when a run_id
            # was threaded (discovery structured fast-path), keyed to the SAME
            # run_id as the A1 Source Bundle so discovery lineage no longer
            # diverges. Built over the COMPLETE instance facts (entity triples +
            # relationship triples) via the shared `build_graph_delta` — the same
            # projection the LLM-extract path emits. `fan_in` records key-join
            # merges: >1 source entity id resolving to ONE final URI is a merge, so
            # the non-canonical sources' natural URIs map to the shared node.
            if run_id is not None:
                ids_by_uri: dict[str, list[str]] = {}
                for eid, uri in entity_uri_map.items():
                    ids_by_uri.setdefault(uri, []).append(eid)
                fan_in: dict[str, str] = {}
                for uri, eids in ids_by_uri.items():
                    if len(eids) > 1:
                        for eid in eids:
                            natural = _entity_uri(entity_type_map.get(eid, ""), eid)
                            if natural != uri:
                                fan_in[natural] = uri
                result.graph_delta = build_graph_delta(
                    instance_graph,
                    (collected_entity_triples or []) + rel_triples,
                    run_id=run_id,
                    fan_in=fan_in,
                ).to_dict()
            return result

        except Exception:
            logger.error(
                "csv_ingest_failed_rolling_back",
                batch_id=batch_id,
                entities_so_far=result.entities_resolved,
                exc_info=True,
            )
            # instance_graph resolved once at method top (ONTA-268 call-local).
            try:
                sparql = delete_batch_query(instance_graph, batch_id)
                await self._neptune.update(sparql)
                logger.info("csv_batch_rollback_complete", batch_id=batch_id)
            except Exception:
                logger.error("csv_batch_rollback_failed", batch_id=batch_id, exc_info=True)
            raise

    async def _extract(
        self,
        content: str,
        content_type: str,
        existing_types: dict[str, str] | None = None,
        constraint: ExtractionConstraint | None = None,
    ) -> ExtractionResult:
        """Extract entities and relationships from raw content.

        ``constraint`` (ONTA-199) is OPT-IN and defaults to ``None``: with no
        constraint the system/user prompt is byte-for-byte the open-ended
        default and the result is returned untouched (the document/CSV/text
        path). An active constraint appends a type/attribute restriction to both
        prompts and drops any off-type entities / unrequested attributes the
        model still emits (the web-discovery path).
        """
        if existing_types:
            types_str = "\n".join(f"- {name}" for name in existing_types)
        else:
            types_str = "(none — this is a fresh ontology)"

        user_content = EXTRACTION_USER_TEMPLATE.format(
            content=content,
            existing_types=types_str,
        )
        # Discovery-only prompt narrowing. Inactive constraint → no change: the
        # system/user prompt AND the ``_extract_via_openrouter`` call are byte-for-
        # byte the pre-ONTA-199 default, so existing tests that patch
        # ``_extract_via_openrouter`` with a mock lacking a ``system_prompt``
        # parameter still pass (the no-op path never sends the kwarg).
        system_prompt = EXTRACTION_SYSTEM
        constraint_block = _build_constraint_user_block(constraint)
        _sys_kw: dict = {}
        if constraint_block:
            # SOFT (seed) → the target-schema PRIOR (decompose faithfully);
            # HARD (ONTA-199) → the flat single-type cage. Both narrow the prompt
            # but only HARD flattens.
            constraint_system = (
                EXTRACTION_TARGET_SYSTEM
                if getattr(constraint, "soft", False)
                else EXTRACTION_CONSTRAINT_SYSTEM
            )
            system_prompt = EXTRACTION_SYSTEM + constraint_system
            user_content = user_content + constraint_block
            _sys_kw = {"system_prompt": system_prompt}

        # ONTA-200: count the records in the chunk being extracted so the
        # per-call log below can be read against output-token size — a slow run
        # with bloated completions is diagnosable directly (records → tokens).
        # Only JSON chunks are a records array; free text has no record count.
        from cograph_client.resolver.chunker import (
            estimate_tokens_per_record_from_input,
            json_array_len,
        )

        records_in_chunk = json_array_len(content) if content_type == "json" else None
        # ONTA-381: adaptive completion budget sized to this chunk's predicted
        # reified output (input-aware tokens/record × headroom), floored at the
        # base cap and clamped to the hard cap. Dense multi-attribute pages no
        # longer hit finish_reason=length at a flat 8192 while still sizing
        # batches proactively against the same density signal.
        tokens_per_record = (
            estimate_tokens_per_record_from_input(content)
            if content_type == "json"
            else None
        )
        completion_budget = self._completion_budget_for(
            records_in_chunk, tokens_per_record=tokens_per_record,
        )

        truncated = False
        finish_reason: str | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        provider = self.EXTRACT_PROVIDER if (
            self.EXTRACT_PROVIDER == "openrouter" and self._openrouter_key
        ) else "anthropic"

        # Time ONLY the LLM round-trip (not the JSON parse below) so duration_ms
        # attributes the latency to the model call itself.
        _t0 = time.perf_counter()
        if provider == "openrouter":
            # ``**_sys_kw`` carries the constraint-narrowed system prompt on a
            # discovery run (ONTA-199); empty on the open-ended document path.
            text, finish_reason, usage = await self._extract_via_openrouter(
                user_content, max_tokens=completion_budget, **_sys_kw,
            )
            # Honest truncation signal on the OpenRouter path, mirroring the
            # Anthropic ``stop_reason == "max_tokens"`` check below: OpenRouter
            # reports ``finish_reason == "length"`` when the model hit the token
            # ceiling mid-output, so the JSON is almost certainly incomplete.
            # Surfacing it lets a JSON chunk be split + retried instead of the
            # whole batch being silently dropped on the parse failure below.
            if finish_reason == "length":
                truncated = True
            if usage:
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")
        else:
            msg = await self._anthropic.messages.create(
                model=self.INFER_MODEL,
                max_tokens=completion_budget,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )
            text = msg.content[0].text
            finish_reason = getattr(msg, "stop_reason", None)
            # Explicit truncation signal from the Anthropic SDK: the model hit
            # the token ceiling mid-output, so the JSON is almost certainly
            # incomplete. Surface it so a JSON chunk can be split + retried
            # instead of silently dropping the whole batch.
            if finish_reason == "max_tokens":
                truncated = True
            msg_usage = getattr(msg, "usage", None)
            if msg_usage is not None:
                prompt_tokens = getattr(msg_usage, "input_tokens", None)
                completion_tokens = getattr(msg_usage, "output_tokens", None)
        duration_ms = (time.perf_counter() - _t0) * 1000.0

        # ONTA-200: ONE structured log per extraction LLM call. Pure
        # observability — no control-flow effect. Lets a slow discovery run
        # reveal output-token bloat directly (completion_tokens vs
        # records_in_chunk) instead of reconstructing it from request gaps.
        # ONTA-381 adds max_tokens so a truncated run is diagnosable against the
        # adaptive budget that was actually requested.
        logger.info(
            "extract_call",
            provider=provider,
            completion_tokens=completion_tokens,
            prompt_tokens=prompt_tokens,
            finish_reason=finish_reason,
            records_in_chunk=records_in_chunk,
            max_tokens=completion_budget,
            duration_ms=duration_ms,
        )

        try:
            # Strip code fences if present
            stripped = text.strip()
            if stripped.startswith("```"):
                lines = [l for l in stripped.split("\n") if not l.strip().startswith("```")]
                stripped = "\n".join(lines)
            data = json.loads(stripped)
            entities = [ExtractedEntity(**e) for e in data.get("entities", [])]
            relationships = [ExtractedRelationship(**r) for r in data.get("relationships", [])]
            result = ExtractionResult(
                entities=entities,
                relationships=relationships,
                source_text=content,
            )
            # Discovery-only post-guard: inactive constraint returns unchanged.
            return _apply_extraction_constraint(result, constraint)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            # A parse failure on a TRUNCATED response is the expected symptom of
            # the output exceeding max_tokens (the recovery loop will split +
            # retry); log it distinctly so it isn't mistaken for a malformed
            # model reply.
            #
            # ``ValueError`` (added belt-and-suspenders) also covers
            # ``pydantic.ValidationError`` — a ``ValueError`` subclass — so a
            # NOVEL bad record shape the extractor returns (e.g. a value type the
            # models don't yet coerce) degrades to empty-extraction + split-retry
            # instead of hard-failing the whole discovery job. The systemic
            # fatal LLM errors (``LLMBillingError`` / ``LLMAuthError``, 402/401)
            # are NOT ``ValueError`` subclasses and are raised in the LLM call
            # ABOVE this try block, so they still propagate and abort the run
            # fast (ONTA-201) rather than being swallowed here.
            logger.warning(
                "extraction_parse_error",
                error=str(e),
                truncated=truncated,
                raw=text[:500],
            )
            return ExtractionResult(source_text=content)

    def _completion_budget_for(
        self,
        n_records: int | None,
        *,
        tokens_per_record: int | None = None,
    ) -> int:
        """Adaptive completion-token budget for one extraction call (ONTA-381).

        Scales with predicted reified output so a dense multi-record chunk gets
        enough room to finish clean JSON (no mid-stream ``finish_reason=length``)
        while staying under :attr:`EXTRACT_MAX_TOKENS_HARD`. Small / unknown
        record counts still receive the base :attr:`EXTRACT_MAX_TOKENS` ceiling.
        """
        from cograph_client.resolver.chunker import adaptive_completion_tokens

        return adaptive_completion_tokens(
            n_records or 0,
            base_cap=self.EXTRACT_MAX_TOKENS,
            hard_cap=self.EXTRACT_MAX_TOKENS_HARD,
            tokens_per_record=tokens_per_record,
        )

    async def _extract_via_openrouter(
        self,
        user_content: str,
        system_prompt: str = EXTRACTION_SYSTEM,
        *,
        max_tokens: int | None = None,
    ) -> tuple[str, str | None, dict | None]:
        """Extract entities via OpenRouter, with primary→fallback routing.

        Returns ``(content, finish_reason, usage)``: ``finish_reason`` lets the
        caller detect a length-truncated reply (``"length"``) and route the chunk
        into split-and-retry instead of dropping it; ``usage`` (the OpenRouter
        ``prompt_tokens`` / ``completion_tokens`` object, or ``None``) is threaded
        back for per-call token accounting (ONTA-200) — previously discarded.

        ``system_prompt`` defaults to the open-ended :data:`EXTRACTION_SYSTEM`;
        a constrained (discovery) extraction passes the type/attribute-narrowed
        system prompt (ONTA-199).

        ``max_tokens`` is the adaptive completion budget (ONTA-381); when omitted
        the base :attr:`EXTRACT_MAX_TOKENS` ceiling is used so existing callers /
        mocks that don't pass the kwarg keep working.
        """
        return await openrouter_chat(
            self._openrouter_key,
            system_prompt,
            user_content,
            model=self.EXTRACT_MODEL,
            temperature=0,
            max_tokens=(
                max_tokens if max_tokens is not None else self.EXTRACT_MAX_TOKENS
            ),
            timeout=60,
            return_finish_reason=True,
            return_usage=True,
        )

    # Floor below which a JSON chunk is no longer worth splitting: a handful of
    # records can't overflow max_tokens, so a still-empty extraction is a genuine
    # extraction failure to account for, not a truncation to recover.
    _RECOVERY_MIN_RECORDS = 3

    async def _extract_json_chunk_with_recovery(
        self,
        chunk: str,
        existing_types: dict[str, str],
        constraint: ExtractionConstraint | None = None,
    ) -> tuple[ExtractionResult, int]:
        """Extract one JSON-array chunk, RECOVERING from a silent batch loss.

        The reification/lift prompt makes each record emit many entities +
        relationships, so a dense chunk's JSON output can exceed the model's
        ``max_tokens``, get truncated, fail to parse, and return an EMPTY
        :class:`ExtractionResult` — silently dropping every record in the chunk.

        When that happens (zero entities extracted from a chunk that actually
        held records) we SPLIT the chunk's JSON array in half and retry each
        half, recursing down to :attr:`_RECOVERY_MIN_RECORDS`. Smaller chunks
        produce smaller outputs that fit under the cap. If a minimal chunk still
        yields nothing it is a real extraction failure: we log at ERROR and
        return its record count as ``dropped`` so the caller can surface it in
        row-conservation accounting instead of presenting the run as complete.

        Returns ``(merged_extraction, dropped_record_count)``.
        """
        from cograph_client.resolver.chunker import split_json_array_chunk, json_array_len

        # A fatal billing/auth error (402/401) raised by the extraction LLM call
        # is SYSTEMIC — the next call fails identically — so it must NOT be
        # treated as a truncation to recover from. It is neither caught by
        # `_extract` (which only swallows JSON/parse errors) nor here, so it
        # propagates straight out of the recovery recursion and aborts the whole
        # ingest, instead of splitting the chunk and burning more doomed calls
        # (ONTA-201). Every other empty extraction still splits + retries below.
        #
        # Only forward ``constraint`` when it's active, so the default document
        # path calls ``_extract`` with the EXACT same argument shape as before
        # ONTA-199 (existing tests patch ``_extract`` with a mock that has no
        # ``constraint`` parameter — the no-op path must not pass the kwarg).
        _c = {"constraint": constraint} if constraint is not None else {}
        extraction = await self._extract(chunk, "json", existing_types, **_c)
        n_records = json_array_len(chunk)
        # Success, or a genuinely empty chunk (no records to lose) → nothing to recover.
        if extraction.entities or n_records == 0:
            return extraction, 0

        # Too small to split further: a few records can't overflow the token
        # cap, so this is a real extraction failure — account for the loss.
        if n_records <= self._RECOVERY_MIN_RECORDS:
            logger.error(
                "extraction_chunk_dropped",
                records=n_records,
                reason="empty_extraction_at_min_chunk",
            )
            return extraction, n_records

        halves = split_json_array_chunk(chunk)
        if not halves:
            # Couldn't split (not a parseable array) — count the loss.
            logger.error("extraction_chunk_dropped", records=n_records, reason="unsplittable")
            return extraction, n_records

        logger.warning(
            "extraction_chunk_split_retry", records=n_records, halves=len(halves),
        )
        merged_entities: list[ExtractedEntity] = []
        merged_relationships: list[ExtractedRelationship] = []
        seen_ids: set[str] = set()
        total_dropped = 0
        for half in halves:
            sub_extraction, sub_dropped = await self._extract_json_chunk_with_recovery(
                half, existing_types, **_c,
            )
            total_dropped += sub_dropped
            for e in sub_extraction.entities:
                if e.id not in seen_ids:
                    merged_entities.append(e)
                    seen_ids.add(e.id)
            merged_relationships.extend(sub_extraction.relationships)
        return (
            ExtractionResult(
                entities=merged_entities,
                relationships=merged_relationships,
                source_text=chunk[:500],
            ),
            total_dropped,
        )

    async def _extract_chunks_concurrently(
        self, extract_calls: list,
    ) -> list[ExtractionResult]:
        """Run per-chunk extraction coroutine-factories under a bounded semaphore.

        ONTA-197 item 3: independent chunks each take ~70s sequentially; running
        them concurrently under an :class:`asyncio.Semaphore` (size
        :attr:`EXTRACT_CONCURRENCY`) overlaps the LLM calls while capping how many
        are in flight. ``extract_calls`` is a list of zero-arg callables each
        returning the extraction coroutine for one chunk; results are returned in
        the SAME order as ``extract_calls`` (``asyncio.gather`` preserves input
        order regardless of completion order), so downstream merge/dedup stays
        deterministic. A tuple-returning factory (recovery: ``(result, dropped)``)
        is passed straight through unchanged.
        """
        sem = asyncio.Semaphore(max(1, self.EXTRACT_CONCURRENCY))

        async def _guarded(make_call):
            async with sem:
                return await make_call()

        return await asyncio.gather(*(_guarded(mk) for mk in extract_calls))

    async def _extract_json_chunks_calibrated(
        self,
        chunks: list[str],
        content: str,
        existing_types: dict[str, str],
        constraint: ExtractionConstraint | None = None,
    ) -> tuple[ExtractionResult, int, int]:
        """Extract multiple JSON chunks with first-batch calibration + concurrency.

        Composes ONTA-197 items 2 and 3 (see :meth:`ingest`):

          1. Extract chunk 1 SEQUENTIALLY (with recovery) — we need its result
             before we can learn the real per-record output size.
          2. CALIBRATE: estimate chunk 1's real output tokens from its serialized
             extraction, derive observed tokens-per-record (clamped to a floor so
             a fluke-light first batch can't oversize the rest), and RE-CHUNK the
             not-yet-processed remainder ONCE against that ratio. Sparse records
             (which the conservative ONTA-196 default over-shrinks) get larger,
             still cap-safe batches; dense records keep small batches, never
             reintroducing truncation.
          3. Extract the re-chunked remainder CONCURRENTLY under the semaphore,
             preserving order, per-chunk recovery, and dropped-record accounting.

        Returns ``(merged_extraction, rows_in, rows_dropped)``.
        """
        from cograph_client.resolver.chunker import (
            json_array_len,
            chunk_json_array,
            estimate_output_tokens,
            calibrated_tokens_per_record,
        )

        merged_entities: list[ExtractedEntity] = []
        merged_relationships: list[ExtractedRelationship] = []
        seen_ids: set[str] = set()
        rows_in = 0
        rows_dropped = 0

        def _merge(ex: ExtractionResult) -> None:
            for e in ex.entities:
                if e.id not in seen_ids:
                    merged_entities.append(e)
                    seen_ids.add(e.id)
            merged_relationships.extend(ex.relationships)

        # --- Step 1: chunk 1, sequential, with recovery ------------------------
        first_chunk = chunks[0]
        first_records = json_array_len(first_chunk)
        rows_in += first_records
        first_ex, first_dropped = await self._extract_json_chunk_with_recovery(
            first_chunk, existing_types, constraint=constraint,
        )
        rows_dropped += first_dropped
        _merge(first_ex)

        # --- Step 2: calibrate + re-chunk the remainder ------------------------
        # The records NOT covered by chunk 1 (chunk_json_array splits in order, so
        # the remainder is exactly the tail of the original array past chunk 1).
        remainder_chunks = chunks[1:]
        observed_tokens = estimate_output_tokens(
            self._serialize_extraction_for_sizing(first_ex)
        )
        # Only re-chunk when chunk 1 actually produced something to learn from.
        # A fluke-empty/dropped first batch → keep the conservative sizing.
        if first_records > 0 and observed_tokens > 0:
            tpr = calibrated_tokens_per_record(observed_tokens, first_records)
            try:
                remainder_records = json.loads(content)[first_records:]
            except (json.JSONDecodeError, TypeError):
                remainder_records = None
            if remainder_records:
                rechunked = chunk_json_array(
                    json.dumps(remainder_records, default=str),
                    max_tokens=self.EXTRACT_MAX_TOKENS,
                    tokens_per_record=tpr,
                )
                remainder_chunks = rechunked
                logger.info(
                    "extract_calibrated_rechunk",
                    first_records=first_records,
                    observed_tokens=observed_tokens,
                    tokens_per_record=tpr,
                    remainder_records=len(remainder_records),
                    remainder_chunks=len(remainder_chunks),
                )

        if not remainder_chunks:
            return (
                ExtractionResult(
                    entities=merged_entities,
                    relationships=merged_relationships,
                    source_text=content[:500],
                ),
                rows_in,
                rows_dropped,
            )

        # --- Step 3: extract the remainder concurrently, preserving order ------
        for chunk in remainder_chunks:
            rows_in += json_array_len(chunk)
        results = await self._extract_chunks_concurrently(
            [
                lambda c=chunk: self._extract_json_chunk_with_recovery(
                    c, existing_types, constraint=constraint,
                )
                for chunk in remainder_chunks
            ]
        )
        for sub_ex, sub_dropped in results:
            rows_dropped += sub_dropped
            _merge(sub_ex)

        return (
            ExtractionResult(
                entities=merged_entities,
                relationships=merged_relationships,
                source_text=content[:500],
            ),
            rows_in,
            rows_dropped,
        )

    @staticmethod
    def _serialize_extraction_for_sizing(ex: ExtractionResult) -> str:
        """Serialize an extraction back to the model's JSON shape for size sizing.

        Calibration needs chunk 1's real OUTPUT size, but the extraction call
        site does not surface provider ``usage`` counts. Re-serializing the parsed
        entities + relationships to the same ``{"entities":[...],
        "relationships":[...]}`` document the model emitted is a faithful proxy
        for that output's length (the driver of :func:`estimate_output_tokens`).
        """
        try:
            return json.dumps(
                {
                    "entities": [e.model_dump() for e in ex.entities],
                    "relationships": [r.model_dump() for r in ex.relationships],
                },
                default=str,
            )
        except Exception:
            return ""

    async def _fetch_ontology(
        self, graph_uri: str
    ) -> tuple[dict[str, str], dict[str, dict[str, AttributeSchema]]]:
        """Fetch existing types and attributes from Neptune.

        Returns:
            (types: {name: description}, attrs: {type_name: {attr_name: schema}})
        """
        try:
            raw = await self._neptune.query(get_full_ontology_query(graph_uri))
            _, bindings = parse_sparql_results(raw)
        except Exception:
            logger.warning("ontology_fetch_failed", exc_info=True)
            return {}, {}

        types: dict[str, str] = {}
        attrs: dict[str, dict[str, AttributeSchema]] = {}

        for row in bindings:
            type_label = row.get("typeLabel", "")
            if not type_label:
                continue
            if type_label not in types:
                types[type_label] = ""
                attrs[type_label] = {}
            if row.get("attrLabel"):
                range_str = row.get("range", "")
                type_uri_prefix = "https://cograph.tech/types/"
                if range_str.startswith(type_uri_prefix):
                    # Range is a reference to another ontology type
                    datatype = range_str[len(type_uri_prefix):]
                elif "#" in range_str:
                    fragment = range_str.split("#")[-1]
                    # Map XSD names to our datatype names
                    dt_map = {
                        "string": "string", "integer": "integer", "float": "float",
                        "boolean": "boolean", "dateTime": "datetime", "Resource": "uri",
                    }
                    datatype = dt_map.get(fragment, "string")
                else:
                    datatype = "string"
                attrs[type_label][row["attrLabel"]] = AttributeSchema(
                    name=row["attrLabel"], datatype=datatype,
                )

        return types, attrs

    async def _fetch_parent_map(
        self, graph_uri: str, layer_stack: LayerStack | None = None
    ) -> dict[str, str]:
        """Fetch the child->parent subclass map (keyed by type *name*).

        Reads every rdfs:subClassOf edge via parent_map_query and reduces each
        URI to its type name so it can feed the pure hierarchy helpers
        (ancestor_chain / config_for_with_hierarchy). Returns {} on any error —
        callers degrade to flat (zero-hierarchy) behavior.

        Layer-aware variant (ADR 0002 §1, COG-37): pass a LayerStack and the
        edges are read from the UNION of the tenant's visible layer graphs in
        one query — subClassOf edges may span layers (a tenant leaf under a
        Public parent). Duplicate child names are resolved by shadowing: edges
        from higher-precedence layers (Tenant > Enhanced > Public) win. With
        no layer_stack the single-graph behavior is exactly as before.
        """
        if layer_stack is None:
            try:
                raw = await self._neptune.query(parent_map_query(graph_uri))
                _, bindings = parse_sparql_results(raw)
            except Exception:
                logger.warning("parent_map_fetch_failed", exc_info=True)
                return {}
            return self._parent_map_from_bindings(bindings)

        try:
            raw = await self._neptune.query(
                parent_map_query(layer_stack.visible_graph_uris())
            )
            _, bindings = parse_sparql_results(raw)
        except Exception:
            logger.warning("parent_map_fetch_failed", exc_info=True)
            return {}

        rows_by_graph: dict[str, list[dict]] = {}
        for row in bindings:
            rows_by_graph.setdefault(row.get("graph", ""), []).append(row)
        # Merge lowest-precedence layer first so higher layers overwrite
        # duplicate child keys — Tenant > Enhanced > Public shadowing.
        parent_of: dict[str, str] = {}
        for g in reversed(layer_stack.visible_graph_uris()):
            parent_of.update(self._parent_map_from_bindings(rows_by_graph.get(g, [])))
        return parent_of

    @staticmethod
    def _parent_map_from_bindings(bindings: list[dict]) -> dict[str, str]:
        """Reduce ?child/?parent URI bindings to a {child_name: parent_name} map.

        Names are extracted via type_name_from_uri, which understands every
        layer namespace — so a tenant-graph edge whose PARENT is a Public-layer
        URI (`types/public/Person`) keys correctly instead of being dropped.
        Edges with either end outside all layer namespaces are skipped, as are
        self-edges.
        """
        parent_of: dict[str, str] = {}
        for row in bindings:
            child_name = type_name_from_uri(row.get("child", ""))
            parent_name = type_name_from_uri(row.get("parent", ""))
            if child_name and parent_name and child_name != parent_name:
                parent_of[child_name] = parent_name
        return parent_of

    async def _synthesize_ancestors(
        self,
        child_type: str,
        parent_type: str | None,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        result: IngestResult,
        parent_chain: list[str] | None = None,
        emit_child_edge: bool = False,
        *,
        parent_of: dict[str, str] | None = None,
    ) -> None:
        """Close the rdfs:subClassOf lineage from `child_type` up to the nearest
        existing root (ADR 0001 rule 3).

        `parent_type` is the immediate parent (may be None when only an extractor
        chain is available). `parent_chain` is the extractor's full ancestor list
        for `child_type`, most-specific first — seeding it lets a brand-new
        MULTI-LEVEL lineage (e.g. Condo < Property < Asset, all new) close in a
        single pass. `emit_child_edge=True` makes this method emit the
        child->immediate-parent subClassOf edge itself; callers that already
        emitted it (the SUBTYPE branches) pass False to avoid a redundant write.

        For each ancestor NOT yet in existing_types, emits insert_type +
        insert_subtype and registers it in existing_types / existing_attrs /
        result.types_created. Idempotent: ancestors already present are skipped.

        ``parent_of`` (ONTA-268): the CALL-LOCAL child->parent map to read+mutate;
        falls back to ``self._parent_of`` for legacy direct callers. Runs under the
        caller's ontology-write lock (``_resolve_type``) — must NOT acquire it here
        (``asyncio.Lock`` is not reentrant).
        """
        from cograph_client.resolver.er import ancestor_chain

        parent_of = self._parent_of if parent_of is None else parent_of
        parent_chain = parent_chain or []
        # Immediate parent: explicit hint wins; otherwise top of the extractor chain.
        if not parent_type:
            parent_type = parent_chain[0] if parent_chain else None
        if not parent_type:
            return

        # Record the child->parent edge so later entities in this batch can climb it.
        if child_type and child_type != parent_type:
            parent_of[child_type] = parent_type
        # Seed the deeper extractor lineage (ancestors of child, most-specific
        # first) without clobbering edges already recorded (setdefault).
        prev = child_type
        for anc in parent_chain:
            if prev and anc and prev != anc:
                parent_of.setdefault(prev, anc)
            prev = anc

        # Brand-new lineage: the caller couldn't link child->parent because the
        # parent didn't exist yet. Emit that edge here.
        if emit_child_edge and child_type and child_type != parent_type:
            await self._neptune.update(insert_subtype(graph_uri, parent_type, child_type))

        # Walk root-ward from the immediate parent. ancestor_chain is cycle-guarded.
        chain = ancestor_chain(parent_type, parent_of)
        for i, ancestor in enumerate(chain):
            grandparent = chain[i + 1] if i + 1 < len(chain) else None
            if ancestor not in existing_types:
                await self._neptune.update(insert_type(graph_uri, ancestor, ""))
                if grandparent:
                    await self._neptune.update(insert_subtype(graph_uri, grandparent, ancestor))
                    parent_of[ancestor] = grandparent
                result.types_created.append(ancestor)
                existing_types[ancestor] = ""
                existing_attrs[ancestor] = {}

    async def _link_parent(
        self,
        entity: ExtractedEntity,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        result: IngestResult,
        *,
        parent_of: dict[str, str] | None = None,
    ) -> None:
        """Attach a freshly-created type to its parent lineage.

        Two cases:
        - immediate parent already exists → link directly, then synthesize any
          deeper ancestors the extractor named (parent_chain);
        - brand-new lineage (parent not in the ontology, or only a parent_chain) →
          let _synthesize_ancestors create every missing ancestor AND the
          child->parent edge (emit_child_edge=True). This closes a fully-new
          multi-level chain like Condo < Property < Asset in one row (ADR rule 3).

        ``parent_of`` (ONTA-268): CALL-LOCAL child->parent map threaded to
        `_synthesize_ancestors`; falls back to ``self._parent_of``. Runs under the
        caller's ontology-write lock — does not acquire it.
        """
        parent_of = self._parent_of if parent_of is None else parent_of
        pt = entity.parent_type
        linked_as_subtype = False
        if pt and pt in existing_types:
            # Immediate parent exists — link directly, then synthesize any deeper
            # ancestors the extractor named.
            await self._neptune.update(insert_subtype(graph_uri, pt, entity.type_name))
            await self._synthesize_ancestors(
                entity.type_name, pt, graph_uri, existing_types, existing_attrs, result,
                parent_chain=entity.parent_chain, parent_of=parent_of,
            )
            logger.info("type_new_with_parent", child=entity.type_name, parent=pt)
            linked_as_subtype = True
        elif entity.parent_chain:
            # Brand-new lineage. We DON'T trust a parent_type that names a
            # non-existing type (preserves the "parent_type must be existing"
            # contract); the full chain comes from parent_chain instead.
            await self._synthesize_ancestors(
                entity.type_name, None, graph_uri, existing_types, existing_attrs, result,
                parent_chain=entity.parent_chain, emit_child_edge=True, parent_of=parent_of,
            )
            logger.info(
                "type_new_lineage", child=entity.type_name, parent=entity.parent_chain[0],
            )
            linked_as_subtype = True

        # The caller's top-level mint wrote NO comment (FIX 3): subtype_description
        # may only describe a real subtype. Now that a parent linkage has made
        # this type a genuine subtype, write the description here. Use the
        # COMMENT-ONLY upsert: the subClassOf edge was just created above (by
        # insert_subtype / _synthesize_ancestors), and plain upsert_type would
        # DELETE it (it clears subClassOf when no parent_type is passed) — the
        # new-parent-edge bug. upsert_type_comment touches only rdfs:comment, so
        # the edge survives while the description stays idempotent on re-ingest.
        if linked_as_subtype and entity.subtype_description:
            await self._neptune.update(
                upsert_type_comment(graph_uri, entity.type_name, entity.subtype_description)
            )

    async def _refresh_ontology(
        self,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
    ) -> None:
        """Re-fetch ontology from Neptune and merge into in-memory state.

        Additive merge only: new types/attrs from concurrent ingestions are added,
        but nothing is removed (this ingestion may have added types not yet visible).
        """
        fresh_types, fresh_attrs = await self._fetch_ontology(graph_uri)
        added = 0
        for t, desc in fresh_types.items():
            if t not in existing_types:
                existing_types[t] = desc
                added += 1
        for t, attrs in fresh_attrs.items():
            if t not in existing_attrs:
                existing_attrs[t] = attrs
            else:
                for a, schema in attrs.items():
                    if a not in existing_attrs[t]:
                        existing_attrs[t][a] = schema
        if added:
            logger.info("ontology_refreshed", new_types=added)

    async def _reconcile_ontology_version(
        self,
        graph_uri: str,
        stamped_version: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        parent_of: dict[str, str],
    ) -> str:
        """ONTA-270 optimistic-concurrency guard: reject-and-recompute a STALE A5
        placement plan at P6 apply time. Returns the CURRENT ontology version.

        ``stamped_version`` is the fingerprint :meth:`ingest` computed at the TOP
        of this run — the ontology state P5 planned against, read BEFORE the long,
        async LLM extraction. Here, at the START of the apply, we re-read the
        CURRENT ontology under the ontology-write lock (so the compare + any
        per-type mint pass 1 then does can't interleave with a concurrent writer)
        and fingerprint it:

        * **Match** (the common case — no ontology write landed during our
          extraction): the plan is fresh, so we return and pass 1 applies it
          unchanged. Cost is one ontology read; nothing is mutated.
        * **Mismatch**: a concurrent run advanced the ontology T→T+1 while we were
          extracting, so the placement about to be applied was computed against a
          STALE snapshot and would mint duplicate terms (a synonym of a type the
          other run just created, a re-declared attribute). We REJECT that stale
          basis and RECOMPUTE by refreshing the in-place snapshot to the current
          ontology (additive merge, mirroring :meth:`_refresh_ontology`), so pass
          1's type/attribute resolution runs against T+1 and lands on the existing
          terms instead of duplicating them.

        Complements ONTA-268: 268's ontology-write lock serializes INDIVIDUAL
        mutations; this version stamp catches a whole PLAN computed before another
        run advanced the ontology — the read-modify-write side of the same race.
        """
        async with self._ontology_lock:
            fresh_types, fresh_attrs = await self._fetch_ontology(graph_uri)
            fresh_parent = await self._fetch_parent_map(graph_uri)
            current = ontology_version(fresh_types, fresh_attrs, fresh_parent)
            if current == stamped_version:
                return current
            logger.info(
                "stale_placement_plan_recomputed",
                stamped_version=stamped_version,
                current_version=current,
                graph_uri=graph_uri,
            )
            # Additive merge (never remove — this run hasn't written yet, so the
            # snapshot == ingest-top state; we only need the concurrent run's new
            # terms). setdefault keeps any snapshot entry, adds the fresh ones.
            for t, desc in fresh_types.items():
                existing_types.setdefault(t, desc)
            for t, attrs in fresh_attrs.items():
                dst = existing_attrs.setdefault(t, {})
                for a, schema in attrs.items():
                    dst.setdefault(a, schema)
            for child, parent in fresh_parent.items():
                parent_of.setdefault(child, parent)
            return current

    async def _resolve_also_types(
        self,
        entity: ExtractedEntity,
        primary_resolved: str,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        result: IngestResult,
        *,
        parent_of: dict[str, str] | None = None,
    ) -> list[str]:
        """Resolve genuine co-classifications (entity.also_types) so each exists
        in the ontology (ADR rule 1). Returns the resolved co-type names, deduped.

        Skips any co-type that is actually in the primary's subClassOf lineage
        (an ancestor or descendant) — those are recovered by query-time closure,
        not asserted. Only genuinely INDEPENDENT types are returned.

        ``parent_of`` (ONTA-268): CALL-LOCAL lineage map; falls back to
        ``self._parent_of``.
        """
        if not entity.also_types:
            return []
        from cograph_client.resolver.er import ancestor_chain

        parent_of = self._parent_of if parent_of is None else parent_of
        resolved: list[str] = []
        seen = {primary_resolved}
        for co in entity.also_types:
            if not co:
                continue
            proxy = ExtractedEntity(type_name=co, id=entity.id)
            rt = await self._resolve_type(
                proxy, graph_uri, existing_types, existing_attrs, result,
                parent_of=parent_of,
            )
            if not rt or rt in seen:
                continue
            # Same-lineage guard: skip if one is an ancestor of the other.
            if rt in ancestor_chain(primary_resolved, parent_of) or \
               primary_resolved in ancestor_chain(rt, parent_of):
                logger.info("also_type_in_lineage_skipped", primary=primary_resolved, co_type=rt)
                continue
            resolved.append(rt)
            seen.add(rt)
        return resolved

    async def _mint_subtype(
        self, graph_uri: str, type_name: str, subtype_description: str | None,
    ) -> None:
        """Create a NEW subtype's type declaration, carrying its description
        idempotently (FIX 3 + FIX 4).

        When a ``subtype_description`` is present it is written via
        :func:`upsert_type_comment`, which REPLACES the single-valued
        ``rdfs:comment`` instead of appending — so re-minting the same subtype
        across ingests can't accumulate duplicate comments — while leaving
        ``rdfs:subClassOf`` untouched (plain :func:`upsert_type` would CLEAR the
        edge a caller's ``insert_subtype`` creates). With no description we emit a
        plain ``insert_type`` (no comment), keeping the common no-description write
        byte-identical to before.
        """
        if subtype_description:
            await self._neptune.update(upsert_type_comment(graph_uri, type_name, subtype_description))
        else:
            await self._neptune.update(insert_type(graph_uri, type_name, ""))

    async def _resolve_type(
        self,
        entity: ExtractedEntity,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        result: IngestResult,
        *,
        parent_of: dict[str, str] | None = None,
    ) -> str | None:
        """Pass 1: Resolve the type for an entity. Returns resolved type name or None.

        ``parent_of`` (ONTA-268): CALL-LOCAL lineage map, threaded to the
        subtype/ancestor synthesis; falls back to ``self._parent_of``.

        The whole read-decide-WRITE of ontology existence runs under the
        ontology-write lock (ONTA-268) so concurrent per-sub-query resolvers
        sharing that lock serialize type creation — no two overlap between the
        "does this type exist / what does the matcher say" decision and the
        insert_type/insert_subtype that acts on it, which is what fragments the
        ontology under a raced ingest. The exact-name in-memory hit short-circuits
        BEFORE the lock (a pure read on the hot path — every repeated row of a
        known type — so it never contends). The lock does NOT cover the LLM
        EXTRACTION (`_extract`, upstream); it does cover the type-MATCH decision
        because that decision + its write must be atomic to avoid a race.
        """
        if entity.type_name in existing_types:
            return entity.type_name
        parent_of = self._parent_of if parent_of is None else parent_of
        async with self._ontology_lock:
            # ONTA-268: point the embedding pre-filter at THIS ingest's tenant
            # store under the lock, right before the match, so a single shared
            # TypeMatcher serving interleaved ingests can't read a clobbered
            # `_graph_uri` (the lock serializes the set→match→write, and in
            # production each per-sub-query resolver holds its own TypeMatcher).
            self._type_matcher._graph_uri = graph_uri
            if entity.same_as and entity.same_as in existing_types:
                match = await self._type_matcher.match(entity.type_name, "", existing_types)
                if match.verdict == MatchVerdict.SAME:
                    logger.info("type_same_as_verified", proposed=entity.type_name, resolved=match.resolved)
                    return match.resolved
                elif match.verdict == MatchVerdict.SUBTYPE:
                    # SUBTYPE branch — subtype_description legitimately describes this
                    # NEW subtype (FIX 3). Written idempotently (FIX 4): upsert
                    # REPLACES the single-valued rdfs:comment so re-minting the same
                    # type across ingests can't accumulate duplicate comments.
                    await self._mint_subtype(graph_uri, entity.type_name, entity.subtype_description)
                    sparql = insert_subtype(graph_uri, match.parent_type, entity.type_name)
                    await self._neptune.update(sparql)
                    logger.info("type_same_as_was_subtype", child=entity.type_name, parent=match.parent_type)
                    result.types_created.append(entity.type_name)
                    existing_types[entity.type_name] = ""
                    existing_attrs[entity.type_name] = {}
                    await self._synthesize_ancestors(
                        entity.type_name, match.parent_type, graph_uri,
                        existing_types, existing_attrs, result,
                        parent_chain=entity.parent_chain, parent_of=parent_of,
                    )
                    return entity.type_name
                elif match.inconclusive:
                    # Verifier couldn't reach a real decision (e.g. LLM unavailable).
                    # Trust the extractor's explicit same_as rather than fabricating a
                    # duplicate type — creating "Home" alongside "Property" is exactly
                    # the ontology pollution this verification step exists to prevent.
                    logger.info("type_same_as_trusted", proposed=entity.type_name, resolved=entity.same_as)
                    return entity.same_as
                else:
                    # same_as REJECTED → this is a genuine TOP-LEVEL type, not a
                    # subtype. subtype_description must NOT be written here (FIX 3):
                    # the field's contract is "describes a NEW SUBTYPE" only.
                    sparql = insert_type(graph_uri, entity.type_name, "")
                    await self._neptune.update(sparql)
                    logger.info("type_same_as_rejected", proposed=entity.type_name, claimed=entity.same_as)
                    result.types_created.append(entity.type_name)
                    existing_types[entity.type_name] = ""
                    existing_attrs[entity.type_name] = {}
                    return entity.type_name
            else:
                match = await self._type_matcher.match(entity.type_name, "", existing_types)
                if match.verdict == MatchVerdict.SAME:
                    logger.info("type_matched_existing", proposed=entity.type_name, resolved=match.resolved)
                    return match.resolved
                elif match.verdict == MatchVerdict.SUBTYPE:
                    # SUBTYPE branch — subtype_description describes this NEW subtype
                    # (FIX 3), written idempotently via upsert (FIX 4).
                    await self._mint_subtype(graph_uri, entity.type_name, entity.subtype_description)
                    sparql = insert_subtype(graph_uri, match.parent_type, entity.type_name)
                    await self._neptune.update(sparql)
                    logger.info("type_subtype", child=entity.type_name, parent=match.parent_type)
                    result.types_created.append(entity.type_name)
                    existing_types[entity.type_name] = ""
                    existing_attrs[entity.type_name] = {}
                    await self._synthesize_ancestors(
                        entity.type_name, match.parent_type, graph_uri,
                        existing_types, existing_attrs, result,
                        parent_chain=entity.parent_chain, parent_of=parent_of,
                    )
                    return entity.type_name
                elif match.verdict == MatchVerdict.FLAGGED:
                    # Top-level mint: do NOT write subtype_description here (FIX 3).
                    # If _link_parent then establishes a parent (the entity carried a
                    # parent_type/parent_chain), it upserts the description there —
                    # the only place the type is actually a subtype.
                    sparql = insert_type(graph_uri, entity.type_name, "")
                    await self._neptune.update(sparql)
                    result.types_created.append(entity.type_name)
                    existing_types[entity.type_name] = ""
                    existing_attrs[entity.type_name] = {}
                    await self._link_parent(
                        entity, graph_uri, existing_types, existing_attrs, result,
                        parent_of=parent_of,
                    )
                    logger.warning("type_flagged_for_review", proposed=entity.type_name)
                    result.flagged_types.append(entity.type_name)
                    return entity.type_name
                else:
                    # Top-level mint: no subtype_description here (FIX 3). _link_parent
                    # upserts it iff this turns out to be a subtype (parent_chain).
                    sparql = insert_type(graph_uri, entity.type_name, "")
                    await self._neptune.update(sparql)
                    result.types_created.append(entity.type_name)
                    existing_types[entity.type_name] = ""
                    existing_attrs[entity.type_name] = {}
                    await self._link_parent(
                        entity, graph_uri, existing_types, existing_attrs, result,
                        parent_of=parent_of,
                    )
                    # Governance seam: the genuinely-new type MAY also be proposed
                    # for the Global-Public layer. No-op unless the flag is on.
                    await self._maybe_govern_new_type(entity, graph_uri)
                    return entity.type_name

    async def _maybe_govern_new_type(self, entity: ExtractedEntity, graph_uri: str) -> None:
        """Governance seam (ADR 0002 §2, COG-43): propose a brand-new type for
        the shared Global-Public layer and, on majority judge approval, write
        a governed copy there with provenance + changelog.

        The tenant-layer write has ALREADY happened (today's behavior — the
        tenant uses the type immediately whatever the verdict); approval only
        ADDS a Public-layer copy.

        Scheduling (COG-46): the judge panel + Public-layer write run as a
        BACKGROUND task — ingest never waits on LLM judges. Semantics are
        eventually consistent: an approved type appears in the Public layer
        shortly AFTER ingest returns. Task references are retained on
        ``self._governance_tasks``; await :meth:`drain_governance` to
        deterministically wait for all scheduled outcomes. Best-effort: any
        failure (scheduling or in-task) is logged and never blocks or crashes
        ingest. No-op when COGRAPH_GOVERNANCE_ENABLED is off (default).
        """
        if not self._governance_enabled:
            return
        from cograph_client.resolver.governance import TypeProposal
        try:
            graphs_prefix = "https://cograph.tech/graphs/"
            tenant_id = (
                graph_uri[len(graphs_prefix):] if graph_uri.startswith(graphs_prefix) else graph_uri
            )
            proposal = TypeProposal(
                type_name=entity.type_name,
                parent_chain=list(entity.parent_chain),
                tenant_id=tenant_id,
                reasoning=(
                    f"Extractor proposed brand-new type '{entity.type_name}' "
                    f"matching no existing ontology type"
                ),
                proposer_model=self.EXTRACT_MODEL,
            )
            # Drop references to finished tasks so the list stays bounded on
            # long-lived resolvers, then schedule the panel off the ingest path.
            self._governance_tasks = [t for t in self._governance_tasks if not t.done()]
            self._governance_tasks.append(
                asyncio.create_task(self._govern_in_background(proposal))
            )
        except Exception:
            logger.warning("governance_failed", type_name=entity.type_name, exc_info=True)

    async def _govern_in_background(self, proposal) -> None:
        """Run propose-and-judge + the Public-layer write off the ingest path
        (COG-46). Exceptions are logged and swallowed here, inside the task —
        a governance failure never crashes ingest and never surfaces as an
        unretrieved task exception.
        """
        try:
            decision = await self._governance.propose_and_judge(proposal, self._judge_panel)
            if decision.approved:
                await self._governance.write_governed_type(proposal, decision)
            else:
                logger.info("governance_type_tenant_only", type_name=proposal.type_name)
        except Exception:
            logger.warning("governance_failed", type_name=proposal.type_name, exc_info=True)

    async def drain_governance(self) -> None:
        """Await all pending background governance tasks (COG-46).

        Governance is eventually consistent: :meth:`_maybe_govern_new_type`
        schedules the judge panel + Public-layer write as background tasks,
        so an approved type appears in the Public layer shortly after ingest
        returns. Call this to deterministically wait for every scheduled
        outcome — tests, and callers that need the Public layer settled
        before reading it. Safe to call any time (no-op with nothing
        pending). Task failures were already logged inside the tasks and are
        never re-raised here.
        """
        tasks, self._governance_tasks = self._governance_tasks, []
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _resolve_and_insert_entity(
        self,
        entity: ExtractedEntity,
        resolved_type: str,
        entity_uri: str,
        is_duplicate: bool,
        graph_uri: str,
        existing_types: dict[str, str],
        existing_attrs: dict[str, dict[str, AttributeSchema]],
        source: str,
        result: IngestResult,
        batch_id: str = "",
        _collect_triples: list[tuple[str, str, str]] | None = None,
        _collect_provenance: list[tuple[str, str, str]] | None = None,
        also_types: list[str] | None = None,
        _collect_text_values: dict[tuple[str, str], list[str]] | None = None,
        drop_placeholder_values: bool = False,
        *,
        instance_graph: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        """Pass 2: Resolve attributes, validate, and collect triples for one entity.

        ``instance_graph`` (ONTA-268): CALL-LOCAL target graph for the legacy
        per-entity insert / provenance write; falls back to ``self._instance_graph``.

        ``observed_at`` (ONTA-271): the run's ``onto/ingested_at`` timestamp. When
        given (the reentrant ``ingest`` path) it is used verbatim so a preserved-
        run_id replay writes the SAME ingested_at and the delta stays byte-
        identical; ``None`` (the CSV / legacy path, unchanged) falls back to
        wall-clock now.

        If _collect_triples is provided, triples are appended to that list instead of
        being inserted immediately. The caller is responsible for batch-inserting them.
        This is ~10-50x faster because it avoids per-entity Neptune INSERT calls.

        If _collect_provenance is provided (COG-46), per-fact provenance triples
        (when COGRAPH_PROVENANCE_ENABLED is on) are likewise appended for the
        caller to flush in one batched INSERT into the companion provenance
        graph, instead of being inserted here per entity.

        `also_types` are genuine independent co-classifications (ADR rule 1): each
        gets its own asserted rdf:type triple alongside the primary resolved_type.

        If _collect_text_values is provided (ONTA-177), validated STRING
        attribute values are sampled into it keyed by (resolved_type,
        resolved attr name) — free-text candidacy evidence the caller decides
        on after the write. Values only, never names: the name-blind
        classification happens downstream (ADR 0003 litmus).

        ``drop_placeholder_values`` (ONTA-259): on the model-proposed extraction
        path (text / JSON / web-discovery) drop any attribute whose VALUE is an
        obvious fabricated placeholder ("1234567890", "0000000000", "N/A", …)
        BEFORE it is resolved or written — a dropped value is treated as
        UNSTATED (the attribute is omitted, as if the source gave no value),
        counted via a structured log. OFF (default) for the authoritative CSV
        path, whose cells are written verbatim.
        """
        # ONTA-259: deterministic anti-fabrication backstop. Filter placeholder
        # VALUES up front so a hallucinated identifier is uniformly invisible to
        # EVERY downstream step (promotion, resolution, the write) — exactly as
        # if the source had never stated it. Prompt-forbidden too; this is the
        # model-agnostic defense-in-depth layer behind the prompt.
        if drop_placeholder_values and entity.attributes:
            kept_attrs = []
            for a in entity.attributes:
                if _is_fabricated_placeholder(a.value):
                    logger.info(
                        "discovery_placeholder_value_dropped",
                        entity_id=entity.id,
                        type_name=resolved_type,
                        attribute=a.name,
                        value=a.value,
                    )
                    continue
                kept_attrs.append(a)
            if len(kept_attrs) != len(entity.attributes):
                entity = entity.model_copy(update={"attributes": kept_attrs})

        type_attrs = existing_attrs.get(resolved_type, {})

        # Option D promotions
        promotions = check_promotion(entity, type_attrs)
        promoted_type_names: set[str] = set()
        for promo in promotions:
            if promo.promoted_type and promo.promoted_type not in promoted_type_names:
                promoted_type_names.add(promo.promoted_type)

        for ptype in promoted_type_names:
            if ptype not in existing_types:
                sparql = insert_type(graph_uri, ptype, f"Promoted from {resolved_type} attributes")
                await self._locked_ontology_update(sparql)  # ONTA-268
                result.types_created.append(ptype)
                existing_types[ptype] = ""
                existing_attrs[ptype] = {}

        rdf_type = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
        rdfs_label = "http://www.w3.org/2000/01/rdf-schema#label"

        # Duplicate entities skip rdf:type triple but still merge attributes
        if is_duplicate:
            triples_to_insert: list[tuple[str, str, str]] = []
        else:
            triples_to_insert: list[tuple[str, str, str]] = [
                (entity_uri, rdf_type, type_uri(resolved_type)),
                (entity_uri, rdfs_label, entity.id),
            ]
            # Multi-typing: emit an additional asserted rdf:type per genuine
            # co-classification (ADR rule 1). Ancestors are NOT asserted here —
            # they are recovered via query-time subclass closure.
            for co_type in (also_types or ()):
                if co_type and co_type != resolved_type:
                    triples_to_insert.append((entity_uri, rdf_type, type_uri(co_type)))

        promoted_entities: dict[str, str] = {}
        # Attribute assertions made for this entity — mirrors the attribute
        # appends to triples_to_insert so per-fact provenance (ADR 0002 §4)
        # can be emitted for them when enabled.
        attr_facts: list[tuple[str, str, str]] = []

        for attr in entity.attributes:
            promo_match = next(
                (p for p in promotions if p.name == attr.name.lower().replace(" ", "_").split("_", 1)[-1]
                 and p.promoted_type is not None),
                None,
            )
            if promo_match and promo_match.promoted_type:
                ptype = promo_match.promoted_type
                if ptype not in promoted_entities:
                    p_uri = f"{_entity_uri(ptype, entity.id)}-{ptype.lower()}"
                    promoted_entities[ptype] = p_uri
                    triples_to_insert.append((p_uri, rdf_type, type_uri(ptype)))
                    rel_pred = f"https://cograph.tech/onto/has_{ptype.lower()}"
                    triples_to_insert.append((entity_uri, rel_pred, p_uri))
                    # Post-write housekeeping must re-embed / re-stat the promoted
                    # node's TYPE too (Part 3), not just the subject type — else a
                    # pre-existing ptype that gains its first node this pass stays
                    # stale until its next write. See IngestResult.affected_types().
                    result.node_target_types.append(ptype)

                p_uri = promoted_entities[ptype]
                attr_name = promo_match.name
                p_attrs = existing_attrs.get(ptype, {})
                if attr_name not in p_attrs:
                    sparql = insert_attribute(graph_uri, ptype, attr_name, "", attr.datatype)
                    await self._locked_ontology_update(sparql)  # ONTA-268
                    result.attributes_added.append(f"{ptype}.{attr_name}")
                    existing_attrs.setdefault(ptype, {})[attr_name] = AttributeSchema(
                        name=attr_name, datatype=attr.datatype,
                    )

                pred_uri = attr_uri(ptype, attr_name)
                # ONTA-373: record the A3 clean outcome (passed/transformed/dropped
                # + reason) into the discovery ingest ledger BEFORE typing — mirrors
                # enrichment's _instance_triples_for_value. Purely additive: the
                # written triple is unchanged (validate_triple re-derives the same
                # CleanFact internally), this only makes the decision non-silent.
                result.clean_report.record(
                    clean_value(
                        attr.value, attr.datatype,
                        entity_id=entity.id, attribute=attr_name,
                    )
                )
                validated = validate_triple(
                    p_uri, pred_uri, attr.value, attr.datatype,
                    entity_id=entity.id, attribute_name=attr_name, type_name=ptype,
                )
                if isinstance(validated, ValidatedTriple):
                    triples_to_insert.append((validated.subject, validated.predicate, validated.object))
                    attr_facts.append((validated.subject, validated.predicate, validated.object))
                    # ONTA-347: preserve the ORIGINAL surface form (attr_meta
                    # companion) when A3 coerced/canonicalized it — rides the SAME
                    # write path, but NOT attr_facts (metadata OF the attribute, not
                    # a domain fact, so it gets no provenance record of its own).
                    if validated.surface_form_companion:
                        triples_to_insert.append(validated.surface_form_companion)
                    result.triples_inserted += 1
                else:
                    result.rejections.append(validated)

                resolved = resolve_attribute(attr, type_attrs)
                if resolved.action == AttrAction.EXTEND:
                    sparql = insert_attribute(graph_uri, resolved_type, resolved.name, "", resolved.datatype)
                    await self._locked_ontology_update(sparql)  # ONTA-268
                    result.attributes_added.append(f"{resolved_type}.{resolved.name}")
                    type_attrs[resolved.name] = AttributeSchema(name=resolved.name, datatype=resolved.datatype)

                pred_uri = attr_uri(resolved_type, resolved.name)
                # ONTA-373: record the A3 clean outcome into the ingest ledger.
                result.clean_report.record(
                    clean_value(
                        resolved.value, resolved.datatype,
                        entity_id=entity.id, attribute=resolved.name,
                    )
                )
                validated = validate_triple(
                    entity_uri, pred_uri, resolved.value, resolved.datatype,
                    entity_id=entity.id, attribute_name=resolved.name,
                    type_name=resolved_type,
                )
                if isinstance(validated, ValidatedTriple):
                    triples_to_insert.append((validated.subject, validated.predicate, validated.object))
                    attr_facts.append((validated.subject, validated.predicate, validated.object))
                    # ONTA-347: preserve the ORIGINAL surface form on transform.
                    if validated.surface_form_companion:
                        triples_to_insert.append(validated.surface_form_companion)
                    result.triples_inserted += 1
                else:
                    result.rejections.append(validated)
                continue

            resolved = resolve_attribute(attr, type_attrs)

            if resolved.action == AttrAction.EXTEND:
                sparql = insert_attribute(graph_uri, resolved_type, resolved.name, "", resolved.datatype)
                await self._locked_ontology_update(sparql)  # ONTA-268
                result.attributes_added.append(f"{resolved_type}.{resolved.name}")
                type_attrs[resolved.name] = AttributeSchema(name=resolved.name, datatype=resolved.datatype)

            if resolved.datatype not in PRIMITIVE_TYPES:
                # This attribute is TYPED as a relationship to `resolved.datatype`
                # (a non-primitive type name), not a literal — so its value is
                # another entity and MUST be minted as a node reached by an edge,
                # never stored as a bare string. Two cases converge here:
                #   * DECLARED (warm): the ontology already declares this an object
                #     property whose range is `resolved.datatype` (an existing type)
                #     — the original promotion path.
                #   * COLD START: THIS extraction typed the attribute as a
                #     relationship (LLM emitted `datatype=<Type>`), so the EXTEND
                #     branch above already declared the object property with
                #     `rdfs:range = types/<datatype>` (insert_attribute maps a
                #     non-primitive datatype to a type URI). Without minting the type
                #     the schema carried a DANGLING range (an object property whose
                #     range type was never created) and the value fell to the literal
                #     path — a literal on attrs/<leaf>, INVISIBLE to NL relationship
                #     traversal, with no target node (the #123-class bug, in the
                #     cold-start branch). Create the target type so the schema is
                #     internally consistent and the edge is NL-queryable.
                # This never OVER-PROMOTES: a plain literal attribute has a PRIMITIVE
                # datatype and takes the else-branch below unchanged — only an
                # attribute EXPLICITLY typed as a relationship is minted as a node.
                if resolved.datatype not in existing_types:
                    await self._locked_ontology_update(  # ONTA-268
                        insert_type(
                            graph_uri,
                            resolved.datatype,
                            f"Relationship target of {resolved_type}.{resolved.name}",
                        )
                    )
                    result.types_created.append(resolved.datatype)
                    existing_types[resolved.datatype] = ""
                    existing_attrs.setdefault(resolved.datatype, {})
                target_uri = _entity_uri(resolved.datatype, resolved.value)
                # Relationship INSTANCE edge → onto/<leaf>. That is the ONLY
                # predicate the NL→SPARQL planner queries a type-ranged attribute on
                # (nlp/ontology_embeddings publishes onto/<leaf> for relationships,
                # with NO attrs/<leaf> fallback), so an edge on attrs/<leaf> is
                # invisible to NL — the exact bug enrichment hit in #123 and fixed in
                # #126. The attrs/<leaf> predicate is the ontology DECLARATION of the
                # property (its range names the target type, via insert_attribute),
                # NOT the instance edge. Matches enrichment
                # (executor._instance_triples_for_value) and the sibling has_<ptype>
                # promotion edge above — both on onto/<leaf>.
                onto_pred = f"https://cograph.tech/onto/{resolved.name}"
                triples_to_insert.append((entity_uri, onto_pred, target_uri))
                attr_facts.append((entity_uri, onto_pred, target_uri))
                # Materialize the target as a FIRST-CLASS node: emit its rdf:type +
                # rdfs:label too. Without them the promoted node is bare — untyped,
                # unlabelled, invisible to "list all <Type>" queries — even though
                # the edge points at it. Mirrors enrichment's node-linking
                # (executor._instance_triples_for_value) so discovery + enrichment
                # mint the identical shared NODE for the same real-world thing.
                # NOT added to attr_facts: this is node materialization, not a fact
                # ABOUT the subject — same as how the subject's own rdf:type/label
                # are emitted untracked above.
                triples_to_insert.append((target_uri, rdf_type, type_uri(resolved.datatype)))
                triples_to_insert.append((target_uri, rdfs_label, resolved.value))
                # refresh coverage (Part 3): the newly-minted node's TYPE must be
                # re-embedded / re-stat'd now, not only on its next write.
                result.node_target_types.append(resolved.datatype)
                result.triples_inserted += 1
            else:
                pred_uri = attr_uri(resolved_type, resolved.name)
                # ONTA-373: record the A3 clean outcome (passed/transformed/dropped
                # + reason) into the discovery ingest ledger. This is the primary
                # literal path — a non-conforming value that yields NO triple below
                # becomes a RECORDED `dropped` entry, not a silent skip. Additive:
                # the write is unchanged.
                result.clean_report.record(
                    clean_value(
                        resolved.value, resolved.datatype,
                        entity_id=entity.id, attribute=resolved.name,
                    )
                )
                validated = validate_triple(
                    entity_uri, pred_uri, resolved.value, resolved.datatype,
                    entity_id=entity.id, attribute_name=resolved.name,
                    type_name=resolved_type,
                )
                if isinstance(validated, ValidatedTriple):
                    triples_to_insert.append((validated.subject, validated.predicate, validated.object))
                    attr_facts.append((validated.subject, validated.predicate, validated.object))
                    # ONTA-347: preserve the ORIGINAL surface form on transform.
                    if validated.surface_form_companion:
                        triples_to_insert.append(validated.surface_form_companion)
                    result.triples_inserted += 1
                    # ONTA-177: sample validated string values as free-text
                    # candidacy evidence (bounded per attribute).
                    if _collect_text_values is not None and resolved.datatype == "string":
                        samples = _collect_text_values.setdefault(
                            (resolved_type, resolved.name), [],
                        )
                        if len(samples) < _TEXT_EVIDENCE_MAX_VALUES:
                            samples.append(validated.object)
                else:
                    result.rejections.append(validated)

        # Per-fact provenance (ADR 0002 §4), gated by COGRAPH_PROVENANCE_ENABLED
        # (default off). Statement-metadata triples target the COMPANION
        # provenance graph — a different graph than the instance-triple
        # collector. With a _collect_provenance collector (the batched fast
        # path, COG-46) they accumulate for ONE batched INSERT by the caller;
        # without one they are inserted here per entity (legacy path).
        # Confidence is 1.0 for directly-ingested facts.
        if self._provenance_enabled and attr_facts:
            instance_graph = (
                instance_graph if instance_graph is not None
                else getattr(self, "_instance_graph", graph_uri)
            )
            prov_ts = datetime.now(timezone.utc)
            prov_triples: list[tuple[str, str, str]] = []
            for s, p, o in attr_facts:
                prov_triples.extend(build_provenance_triples(
                    s, p, o, source=source, confidence=1.0,
                    timestamp=prov_ts, graph_uri=instance_graph,
                ))
            if _collect_provenance is not None:
                _collect_provenance.extend(prov_triples)
            else:
                for sparql in batched_insert_triples(provenance_graph_uri(instance_graph), prov_triples):
                    await self._neptune.update(sparql)

        # Per-attribute DISPLAY provenance companions (ONTA-245 F1), gated by
        # COGRAPH_DISCOVERY_ATTR_PROVENANCE (default off). The SAME
        # `<attr>_source_url` / `<attr>_verified_at` instance companions enrichment
        # always writes, so a DISCOVERED fact and an ENRICHED fact are
        # provenance-symmetric at the ATTRIBUTE level (not just the per-record
        # `onto/source`). Built via the shared builder and appended to
        # `triples_to_insert` so they flow through the SAME shared write path
        # (insert_facts) as every other fact — no separate writer. The record's
        # `source` (a URL for web discovery) becomes each attribute's
        # `_source_url`/`_provenance`; the freshness stamp is now-UTC (first-seen).
        if self._attr_provenance_enabled and attr_facts:
            attr_prov_ts = datetime.now(timezone.utc)
            for s, p, _o in attr_facts:
                leaf = p.rstrip("/").rsplit("/", 1)[-1]
                if not leaf:
                    continue
                triples_to_insert.extend(
                    build_attribute_provenance_companions(
                        s,
                        resolved_type,
                        leaf,
                        source_url=source if _looks_like_url(source) else "",
                        provenance=source or "",
                        verified_at=attr_prov_ts,
                    )
                )

        # Provenance triples. ingested_at is sourced from the run's observed_at
        # (ONTA-271) when threaded, so a preserved-run_id replay writes the
        # identical stamp (idempotent) instead of a fresh wall-clock nonce;
        # legacy/CSV callers pass None → wall-clock now, unchanged.
        now = (observed_at or datetime.now(timezone.utc)).isoformat()
        triples_to_insert.append((entity_uri, "https://cograph.tech/onto/ingested_at", now))
        if source:
            triples_to_insert.append((entity_uri, "https://cograph.tech/onto/source", source))
        if batch_id:
            triples_to_insert.append((entity_uri, BATCH_PREDICATE, batch_id))

        # Collect triples for batch insert (or insert immediately if no collector)
        if triples_to_insert:
            if _collect_triples is not None:
                _collect_triples.extend(triples_to_insert)
                result.triples_inserted += len(triples_to_insert)
            else:
                # Legacy path: insert per-entity (used when called without collector)
                instance_graph = (
                    instance_graph if instance_graph is not None
                    else getattr(self, "_instance_graph", graph_uri)
                )
                for sparql in batched_insert_triples(instance_graph, triples_to_insert):
                    await self._neptune.update(sparql)
                result.triples_inserted += len(triples_to_insert)

    # --- ONTA-177: free-text candidacy (semantic instance index) ------------

    async def _mark_free_text_attributes(
        self,
        graph_uri: str,
        text_values: dict[tuple[str, str], list[str]],
        result: IngestResult,
    ) -> None:
        """Decide + persist free-text candidacy for schema-pass attributes.

        The seam lives HERE (not only in the CSV resolver) so every ingest
        modality that runs a schema pass — text, JSON ``/ingest``, and
        web-discovery — produces ``textKind`` markers, not just CSV
        (ONTA-177: candidacy must not be CSV-only).

        Two-tier decision, mirroring the CSV pipeline's:

        1. Name-blind classification of the sampled values
           (:func:`classify_text_candidacy` — the profiler's ``ValueShape.TEXT``
           proposes; ADR 0003 litmus: no attribute-name inspection here).
           Unambiguously long prose is marked directly.
        2. The AMBIGUOUS band (text-shaped but borderline: could be addresses,
           org names, composite titles) goes to ONE LLM adjudication call
           (:meth:`_adjudicate_free_text`) — the REASON layer, the only place
           the attribute NAME may be consulted.

        Confirmed attributes get the single-valued, idempotent
        ``<attr> <onto/textKind> "free_text"`` upsert; attributes the LLM
        EXPLICITLY declined get the durable decided-no ``"not_text"`` upsert
        (ONTA-173: an unpersisted NO is indistinguishable from never-decided —
        the reconciler would re-sample it every run and its name-blind
        ≥120-char auto tier could later overrule the LLM). Non-candidates
        (non-TEXT shapes) are never marked at all — absence = not-a-candidate,
        and the reconciler's cheap heuristic re-classifies them itself. Both
        upserts are written alongside the other schema-apply attribute upserts,
        and the tenant's marker cache is invalidated HERE (the write site owns
        it — refresh_after_write deliberately doesn't). Best-effort throughout:
        any failure logs a warning and never blocks or fails the ingest (the
        ONTA-181 reconciler heuristic can revisit undecided attributes).
        """
        try:
            auto: list[tuple[str, str]] = []
            ambiguous: dict[tuple[str, str], list[str]] = {}
            for (type_name, attr_name), values in text_values.items():
                verdict = classify_text_candidacy(values)
                if verdict is TextCandidacy.FREE_TEXT:
                    auto.append((type_name, attr_name))
                elif verdict is TextCandidacy.AMBIGUOUS:
                    ambiguous[(type_name, attr_name)] = values
            confirmed: set[tuple[str, str]] = set(auto)
            declined: set[tuple[str, str]] = set()
            if ambiguous:
                adjudicated_yes, adjudicated_no = await self._adjudicate_free_text(
                    ambiguous
                )
                confirmed |= adjudicated_yes
                declined |= adjudicated_no - confirmed
            for type_name, attr_name in sorted(confirmed):
                await self._neptune.update(
                    upsert_attribute_text_kind(graph_uri, type_name, attr_name)
                )
                result.free_text_attributes.append(f"{type_name}.{attr_name}")
            for type_name, attr_name in sorted(declined):
                await self._neptune.update(
                    upsert_attribute_text_kind(
                        graph_uri, type_name, attr_name, TEXT_KIND_NOT_TEXT
                    )
                )
            if confirmed or declined:
                # Marker write site self-invalidates (mirrors the reconciler's
                # heuristic) so query-side consumers see the fresh verdicts
                # before the TTL; the TTL stays the cross-process backstop.
                invalidate_text_marker_cache(graph_uri)
                logger.info(
                    "free_text_attributes_marked",
                    auto=len(auto),
                    adjudicated=len(confirmed) - len(auto),
                    declined=len(declined),
                    attributes=sorted(f"{t}.{a}" for t, a in confirmed),
                    not_text_attributes=sorted(f"{t}.{a}" for t, a in declined),
                )
        except Exception:
            logger.warning("free_text_marking_failed", exc_info=True)

    async def _adjudicate_free_text(
        self, candidates: dict[tuple[str, str], list[str]],
    ) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        """One REASON-layer LLM call adjudicating AMBIGUOUS free-text candidates.

        This is the only layer where the attribute NAME is consulted
        (ADR 0003 keeps names out of the deterministic layers; ONTA-177).
        Returns ``(confirmed, declined)`` — the ``(type_name, attr_name)``
        pairs the model judged free-running prose, and the pairs it EXPLICITLY
        judged not (``free_text`` falsy in its response). Both sets are
        filtered to the offered candidate set — the model cannot mint (or
        decline) candidacy for attributes the name-blind classifier never
        proposed. A candidate absent from the response stays UNDECIDED (in
        neither set): a genuine adjudication is required before ONTA-173's
        durable ``not_text`` marker may be persisted. Fail-closed and
        best-effort: any LLM/parse failure returns two empty sets (attributes
        stay unmarked AND undecided; a later re-ingest or the ONTA-181
        reconciler heuristic gets another look), never raises.
        """
        try:
            lines = []
            for (type_name, attr_name), values in sorted(candidates.items()):
                samples = [
                    v[:_TEXT_ADJUDICATION_SAMPLE_MAX_LEN]
                    for v in values[:_TEXT_ADJUDICATION_SAMPLES]
                ]
                lines.append(json.dumps({
                    "type": type_name,
                    "attribute": attr_name,
                    "sample_values": samples,
                }))
            user_content = TEXT_CANDIDACY_USER.format(
                n_samples=_TEXT_ADJUDICATION_SAMPLES,
                candidates="\n".join(lines),
            )
            if self.EXTRACT_PROVIDER == "openrouter" and self._openrouter_key:
                text = await openrouter_chat(
                    self._openrouter_key,
                    TEXT_CANDIDACY_SYSTEM,
                    user_content,
                    model=self.EXTRACT_MODEL,
                    temperature=0,
                    max_tokens=2048,
                    timeout=60,
                )
            else:
                msg = await self._anthropic.messages.create(
                    model=self.INFER_MODEL,
                    max_tokens=2048,
                    system=TEXT_CANDIDACY_SYSTEM,
                    messages=[{"role": "user", "content": user_content}],
                )
                text = msg.content[0].text
            stripped = text.strip()
            if stripped.startswith("```"):
                stripped = "\n".join(
                    l for l in stripped.split("\n") if not l.strip().startswith("```")
                )
            data = json.loads(stripped)
            confirmed: set[tuple[str, str]] = set()
            declined: set[tuple[str, str]] = set()
            for item in data.get("attributes", []):
                if not isinstance(item, dict):
                    continue
                key = (str(item.get("type")), str(item.get("attribute")))
                if key not in candidates:
                    continue  # offered candidates only — never mint new ones
                if item.get("free_text"):
                    confirmed.add(key)
                else:
                    # An entry the model returned with free_text falsy is a
                    # genuine adjudicated NO — persisted durably by the caller.
                    declined.add(key)
            logger.info(
                "free_text_adjudicated",
                candidates=len(candidates),
                confirmed=len(confirmed),
                declined=len(declined),
            )
            return confirmed, declined
        except Exception:
            logger.warning(
                "free_text_adjudication_failed",
                candidates=len(candidates),
                exc_info=True,
            )
            return set(), set()

    async def _apply_mapping_text_markers(
        self,
        mapping: CSVSchemaMapping,
        resolved_by_decl_type: dict[str, str],
        graph_uri: str,
        result: IngestResult,
    ) -> None:
        """Persist a mapping's schema-time ``text_kind`` verdicts as markers.

        The CSV pipeline decides candidacy ONCE, at schema-inference time
        (profiler proposes → REASON pass adjudicates → the verdict rides on
        ``ColumnMapping.text_kind``, ONTA-177); this applies that verdict at
        schema-apply time as the idempotent ``textKind`` upsert on the
        RESOLVED attribute URI (the mapping's declared type may have been
        matched onto an existing ontology type). BOTH verdict polarities are
        persisted (ONTA-173): ``"free_text"`` marks the attribute for the
        semantic index; ``"not_text"`` (the REASON pass explicitly declined a
        TEXT-shaped column) durably records the decided NO so the reconciler
        stops re-sampling it and its name-blind auto tier can never overrule
        the LLM. Attribute names are normalized exactly like the ingest pass
        normalizes them (:func:`_normalize_attr_name`) so the marker lands on
        the same attr URI the instance triples use. Legacy / hand-written
        mappings carry no ``text_kind`` → no markers, no LLM (candidacy
        undecided; the reconciler-side default heuristic covers those later —
        ONTA-181). After any marker write the tenant's marker cache is
        invalidated HERE (write sites own it — refresh_after_write
        deliberately doesn't). Best-effort: failures log a warning and never
        block ingest.
        """
        try:
            specs_by_name = {s.name: s for s in (mapping.entities or [])}
            seen: set[tuple[str, str]] = set()
            marked_free_text: list[str] = []
            marked_not_text: list[str] = []
            for col in mapping.columns:
                if col.role != ColumnRole.ATTRIBUTE or col.text_kind not in (
                    TEXT_KIND_FREE_TEXT,
                    TEXT_KIND_NOT_TEXT,
                ):
                    continue
                if col.entity and col.entity in specs_by_name:
                    decl_type = specs_by_name[col.entity].type_name
                else:
                    decl_type = mapping.entity_type
                if not decl_type:
                    continue
                resolved_type = resolved_by_decl_type.get(decl_type, decl_type)
                attr_name = _normalize_attr_name(col.attribute_name or col.column_name)
                key = (resolved_type, attr_name)
                if not attr_name or key in seen:
                    continue
                seen.add(key)
                await self._neptune.update(
                    upsert_attribute_text_kind(
                        graph_uri, resolved_type, attr_name, col.text_kind
                    )
                )
                if col.text_kind == TEXT_KIND_FREE_TEXT:
                    result.free_text_attributes.append(f"{resolved_type}.{attr_name}")
                    marked_free_text.append(f"{resolved_type}.{attr_name}")
                else:
                    marked_not_text.append(f"{resolved_type}.{attr_name}")
            if seen:
                # Marker write site self-invalidates (mirrors the reconciler's
                # heuristic); the TTL stays the cross-process backstop.
                invalidate_text_marker_cache(graph_uri)
                logger.info(
                    "free_text_mapping_markers_applied",
                    attributes=sorted(marked_free_text),
                    not_text_attributes=sorted(marked_not_text),
                )
        except Exception:
            logger.warning("free_text_mapping_markers_failed", exc_info=True)
