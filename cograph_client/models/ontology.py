from enum import Enum
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


class GlobalOntologySkill(BaseModel):
    """A curated GLOBAL-layer skill attached to a type — type-attached PROSE
    whose consumer is an LM agent.

    A skill TEACHES ("a ``Clinic`` here is a billing location, not a building");
    a :class:`~cograph_client.models.function.FunctionRef` COMPUTES. They are
    separate subsystems with separate storage and separate consumers — this
    field is not a second flavour of ``functions`` (boundary doc §27; ADR 0002's
    "strategy bundles (skills)" phrasing predates the product definition and is
    stale).

    **The BODY is deliberately not carried.** A skill body is capped at
    ``skills.models.MAX_BODY_CHARS`` (20 000 chars) and this endpoint returns
    EVERY type in both global layers in one payload, so inlining bodies would
    let one ontology read drag hundreds of KB of prose for types the reader
    never opens. Instead this mirrors the skills API's own list projection
    (``api/routes/skills.py::SkillSummary``, which carries ``body_chars`` and no
    body): the authored ``summary``, a bounded ``excerpt`` of the body, the true
    ``body_chars``, and the identity needed to read the full text on demand from
    the ONE canonical route that already serves it —
    ``GET /graphs/{tenant}/skills/{type_name}/{slug}``. No second full-body
    endpoint is minted for this page; that would be exactly the per-interface
    endpoint drift the convergence rule forbids.

    Every field is carried verbatim from
    :class:`~cograph_client.skills.models.TypeSkill` except ``excerpt`` /
    ``body_chars``, which are derived from its body. Nothing is invented.
    """

    slug: str = Field(description="Skill id within (type, layer); URL-safe")
    type_name: str = Field(
        description=(
            "The type name the skill declares itself attached to. Normally the "
            "enclosing type's name, but attachment is matched CASE-INSENSITIVELY, "
            "so the two can differ in case — this is the exact spelling the "
            "canonical `/graphs/{tenant}/skills/{type_name}/{slug}` read wants."
        )
    )
    title: str = Field(default="", description="Human title; empty ⇒ fall back to the slug")
    summary: str = Field(
        default="",
        description=(
            "The AUTHORED one-line gist (front-matter `summary`), capped at 500 "
            "chars by validation. Empty is common — it is optional on the author's "
            "side — which is why `excerpt` exists as well."
        ),
    )
    excerpt: str = Field(
        default="",
        description=(
            "First ~400 chars of the markdown body with runs of whitespace "
            "collapsed, cut on a word boundary and suffixed with '…' when it was "
            "truncated. DERIVED, never authored. Whitespace collapsing means "
            "markdown structure (headings, bullets) does not survive it — render "
            "it as a plain prose preview, never as markdown."
        ),
    )
    body_chars: int = Field(
        default=0,
        description=(
            "Length of the FULL raw body (not the excerpt). `body_chars > "
            "len(excerpt)` ⇒ there is more text behind the canonical read."
        ),
    )
    layer: str = Field(
        default="",
        description=(
            'The SKILL\'s own ontology layer: "public" or "enhanced". Unlike '
            "``GlobalOntologySource.registry_layer`` this is the SAME axis as "
            "``GlobalOntologyType.layer`` and may be rendered with the same badge "
            "— but it can legitimately DIFFER from the enclosing type's layer: "
            "skills are looked up by type NAME across both global layers, so a "
            "public-layer type can carry a curated enhanced-layer skill (and that "
            "skill is only visible to entitled workspaces at resolution time). "
            "TENANT-layer skills can never appear here — see "
            "``GlobalOntologyType.skills``."
        ),
    )
    enabled: bool = Field(
        default=True,
        description=(
            "False ⇒ authored but switched off. A disabled skill is still listed "
            "(this is the operator's raw browse view) and is NOT injected into any "
            "agent prompt; it also SUPPRESSES a same-slug skill from a lower layer "
            "rather than falling through to it (`skills.resolve.merge_layers`)."
        ),
    )
    version: int = Field(
        default=1, description="Monotonic per-(scope, type, slug) revision, bumped on upsert"
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
            "graph. Enhanced attachments use ``types/x/<T>`` via "
            "``queries.register_function_triple`` (ONTA-399); Public may not "
            "carry functions (ONTA-400). ``entity_type`` is the enclosing "
            "type's name; ``tier`` is not stored in the graph and carries the "
            "model default, exactly as the tenant "
            "``GET /graphs/{tenant}/functions`` route reports it."
        ),
    )
    skills: list[GlobalOntologySkill] = Field(
        default_factory=list,
        description=(
            "Curated GLOBAL-layer skills attached to this type NAME — markdown "
            "prose taught to an LM agent, NOT executable code (that is "
            "``functions``). Sorted by slug, with the skill's own layer breaking "
            "ties so a slug curated in BOTH global layers lists as two adjacent "
            "rows: this is the operator's raw browse view, so that override is "
            "SHOWN, not silently resolved (Enhanced wins at resolution time). "
            "Bodies are NOT inlined — see ``GlobalOntologySkill``. "
            "**Global layers only.** A workspace's private tenant-layer skills "
            "live in the durable per-tenant store and are structurally "
            "unreachable from here: this reader calls "
            "``skills.registry.global_skills_for_type``, which reads only the "
            "process-wide curated registry (whose writer, ``register_skill_layer``, "
            "REJECTS ``Layer.TENANT`` and blanks ``tenant_id``) plus the OSS seed "
            "directory. It never touches the store and takes no tenant context. "
            "Empty is normal: no curated skill for the name, or the skills "
            "subsystem was unavailable (which degrades to [] and never fails the "
            "request)."
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


# ---------------------------------------------------------------------------
# Workspace-scoped layered ontology (ONTA-397; Wave 0 signature freeze)
#
# NEW models on purpose — same rationale as GlobalOntology*: TypeResponse is
# the tenant routes' contract and GlobalOntologyResponse is the operator's
# raw two-layer browse. Neither is contorted. The workspace reader returns
# the EFFECTIVE ontology for one tenant: LayerStack shadowing applied,
# Enhanced only when entitled.
# ---------------------------------------------------------------------------


class WorkspaceOntologyType(BaseModel):
    """One type in the workspace's effective (shadowed) ontology.

    ``layer`` is the layer that WON under shadowing (first visible definition),
    so a Public type overridden by a tenant redefinition reports ``"tenant"``.
    """

    name: str
    layer: str = Field(
        description='Winning layer under shadowing: "tenant", "enhanced", or "public"'
    )
    description: str | None = None
    parent_type: str | None = Field(
        default=None, description="Bare parent type NAME from rdfs:subClassOf, not a URI"
    )
    subtypes: list[str] = Field(default_factory=list)
    attributes: list[GlobalOntologyAttribute] = Field(default_factory=list)
    relationships: list[GlobalOntologyRelationship] = Field(default_factory=list)
    sources: list[GlobalOntologySource] = Field(default_factory=list)
    functions: list[FunctionRef] = Field(default_factory=list)
    skills: list[GlobalOntologySkill] = Field(default_factory=list)


class WorkspaceOntologyLayer(BaseModel):
    """Per-layer status line for a workspace read (mirrors GlobalOntologyLayer)."""

    layer: str
    graph_uri: str
    type_count: int = 0
    available: bool = True


class WorkspaceOntologyResponse(BaseModel):
    """Body of the workspace-scoped layered ontology read (ONTA-397).

    Distinct from :class:`GlobalOntologyResponse` (operator, both global layers,
    shadowing shown not applied) and from :class:`TypeResponse` (single-tenant
    legacy routes). Empty is 200 with ``types: []``, never an error.
    """

    tenant_id: str = ""
    entitled: bool = False
    layers: list[WorkspaceOntologyLayer] = Field(default_factory=list)
    types: list[WorkspaceOntologyType] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Ontology change / commit vocabulary (ONTA-403/404/406; Wave 0 freeze)
#
# ChangeRecord is the typed change vocabulary shared by the diff producer
# (ONTA-406) and the compat classifier (ONTA-404). OntologyMutation is what
# every schema-write site will hand to commit_ontology (ONTA-403).
# ---------------------------------------------------------------------------


class ChangeKind(str, Enum):
    """Typed ontology change kinds. Keep exhaustive: pin tests fail if a
    required kind is renamed or dropped.
    """

    ADD_TYPE = "add_type"
    REMOVE_TYPE = "remove_type"
    ADD_ATTRIBUTE = "add_attribute"
    REMOVE_ATTRIBUTE = "remove_attribute"
    ADD_RELATIONSHIP = "add_relationship"
    REMOVE_RELATIONSHIP = "remove_relationship"
    ADD_SUBCLASS = "add_subclass"
    REMOVE_SUBCLASS = "remove_subclass"
    CHANGE_COMMENT = "change_comment"
    CHANGE_RANGE = "change_range"
    CHANGE_CORE_SLOT = "change_core_slot"
    CHANGE_TEXT_KIND = "change_text_kind"
    DEPRECATE = "deprecate"
    RENAME_WITH_ALIAS = "rename_with_alias"


class ChangeRecord(BaseModel):
    """One typed ontology change — diff unit for ONTA-406 and input to ONTA-404.

    Companions under ``attr_meta/`` are NOT ontology content and must never
    appear as a ChangeRecord (plan §2).
    """

    kind: ChangeKind
    type_name: str | None = Field(
        default=None, description="Bare type name the change attaches to, when applicable"
    )
    slot_name: str | None = Field(
        default=None,
        description="Attribute or relationship leaf name, when the change is slot-scoped",
    )
    parent_type: str | None = Field(
        default=None, description="Parent type name for subclass-edge changes"
    )
    old_value: str | None = None
    new_value: str | None = None
    from_name: str | None = Field(
        default=None, description="Prior name for RENAME_WITH_ALIAS"
    )
    to_name: str | None = Field(
        default=None, description="New name for RENAME_WITH_ALIAS"
    )
    superseded_by: str | None = Field(
        default=None, description="Replacement identity for DEPRECATE"
    )


class OntologyOpKind(str, Enum):
    """Schema-write ops accepted by :func:`commit_ontology` (ONTA-403)."""

    UPSERT_TYPE = "upsert_type"
    UPSERT_ATTRIBUTE = "upsert_attribute"
    UPSERT_RELATIONSHIP = "upsert_relationship"
    SET_SUBCLASS = "set_subclass"
    DELETE_TYPE = "delete_type"
    DELETE_ATTRIBUTE = "delete_attribute"
    SET_CORE_SLOT = "set_core_slot"
    SET_TEXT_KIND = "set_text_kind"
    SET_COMMENT = "set_comment"
    REGISTER_ALIAS = "register_alias"
    DEPRECATE = "deprecate"


class OntologyMutation(BaseModel):
    """One schema op submitted to :func:`commit_ontology`.

    Fields not relevant to a given ``op`` stay ``None``; the commit body
    (ONTA-403) validates required fields per op.
    """

    op: OntologyOpKind
    type_name: str = Field(description="Bare type name the op attaches to")
    slot_name: str | None = None
    description: str | None = None
    datatype: str | None = None
    target_type: str | None = Field(
        default=None, description="Relationship range (bare type name)"
    )
    parent_type: str | None = None
    core_slot: bool | None = None
    text_kind: str | None = None
    alias_from: str | None = None
    alias_to: str | None = None
    superseded_by: str | None = None


class OntologyCommitResult(BaseModel):
    """Return of :func:`commit_ontology` — fingerprint + applied ops + diffs."""

    graph_uri: str
    version_before: str | None = None
    version_after: str = ""
    applied: list[OntologyMutation] = Field(default_factory=list)
    change_records: list[ChangeRecord] = Field(default_factory=list)


class AttributeAdd(BaseModel):
    attributes: list[AttributeDefinition] = Field(min_length=1)


class SubtypeAdd(BaseModel):
    subtype: str = Field(min_length=1, description="Name of the child type")


class AliasRegister(BaseModel):
    """Register an attribute alias (old → new) on the tenant ontology graph.

    ONTA-407a authoring path. Records
    ``<old-attr-IRI> onto/aliasOf <new-attr-IRI>`` so the NL query rewriter can
    resolve renamed predicates immediately (ADR 0002 §7). Lazy instance
    backfill + retirement are ONTA-407b.
    """

    type_name: str = Field(
        min_length=1,
        description="Type owning the OLD attribute (context for bare leaves and ChangeRecord)",
    )
    from_slot: str = Field(
        min_length=1,
        description="Old attribute leaf name, or a full attribute IRI",
    )
    to_slot: str = Field(
        min_length=1,
        description="New attribute leaf name, or a full attribute IRI",
    )
    to_type: str | None = Field(
        default=None,
        description=(
            "Type owning the NEW attribute when different from type_name "
            "(hierarchy move, e.g. Guest.phone_num → Person.phone)"
        ),
    )


class AliasMapResponse(BaseModel):
    """Flattened old→new attribute IRI map for a tenant ontology graph."""

    aliases: dict[str, str] = Field(
        default_factory=dict,
        description="old_attr_iri → new_attr_iri (chains flattened to one hop)",
    )


# ---------------------------------------------------------------------------
# Ontology changelog reader (ONTA-401)
# ---------------------------------------------------------------------------


class OntologyChangelogEntry(BaseModel):
    """One append-only ontology changelog entry (workspace companion graph).

    The ``changes`` list is the full :class:`ChangeRecord` delta written at
    commit time — enough to describe the mutation without consulting the live
    ontology graph. Thin governance-shaped entries may leave ``changes`` empty.
    """

    entry_uri: str
    action: str
    subject: str = Field(
        description=(
            "Target graph URI for commit_ontology writes; type/shape URI for "
            "global governance entries"
        )
    )
    timestamp: str
    tenant_id: str | None = None
    actor: str | None = None
    message: str | None = None
    version_before: str | None = None
    version_after: str | None = None
    revision: int | None = None
    changes: list[ChangeRecord] = Field(default_factory=list)


class OntologyChangelogResponse(BaseModel):
    """Paginated workspace ontology changelog (newest first)."""

    tenant_id: str
    graph_uri: str
    count: int
    offset: int
    limit: int
    entries: list[OntologyChangelogEntry] = Field(default_factory=list)


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
