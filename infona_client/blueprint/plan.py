"""Install planning helpers — hash, validate, ontology mutations, sample facts.

Kept beside :mod:`install` so that module stays under the file-size cap.
Does not write a graph.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from infona_client.blueprint.load import load_blueprint_package, validate_blueprint_package
from infona_client.blueprint.models import (
    BlueprintManifest,
    Concept,
    SampleEntity,
    dumps_blueprint,
    parse_blueprint,
)
from infona_client.blueprint.validate import validate_blueprint
from infona_client.graph.facts import Fact
from infona_client.graph.iri import ONTO_PRED_PREFIX
from infona_client.graph.ontology_queries import entity_uri
from infona_client.models.ontology import OntologyMutation, OntologyOpKind

SAMPLE_SOURCE_MARK = "blueprint-sample"


def instance_edge_predicate(leaf: str) -> str:
    """Relationship INSTANCE edge — ``onto/<leaf>``, never ``attrs/<leaf>``.

    ADR 0009 / INF-576. ``insert_facts`` with ``Fact.kind="rel"`` is what
    lands the edge on this predicate. A ``kind="literal"`` write of the
    same leaf is a string on the attribute namespace — invisible to NL.
    """
    if not isinstance(leaf, str) or not leaf.strip():
        raise BlueprintValidationError("relationship leaf must be a non-empty string")
    return f"{ONTO_PRED_PREFIX}{leaf.strip()}"


class BlueprintError(Exception):
    """Install/inspect/uninstall failure with an HTTP status."""

    status_code = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class BlueprintValidationError(BlueprintError):
    status_code = 400


class BlueprintNotInstalled(BlueprintError):
    status_code = 404


class BlueprintUninstallRefused(BlueprintError):
    status_code = 409


class BlueprintNotFound(BlueprintError):
    status_code = 404


class BlueprintForkConflict(BlueprintError):
    status_code = 409


def manifest_content_hash(manifest: BlueprintManifest) -> str:
    """Pin the v1 document (ADR 0014 F5). Canonical JSON, not archive bytes."""
    payload = dumps_blueprint(manifest).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parse_document(text: str) -> BlueprintManifest:
    try:
        return parse_blueprint(text)
    except Exception as exc:  # noqa: BLE001 — document parse → 400
        raise BlueprintValidationError(
            "blueprint package is invalid",
            details={"errors": [str(exc)]},
        ) from exc


def _load_path(path: Path) -> BlueprintManifest:
    errors = validate_blueprint_package(path)
    if errors:
        raise BlueprintValidationError(
            "blueprint package is invalid",
            details={"errors": errors},
        )
    return load_blueprint_package(path)


def load_and_validate(
    source: str | Path | Mapping[str, Any] | BlueprintManifest,
) -> BlueprintManifest:
    """Load a package directory, document, or already-parsed manifest and validate.

    CLI / MCP POST the YAML (or JSON) document as a string. Only an
    existing short path — no newlines — is treated as a filesystem root.
    """
    if isinstance(source, BlueprintManifest):
        manifest = source
    elif isinstance(source, Mapping):
        manifest = parse_blueprint(source)
    elif isinstance(source, Path):
        manifest = _load_path(source)
    else:
        text = source if isinstance(source, str) else str(source)
        path = Path(text)
        looks_like_path = (
            "\n" not in text
            and "\r" not in text
            and len(text) < 1024
            and path.exists()
        )
        manifest = _load_path(path) if looks_like_path else _parse_document(text)
    errors = validate_blueprint(manifest)
    if errors:
        raise BlueprintValidationError(
            "blueprint package is invalid",
            details={"errors": errors},
        )
    return manifest


def mutations_from_manifest(manifest: BlueprintManifest) -> list[OntologyMutation]:
    """Map concepts onto the sanctioned ontology commit ops."""
    out: list[OntologyMutation] = []
    for concept in manifest.concepts:
        out.append(
            OntologyMutation(
                op=OntologyOpKind.UPSERT_TYPE,
                type_name=concept.name,
                description=concept.description or concept.label,
            )
        )
        if concept.parent_type:
            out.append(
                OntologyMutation(
                    op=OntologyOpKind.SET_SUBCLASS,
                    type_name=concept.name,
                    parent_type=concept.parent_type,
                )
            )
        for attr in concept.attributes:
            if attr.kind == "literal":
                out.append(
                    OntologyMutation(
                        op=OntologyOpKind.UPSERT_ATTRIBUTE,
                        type_name=concept.name,
                        slot_name=attr.name,
                        datatype=attr.datatype or "string",
                        description=attr.description or "",
                    )
                )
            else:
                out.append(
                    OntologyMutation(
                        op=OntologyOpKind.UPSERT_RELATIONSHIP,
                        type_name=concept.name,
                        slot_name=attr.name,
                        target_type=attr.range_type,
                        description=attr.description or "",
                    )
                )
    return out


def sample_subject(concept: Concept, entity: SampleEntity) -> str:
    """Mint the shared entity IRI from the concept identity keys."""
    parts: list[str] = []
    for key in concept.identity:
        value = entity.attributes.get(key)
        if value is None:
            raise BlueprintValidationError(
                f"sample {entity.type} is missing identity key {key}"
            )
        parts.append(str(value))
    return entity_uri(entity.type, "_".join(parts))


def sample_source(manifest: BlueprintManifest) -> str:
    return f"blueprint:{manifest.id}@{manifest.version}#sample"


def facts_for_sample(manifest: BlueprintManifest) -> tuple[list[Fact], list[str]]:
    if manifest.sample is None:
        return [], []
    by_type = {c.name: c for c in manifest.concepts}
    facts: list[Fact] = []
    subjects: list[str] = []
    source = sample_source(manifest)
    for entity in manifest.sample.entities:
        concept = by_type.get(entity.type)
        if concept is None:
            raise BlueprintValidationError(
                f"sample entity type {entity.type!r} is not in concepts"
            )
        subject = sample_subject(concept, entity)
        subjects.append(subject)
        facts.append(Fact(subject_id=subject, kind="type", key=entity.type, source=source))
        attrs = {a.name: a for a in concept.attributes}
        for key, value in entity.attributes.items():
            spec = attrs.get(key)
            if spec is not None and spec.kind == "relationship":
                # INF-576: kind=rel → insert_facts lands
                # instance_edge_predicate(key) == onto/<leaf>.
                # kind=literal of the same slot is attrs/<leaf> and
                # invisible to the NL planner (ADR 0009).
                target_type = spec.range_type or ""
                raw = str(value)
                target = (
                    raw
                    if raw.startswith("https://graph.infona.ai/entities/")
                    else entity_uri(target_type, raw)
                )
                facts.append(
                    Fact(
                        subject_id=subject,
                        kind="rel",
                        key=key,
                        value=target,
                        source=source,
                        provenance=SAMPLE_SOURCE_MARK,
                    )
                )
                continue
            facts.append(
                Fact(
                    subject_id=subject,
                    kind="literal",
                    key=key,
                    value=value,
                    source=source,
                    provenance=SAMPLE_SOURCE_MARK,
                )
            )
    return facts, subjects
