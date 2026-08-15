"""CSV mapping, entity-spec, and ontology-extension models.

Extracted from ``resolver/models.py``. Public names stay importable from
``infona_client.resolver.models``.

``EntitySpec`` / ``EntityRelationSpec`` must stay importable from the
``models`` facade — csv_llm and the CSV inferencer construct them at runtime.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

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

