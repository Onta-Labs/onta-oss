"""Blueprint manifest schema v1-frozen (INF-563, INF-587).

A Blueprint is the *means* to acquire and maintain a domain (INF-564). This
module is the structural encoding of that decision: the models have no field
for instance records (outside the bounded ``sample`` section), credentials,
scheduled jobs, citations/provenance, or freshness *status*. A field that
does not exist cannot be filled by an author in a hurry.

The JSON Schema export (``v1_frozen.schema.json``) sets
``additionalProperties: false`` at every object. The validator rejects
unknown top-level keys rather than ignoring them.

Field names follow the Sprint 1 paper contract
(``docs/blueprints/INF-559-package-contract-v0.md`` in the parent repo).
This file is the schema; if a label there disagrees, this file wins.

Boundary: OSS protocol. Pure ``infona_client.*`` / pydantic / stdlib — no
``from infona.*``. Keyed sources are BYOK: the package may name an env var,
never a secret value, never a platform key (ADR 0011).
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Frozen identity
# ---------------------------------------------------------------------------

SCHEMA_VERSION: Literal["v1-frozen"] = "v1-frozen"
"""The only legal ``schema_version``. v1 is frozen: additive optional
fields require a new schema version, not a silent edit of this one."""

SEMVER_RE = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"

TYPE_NAME_RE = r"^[A-Za-z][A-Za-z0-9_]{0,127}$"
ATTR_NAME_RE = r"^[a-z][a-z0-9_]{0,127}$"
SLUG_RE = r"^[a-z0-9][a-z0-9_-]{0,63}$"
KEY_ENV_RE = r"^[A-Z][A-Z0-9_]{1,127}$"

# Top-level keys a v1-frozen manifest may carry. Anything else is rejected.
# Workspace facts (records, credentials, jobs, citations, freshness status)
# are deliberately absent — not optional, not nullable.
ALLOWED_TOP_LEVEL: frozenset[str] = frozenset(
    {
        "schema_version",
        "id",
        "namespace",
        "name",
        "version",
        "acquisition_revision",
        "license",
        "attribution",
        "published_at",
        "last_reviewed_at",
        "lineage",
        "concepts",
        "relationships",
        "tasks",
        "rules",
        "sources",
        "acquisition",
        "validation",
        "freshness",
        "skills",
        "examples",
        "evals",
        "sample",
    }
)

# Names that must not appear as a property anywhere in the schema. These are
# the INF-563 / INF-564 excluded categories. ``sample`` is the one legal
# instance-shaped section; it is not in this set.
#
# Source-definition URLs (``url``, ``docs_url``) stay representable — they
# name the binding, they are not per-cell citations (INF-560 C3).
UNREPRESENTABLE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "records",
        "instances",
        "triples",
        "credentials",
        "credential_value",
        "api_key",
        "secret",
        "password",
        "token",
        "scheduled_jobs",
        "schedule",
        "cron",
        "jobs",
        "last_run",
        "next_run",
        "citations",
        "provenance",
        "freshness_status",
        "last_refresh",
        "last_pulled_at",
        "source_health",
        "is_current",
    }
)


class StrictModel(BaseModel):
    """Every Blueprint object rejects unknown keys. Extra='ignore' would
    silently accept a hurried author stuffing credentials into a typo'd
    field; that is the failure INF-563 exists to make impossible."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Metadata / lineage
# ---------------------------------------------------------------------------


class LineageParent(StrictModel):
    """Exact parent pin. Required on a fork; absent on a seed (INF-560 C2)."""

    id: str = Field(min_length=1, max_length=200)
    version: str = Field(pattern=SEMVER_RE)


class LineageEntry(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    version: str = Field(pattern=SEMVER_RE)
    forked_at: date


class Lineage(StrictModel):
    """A chain, not a parent pointer. A fork of a fork keeps the whole chain
    (INF-579). Seeds omit ``parent`` and leave ``chain`` empty."""

    parent: LineageParent | None = None
    chain: list[LineageEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Concepts + relationships (domain model; INF-576 predicate kind)
# ---------------------------------------------------------------------------


class AttributeKind(str, Enum):
    """Decides the instance-edge predicate namespace at install (INF-576).

    * ``literal`` → ``https://graph.infona.ai/types/<Type>/attrs/<leaf>``
    * ``type_ranged`` → ``https://graph.infona.ai/onto/<leaf>``

    These are not interchangeable. A type-ranged attribute written as a
    literal is invisible to the NL planner.
    """

    literal = "literal"
    type_ranged = "type_ranged"


class Cardinality(str, Enum):
    one = "1"
    many = "N"


class Attribute(StrictModel):
    name: str = Field(pattern=ATTR_NAME_RE)
    kind: AttributeKind
    range: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Literal: an XSD-ish datatype name (string, integer, date, …). "
            "Type-ranged: a concept name in this manifest."
        ),
    )
    required: bool = False
    identity: bool = False
    description: str = ""
    allowed_values: list[str] | None = None


class Concept(StrictModel):
    name: str = Field(pattern=TYPE_NAME_RE)
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    attributes: list[Attribute] = Field(min_length=1)
    identity_keys: list[str] = Field(min_length=1)


class Relationship(StrictModel):
    """A type-ranged instance edge. ``kind`` is fixed: a relationship cannot
    be a literal. Cardinality tells acquisition whether the slot is one or
    many (INF-559)."""

    name: str = Field(pattern=ATTR_NAME_RE)
    source: str = Field(pattern=TYPE_NAME_RE)
    target: str = Field(pattern=TYPE_NAME_RE)
    cardinality: Cardinality
    description: str = ""
    kind: Literal["type_ranged"] = "type_ranged"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class Task(StrictModel):
    """A named job in this domain. Not a skill, not a function, not a
    scheduled job instance. Credentials and the cron row stay workspace-side."""

    name: str = Field(pattern=SLUG_RE)
    description: str = Field(min_length=1, max_length=2000)
    writes: bool
    attaches_to: str | None = Field(default=None, pattern=TYPE_NAME_RE)
    inputs: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Rules (conflict / tombstone policy — what maintenance does)
# ---------------------------------------------------------------------------


class ConflictRule(StrictModel):
    attribute: str = Field(min_length=1, max_length=200)
    winner: str = Field(min_length=1, max_length=200)
    notes: str = ""


class TombstoneRule(StrictModel):
    when: str = Field(min_length=1, max_length=200)
    action: Literal["mark_withdrawn", "stop_refresh"]
    notes: str = ""


class Rules(StrictModel):
    conflict: list[ConflictRule] = Field(default_factory=list)
    tombstones: list[TombstoneRule] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Sources — definitions and mappings only (INF-564)
# ---------------------------------------------------------------------------


class SourceCredential(str, Enum):
    """How a source authenticates. Never a secret value.

    * ``none`` — public, no key.
    * ``byok`` — the installing workspace supplies its own env var (named
      in ``key_env``). Dormant until that var is present. Never a platform
      key (ADR 0011).
    """

    none = "none"
    byok = "byok"


class SourceDefinition(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=80)
    url: str | None = None
    docs_url: str | None = None
    license: str | None = None
    publisher: str = ""
    credential: SourceCredential = SourceCredential.none
    key_env: str | None = Field(
        default=None,
        description="Env-var NAME only. Required when credential=byok. Never a value.",
    )

    @field_validator("key_env")
    @classmethod
    def _key_env_is_a_name(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        import re

        if not re.match(KEY_ENV_RE, value):
            raise ValueError(
                "key_env must be an UPPER_SNAKE env-var NAME, never a secret value"
            )
        return value


class FieldMapping(StrictModel):
    source_field: str = Field(min_length=1, max_length=400)
    lands_on: str = Field(min_length=1, max_length=200)
    kind: AttributeKind


class Source(StrictModel):
    id: str = Field(pattern=SLUG_RE)
    definition: SourceDefinition
    mapping: list[FieldMapping] = Field(min_length=1)
    cadence: str | None = Field(
        default=None,
        description="Declared refresh cadence *policy*. Not last-run status.",
    )


# ---------------------------------------------------------------------------
# Acquisition — how to go get the data, not the data
# ---------------------------------------------------------------------------


class AcquisitionStep(StrictModel):
    name: str = Field(pattern=SLUG_RE)
    instruction: str = Field(min_length=1, max_length=4000)


class EnrichmentInstruction(StrictModel):
    """Fills an existing node. Does not mint a second one."""

    name: str = Field(pattern=SLUG_RE)
    instruction: str = Field(min_length=1, max_length=4000)
    fills: str = Field(min_length=1, max_length=200)
    source: str = Field(pattern=SLUG_RE)


class Acquisition(StrictModel):
    steps: list[AcquisitionStep] = Field(min_length=1)
    enrichment: list[EnrichmentInstruction] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Validation + freshness *policy* + ER (INF-563)
# ---------------------------------------------------------------------------


class ValidationCheck(StrictModel):
    name: str = Field(pattern=SLUG_RE)
    rule: str = Field(min_length=1, max_length=2000)
    on: str = Field(min_length=1, max_length=200)


class EntityResolution(StrictModel):
    """Identity rule for one concept. Tenant isolation is not configurable."""

    type: str = Field(pattern=TYPE_NAME_RE)
    identity: str = Field(min_length=1, max_length=400)
    blocking: list[str] = Field(min_length=1)
    auto_merge: str = Field(min_length=1, max_length=400)
    review: str = Field(min_length=1, max_length=400)
    reversible: Literal[True] = True


class Validation(StrictModel):
    checks: list[ValidationCheck] = Field(min_length=1)
    entity_resolution: list[EntityResolution] = Field(min_length=1)


class StaleAfter(StrictModel):
    """Per-attribute stale-after *policy*. ``never=true`` means the value
    does not go stale (identity keys, official titles). There is no field
    for the live staleness of a cell."""

    attribute: str = Field(min_length=1, max_length=200)
    days: int | None = Field(default=None, ge=1)
    never: bool = False


class Freshness(StrictModel):
    stale_after: list[StaleAfter] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Skills — prose + named functions + expected MCP tools (no implementations)
# ---------------------------------------------------------------------------


class SkillProse(StrictModel):
    """Type-attached markdown. The skill IS the body. Not executable code."""

    slug: str = Field(pattern=SLUG_RE)
    type_name: str = Field(pattern=TYPE_NAME_RE)
    title: str = ""
    body: str = Field(min_length=1, max_length=20_000)


class FunctionName(StrictModel):
    """A computed function the agent may invoke. Name and contract only —
    no SPARQL, no endpoint, no lambda body (INF-560 C1)."""

    name: str = Field(pattern=SLUG_RE)
    entity_type: str = Field(pattern=TYPE_NAME_RE)
    description: str = ""


class Skills(StrictModel):
    prose: list[SkillProse] = Field(default_factory=list)
    functions: list[FunctionName] = Field(default_factory=list)
    mcp_tools: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Examples + evals (INF-567 leak policy is structural)
# ---------------------------------------------------------------------------


class ExampleQuestion(StrictModel):
    question: str = Field(min_length=1, max_length=1000)


class EvalItem(StrictModel):
    name: str = Field(pattern=SLUG_RE)
    kind: Literal["structural", "question"]
    still_works_when: str = Field(min_length=1, max_length=2000)


class Examples(StrictModel):
    """Supported questions. ``leak_policy`` is required so an author cannot
    omit the INF-567 constraint. Enforcement that these stay out of other
    tenants' example banks is a later platform guard; the schema only
    records the commitment."""

    leak_policy: Literal["workspace_only"]
    questions: list[ExampleQuestion] = Field(min_length=3)


class Evals(StrictModel):
    leak_policy: Literal["workspace_only"]
    items: list[EvalItem] = Field(min_length=3)


# ---------------------------------------------------------------------------
# Sample — the one bounded instance-data exception (INF-587)
# ---------------------------------------------------------------------------

# Hard caps. 25 entities is the INF-559 interview number: enough to render
# an entity map and run two labeled questions, not enough to skip
# acquisition. Every force (better preview, more convincing demo, one more
# entity) pushes the same way; the cap exists to lose those arguments.
#
# 64 KiB serialized is the byte backstop so a few huge entities cannot
# hide a dump behind a passing count. Whichever bound is hit first wins.
MAX_SAMPLE_ENTITIES = 25
MAX_SAMPLE_BYTES = 64 * 1024


class SampleEntity(StrictModel):
    """One preview row. ``is_sample`` is required and must be true — a row
    that is not marked as sample fails validation (INF-587)."""

    type: str = Field(pattern=TYPE_NAME_RE)
    id: str = Field(min_length=1, max_length=200)
    is_sample: Literal[True]
    attributes: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict
    )
    relationships: dict[str, str | list[str]] = Field(default_factory=dict)


class Sample(StrictModel):
    """Own top-level section. Installable, inspectable, and droppable
    independently of the ontology and sources. Never current."""

    kind: Literal["sample"]
    origin: Literal["synthetic", "open"]
    license: str = Field(min_length=1, max_length=80)
    captured_at: date
    entities: list[SampleEntity] = Field(min_length=1, max_length=MAX_SAMPLE_ENTITIES)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class BlueprintManifest(StrictModel):
    """v1-frozen Blueprint package. Unknown top-level keys are rejected."""

    schema_version: Literal["v1-frozen"]
    id: str = Field(pattern=SLUG_RE)
    namespace: str = Field(pattern=SLUG_RE)
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(pattern=SEMVER_RE)
    acquisition_revision: int = Field(
        ge=1,
        description=(
            "Independent of ``version``. Bump when an acquisition or "
            "freshness *instruction* changes (INF-560 C4). Not a silent MINOR."
        ),
    )
    license: str = Field(min_length=1, max_length=80)
    attribution: str = Field(min_length=1, max_length=500)
    published_at: date
    last_reviewed_at: date
    lineage: Lineage = Field(default_factory=Lineage)
    concepts: list[Concept] = Field(min_length=1)
    relationships: list[Relationship] = Field(default_factory=list)
    tasks: list[Task] = Field(min_length=1)
    rules: Rules = Field(default_factory=Rules)
    sources: list[Source] = Field(min_length=1)
    acquisition: Acquisition
    validation: Validation
    freshness: Freshness
    skills: Skills = Field(default_factory=Skills)
    examples: Examples
    evals: Evals
    sample: Sample | None = None

    @field_validator("version")
    @classmethod
    def _version_is_not_latest(cls, value: str) -> str:
        if value.strip().lower() == "latest":
            raise ValueError(
                "'latest' is a listing decoration, never what install writes "
                "(INF-560 C4)"
            )
        return value


def iter_schema_property_names(node: Any = None) -> set[str]:
    """Every ``properties`` key in the exported JSON Schema. Used to prove
    excluded categories are unrepresentable — no field at all.

    Walks objects and ``$defs`` once. Does not follow ``$ref`` (Pydantic
    schemas are cyclic through ``$defs``).
    """

    if node is None:
        node = BlueprintManifest.model_json_schema()
    names: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            props = obj.get("properties")
            if isinstance(props, dict):
                names.update(props)
                for value in props.values():
                    walk(value)
            defs = obj.get("$defs")
            if isinstance(defs, dict):
                for value in defs.values():
                    walk(value)
            for key in ("items", "additionalProperties", "anyOf", "oneOf", "allOf"):
                if key in obj:
                    walk(obj[key])
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(node)
    return names


def frozen_json_schema() -> dict[str, Any]:
    """JSON Schema for the v1-frozen manifest. ``additionalProperties`` is
    false on every object because every model is ``extra='forbid'``."""

    return BlueprintManifest.model_json_schema()
