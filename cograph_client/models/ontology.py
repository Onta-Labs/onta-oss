from typing import Literal

from pydantic import BaseModel, Field

from cograph_client.models.function import FunctionRef


class AttributeDefinition(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    datatype: str = Field(default="string", description="string, integer, float, boolean, datetime, uri, geo (WKT point / 'lat,lon'), or a type name for relationships")


class TypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    parent_type: str | None = Field(default=None, description="Parent type name for subtype relationship")
    attributes: list[AttributeDefinition] = Field(default_factory=list)


class TypeResponse(BaseModel):
    name: str
    description: str = ""
    parent_type: str | None = None
    attributes: list[AttributeDefinition] = Field(default_factory=list)
    subtypes: list[str] = Field(default_factory=list)
    functions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Global ontology browser (operator-only, ONTA-234 visibility seam)
#
# GET /operator/ontology/global returns the ENTIRE Global ontology — both
# layers, Public + Enhanced (ADR 0002 §1) — in ONE payload, rich enough for a
# web page to search client-side across type names, slot names and descriptions
# and to sort alphabetically. These are NEW models on purpose: TypeResponse is
# the tenant ontology routes' contract (description "" not null, no layer, no
# relationship/attribute split, no core_slot) and must not be contorted.
# ---------------------------------------------------------------------------


class GlobalOntologyAttribute(BaseModel):
    """A LITERAL-valued slot: its ``rdfs:range`` is not a type in any layer."""

    name: str
    datatype: str = Field(
        default="string",
        description="Primitive datatype name: string, integer, float, boolean, datetime, uri, geo",
    )
    description: str | None = Field(
        default=None, description="rdfs:comment on the property URI; null when absent"
    )
    core_slot: bool = Field(
        default=False, description="onto/coreSlot marker — a CONSTITUTIVE slot (ADR 0003 Pass D)"
    )


class GlobalOntologyRelationship(BaseModel):
    """A NODE-valued slot: its ``rdfs:range`` resolves to a type in a layer namespace."""

    name: str
    target_type: str = Field(description="Bare type NAME the range points at, not a URI")
    description: str | None = Field(
        default=None, description="rdfs:comment on the property URI; null when absent"
    )
    core_slot: bool = False


class GlobalOntologySource(BaseModel):
    """A registered API source whose declared coverage PLAUSIBLY covers a type.

    **This is a fuzzy token match, not a stored foreign key.** Nothing in the
    graph or the catalog binds a source to an ontology type. The association is
    computed at read time by
    :func:`~cograph_client.api_registry.matching.type_matches` — the SAME
    predicate the enrichment rail self-gates on and the selector pre-filters
    with — which overlaps the type's name tokens with the entry's declared
    ``coverage.entity_kinds`` (camelCase-split, with a generic-token guard).

    So the ONLY honest UI phrasing is "sources that plausibly cover this type"
    (or "…that could be asked about this type"), NEVER "sources bound to this
    type", "the source of this type", or anything implying provenance: a source
    listed here may never have written a single fact, and a source that DID
    write facts about a differently-named type will not appear. It answers
    "would the enrichment rail even consider this API for this type?" — nothing
    more, and it answers it identically to the rail itself, by construction.

    Every field is carried verbatim from
    :class:`~cograph_client.api_registry.spec.ApiSourceSpec`; nothing is
    invented. Call-volume and refresh-cadence in particular are NOT here —
    nothing records them.
    """

    slug: str = Field(description="Catalog entry id, unique across layers")
    title: str = ""
    publisher: str = ""
    registry_layer: str = Field(
        default="",
        description=(
            "The SOURCE CATALOG's layer — NOT an ontology layer. Values are "
            '"global_public" / "global_enhanced" (and "tenant_custom", which can '
            "never appear here). This is a DIFFERENT AXIS from "
            "``GlobalOntologyType.layer`` (\"public\" / \"enhanced\"): different "
            "subsystem, different vocabulary, different precedence ranks, no "
            "relationship whatsoever between the two. A ``global_public`` API can "
            "cover an ``enhanced``-layer type and a ``global_enhanced`` API can "
            "cover a ``public`` one. The field is NAMED apart from the type's "
            "``layer`` precisely so the two can never be rendered with the same "
            "badge by mistake — do not shorten it back to ``layer``."
        ),
    )
    authority_level: str = Field(
        default="",
        description="ApiSourceSpec.authority_level, e.g. source_of_truth / authoritative / supplementary",
    )
    enabled: bool = Field(
        default=True, description="False ⇒ the entry is catalogued but not served"
    )
    verified_at: str = Field(
        default="",
        description="ISO date (YYYY-MM-DD) the entry's call spec was last hand-verified; empty ⇒ never",
    )
    freshness: str = Field(
        default="OK",
        description=(
            "Verification grade from the EXISTING catalog audit "
            "(``api_registry/catalog_audit.py``), never a second health scale: "
            '"UNVERIFIED" (no/unparseable verified_at), "STALE" (older than the '
            'audit\'s max age), "FUTURE" (stamp in the future — a typo), or "OK". '
            "Live reachability (the audit's optional EMPTY / UNREACHABLE smoke) is "
            "deliberately NOT computed here: this read must stay offline."
        ),
    )
    entity_kinds: list[str] = Field(
        default_factory=list,
        description=(
            "The entry's declared ``coverage.entity_kinds`` — the EVIDENCE the "
            "token match ran against, so an operator can see WHY a source was "
            "attached (or was not)."
        ),
    )


class GlobalOntologyType(BaseModel):
    """One type as declared in ONE Global layer.

    A name declared in BOTH layers yields TWO entries (one per layer) — this is
    the operator's raw browse view, so shadowing (Enhanced > Public) is shown,
    not silently applied.
    """

    name: str
    layer: str = Field(description='Layer that declares this type: "public" or "enhanced"')
    description: str | None = None
    parent_type: str | None = Field(
        default=None, description="Bare parent type NAME from rdfs:subClassOf, not a URI"
    )
    subtypes: list[str] = Field(
        default_factory=list,
        description="Bare NAMES of types (in EITHER Global layer) whose rdfs:subClassOf points here",
    )
    attributes: list[GlobalOntologyAttribute] = Field(default_factory=list)
    relationships: list[GlobalOntologyRelationship] = Field(default_factory=list)
    sources: list[GlobalOntologySource] = Field(
        default_factory=list,
        description=(
            "Registered API sources that PLAUSIBLY cover this type (fuzzy token "
            "match on coverage.entity_kinds — see GlobalOntologySource), sorted "
            "by slug. Empty is normal: no covering source, or the registry was "
            "unavailable (which degrades to [] and never fails the request)."
        ),
    )
    functions: list[FunctionRef] = Field(
        default_factory=list,
        description=(
            "Executable code attached to this type, read from THIS LAYER's "
            "graph. Read-only, and "
            "EMPTY today for every type: no writer mints a function against a "
            "layer-qualified type URI yet — ``queries.register_function_triple`` "
            "still attaches to the bare tenant namespace. ``entity_type`` is the "
            "enclosing type's name; ``tier`` is not stored in the graph and "
            "carries the model default, exactly as the tenant "
            "``GET /graphs/{tenant}/functions`` route reports it."
        ),
    )


class GlobalOntologyLayer(BaseModel):
    """Per-layer status line. ``available=False`` is the graceful-degradation
    signal: that layer's graph was unreachable or its query errored, so it
    contributed no types — the request still succeeds with 200."""

    layer: str
    graph_uri: str
    type_count: int = 0
    available: bool = True


class GlobalOntologyResponse(BaseModel):
    """Body of ``GET /operator/ontology/global``.

    ``types`` is sorted alphabetically by name (case-insensitive); each type's
    ``attributes`` and ``relationships`` are likewise sorted by name. An empty
    Global ontology is NOT an error — it returns 200 with ``types: []``.
    """

    layers: list[GlobalOntologyLayer] = Field(default_factory=list)
    types: list[GlobalOntologyType] = Field(default_factory=list)


class AttributeAdd(BaseModel):
    attributes: list[AttributeDefinition] = Field(min_length=1)


class SubtypeAdd(BaseModel):
    subtype: str = Field(min_length=1, description="Name of the child type")


# ---------------------------------------------------------------------------
# Ontology evolution resolver (COG-84)
#
# The OntologyResolver (cograph_client/resolver/ontology_resolver.py) turns a
# fuzzy natural-language "ask" into a structured PLAN of ontology changes. These
# models are the plan it returns over REST: each ResolvedChange is one
# attribute/relationship the ask implies, classified as a clear REUSE/EXTEND on
# an existing type (auto-APPLY) or a creation / ambiguous match (PROPOSE). The
# resolver NEVER writes to Neptune — a later /resolve→/apply REST pair (COG-81)
# consumes this plan. JSON-serializable by construction (plain pydantic).
# ---------------------------------------------------------------------------


class ResolvedChange(BaseModel):
    """One concrete ontology change the ask implies, already resolved against
    the current ontology.

    ``action`` is the verb the apply layer (COG-81) executes:
      - ``reuse``  — the attribute/relationship already exists on ``subject_type``;
        nothing to write (the ask is already satisfied).
      - ``extend`` — ``subject_type`` exists, but this attribute/relationship is
        new on it: add the property (``insert_attribute`` / object-property).
      - ``create`` — a NEW type must be minted first (the subject type itself is
        new, or a relationship target type doesn't exist yet), then the
        property is added. These are the changes that land in ``proposals``.
    """

    kind: Literal["attribute", "relationship"]
    subject_type: str = Field(description="Resolved type the change attaches to (existing name, or a proposed new one)")
    name: str = Field(description="Resolved attribute name (attribute) or predicate (relationship), normalized")
    datatype_or_target: str = Field(
        description=(
            "For an attribute: the primitive datatype (string/integer/float/"
            "boolean/datetime/uri). For a relationship: the target type name "
            "(its range) the predicate points at."
        ),
    )
    action: Literal["reuse", "extend", "create"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(default="", description="One-line human-readable rationale for the action/gate decision")


class ResolutionResult(BaseModel):
    """The full PLAN the OntologyResolver returns for one ask.

    ``applied`` are high-confidence changes the caller may auto-APPLY (existing
    subject type + a clear reuse/extend, no new type). ``proposals`` are changes
    that need confirmation — a new type must be created, or the match was
    mid-band/ambiguous. The split is advisory: the resolver writes nothing, and
    the apply layer (COG-81) decides what to commit.
    """

    applied: list[ResolvedChange] = Field(default_factory=list)
    proposals: list[ResolvedChange] = Field(default_factory=list)
    summary: str = ""
    dry_run: bool = Field(
        default=False,
        description="True when the caller requested plan-only mode: nothing was written and every change is surfaced under `proposals`.",
    )


class ApplyBatchRequest(BaseModel):
    """Body for ``POST /graphs/{tenant}/ontology/apply/batch``.

    A list of the SAME ``ResolvedChange`` objects the single ``/apply`` route
    takes — apply many proposals from one ``/resolve`` call in ONE round-trip
    instead of N. The canonical batch surface every client (SDK / MCP) rides;
    no interface hand-rolls its own multi-apply loop.
    """

    changes: list[ResolvedChange] = Field(
        min_length=1,
        description="The resolved changes to apply, in order. Idempotent (upserts).",
    )


class ApplyChangeResult(BaseModel):
    """Per-change outcome inside an :class:`ApplyBatchResult`.

    ``ok`` is the well-defined partial-failure signal: a change that raised is
    reported with ``ok=False`` + ``error`` and does NOT abort the rest of the
    batch (each change's writes are independent + idempotent, so a re-POST of the
    whole batch safely retries the failed ones).
    """

    change: ResolvedChange
    ok: bool = True
    operations: int = 0
    error: str = ""


class ApplyBatchResult(BaseModel):
    """Response for the batch-apply route — one entry per submitted change."""

    results: list[ApplyChangeResult] = Field(default_factory=list)
    #: Count of changes that applied cleanly (== number with ok=True).
    applied_count: int = 0
    #: Count of changes that raised (ok=False). 0 ⇒ the whole batch succeeded.
    failed_count: int = 0
    #: Sum of individual SPARQL operations run across all successful changes.
    operations: int = 0
    summary: str = ""


class ResolveRequest(BaseModel):
    """Body for ``POST /graphs/{tenant}/ontology/resolve`` (COG-81).

    ``ask`` is the fuzzy natural-language evolution request (e.g. "track which
    company a person works for"). ``knowledge_graph`` is an optional scope hint
    carried for parity with the rest of the API; the resolver resolves against
    the tenant's ontology graph regardless.
    """

    ask: str = Field(min_length=1, description="Natural-language ontology-evolution request")
    knowledge_graph: str | None = Field(default=None, description="Optional KG scope hint")
    dry_run: bool = Field(
        default=False,
        description=(
            "Plan-only mode. When false (default, the MCP/agent path) the route "
            "auto-applies the resolver's high-confidence changes and returns the "
            "rest as proposals. When true (the interactive Explorer path) nothing "
            "is written: every change — what would have auto-applied plus the "
            "proposals — is returned under `proposals`, `applied` is empty."
        ),
    )
