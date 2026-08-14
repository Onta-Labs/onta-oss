from __future__ import annotations

"""LLM extraction prompt templates for SchemaResolver.

Job: own the open / constrained / soft-focus system+user strings used by
``_extract``. Do not fork a second prompt family in another rail — change
these strings here (and the tests that pin them) so every caller sees one
wording.
"""

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

Never fabricate attributes (names OR values):
Extract only facts the source actually STATES. Unknown → omit (null), never \
invent. This covers BOTH attribute NAMES and attribute VALUES:
- NAMES: emit an attribute only when the source states that concept. Do NOT \
mint speculative attribute families the page never mentions (e.g. inventing \
``online_activity_percentage_of_summer_instruction`` or \
``affordability_ranking`` when the source says nothing about those). An \
unstated field is omitted entirely — not filled with a made-up name.
- VALUES: when the text does not give a value for an attribute, that attribute \
is UNKNOWN — OMIT it entirely (leave it out; never emit it with a made-up \
value and never null-pad it). NEVER invent an identifier, code, NPI, SKU, \
price, date, phone number, ranking, percentage, or any other value to fill a \
field: a value you cannot find is omitted, not guessed. Do NOT emit \
placeholder filler such as "1234567890", "0000000000", "123-45-6789", "N/A", \
"unknown", or "TBD" — a fabricated identifier silently corrupts every join \
keyed on it, so a missing value is correct and a made-up one is a bug.
Two records that both lack a field must NOT share a hallucinated stand-in \
value for it.

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
Existing ontology (prefer these type names and EXACT attribute names when the \
content is about the same concepts — do NOT invent synonyms like summary for \
description, or reason for rationale, when those attributes already exist):
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
distinguishing role as a bare string attribute. BUT when the confirmed focus \
already NAMES the kind and records differ only by surface label (College / \
University / PublicInstitution all under a confirmed Institution focus), keep them \
ALL as the focus type — do NOT spin up near-synonym subtypes the user did not ask \
to separate.
- REAL-WORLD THINGS BECOME NODES. When an attribute value is itself a reusable \
real-world entity — a place (city, state, country), an organization, a person, a \
category / specialty / sector — model it as its OWN entity reached by a \
relationship, so rows sharing that value share ONE node. Split a composite like \
"City, State" into the two nodes it names. A value that is NOT a proper name of \
that kind — a bare year or number, a URL or navigation fragment, a slug, or \
truncated text — is NOT a real-world entity: keep it a LITERAL, never a node.
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
- NEVER FABRICATE ATTRIBUTES (NAMES OR VALUES). Extract only what the source \
STATES. Unknown → omit (null), never invent. If a requested attribute (an \
identifier, code, NPI, price, date, phone, ranking, percentage, …) is not \
given for a record, OMIT it — never invent a value, never null-pad, and never \
emit placeholder filler like "1234567890", "0000000000", "N/A", "unknown", or \
"TBD". Do NOT mint extra attribute names for concepts the source never states \
(no speculative families like ``online_activity_percentage_of_summer_instruction`` \
or ``affordability_ranking`` when the page is silent). Two records that both \
lack a field must NOT share a hallucinated stand-in. Never MERGE two requested \
fields into one compound name (e.g. ``website_city`` from ``website`` + ``city``); \
keep each requested attribute SEPARATE. A made-up identifier or fabricated \
attribute silently corrupts joins and the ontology, so a missing field is correct \
and an invented one is a bug.
The focus type + expected attributes below say what to look for; add exactly the \
structure the data justifies and keep it tight."""

# ONTA-382: appended to EXTRACTION_TARGET_SYSTEM when attributes_exhaustive is
# True. Soft type decomposition stays (subtypes, real-world nodes, relationships);
# the listed attributes become a hard CEILING on focus-type records.
EXTRACTION_TARGET_ATTR_CEILING = """\

ATTRIBUTE CEILING (overrides the "guide, not a limit" reading of attributes above):
The listed attributes for each focus type are EXHAUSTIVE — emit ONLY those \
attributes (plus each entity's key/identifier: name/label/title) on focus-type \
records. Do NOT invent, inventively rename, or emit extra attributes on the focus \
type just because the source happens to carry them. Off-type nodes you lift out \
(places, orgs, categories) may keep their own identifying attributes; measurements \
about the focus subject still attach to the focus subject as literals from the \
listed set only."""

EXTRACTION_TARGET_USER_TEMPLATE = """\

FOCUS — you are collecting records of:
{constraint_lines}
Model each record with its most specific type (subtypes encouraged), lift \
real-world values (places, orgs, people, categories) into their own nodes via \
relationships, split multi-valued fields into separate assertions, and keep pure \
measurements / identifiers as literals. The focus type + attributes are a guide, \
not a limit — add the structure the data justifies, reuse types, stay compact."""

EXTRACTION_TARGET_USER_CEILING_TEMPLATE = """\

FOCUS — you are collecting records of:
{constraint_lines}
Model each record with its most specific type (subtypes encouraged), lift \
real-world values (places, orgs, people, categories) into their own nodes via \
relationships, split multi-valued fields into separate assertions, and keep pure \
measurements / identifiers as literals. For FOCUS-TYPE records, emit ONLY the \
listed attributes (plus name/label/title) — that list is a CEILING, not a floor. \
Off-type nodes you lift out keep their own identity attributes. Reuse types, \
stay compact."""

