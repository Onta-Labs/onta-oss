"""Data models for the schema resolver pipeline."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# LLM extraction output (non-deterministic, proposed)
# ---------------------------------------------------------------------------


class ExtractedAttribute(BaseModel):
    """A single attribute proposed by the LLM extractor."""

    name: str
    value: str
    datatype: str = "string"


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
    attributes: list[ExtractedAttribute] = Field(default_factory=list)


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
    """A triple that passed schema-on-write validation."""

    subject: str
    predicate: str
    object: str
    outcome: ValidationOutcome = ValidationOutcome.OK
    original_value: str | None = None  # set when coerced


class RejectedValue(BaseModel):
    """A value that failed validation."""

    entity_id: str
    attribute: str
    value: str
    expected_datatype: str
    reason: str


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


class EntityRelationSpec(BaseModel):
    """An edge between two in-row entities (names refer to EntitySpec.name)."""

    subject: str
    predicate: str
    object: str


class CSVSchemaMapping(BaseModel):
    entity_type: str
    columns: list[ColumnMapping]
    # Multi-entity mode (optional, backward-compatible): when `entities` is set,
    # one row expands into several fully-attributed, linked entities and
    # `entity_type` is ignored. When None, the legacy single-entity path runs.
    entities: list[EntitySpec] | None = None
    relationships: list[EntityRelationSpec] | None = None


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


class CSVRowsRequest(BaseModel):
    """Request body for POST /graphs/{tenant}/ingest/csv/rows."""

    mapping: CSVSchemaMapping
    rows: list[dict[str, str]]
    source: str = ""
    kg_name: str | None = None


class IngestResult(BaseModel):
    """Response for the ingest endpoint."""

    batch_id: str = Field(default="", description="Batch ID for rollback support")
    entities_extracted: int = 0
    entities_resolved: int = 0
    triples_inserted: int = 0
    types_created: list[str] = Field(default_factory=list)
    attributes_added: list[str] = Field(default_factory=list)
    rejections: list[RejectedValue] = Field(default_factory=list)
    flagged_types: list[str] = Field(default_factory=list, description="Types needing user review")
    chunks_processed: int = 0
    entities_deduplicated: int = 0
