"""Data models for the schema resolver pipeline."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from cograph_client.api_registry.spec import AuthorityLevel

_URI_SCHEME = "://"


# ---------------------------------------------------------------------------
# LLM extraction output (non-deterministic, proposed)
# ---------------------------------------------------------------------------


class ExtractedAttribute(BaseModel):
    """A single attribute proposed by the LLM extractor."""

    name: str
    value: str
    datatype: str = "string"
    # ONTA-272: OPTIONAL evidence span / citation supporting this attribute value
    # (a source URL or the source snippet it was drawn from). Default "" keeps the
    # A2 models back-compat — existing extraction that never sets it parses and
    # validates unchanged; the pre-structured fast path populates it from the
    # per-record source_url so an A2 payload can be asserted EVIDENCE-LINKED.
    evidence: str = ""

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_scalar(cls, v):
        """Stringify non-string SCALAR values the extractor returns.

        The LLM / Firecrawl JSON extraction legitimately emits a bare ``true`` /
        ``false`` or a number for a boolean- or numeric-valued attribute (e.g.
        ``streaming_support: true``, ``context_window: 8192``). ``value`` is
        typed ``str``, so Pydantic v2 would raise ``ValidationError`` on those and
        — because the extraction handler only caught JSON/Key/Type errors and
        ``ValidationError`` subclasses ``ValueError`` — the error propagated and
        failed the WHOLE discovery job with 0 records. Coerce genuine scalars to
        their string form here so extraction proceeds; the downstream validator
        (#166 ``_typed_value``) still canonicalizes the lexical form.

        ``bool`` maps to lowercase ``"true"``/``"false"`` — the canonical
        ``xsd:boolean`` lexical form the validator expects. ``None`` / dict /
        list are left untouched so they fall through to the existing validation
        (not silently swallowed).
        """
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return v


class ExtractedEntity(BaseModel):
    """An entity proposed by the LLM extractor."""

    type_name: str = Field(description="Proposed type name (e.g. 'Property', 'Address')")
    id: str = Field(description="Identifier for this entity (name, URI, or generated)")
    same_as: str | None = Field(default=None, description="Existing type name if this is the same concept")
    parent_type: str | None = Field(default=None, description="Existing type name if this is a subtype")
    parent_chain: list[str] = Field(
        default_factory=list,
        description=(
            "Full ancestor lineage of type_name, most-specific first "
            "(e.g. Condo -> ['Property', 'Asset']). Lets ingest close a brand-new "
            "multi-level subClassOf chain in one row (ADR 0001 rule 3). May include "
            "types not yet in the ontology."
        ),
    )
    also_types: list[str] = Field(
        default_factory=list,
        description=(
            "Genuine ADDITIONAL independent classifications (NOT ancestors of "
            "type_name) — e.g. a hotel employee who is also a guest: type_name="
            "'Employee', also_types=['Guest']. Each becomes a separate asserted "
            "rdf:type (ADR 0001 rule 1). Leave empty unless the entity truly IS "
            "two unrelated things."
        ),
    )
    subtype_description: str | None = Field(
        default=None,
        description=(
            "A brief, human-readable definition of type_name, set ONLY when "
            "type_name is a NEW specialized kind (a subtype) the extractor is "
            "minting — e.g. a 'HumannessIndex' subtype of Score: \"a score "
            "measuring how human a generated voice sounds\". Written as the new "
            "type's rdfs:comment so the ontology carries the definition. Leave "
            "null for pre-existing types and ordinary top-level types."
        ),
    )
    attributes: list[ExtractedAttribute] = Field(default_factory=list)
    # ONTA-272: OPTIONAL evidence span / citation supporting this candidate entity
    # (the source URL / snippet it was drawn from). Default "" keeps A2 back-compat;
    # the pre-structured fast path fills it from the per-record source_url so the
    # zero-ontology-commitment contract can assert the payload is EVIDENCE-LINKED.
    evidence: str = ""


class ExtractedRelationship(BaseModel):
    """A relationship between two extracted entities."""

    source_id: str
    predicate: str
    target_id: str


class ExtractionResult(BaseModel):
    """Full output of the LLM extraction step."""

    entities: list[ExtractedEntity] = Field(default_factory=list)
    relationships: list[ExtractedRelationship] = Field(default_factory=list)
    source_text: str = ""


# ---------------------------------------------------------------------------
# A2 zero-ontology-commitment contract (ONTA-272)
# ---------------------------------------------------------------------------
#
# A2 (``ExtractionResult``) is the CANDIDATE-FACTS tier of the P2/P5 seam: the
# extractor PROPOSES soft-typed, evidence-linked candidates and the downstream
# placement layer (P5) decides their final ontology home. "Zero ontology
# commitment" means A2 must never HARD-commit a type TO the ontology — it emits
# candidate NAMES (with their soft lineage suggestions), never a resolved /
# committed ontology reference. Soft lineage (``parent_chain`` / ``also_types`` /
# subtypes) is DELIBERATELY preserved: those are SUGGESTIONS for P5, not
# commitments (ONTA-199 soft-seed extraction beat the hard cage precisely because
# it keeps them). The ONE thing A2 may not do is smuggle a COMMITTED ontology IRI
# into a type slot — a resolved reference pre-empts P5's placement decision. The
# helpers below make that contract explicit + testable, and the pre-structured
# fast path builds a valid A2 from already-structured rows without the LLM.


class SoftContractViolation(ValueError):
    """Raised when an A2 payload breaks the zero-ontology-commitment contract."""


def _is_committed_type_ref(name: str | None) -> bool:
    """True when a type slot carries a COMMITTED ontology reference (a URI) rather
    than a soft candidate NAME. A candidate type is a bare identifier
    ("Physician", "NursePractitioner"); a committed reference is a resolved IRI
    ("https://cograph.tech/types/Physician") — the hard-commitment leak A2 must
    never carry."""
    return bool(name) and _URI_SCHEME in str(name)


def validate_soft_a2(
    result: ExtractionResult, *, require_evidence: bool = False
) -> list[str]:
    """Check an A2 payload (``ExtractionResult``) is SOFT-TYPED-ONLY and (opt-in)
    EVIDENCE-LINKED. Returns a list of human-readable violations — an EMPTY list
    means the payload honors the zero-ontology-commitment contract.

    Assertions (additive + back-compat — existing extraction passes unchanged):
      * every entity proposes a candidate ``type_name`` (a non-empty NAME);
      * NO type slot (``type_name`` / ``same_as`` / ``parent_type`` /
        ``parent_chain`` / ``also_types``) carries a COMMITTED ontology IRI — a
        candidate is a bare name; a URI is a hard commitment that pre-empts P5;
      * when ``require_evidence`` is True, every entity is evidence-linked — it
        carries its own ``evidence`` span or at least one attribute that does.

    Soft lineage is NOT a violation — it is the correct, preserved suggestion the
    placement layer consumes. NEVER re-cage extraction to satisfy this."""
    violations: list[str] = []
    if not isinstance(result, ExtractionResult):
        return [f"A2 payload is not an ExtractionResult (got {type(result).__name__})"]
    for i, e in enumerate(result.entities):
        if not (e.type_name or "").strip():
            violations.append(f"entity[{i}] (id={e.id!r}) has no candidate type_name")
        slots: list[tuple[str, str | None]] = [
            ("type_name", e.type_name),
            ("same_as", e.same_as),
            ("parent_type", e.parent_type),
        ]
        slots += [("parent_chain", p) for p in e.parent_chain]
        slots += [("also_types", a) for a in e.also_types]
        for slot, val in slots:
            if _is_committed_type_ref(val):
                violations.append(
                    f"entity[{i}] (id={e.id!r}) {slot}={val!r} is a committed "
                    "ontology reference (URI), not a soft candidate — A2 must "
                    "emit candidate type NAMES only (zero ontology commitment)"
                )
        if require_evidence:
            linked = bool((e.evidence or "").strip()) or any(
                (a.evidence or "").strip() for a in e.attributes
            )
            if not linked:
                violations.append(
                    f"entity[{i}] (id={e.id!r}) is not evidence-linked (no evidence "
                    "span on the entity or any of its attributes)"
                )
    return violations


def assert_soft_a2(
    result: ExtractionResult, *, require_evidence: bool = False
) -> None:
    """Raise :class:`SoftContractViolation` if ``result`` breaks the A2 contract
    (soft-typed-only + optionally evidence-linked). The fatal enforcement seam for
    the DETERMINISTIC pre-structured fast path, where a violation can only mean a
    code bug (structured rows are provably soft), so failing fast is correct. The
    non-deterministic LLM discovery path uses :func:`validate_soft_a2` and only
    LOGS — imperfect model output must never hard-fail a run."""
    violations = validate_soft_a2(result, require_evidence=require_evidence)
    if violations:
        raise SoftContractViolation("; ".join(violations))


def soft_a2_from_structured_rows(
    rows: list[dict],
    type_name: str,
    *,
    key_field: str | None = None,
    source_url_field: str = "source_url",
) -> ExtractionResult:
    """Build a SOFT-TYPED, evidence-linked A2 (``ExtractionResult``) from
    already-structured rows — DETERMINISTICALLY, NO LLM (ONTA-272 fast path).

    Each row becomes ONE candidate entity typed ``type_name`` (a soft SUGGESTION —
    the pre-structured source confirmed the type, but A2 still only PROPOSES it for
    P5). Every non-empty field except the source-URL becomes a literal
    ``ExtractedAttribute``; the row's ``source_url`` (when present) is carried as
    the entity's + its attributes' ``evidence`` link (the per-record citation). The
    id is the ``key_field`` value, else the row's ``name``, else its positional
    index. Pre-structured rows are inherently soft — flat literal candidates with
    no minted ontology commitment — so the result always passes
    :func:`validate_soft_a2` (and passes ``require_evidence`` when the rows carry a
    source_url)."""
    entities: list[ExtractedEntity] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        evidence = str(row.get(source_url_field) or "").strip()
        rid_raw = (row.get(key_field) if key_field else None) or row.get("name") or str(i)
        rid = str(rid_raw).strip() or str(i)
        attrs: list[ExtractedAttribute] = []
        for k, v in row.items():
            if k == source_url_field:
                continue
            if v is None or str(v).strip() == "":
                continue
            attrs.append(ExtractedAttribute(name=str(k), value=v, evidence=evidence))
        entities.append(
            ExtractedEntity(
                type_name=type_name, id=rid, attributes=attrs, evidence=evidence
            )
        )
    return ExtractionResult(entities=entities)


class ExtractionConstraint(BaseModel):
    """Opt-in constraint that narrows extraction to a confirmed type + attributes.

    Default document / CSV / text ingestion passes ``None`` and stays fully
    open-ended (discovering every type the source justifies — that is its job).
    WEB DISCOVERY (ONTA-199), by contrast, has already CONFIRMED the single
    target type and the exact attribute set with the user, so re-running the
    open-ended multi-type reifier over a rich source payload just mints ~20
    unwanted sub-entities (Address, Taxonomy, Organization, …) and ~3x output
    tokens, which is what blew the extraction-time watchdog. When present, this
    constraint tells the extractor to emit ONLY records of ``types`` with ONLY
    the listed attributes (the key attribute always allowed), and drives a light
    post-extraction guard that drops off-type entities / unrequested attributes.

    A single-type constraint (the discovery case) is the common shape:
    ``types=["Physician"]`` +
    ``attributes={"Physician": ["name", "specialty", "city", "phone"]}``.
    """

    types: list[str] = Field(
        default_factory=list,
        description="The confirmed target type(s) the extractor may emit.",
    )
    attributes: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Per-type allowed attribute names (snake_case). A type absent from "
            "this map has no attribute restriction (all attributes allowed)."
        ),
    )
    soft: bool = Field(
        default=False,
        description=(
            "SEED vs CAGE. False (default, ONTA-199): a HARD constraint — extract "
            "ONLY the flat focus type + listed attributes, drop off-type entities, "
            "strip lineage, emit no relationships. True: a SOFT prior — the focus "
            "type + attributes are a hint, but the extractor decomposes faithfully "
            "(most-specific subtypes, real-world values as nodes, multi-valued "
            "splits, reuse-first) and the post-extraction guard is a no-op. Soft "
            "restores correct ontology shape on discovery while the prior keeps "
            "extraction focused + compact (measurements stay literal, no per-column "
            "type explosion)."
        ),
    )

    @property
    def is_active(self) -> bool:
        """True only when the constraint actually restricts something."""
        return bool(self.types)

    def allowed_attributes(self, type_name: str) -> set[str] | None:
        """Allowed attribute names for ``type_name``, or ``None`` = unrestricted."""
        attrs = self.attributes.get(type_name)
        return set(attrs) if attrs else None


# ---------------------------------------------------------------------------
# Type matching
# ---------------------------------------------------------------------------


class MatchVerdict(str, Enum):
    SAME = "SAME"
    SUBTYPE = "SUBTYPE"
    DIFFERENT = "DIFFERENT"
    FLAGGED = "FLAGGED"  # 3-way split, needs user review


class TypeMatch(BaseModel):
    """Result of matching a proposed type against the existing ontology."""

    proposed: str
    resolved: str = Field(description="The resolved type name (existing or new)")
    verdict: MatchVerdict
    confidence: float = Field(ge=0.0, le=1.0)
    is_new: bool = False
    parent_type: str | None = None  # set when verdict is SUBTYPE
    inconclusive: bool = False  # True when the verifier couldn't reach a real decision (e.g. LLM unavailable)


# ---------------------------------------------------------------------------
# Attribute resolution
# ---------------------------------------------------------------------------


class AttrAction(str, Enum):
    REUSE = "REUSE"
    COERCE = "COERCE"
    EXTEND = "EXTEND"
    PROMOTE = "PROMOTE"  # Option D: flat → structured coexistence


class ResolvedAttribute(BaseModel):
    """Result of resolving one attribute against the ontology."""

    name: str
    value: str
    datatype: str
    action: AttrAction
    original_value: str | None = None  # set when coerced
    promoted_type: str | None = None  # set when action is PROMOTE


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationOutcome(str, Enum):
    OK = "OK"
    COERCED = "COERCED"
    REJECTED = "REJECTED"


class ValidatedTriple(BaseModel):
    """A triple that passed schema-on-write validation — the A4 (Verified) fact.

    ONTA-276: a verified fact optionally carries the trust signals the write-time
    conflict policy (``pipeline/conflict.py``) arbitrates on when this fact
    collides with an existing value on a FUNCTIONAL attribute. Source-of-truth
    priority is set upstream (P1) but dies before the conflict point unless it is
    carried on the fact through A4 — these fields are that carrier. All are
    OPTIONAL with defaults, so every existing ``ValidatedTriple(...)`` construction
    parses and validates unchanged; a fact with no explicit ``authority`` /
    ``confidence`` simply falls to the policy's neutral defaults.
    """

    subject: str
    predicate: str
    object: str
    outcome: ValidationOutcome = ValidationOutcome.OK
    original_value: str | None = None  # set when coerced
    # ONTA-347: the per-attribute SURFACE-FORM companion triple
    # (``<entity> <attr_meta/<Type>/<attr>/surface_form> "<original>"``) built when
    # the A3 clean stage COERCED or CANONICALIZED this value (raw != canonical),
    # else None. It preserves the ORIGINAL pre-clean value in the graph — metadata
    # OF the attribute on the attr_meta namespace, structurally invisible to every
    # user surface (is_internal_predicate) yet queryable — so P4 Verify can compare
    # the stored canonical value against evidence in its original form. The writer
    # threads it into the SAME insert_facts call as ``object`` (never a domain fact
    # about the subject, so it gets no provenance record of its own). Optional +
    # back-compat: every existing ``ValidatedTriple(...)`` construction (and the
    # frozen a4/a5 boundary fixtures, which read explicit fields) is unchanged.
    surface_form_companion: tuple[str, str, str] | None = None
    # Trust signals carried through A4 for write-time conflict resolution (ONTA-276).
    authority: AuthorityLevel | None = Field(
        default=None,
        description=(
            "Source-authority level this fact was verified under (reuses the "
            "AuthorityLevel scale: source_of_truth > authoritative > "
            "supplementary). None = authority unknown; the conflict policy ranks "
            "it weakest."
        ),
    )
    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description=(
            "Verification confidence in this fact's value (0-1). None lets the "
            "conflict policy fall back to the calibrated confidence implied by "
            "``authority``."
        ),
    )
    source: str = Field(
        default="",
        description="Provenance source label this fact was verified from (carried onto its provenance record).",
    )


class RejectedValue(BaseModel):
    """A value that failed validation."""

    entity_id: str
    attribute: str
    value: str
    expected_datatype: str
    reason: str


# ---------------------------------------------------------------------------
# A3 — the explicit Clean stage (ONTA-344)
# ---------------------------------------------------------------------------


class CleanOutcome(str, Enum):
    """How one A2 candidate value fared in the A3 clean stage — the three-way
    partition every consumed value lands in EXACTLY once (the zero-silent-drops
    ledger).

    Distinct from :class:`ValidationOutcome` (the A4 typing outcome): a value that
    conforms yet is lexically canonicalized (``"True"`` -> ``"true"``) is A3
    ``TRANSFORMED`` but still A4 ``OK`` — A3 records the cleaning A4's typing
    silently hides."""

    PASSED = "passed"  # conforms as-is AND already canonical → written verbatim
    TRANSFORMED = "transformed"  # coerced and/or lexically canonicalized to fit
    DROPPED = "dropped"  # cannot be coerced to the datatype → not written


class CleanFact(BaseModel):
    """One A3 clean fact: a single A2 candidate value after the clean stage.

    ``clean_value`` is the canonical lexical form the A4 typing step
    (``validate_triple``) will stamp with an XSD datatype (``None`` when DROPPED).
    ``conformed`` records whether the value passed ``validate_value`` as-is (no
    coercion needed) — it drives A4's OK vs COERCED outcome, so A3 owns the
    coerce/canonicalize/reject DECISION while A4 owns the typing. Every consumed
    value yields exactly one CleanFact."""

    datatype: str
    raw_value: str
    clean_value: str | None
    outcome: CleanOutcome
    conformed: bool = True
    reason: str = ""
    entity_id: str = ""
    attribute: str = ""


class CleanReport(BaseModel):
    """The A3 ledger: every value the clean stage consumed, partitioned exactly
    once into ``passed`` / ``transformed`` / ``dropped`` — the zero-silent-drops
    guarantee (mirrors ADR 0003 §2 row conservation). ``total`` conserves:
    ``len(inputs) == passed + transformed + dropped``."""

    passed: list[CleanFact] = Field(default_factory=list)
    transformed: list[CleanFact] = Field(default_factory=list)
    dropped: list[CleanFact] = Field(default_factory=list)

    def record(self, fact: CleanFact) -> CleanFact:
        """File one clean fact into its outcome partition and return it."""
        bucket = {
            CleanOutcome.PASSED: self.passed,
            CleanOutcome.TRANSFORMED: self.transformed,
            CleanOutcome.DROPPED: self.dropped,
        }[fact.outcome]
        bucket.append(fact)
        return fact

    @property
    def total(self) -> int:
        return len(self.passed) + len(self.transformed) + len(self.dropped)

    def counts(self) -> dict[str, int]:
        """Partition sizes + total — the count-conservation summary."""
        return {
            "passed": len(self.passed),
            "transformed": len(self.transformed),
            "dropped": len(self.dropped),
            "total": self.total,
        }


# ---------------------------------------------------------------------------
# CSV schema inference
# ---------------------------------------------------------------------------


class ColumnRole(str, Enum):
    TYPE_ID = "type_id"
    ATTRIBUTE = "attribute"
    RELATIONSHIP = "relationship"


class ColumnMapping(BaseModel):
    column_name: str
    role: ColumnRole
    target_type: str | None = None
    datatype: str = "string"
    attribute_name: str | None = None
    # Multi-entity ingest: which in-row entity (EntitySpec.name) owns this
    # column. None = the main/legacy entity (single-entity mode).
    entity: str | None = None
    # ADR 0003 Pass B/C provenance (v2 inference only; defaults keep old
    # serialized mappings parsing unchanged).
    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="LLM confidence in this column decision (v2 inference)",
    )
    why: str | None = Field(
        default=None,
        description="Profile-evidence rationale for this column decision (v2 inference)",
    )
    # ONTA-177: schema-time free-text candidacy verdict for the semantic
    # instance index. "free_text" = this column's values are free-running
    # prose (profiler ValueShape.TEXT proposed it; unambiguously long text is
    # set deterministically, borderline cases carry the REASON pass's
    # name-informed adjudication). "not_text" (ONTA-173) = the column was a
    # TEXT-shaped candidate and the REASON pass EXPLICITLY declined it — a
    # durable decided-no, persisted so the reconciler stops re-sampling the
    # attribute and its name-blind auto tier can never overrule the LLM.
    # None = candidacy undecided: a non-candidate column (non-TEXT shape —
    # never marked either way), a mapping that predates ONTA-177, or a
    # hand-written mapping (ONTA-181's reconciler-side heuristic covers those
    # later). Default keeps old serialized mappings parsing unchanged.
    text_kind: str | None = Field(
        default=None,
        description=(
            "'free_text' when this column holds free-running prose worth "
            "semantic indexing (ONTA-177); 'not_text' when a text-shaped "
            "column was explicitly adjudicated NOT prose (durable decided-no, "
            "ONTA-173); both persisted as an ontology `textKind` marker on "
            "the attribute at ingest time; None = undecided"
        ),
    )


class EntitySpec(BaseModel):
    """One real-world entity embedded in a (wide) CSV row.

    A denormalized row often packs several entities — e.g. a hotel PMS row holds
    a guest (Person), a reservation (Reservation), and a property (Property).
    Each EntitySpec names one of them and how to key it: a single natural-key
    column (`id_column`) or a deterministic composite of columns (`id_from`).
    """

    name: str                         # local handle referenced by columns + relationships
    type_name: str                    # ontology type, e.g. "Person" / "Reservation"
    id_column: str | None = None      # column whose value is this entity's key
    id_from: list[str] | None = None  # OR deterministic composite key from these columns
    # ADR 0003 Pass B/C provenance (v2 inference only; defaults keep old
    # serialized mappings parsing unchanged).
    key_strategy: Literal["column", "composite", "synthetic"] | None = Field(
        default=None,
        description=(
            "How this entity is keyed: 'column' = id_column natural key, "
            "'composite' = deterministic id_from composite, 'synthetic' = "
            "content-hash key minted per row (ADR 0003 §2). None = legacy "
            "mapping that predates the v2 inference pipeline."
        ),
    )
    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="LLM confidence in this entity decision (v2 inference)",
    )
    why: str | None = Field(
        default=None,
        description="Profile-evidence rationale for this entity decision (v2 inference)",
    )


class EntityRelationSpec(BaseModel):
    """An edge between two in-row entities (names refer to EntitySpec.name)."""

    subject: str
    predicate: str
    object: str
    why: str | None = Field(
        default=None,
        description="Profile-evidence rationale for this edge (v2 inference)",
    )


class SchemaViolation(BaseModel):
    """One structural violation found by the adversarial refute pass
    (ADR 0003 Pass C). Templates are domain-free: KEY DROPS ROWS, DIMENSION AS
    LITERAL, COLUMN-NAMED EDGE, KEYLESS ENTITY, DUPLICATE/DEAD ATTR, LOST KEY,
    SPARSE / MIS-DOMAINED EDGE (ADR 0004 drift template).
    """

    template: str = Field(description="Which of the structural failure templates fired")
    location: str = Field(
        default="", description="Where in the proposed schema (entity/column/edge)"
    )
    evidence: str = Field(
        default="", description="Profile evidence the reviewer cited"
    )
    severity: str = Field(default="warning", description="Reviewer-assigned severity")


class CoreSlotTests(BaseModel):
    """The three constitutive-slot tests (ADR 0003 §1, Pass D). A slot is
    CORE only when it passes all three; the completion pass records the
    model's verdict per test so reviewers can audit the reasoning."""

    existence: bool = Field(
        default=False,
        description="an instance cannot exist in reality without this slot",
    )
    identity: bool = Field(
        default=False,
        description=(
            "needed to individuate instances, OR the type is a dependent "
            "entity existing only relative to the slot's target"
        ),
    )
    universality: bool = Field(
        default=False,
        description="holds for every instance of the concept in any dataset",
    )


class DatasetConstant(BaseModel):
    """A single value the dataset context implies for a missing core slot
    (ADR 0003 §3) — e.g. the whole file is one party's catalog, so that party
    fills the issuer slot. ``apply_mapping`` materializes ONE instance of the
    slot's target type plus per-instance edges instead of leaving the slot
    empty."""

    value: str
    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="model confidence that the constant is implied; <0.7 (or absent) holds the slot for review",
    )


class CoreSlot(BaseModel):
    """One CONSTITUTIVE slot of a type, proposed by the completion pass
    (ADR 0003 Pass D). May exist in the ontology with zero data in this
    dataset — an empty core slot is a declared enrichment target (§3).

    ``held_for_review`` is a client-side confirm gate: ``/ingest/csv/schema``
    returns held items flagged so the Explorer can ask the user to confirm;
    whatever (possibly user-edited) mapping the client posts back to
    ``/ingest/csv/rows`` is applied as-is. Server-side judge-panel gating is
    COG-56."""

    name: str
    kind: Literal["relationship", "attribute"] = "attribute"
    target_type: str | None = Field(
        default=None,
        description="PascalCase type a relationship-kind slot points at",
    )
    why: str | None = None
    tests: CoreSlotTests | None = Field(
        default=None, description="per-test verdicts (existence/identity/universality)",
    )
    dataset_constant: DatasetConstant | None = None
    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="optional model confidence in this slot (when emitted)",
    )
    held_for_review: bool = Field(
        default=False,
        description=(
            "True when this slot needs user confirmation before ingest: its "
            "confidence (or its dataset constant's) is below 0.7, or the "
            "constant carries no confidence at all"
        ),
    )


class RejectedSlot(BaseModel):
    """A candidate slot the completion pass considered and rejected, with the
    constitutive test it failed — the audit trail that keeps Pass D bounded
    (ADR 0003: every considered-but-rejected candidate is recorded)."""

    name: str
    failed_test: str = Field(
        default="", description="which test failed: existence, identity, or universality",
    )
    why: str | None = None


class TypeExtension(BaseModel):
    """Pass D output for ONE type: its constitutive core slots (max 3 — the
    boundedness cap is enforced here, not just in the prompt) plus the
    rejected-candidate audit list. When ``promoted_from_attribute`` is set,
    the type is a DEPENDENT ENTITY the completion pass promoted out of an
    attribute (e.g. a party-specific identifier), and ``apply_mapping`` turns
    that attribute's values into instances of this type.

    ``held_for_review`` is a client-side confirm gate (ALL promotions are
    judge-panel material): ``/ingest/csv/schema`` returns held items flagged;
    whatever (possibly user-edited) mapping the client posts back to
    ``/ingest/csv/rows`` is applied as-is. Server-side gating lands in COG-56."""

    type_name: str
    promoted_from_attribute: str | None = Field(
        default=None,
        description="the schema attribute this dependent-entity type was promoted from (None = pre-existing type)",
    )
    core_slots: list[CoreSlot] = Field(
        default_factory=list,
        max_length=3,
        description="constitutive slots — more than 3 fails validation (ADR 0003 boundedness cap)",
    )
    rejected: list[RejectedSlot] = Field(
        default_factory=list,
        description="considered-but-rejected slot candidates, each with the failed test",
    )
    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="optional model confidence in this extension (when emitted)",
    )
    held_for_review: bool = Field(
        default=False,
        description=(
            "True when this extension needs user confirmation before ingest: "
            "every promotion is held, as is any extension with confidence < 0.7"
        ),
    )


class OntologyExtensions(BaseModel):
    """ADR 0003 Pass D (COMPLETE) output: how the ontology may exceed the
    data — by exactly the constitutive core slots. Carried on
    ``CSVSchemaMapping.ontology_extensions`` (v2 inference only).

    The confirm gate for ``held_for_review`` items is CLIENT-SIDE:
    ``/ingest/csv/schema`` returns this object with held items flagged so the
    Explorer can ask the user; ``/ingest/csv/rows`` applies whatever the
    client posts back, unfiltered. Judge-panel gating (COG-56) lands later."""

    types: list[TypeExtension] = Field(default_factory=list)


class InferenceAudit(BaseModel):
    """Provenance of how a CSVSchemaMapping was inferred (ADR 0003 Passes A–C).

    Rendered by the web Explorer alongside per-decision `why`/`confidence`
    (on EntitySpec/ColumnMapping) and the mapping-level `violations`.
    """

    pipeline: str = Field(
        default="reason_refute_v2",
        description=(
            "'reason_refute_v2' (profile → reason → refute → complete; the "
            "completion pass's output lives in ontology_extensions) — the "
            "legacy single-call path emits no audit"
        ),
    )
    rows_profiled: int = Field(default=0, ge=0, description="sample rows Pass A profiled")
    total_rows: int = Field(default=0, ge=0, description="declared full-file size")
    profile: dict[str, Any] | None = Field(
        default=None,
        description="compact Pass A profile (TableProfile.to_prompt_dict) the decisions were grounded in",
    )


class CSVSchemaMapping(BaseModel):
    entity_type: str
    columns: list[ColumnMapping]
    # Multi-entity mode (optional, backward-compatible): when `entities` is set,
    # one row expands into several fully-attributed, linked entities and
    # `entity_type` is ignored. When None, the legacy single-entity path runs.
    entities: list[EntitySpec] | None = None
    relationships: list[EntityRelationSpec] | None = None
    # ADR 0003 v2 inference output (optional, backward-compatible — old
    # payloads without these fields parse unchanged).
    violations: list[SchemaViolation] = Field(
        default_factory=list,
        description="Structural violations the refute pass found in the proposed schema (already corrected in this mapping)",
    )
    inference_audit: InferenceAudit | None = Field(
        default=None,
        description="How this mapping was inferred (v2 pipeline only)",
    )
    ontology_extensions: OntologyExtensions | None = Field(
        default=None,
        description=(
            "Pass D (COMPLETE) output: dependent-entity promotions, "
            "constitutive core slots (max 3/type), dataset constants, and the "
            "rejected-candidate audit list. None on the legacy path and on "
            "payloads serialized before COG-52. held_for_review items are a "
            "client-side confirm gate — /ingest/csv/rows applies whatever "
            "the client posts back (judge-panel gating is COG-56)."
        ),
    )


# ---------------------------------------------------------------------------
# CSV profiling (ADR 0003 Pass A)
# ---------------------------------------------------------------------------


class ValueShape(str, Enum):
    """Structural shape of a column's non-empty values. Decided purely from
    value statistics — never from the column name (ADR 0003 litmus test)."""

    EMPTY = "empty"
    DATE = "date"
    NUMBER = "number"
    CODE_ID = "code/id"
    LABEL = "label"
    TEXT = "text"


class ColumnProfile(BaseModel):
    """Statistical evidence for one column of the profiled sample."""

    name: str
    completeness: float = Field(
        ge=0.0, le=1.0, description="non-empty cells / rows profiled"
    )
    distinct: int = Field(ge=0, description="count of distinct non-empty values")
    uniqueness: float = Field(
        ge=0.0, le=1.0, description="distinct / non-empty cells"
    )
    card_ratio: float = Field(
        ge=0.0, le=1.0, description="distinct / rows profiled"
    )
    value_shape: ValueShape = ValueShape.EMPTY
    examples: list[str] = Field(
        default_factory=list, description="top-3 most frequent non-empty values"
    )
    complete_unique_key: bool = Field(
        default=False,
        description="completeness > 0.99 and uniqueness > 0.99 — safe natural key",
    )
    incomplete: bool = Field(
        default=False,
        description="completeness < 0.98 — keying on this column drops rows",
    )
    low_cardinality_repeated: bool = Field(
        default=False,
        description=(
            "1 < distinct, card_ratio < 0.5, values repeat — dimension-shaped, "
            "candidate entity rather than string literal"
        ),
    )


class TableProfile(BaseModel):
    """ADR 0003 Pass A output: deterministic statistical profile of the sample
    rows sent to /ingest/csv/schema. Grounds the reason/refute passes (B+C)."""

    rows_profiled: int = Field(ge=0, description="rows actually profiled (the sample)")
    total_rows: int = Field(
        ge=0,
        description="declared size of the full file; rows_profiled/total_rows = sample coverage",
    )
    columns: list[ColumnProfile] = Field(default_factory=list)
    fd_mutual: list[tuple[str, str]] = Field(
        default_factory=list,
        description=(
            "A<->B functional dependencies (both directions hold) — column pairs "
            "describing ONE entity, e.g. code<->title"
        ),
    )
    fd_oneway: list[tuple[str, str]] = Field(
        default_factory=list,
        description="(determinant, dependent) pairs where only A->B holds",
    )

    def column(self, name: str) -> ColumnProfile | None:
        """Lookup one column's profile by header name."""
        return next((c for c in self.columns if c.name == name), None)

    def to_prompt_dict(self, max_example_len: int = 40) -> dict[str, Any]:
        """Compact, JSON-serializable view for embedding in LLM prompts
        (Pass B+C). Floats rounded, long examples truncated, flags listed
        only when set, FDs rendered as readable arrow strings."""
        columns: dict[str, Any] = {}
        for c in self.columns:
            entry: dict[str, Any] = {
                "shape": c.value_shape.value,
                "complete": round(c.completeness, 3),
                "distinct": c.distinct,
                "unique": round(c.uniqueness, 3),
                "examples": [
                    e if len(e) <= max_example_len else e[: max_example_len - 1] + "…"
                    for e in c.examples
                ],
            }
            flags = [
                flag
                for flag in ("complete_unique_key", "incomplete", "low_cardinality_repeated")
                if getattr(c, flag)
            ]
            if flags:
                entry["flags"] = flags
            columns[c.name] = entry
        return {
            "rows_profiled": self.rows_profiled,
            "total_rows": self.total_rows,
            "columns": columns,
            "fd_mutual": [f"{a} <-> {b}" for a, b in self.fd_mutual],
            "fd_oneway": [f"{a} -> {b}" for a, b in self.fd_oneway],
        }


# ---------------------------------------------------------------------------
# Ingest endpoint
# ---------------------------------------------------------------------------


class IngestRequest(BaseModel):
    """Request body for POST /graphs/{tenant}/ingest."""

    content: str = Field(description="Raw text, JSON, or CSV to ingest")
    content_type: str = Field(default="text", description="text, json, or csv")
    source: str = Field(default="", description="Source identifier for provenance")
    kg_name: str | None = Field(default=None, description="Knowledge graph name. If set, data goes into a KG-specific graph.")


class CSVSchemaRequest(BaseModel):
    """Request body for POST /graphs/{tenant}/ingest/csv/schema."""

    headers: list[str]
    # Cell values may arrive as JSON numbers/booleans/null, not just strings —
    # accept Any so a client sending typed JSON isn't rejected with a 422. The
    # inferencer reads them via json.dumps(..., default=str), so non-strings are
    # fine; the LLM judges datatype from the value.
    sample_rows: list[dict[str, Any]]
    total_rows: int = 0


class KeyJoin(BaseModel):
    """Join-by-exact-key ingest mode (ONTA-250).

    When set, each incoming row is matched to an EXISTING entity by an exact key
    attribute (``key_attribute`` — the snake_case attribute name the key column
    maps to, e.g. an id column) and the row's attributes are merged ONTO that
    existing entity's node via the shared write path, instead of minting a
    duplicate. A row whose key value matches no existing entity mints a new node
    when ``mint_unmatched`` is true (default), else it is skipped and counted.

    Fully general — the caller names the key attribute; there is NO per-domain
    (NPI/sku/…) special-casing. The match is on the LEXICAL value of the
    schema-declared ``attrs/<key_attribute>`` literal, so it is datatype-agnostic.
    """

    key_attribute: str = Field(
        description=(
            "The snake_case attribute name to join on (the attribute the key "
            "column maps to). Existing entities of the row's type carrying this "
            "attribute equal to the row's key value are merged onto."
        ),
    )
    mint_unmatched: bool = Field(
        default=True,
        description=(
            "When a row's key value matches no existing entity: True (default) "
            "mints a new node; False skips the row and reports it unmatched "
            "(never silently dropped)."
        ),
    )


class CSVRowsRequest(BaseModel):
    """Request body for POST /graphs/{tenant}/ingest/csv/rows."""

    mapping: CSVSchemaMapping
    rows: list[dict[str, str]]
    source: str = ""
    kg_name: str | None = None
    # ONTA-250: join-by-exact-key mode. None = ordinary ingest (mint by URI, the
    # existing behavior). Set = match each row to an existing entity by an exact
    # key attribute and merge onto it instead of minting a duplicate.
    key_join: KeyJoin | None = None


class IngestResult(BaseModel):
    """Response for the ingest endpoint."""

    batch_id: str = Field(default="", description="Batch ID for rollback support")
    entities_extracted: int = 0
    entities_resolved: int = 0
    triples_inserted: int = 0
    types_created: list[str] = Field(default_factory=list)
    attributes_added: list[str] = Field(default_factory=list)
    # Types of TARGET nodes minted for node-valued attributes this ingest — e.g.
    # a `Physician.located_in -> City` fill mints a `City` node (schema_resolver's
    # promotion branch). These are NOT in `types_created` (the target type already
    # exists) nor recoverable from `attributes_added` (that carries the SUBJECT
    # type), so they are tracked here purely so post-write housekeeping re-embeds /
    # re-stats them too — see `affected_types()`. Default keeps older callers /
    # serialized payloads compatible.
    node_target_types: list[str] = Field(default_factory=list)
    rejections: list[RejectedValue] = Field(default_factory=list)
    flagged_types: list[str] = Field(default_factory=list, description="Types needing user review")
    chunks_processed: int = 0
    entities_deduplicated: int = 0
    # Row-conservation accounting (ADR 0003 §2): input rows are never silently
    # dropped. Defaults keep older callers and serialized payloads compatible.
    rows_in: int = Field(default=0, description="Input rows received by this ingest call (CSV paths)")
    rows_dropped: int = Field(
        default=0,
        description=(
            "Rows that produced no entity at all — only possible when every "
            "owned value in the row is empty (nothing to assert). Never silent: "
            "a structured warning is logged whenever this is > 0."
        ),
    )
    drops_by_entity: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Skipped entity-instances per mapping entity. Keys are the "
            "entity_type in single-entity mode, or the EntitySpec.name in "
            "multi-entity mode (where one row can mint some entities while "
            "skipping an all-empty one without the row itself being dropped)."
        ),
    )
    # ONTA-177: free-text candidacy verdicts persisted during this ingest.
    # Default keeps older callers and serialized payloads compatible.
    free_text_attributes: list[str] = Field(
        default_factory=list,
        description=(
            "'Type.attr' entries that received a textKind='free_text' "
            "ontology marker during this ingest (schema-time semantic-index "
            "candidacy, ONTA-177)"
        ),
    )
    # ONTA-250: join-by-exact-key accounting. All zero on ordinary ingest.
    rows_key_merged: int = Field(
        default=0,
        description=(
            "Rows whose key value matched an existing entity, so their "
            "attributes were merged ONTO that node (no duplicate minted)."
        ),
    )
    rows_key_minted: int = Field(
        default=0,
        description=(
            "Rows whose key value matched no existing entity and minted a new "
            "node (only when the key-join allows minting unmatched rows)."
        ),
    )
    rows_key_unmatched: int = Field(
        default=0,
        description=(
            "Rows whose key value matched no existing entity and were SKIPPED "
            "(key-join with mint_unmatched=false). Reported, never silent."
        ),
    )
    # ONTA-271: deterministic A6 Graph Delta receipt of the instance facts this
    # ingest wrote — the sorted, fact_id-keyed, nonce-excluded projection
    # (kg_writer.GraphDelta.to_dict()). Byte-identical across replays of the same
    # run_id, so P6 can prove an upstream retry reproduced the graph exactly
    # instead of duplicating it. A JSON-able dict; None on the CSV / legacy paths
    # (and any caller that threads no run_id). Additive + back-compat.
    graph_delta: dict | None = None
    # ONTA-373: the A3 clean+validate LEDGER for this ingest — every primitive
    # value the discovery path fed through `clean_value`/`validate_triple`,
    # partitioned exactly once into passed / transformed / dropped WITH a reason
    # (the zero-silent-drops guarantee, mirroring how `enrichment/executor.py`
    # assembles one). Purely observability: it records the same A3 decision the
    # writer already made, so the set of written triples is unchanged. Empty
    # `CleanReport` on paths that cleaned nothing; `total` conserves
    # (`passed + transformed + dropped`). Reuses the SAME `CleanReport` type
    # enrichment/qc use — not a parallel report.
    clean_report: CleanReport = Field(default_factory=CleanReport)
    # ONTA-370: the A4 Verify verdicts for this ingest — one `VerifiedFact`
    # (verdict + independent evidence + confidence + A4 lineage envelope) per A3
    # clean fact, produced by the DEFAULT-OFF verify seam wedged between the A3
    # clean ledger and the write (`schema_resolver._verify_clean_facts`). EMPTY
    # by default — the seam short-circuits before verifying when no VerifyPolicy
    # is configured (the default), so an ordinary ingest returns this empty and
    # the written graph / rest of the result stay byte-identical to pre-370. Only
    # an OPT-IN enabled policy (or a premium `register_fact_verifier`) populates
    # it. Typed `list[Any]` deliberately: `VerifiedFact` lives in
    # `verification.types`, which imports `CleanFact` from THIS module — typing it
    # concretely here would be an import cycle, so the elements are held loosely.
    verified_facts: list[Any] = Field(default_factory=list)

    def affected_types(self) -> set[str]:
        """Types whose embeddings + Explorer stats a post-write refresh must touch
        after this ingest: every CREATED type, the (subject) type of each ADDED
        attribute, AND the type of every TARGET node minted for a node-valued
        attribute (`node_target_types`).

        Single source of truth so the ``/ingest`` and ``/ingest/csv/rows`` routes
        pass the SAME set to ``refresh_after_write`` — including the target-node
        types, so a freshly-linked ``City`` node is re-embedded / re-stat'd now,
        not only on ``City``'s next write."""
        types = set(self.types_created)
        for attr_added in self.attributes_added:
            if "." in attr_added:
                types.add(attr_added.split(".")[0])
        types.update(self.node_target_types)
        return types
