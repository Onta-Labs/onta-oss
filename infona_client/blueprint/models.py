"""Frozen v1 Blueprint manifest models (INF-563 / ADR 0014).

This is the *document* schema for ``blueprint.yaml`` (JSON alias allowed).
The directory layout is ADR 0014. ``load.load_blueprint_package`` (INF-566)
reads a root manifest; ``package.load_blueprint_package`` (INF-565) also
assembles sibling section files. This module does not read a graph and does
not write one.

Pydantic shapes only. Cross-field rules live in ``validate.py``. Unknown
keys are rejected (``extra="forbid"``) at every object — a field that does
not exist cannot be filled by an author in a hurry (INF-564).
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION: Literal["1"] = "1"
SCHEMA_STATUS: Literal["v1-frozen"] = "v1-frozen"

#: INF-559 / INF-587 proposed bound — validator hard cap, not a guideline.
SAMPLE_MAX_ENTITIES = 25
SAMPLE_MAX_BYTES = 64 * 1024

SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$"
)
#: ``namespace/name`` — house-marked package id, lowercase kebab.
PACKAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}/[a-z0-9][a-z0-9-]{0,62}$")
SPDX_RE = re.compile(r"^[A-Za-z0-9.+-]+$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: Same shape as ``infona_client.skills.models.TYPE_NAME_RE`` / body caps —
#: copied so this protocol module does not import the skill store.
TYPE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
MAX_SKILL_BODY_CHARS = 20_000
MAX_SKILL_TITLE_CHARS = 200
MAX_SKILL_SUMMARY_CHARS = 500
#: ClinicalTrials.gov NCT form. Synthetic samples must not mint this.
REAL_NCT_RE = re.compile(r"^NCT\d{8}$")

#: Top-level keys a v1 manifest may carry. Anything else is rejected.
ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "schema_status",
        "id",
        "name",
        "namespace",
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
        "functions",
        "mcp_tools",
        "examples",
        "evals",
        "sample",
    }
)

#: Workspace-only categories. Not optional, not nullable — no field at all.
FORBIDDEN_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "records",
        "entities",
        "instances",
        "data",
        "triples",
        "graph",
        "credentials",
        "secrets",
        "tokens",
        "api_keys",
        "api_key",
        "jobs",
        "schedules",
        "cron",
        "last_run",
        "next_run",
        "citations",
        "provenance",
        "freshness_status",
        "last_refresh",
        "source_health",
        "staleness",
        "latest",
    }
)

_Strict = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _StrictModel(BaseModel):
    model_config = _Strict


class LineageEntry(_StrictModel):
    """One ancestor. A fork records parent id + exact version (INF-560 C2)."""

    id: str = Field(min_length=1, max_length=200)
    version: str

    @field_validator("version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if not SEMVER_RE.match(value):
            raise ValueError(f"version must be semver, got {value!r}")
        return value


class Lineage(_StrictModel):
    """Seed: no parent. Fork: ``parent`` required; ``chain`` keeps ancestors."""

    parent: LineageEntry | None = None
    chain: list[LineageEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fork_has_parent_in_chain(self) -> Lineage:
        if self.parent is None:
            return self
        if not self.chain:
            raise ValueError("a fork must keep the lineage chain (INF-560 C2)")
        head = self.chain[0]
        if head.id != self.parent.id or head.version != self.parent.version:
            raise ValueError("lineage.chain[0] must equal lineage.parent")
        return self


class ConceptAttribute(_StrictModel):
    """One slot on a concept. ``kind`` is required — literal vs type-ranged
    is load-bearing (predicate namespace at install, INF-576).

    Field names match ``OntoAttrRecord``: ``name``, ``kind``, ``datatype``,
    ``range_type``, ``cardinality``. Classification is delegated to
    ``classify_attr_range`` in the validator, not reimplemented here.
    """

    name: str = Field(min_length=1, max_length=200)
    kind: Literal["literal", "relationship"]
    description: str = ""
    optional: bool = False
    datatype: str | None = None
    range_type: str | None = None
    cardinality: str = "1:1"

    @model_validator(mode="after")
    def _kind_fields(self) -> ConceptAttribute:
        if self.kind == "literal":
            if not self.datatype:
                raise ValueError("literal attributes require datatype")
            if self.range_type is not None:
                raise ValueError("literal attributes must not set range_type")
        else:
            if not self.range_type:
                raise ValueError("relationship attributes require range_type")
            if self.datatype is not None:
                raise ValueError("relationship attributes must not set datatype")
        return self


class Concept(_StrictModel):
    """One typed entity. Names and identity keys align with ``OntoTypeRecord``."""

    name: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)
    description: str = ""
    identity: list[str] = Field(min_length=1)
    parent_type: str | None = None
    attributes: list[ConceptAttribute] = Field(min_length=1)


class Relationship(_StrictModel):
    """Named directed edge. Cardinality is how acquisition fans out."""

    name: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, description="From-concept name")
    target: str = Field(min_length=1, description="To-concept name")
    cardinality: str
    description: str = ""


class Task(_StrictModel):
    """Named job in the domain. The cron row stays workspace-side."""

    id: str
    description: str = Field(min_length=1)
    writes: bool
    attaches_to: str | None = None
    inputs: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError(f"task id must be a slug, got {value!r}")
        return value


class ConflictRule(_StrictModel):
    attribute: str = Field(min_length=1)
    winner: str = Field(min_length=1)
    notes: str = ""


class TombstoneRule(_StrictModel):
    disappeared_row: Literal["withdrawn", "keep"]
    delete_forbidden: Literal[True] = True


class Rules(_StrictModel):
    """Maintenance rules that are not freshness windows or ER config."""

    conflict: list[ConflictRule] = Field(min_length=1)
    tombstones: TombstoneRule


class SourceMapping(_StrictModel):
    """Field mapping only. Credentials are not representable here."""

    source_field: str = Field(min_length=1)
    lands_on: str = Field(min_length=1)
    kind: Literal["literal", "relationship"]


class SourceDefinition(_StrictModel):
    """Definition + mapping. Credential *mode* only — never a secret value.

    ``key_env`` is the environment-variable NAME (BYOK, ADR 0011). The
    package never ships or implies a shared platform key.
    """

    id: str
    title: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    description: str = ""
    license: str
    url: str = Field(min_length=1)
    credential: Literal["none", "byok"]
    key_env: str = ""
    declared_cadence: str = Field(min_length=1)
    mappings: list[SourceMapping] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError(f"source id must be a slug, got {value!r}")
        return value

    @field_validator("license")
    @classmethod
    def _spdx(cls, value: str) -> str:
        if not SPDX_RE.match(value):
            raise ValueError(f"license must be an SPDX id, got {value!r}")
        return value

    @model_validator(mode="after")
    def _credential_is_name_only(self) -> SourceDefinition:
        if self.credential == "none":
            if self.key_env:
                raise ValueError("key_env is only legal when credential is byok")
            return self
        if not ENV_NAME_RE.match(self.key_env):
            raise ValueError(
                "byok sources must set key_env to an environment-variable NAME "
                f"(A-Z / digit / _), got {self.key_env!r}"
            )
        return self


class AcquisitionInstruction(_StrictModel):
    """How first pull differs from later refresh. Not the records."""

    source: str
    seed_query: str = Field(min_length=1)
    page_cap: int = Field(ge=1)
    first_pull: str = Field(min_length=1)
    later_refresh: str = Field(min_length=1)
    disappeared_row: str = Field(min_length=1)
    conflict: str = Field(min_length=1)


class ValidationCheck(_StrictModel):
    id: str
    description: str = Field(min_length=1)
    rule: str = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError(f"validation id must be a slug, got {value!r}")
        return value


class FreshnessPolicy(_StrictModel):
    """Stale-after / cadence promise. Not last-refresh status."""

    target: str = Field(min_length=1, description="source id or Type.attribute")
    stale_after_days: int | Literal["never"]
    cadence: str = Field(min_length=1)

    @field_validator("stale_after_days")
    @classmethod
    def _window(cls, value: int | Literal["never"]) -> int | Literal["never"]:
        if value == "never":
            return value
        if isinstance(value, int) and value >= 1:
            return value
        raise ValueError("stale_after_days must be >= 1 or the sentinel 'never'")


class EntityResolutionConfig(_StrictModel):
    """Identity rule for one concept. Field names match ``ERConfig`` plus
    the interview-facing identity / blocking keys from INF-559.
    """

    type_name: str = Field(min_length=1)
    identity: list[str] = Field(min_length=1)
    blocking: list[str] = Field(min_length=1)
    signals: list[str] = Field(default_factory=list)
    weights: list[float] = Field(default_factory=list)
    auto_merge_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    review_threshold: float = Field(default=0.70, ge=0.0, le=1.0)
    decisive_signals: list[str] = Field(default_factory=list)
    reversible: Literal[True] = True

    @model_validator(mode="after")
    def _weights_align(self) -> EntityResolutionConfig:
        if len(self.signals) != len(self.weights):
            raise ValueError("ER signals and weights must be the same length")
        if self.signals:
            total = sum(self.weights)
            if abs(total - 1.0) > 1e-6:
                raise ValueError(f"ER weights must sum to 1.0, got {total}")
        if self.review_threshold > self.auto_merge_threshold:
            raise ValueError("review_threshold must be <= auto_merge_threshold")
        extra = set(self.decisive_signals) - set(self.signals) - set(self.identity)
        if extra:
            raise ValueError(f"decisive_signals not in signals/identity: {sorted(extra)}")
        return self


class Freshness(_StrictModel):
    """Policies + ER config. Status lives in the workspace."""

    policies: list[FreshnessPolicy] = Field(min_length=1)
    er: list[EntityResolutionConfig] = Field(min_length=1)


class BlueprintSkill(_StrictModel):
    """Type-attached prose. Not a function. Identifiers share skill regexes."""

    slug: str
    type_name: str
    body: str = Field(min_length=1)
    title: str = ""
    summary: str = ""


class NamedFunction(_StrictModel):
    """Name of a derived value. Implementations are not representable (C1)."""

    name: str
    type_name: str
    description: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError(f"function name must be a slug, got {value!r}")
        return value


class ExampleQuestion(_StrictModel):
    """Supported question. Answers are computed in a workspace, not shipped."""

    question: str = Field(min_length=1)


class EvalCheck(_StrictModel):
    id: str
    kind: Literal["structural", "question"]
    description: str = Field(min_length=1)
    still_works_when: str = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError(f"eval id must be a slug, got {value!r}")
        return value


class SampleEntity(_StrictModel):
    type: str = Field(min_length=1)
    attributes: dict[str, str | int | float | bool] = Field(min_length=1)


class SampleDataset(_StrictModel):
    """Bounded preview. Explicitly separated. Never current (INF-587)."""

    captured_at: date
    license: str
    kind: Literal["synthetic", "openly_licensed"]
    entities: list[SampleEntity] = Field(min_length=1, max_length=SAMPLE_MAX_ENTITIES)

    @field_validator("license")
    @classmethod
    def _spdx(cls, value: str) -> str:
        if not SPDX_RE.match(value):
            raise ValueError(f"sample license must be an SPDX id, got {value!r}")
        return value


class BlueprintManifest(_StrictModel):
    """Frozen v1 Blueprint document.

    Required sections match INF-559 / INF-563. ``sample`` is the only
    legal instance-shaped section. ``functions`` and ``mcp_tools`` may be
    omitted (empty). Everything else listed in ``ALLOWED_TOP_LEVEL_KEYS``
    that is not those three is required.
    """

    schema_version: Literal["1"]
    schema_status: Literal["v1-frozen"]
    id: str
    name: str = Field(min_length=1, max_length=200)
    namespace: str = Field(min_length=1, max_length=80)
    version: str
    acquisition_revision: int = Field(ge=1)
    license: str
    attribution: str = Field(min_length=1)
    published_at: date
    last_reviewed_at: date
    lineage: Lineage
    concepts: list[Concept] = Field(min_length=1)
    relationships: list[Relationship]
    tasks: list[Task] = Field(min_length=1)
    rules: Rules
    sources: list[SourceDefinition] = Field(min_length=1)
    acquisition: list[AcquisitionInstruction] = Field(min_length=1)
    validation: list[ValidationCheck] = Field(min_length=1)
    freshness: Freshness
    skills: list[BlueprintSkill]
    functions: list[NamedFunction] = Field(default_factory=list)
    mcp_tools: list[str] = Field(default_factory=list)
    examples: list[ExampleQuestion] = Field(min_length=3)
    evals: list[EvalCheck] = Field(min_length=3)
    sample: SampleDataset | None = None

    @field_validator("id")
    @classmethod
    def _package_id(cls, value: str) -> str:
        if not PACKAGE_ID_RE.match(value):
            raise ValueError(
                "id must be namespace/name in lowercase kebab, "
                f"got {value!r}"
            )
        return value

    @field_validator("version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if not SEMVER_RE.match(value):
            raise ValueError(f"version must be semver, got {value!r}")
        return value

    @field_validator("license")
    @classmethod
    def _spdx(cls, value: str) -> str:
        if not SPDX_RE.match(value):
            raise ValueError(f"license must be an SPDX id, got {value!r}")
        return value

    @model_validator(mode="after")
    def _id_matches_namespace(self) -> BlueprintManifest:
        expected_prefix = f"{self.namespace}/"
        if not self.id.startswith(expected_prefix):
            raise ValueError(
                f"id {self.id!r} must start with namespace {self.namespace!r}"
            )
        return self


def _load_manifest_text(raw: str | bytes) -> Any:
    """JSON if the text is an object/array; otherwise YAML (ADR 0014 default)."""
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return json.loads(text)
    try:
        import yaml
    except ImportError as exc:
        raise ValueError(
            "YAML blueprints require PyYAML (pip install pyyaml)"
        ) from exc
    payload = yaml.safe_load(text)
    if payload is None:
        raise ValueError("blueprint manifest is empty")
    return payload


def parse_blueprint(raw: Mapping[str, Any] | str | bytes) -> BlueprintManifest:
    """Parse JSON, YAML, or a mapping into a v1 manifest.

    Unknown top-level keys fail here (``extra="forbid"``), not by ignore.
    YAML is the human default (ADR 0014 F1); JSON is the machine alias.
    """
    if isinstance(raw, (str, bytes)):
        payload = _load_manifest_text(raw)
    else:
        payload = raw
    if not isinstance(payload, Mapping):
        raise ValueError("blueprint manifest must be a JSON object")
    return BlueprintManifest.model_validate(payload)


def dumps_blueprint(manifest: BlueprintManifest) -> str:
    """Canonical JSON round-trip form (dates as ISO, no NaN)."""
    return json.dumps(
        manifest.model_dump(mode="json", exclude_none=True),
        indent=2,
        sort_keys=False,
        allow_nan=False,
    ) + "\n"


__all__ = [
    "ALLOWED_TOP_LEVEL_KEYS",
    "FORBIDDEN_TOP_LEVEL_KEYS",
    "REAL_NCT_RE",
    "SAMPLE_MAX_BYTES",
    "SAMPLE_MAX_ENTITIES",
    "SCHEMA_STATUS",
    "SCHEMA_VERSION",
    "AcquisitionInstruction",
    "BlueprintManifest",
    "BlueprintSkill",
    "Concept",
    "ConceptAttribute",
    "EntityResolutionConfig",
    "EvalCheck",
    "ExampleQuestion",
    "Freshness",
    "NamedFunction",
    "Relationship",
    "Rules",
    "SampleDataset",
    "SourceDefinition",
    "Task",
    "dumps_blueprint",
    "parse_blueprint",
]
