"""Validate a Blueprint manifest against the v1-frozen schema (INF-563).

Unknown top-level keys are rejected. Excluded workspace categories
(records, credentials, scheduled jobs, citations/provenance, freshness
status) have no field and fail if an author invents one. The bounded
``sample`` section is the only instance-shaped exception; INF-587 caps
and markers are enforced here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from infona_client.blueprints.schema import (
    ALLOWED_TOP_LEVEL,
    MAX_SAMPLE_BYTES,
    MAX_SAMPLE_ENTITIES,
    UNREPRESENTABLE_FIELD_NAMES,
    AttributeKind,
    BlueprintManifest,
    Sample,
    SourceCredential,
)


class BlueprintValidationError(ValueError):
    """One or more structural problems. ``errors`` is the full list."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _as_mapping(source: Any) -> dict[str, Any]:
    if isinstance(source, (BlueprintManifest, Sample)):
        return source.model_dump(mode="json")
    if isinstance(source, Mapping):
        return dict(source)
    if isinstance(source, Path) or (
        isinstance(source, str)
        and not source.lstrip().startswith(("{", "["))
        and len(source) < 4096
    ):
        path = Path(source)
        if path.exists() and path.is_file():
            raw = path.read_text(encoding="utf-8")
        elif isinstance(source, Path):
            raise BlueprintValidationError([f"file not found: {source}"])
        else:
            raw = source
        text = raw.strip()
    elif isinstance(source, str):
        text = source.strip()
    else:
        text = None
    if text is not None:
        if text.startswith("{") or text.startswith("["):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise BlueprintValidationError([f"invalid JSON: {exc}"]) from exc
        else:
            try:
                import yaml  # type: ignore
            except ImportError as exc:
                raise BlueprintValidationError(
                    [
                        "YAML input requires PyYAML; pass JSON, a dict, "
                        "or install PyYAML"
                    ]
                ) from exc
            data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise BlueprintValidationError(
                ["manifest must be a JSON/YAML object at the top level"]
            )
        return data
    raise BlueprintValidationError(
        [f"unsupported manifest type: {type(source).__name__}"]
    )


def _pydantic_errors(exc: ValidationError) -> list[str]:
    out: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        msg = err.get("msg", "invalid")
        out.append(f"{loc}: {msg}" if loc else msg)
    return out


def _check_unknown_top_level(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    unknown = [key for key in data if key not in ALLOWED_TOP_LEVEL]
    if not unknown:
        return errors
    for key in unknown:
        if key in UNREPRESENTABLE_FIELD_NAMES:
            errors.append(
                f"top-level {key!r} is unrepresentable in a Blueprint "
                f"(workspace-side; INF-564 / INF-563)"
            )
        else:
            errors.append(
                f"unknown top-level key {key!r} (v1-frozen rejects unknown keys)"
            )
    return errors


def _check_source_byok(manifest: BlueprintManifest) -> list[str]:
    errors: list[str] = []
    for source in manifest.sources:
        cred = source.definition.credential
        key_env = source.definition.key_env
        if cred is SourceCredential.byok and not key_env:
            errors.append(
                f"sources[{source.id}].definition.key_env is required "
                f"when credential=byok (env-var NAME only; ADR 0011)"
            )
        if cred is SourceCredential.none and key_env:
            errors.append(
                f"sources[{source.id}].definition.key_env must be empty "
                f"when credential=none"
            )
    return errors


def _check_model_consistency(manifest: BlueprintManifest) -> list[str]:
    errors: list[str] = []
    concept_names = {c.name for c in manifest.concepts}
    attrs: dict[tuple[str, str], AttributeKind] = {}
    identity: dict[str, set[str]] = {}
    for concept in manifest.concepts:
        identity[concept.name] = set(concept.identity_keys)
        attr_names = {a.name for a in concept.attributes}
        for key in concept.identity_keys:
            if key not in attr_names:
                errors.append(
                    f"concepts[{concept.name}].identity_keys refers to "
                    f"unknown attribute {key!r}"
                )
        for attr in concept.attributes:
            attrs[(concept.name, attr.name)] = attr.kind
            if attr.identity and attr.name not in concept.identity_keys:
                errors.append(
                    f"concepts[{concept.name}].attributes[{attr.name}] is "
                    f"identity=true but is not in identity_keys"
                )
            if attr.kind is AttributeKind.type_ranged and attr.range not in concept_names:
                errors.append(
                    f"concepts[{concept.name}].attributes[{attr.name}].range "
                    f"{attr.range!r} is not a concept in this manifest"
                )

    rel_names = {rel.name for rel in manifest.relationships}
    for rel in manifest.relationships:
        if rel.source not in concept_names:
            errors.append(f"relationships[{rel.name}].source {rel.source!r} is unknown")
        if rel.target not in concept_names:
            errors.append(f"relationships[{rel.name}].target {rel.target!r} is unknown")
        slot = attrs.get((rel.source, rel.name))
        if slot is None:
            errors.append(
                f"relationships[{rel.name}] has no matching type-ranged "
                f"attribute on {rel.source}"
            )
        elif slot is not AttributeKind.type_ranged:
            errors.append(
                f"relationships[{rel.name}] maps to a literal attribute; "
                f"type-ranged vs literal must agree (INF-576)"
            )

    for (type_name, attr_name), kind in attrs.items():
        if kind is AttributeKind.type_ranged and attr_name not in rel_names:
            errors.append(
                f"concepts[{type_name}].attributes[{attr_name}] is "
                f"type_ranged but has no relationships[] entry "
                f"(predicate namespace is onto/<leaf> at install)"
            )

    source_ids = {s.id for s in manifest.sources}
    for step in manifest.acquisition.enrichment:
        if step.source not in source_ids:
            errors.append(
                f"acquisition.enrichment[{step.name}].source {step.source!r} "
                f"is not a declared source"
            )

    er_types = {e.type for e in manifest.validation.entity_resolution}
    for name in concept_names:
        if name not in er_types:
            errors.append(
                f"validation.entity_resolution is missing type {name!r} "
                f"(ER is required per type that can collide; INF-559)"
            )

    if manifest.sample is not None:
        errors.extend(_check_sample(manifest.sample, concept_names))
    return errors


def _check_sample(sample: Sample, concept_names: set[str] | None = None) -> list[str]:
    """INF-587: separated, timestamped, size-capped, marked as sample."""

    errors: list[str] = []
    if sample.kind != "sample":
        errors.append("sample.kind must be 'sample' (never presented as current)")
    if sample.captured_at is None:
        errors.append("sample.captured_at is required (timestamp as data, not README)")
    if len(sample.entities) > MAX_SAMPLE_ENTITIES:
        errors.append(
            f"sample has {len(sample.entities)} entities; hard cap is "
            f"{MAX_SAMPLE_ENTITIES} (INF-587 / INF-559)"
        )
    serialized = json.dumps(sample.model_dump(mode="json"), separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_SAMPLE_BYTES:
        errors.append(
            f"sample serializes to {len(serialized.encode('utf-8'))} bytes; "
            f"hard cap is {MAX_SAMPLE_BYTES} (64 KiB; INF-587)"
        )
    for i, entity in enumerate(sample.entities):
        if entity.is_sample is not True:
            errors.append(
                f"sample.entities[{i}].is_sample must be true "
                f"(not marked as sample)"
            )
        if concept_names is not None and entity.type not in concept_names:
            errors.append(
                f"sample.entities[{i}].type {entity.type!r} is not a concept"
            )
    return errors


def validate_sample(source: Any) -> Sample:
    """Validate a sample section on its own (independently droppable)."""

    data = _as_mapping(source)
    # A whole manifest passed by mistake: pull the section.
    if "schema_version" in data and "sample" in data:
        inner = data["sample"]
        if inner is None:
            raise BlueprintValidationError(["sample section is absent"])
        data = inner
    unknown = [key for key in data if key not in {"kind", "origin", "license", "captured_at", "entities"}]
    if unknown:
        raise BlueprintValidationError(
            [f"unknown sample key {key!r}" for key in unknown]
        )
    try:
        sample = Sample.model_validate(data)
    except ValidationError as exc:
        raise BlueprintValidationError(_pydantic_errors(exc)) from exc
    extra = _check_sample(sample)
    if extra:
        raise BlueprintValidationError(extra)
    return sample


def validate_manifest(source: Any) -> BlueprintManifest:
    """Load JSON/YAML/dict and return a valid v1-frozen manifest.

    Raises :class:`BlueprintValidationError` on any problem. Collects every
    independent error so an author sees the full list in one pass.
    """

    data = _as_mapping(source)
    errors = _check_unknown_top_level(data)
    if errors:
        # Unknown keys include the excluded categories. Fail before pydantic
        # so the message names INF-564 rather than "extra_forbidden".
        raise BlueprintValidationError(errors)
    try:
        manifest = BlueprintManifest.model_validate(data)
    except ValidationError as exc:
        raise BlueprintValidationError(_pydantic_errors(exc)) from exc
    extra = _check_source_byok(manifest) + _check_model_consistency(manifest)
    if extra:
        raise BlueprintValidationError(extra)
    return manifest


def dump_manifest(manifest: BlueprintManifest) -> dict[str, Any]:
    """Canonical JSON-ready dict (dates as ISO strings). Round-trips."""

    return manifest.model_dump(mode="json")
