"""Data models for the schema resolver pipeline.

Implementation lives in sibling ``models_*.py`` modules. Every previously
importable name is re-exported here — callers must keep importing from
``infona_client.resolver.models``.
"""

from __future__ import annotations

from infona_client.resolver.models_clean import (
    AttrAction,
    CleanFact,
    CleanOutcome,
    CleanReport,
    MatchVerdict,
    RejectedValue,
    ResolvedAttribute,
    TypeMatch,
    ValidatedTriple,
    ValidationOutcome,
)
from infona_client.resolver.models_csv import (
    CSVSchemaMapping,
    ColumnMapping,
    ColumnRole,
    CoreSlot,
    CoreSlotTests,
    DatasetConstant,
    EntityRelationSpec,
    EntitySpec,
    InferenceAudit,
    OntologyExtensions,
    RejectedSlot,
    SchemaViolation,
    TypeExtension,
)
from infona_client.resolver.models_extract import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionConstraint,
    ExtractionResult,
    SoftContractViolation,
    _URI_SCHEME,
    _is_committed_type_ref,
    assert_soft_a2,
    soft_a2_from_structured_rows,
    validate_soft_a2,
)
from infona_client.resolver.models_schema import (
    CSVRowsRequest,
    CSVSchemaRequest,
    ColumnProfile,
    IngestRequest,
    IngestResult,
    KeyJoin,
    TableProfile,
    ValueShape,
)

__all__ = [
    "AttrAction",
    "CSVRowsRequest",
    "CSVSchemaMapping",
    "CSVSchemaRequest",
    "CleanFact",
    "CleanOutcome",
    "CleanReport",
    "ColumnMapping",
    "ColumnProfile",
    "ColumnRole",
    "CoreSlot",
    "CoreSlotTests",
    "DatasetConstant",
    "EntityRelationSpec",
    "EntitySpec",
    "ExtractedAttribute",
    "ExtractedEntity",
    "ExtractedRelationship",
    "ExtractionConstraint",
    "ExtractionResult",
    "InferenceAudit",
    "IngestRequest",
    "IngestResult",
    "KeyJoin",
    "MatchVerdict",
    "OntologyExtensions",
    "RejectedSlot",
    "RejectedValue",
    "ResolvedAttribute",
    "SchemaViolation",
    "SoftContractViolation",
    "TableProfile",
    "TypeExtension",
    "TypeMatch",
    "ValidatedTriple",
    "ValidationOutcome",
    "ValueShape",
    "_URI_SCHEME",
    "_is_committed_type_ref",
    "assert_soft_a2",
    "soft_a2_from_structured_rows",
    "validate_soft_a2",
]
