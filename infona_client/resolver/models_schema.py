"""CSV profiling and ingest request/result models.

Extracted from ``resolver/models.py``. Public names stay importable from
``infona_client.resolver.models``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from infona_client.resolver.models_clean import CleanReport, RejectedValue
from infona_client.resolver.models_csv import CSVSchemaMapping

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
    # ONTA-386: tracked Jobs-page id when the route opened a file-ingest job.
    # Optional / default None so older clients and unit-constructed results stay
    # compatible; when set, clients can poll GET /jobs or the operator trace.
    job_id: str | None = Field(
        default=None,
        description=(
            "Tracked EnrichJob id for this file ingest (category=ingest), when "
            "the API recorded a Jobs entry with live stage_trace. None when the "
            "job store was unavailable or the call path does not track jobs."
        ),
    )
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
