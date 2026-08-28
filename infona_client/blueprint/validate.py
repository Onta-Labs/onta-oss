"""Semantic validation for a frozen v1 Blueprint manifest.

Structural unknown-key rejection is pydantic ``extra="forbid"``. This
module adds cross-field rules and reuses the existing ontology-catalog
classifiers — it does not read a graph.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from pydantic import ValidationError

from infona_client.blueprint.models import (
    FORBIDDEN_TOP_LEVEL_KEYS,
    MAX_SKILL_BODY_CHARS,
    MAX_SKILL_SUMMARY_CHARS,
    MAX_SKILL_TITLE_CHARS,
    REAL_NCT_RE,
    SAMPLE_MAX_BYTES,
    SLUG_RE,
    TYPE_NAME_RE,
    BlueprintManifest,
    BlueprintSkill,
    ConceptAttribute,
    parse_blueprint,
)
from infona_client.graph.iri import ENTITY_URI_PREFIX
from infona_client.graph.ontology_catalog_models import (
    VALID_CARDINALITIES,
    classify_attr_range,
    _validate_attr_leaf,
    _validate_type_leaf,
)
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.predicates import ATTR_META_SUFFIXES

#: Same shapes as ``tests/test_api_registry_byok_guard.py`` — a source
#: *definition* URL must not smuggle a credential (INF-564 / ADR 0014 §2.4).
_URL_USERINFO = re.compile(r"://[^/\s@]+:[^/\s@]+@")
_URL_CRED_PARAM = re.compile(
    r"[?&](?:api[_-]?key|apikey|token|access[_-]?token|secret|password|passwd|key)="
    r"[^&\s]+",
    re.IGNORECASE,
)

_FORBIDDEN_SAMPLE_LEAVES: frozenset[str] = (
    frozenset(ATTR_META_SUFFIXES)
    | FORBIDDEN_TOP_LEVEL_KEYS
    | frozenset(
        {
            "citation",
            "citations",
            "password",
            "token",
            "api_key",
            "access_token",
            "secret",
            "last_refresh",
            "freshness_status",
            "source_health",
        }
    )
)


def validate_blueprint(raw: Mapping[str, Any] | str | bytes | BlueprintManifest) -> list[str]:
    """Return every problem at once. Empty list = valid.

    Same contract as ``api_registry.spec.validate_spec`` / ``validate_skill``.
    """
    if isinstance(raw, BlueprintManifest):
        manifest = raw
        errors: list[str] = []
    else:
        try:
            manifest = parse_blueprint(raw)
            errors = []
        except ValidationError as exc:
            return [
                f"{_loc(err['loc'])}: {err['msg']}"
                for err in exc.errors()
            ]
        except (ValueError, json.JSONDecodeError) as exc:
            return [str(exc)]

    errors.extend(_semantic_errors(manifest))
    return errors


def _loc(loc: tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in loc) or "<root>"


def _semantic_errors(manifest: BlueprintManifest) -> list[str]:
    errors: list[str] = []
    concepts = {c.name: c for c in manifest.concepts}
    if len(concepts) != len(manifest.concepts):
        errors.append("concepts: duplicate concept name")

    attr_index: dict[str, ConceptAttribute] = {}
    for concept in manifest.concepts:
        errors.extend(_concept_errors(concept, concepts))
        for attr in concept.attributes:
            attr_index[f"{concept.name}.{attr.name}"] = attr

    rel_names: set[str] = set()
    for rel in manifest.relationships:
        if rel.name in rel_names:
            errors.append(f"relationships.{rel.name}: duplicate relationship name")
        rel_names.add(rel.name)
        errors.extend(_relationship_errors(rel, concepts, attr_index))

    source_ids = {s.id for s in manifest.sources}
    if len(source_ids) != len(manifest.sources):
        errors.append("sources: duplicate source id")

    for source in manifest.sources:
        errors.extend(_source_errors(source, attr_index, rel_names, source_ids))

    for i, instr in enumerate(manifest.acquisition):
        if instr.source not in source_ids:
            errors.append(
                f"acquisition[{i}].source: unknown source {instr.source!r}"
            )

    for rule in manifest.rules.conflict:
        if rule.winner not in source_ids:
            errors.append(
                f"rules.conflict: winner {rule.winner!r} is not a declared source"
            )

    task_ids = {t.id for t in manifest.tasks}
    if len(task_ids) != len(manifest.tasks):
        errors.append("tasks: duplicate task id")
    for task in manifest.tasks:
        if task.attaches_to and task.attaches_to not in concepts:
            errors.append(
                f"tasks.{task.id}.attaches_to: unknown concept {task.attaches_to!r}"
            )

    for skill in manifest.skills:
        errors.extend(_skill_errors(skill, concepts))

    for fn in manifest.functions:
        if fn.type_name not in concepts:
            errors.append(f"functions.{fn.name}: unknown type {fn.type_name!r}")
        try:
            _validate_type_leaf(fn.type_name)
        except ValueError as exc:
            errors.append(f"functions.{fn.name}.type_name: {exc}")

    for policy in manifest.freshness.policies:
        errors.extend(_freshness_target_errors(policy.target, source_ids, attr_index))

    er_types = [cfg.type_name for cfg in manifest.freshness.er]
    if len(set(er_types)) != len(er_types):
        errors.append("freshness.er: duplicate type_name")
    for cfg in manifest.freshness.er:
        errors.extend(_er_errors(cfg, concepts, attr_index))

    colliding = [c.name for c in manifest.concepts if c.name not in er_types]
    # INF-559: ER is required per type that can collide. Identity keys exist
    # on every concept, so every concept needs a config.
    for name in colliding:
        errors.append(
            f"freshness.er: concept {name!r} has no entity-resolution config"
        )

    if manifest.sample is not None:
        errors.extend(_sample_errors(manifest, attr_index))

    return errors


def _concept_errors(concept, concepts) -> list[str]:
    errors: list[str] = []
    try:
        _validate_type_leaf(concept.name)
    except ValueError as exc:
        errors.append(f"concepts.{concept.name}: {exc}")
    if concept.parent_type and concept.parent_type not in concepts:
        errors.append(
            f"concepts.{concept.name}.parent_type: unknown concept "
            f"{concept.parent_type!r}"
        )
    seen = set()
    for attr in concept.attributes:
        if attr.name in seen:
            errors.append(
                f"concepts.{concept.name}.attributes: duplicate {attr.name!r}"
            )
        seen.add(attr.name)
        errors.extend(_attribute_errors(concept.name, attr, concepts))
    by_name = {a.name: a for a in concept.attributes}
    identity_unknown = [key for key in concept.identity if key not in by_name]
    if identity_unknown:
        errors.append(
            f"concepts.{concept.name}.identity: unknown attributes "
            f"{identity_unknown}"
        )
    for key in concept.identity:
        slot = by_name.get(key)
        if slot is not None and slot.kind != "literal":
            errors.append(
                f"concepts.{concept.name}.identity: {key!r} must be a literal "
                "(identity keys are not type-ranged)"
            )
    return errors


def _attribute_errors(
    type_name: str, attr: ConceptAttribute, concepts: dict
) -> list[str]:
    errors: list[str] = []
    prefix = f"concepts.{type_name}.attributes.{attr.name}"
    try:
        _validate_attr_leaf(attr.name)
    except ValueError as exc:
        errors.append(f"{prefix}: {exc}")
    if attr.cardinality not in VALID_CARDINALITIES:
        errors.append(
            f"{prefix}.cardinality: must be one of {sorted(VALID_CARDINALITIES)}"
        )
    range_token = attr.datatype if attr.kind == "literal" else attr.range_type
    try:
        kind, datatype, range_type = classify_attr_range(range_token or "")
    except ValueError as exc:
        errors.append(f"{prefix}: {exc}")
        return errors
    if attr.kind == "relationship" and attr.range_type not in concepts:
        errors.append(
            f"{prefix}.range_type: unknown concept {attr.range_type!r}"
        )
    if kind != attr.kind:
        errors.append(
            f"{prefix}: classify_attr_range({range_token!r}) is {kind}, "
            f"not declared kind {attr.kind!r}"
        )
    if attr.kind == "literal" and datatype != attr.datatype:
        errors.append(
            f"{prefix}.datatype: catalog canonical is {datatype!r}, "
            f"got {attr.datatype!r}"
        )
    if attr.kind == "relationship" and range_type != attr.range_type:
        errors.append(
            f"{prefix}.range_type: catalog leaf is {range_type!r}, "
            f"got {attr.range_type!r}"
        )
    return errors


def _relationship_errors(rel, concepts, attr_index) -> list[str]:
    errors: list[str] = []
    prefix = f"relationships.{rel.name}"
    if rel.cardinality not in VALID_CARDINALITIES:
        errors.append(
            f"{prefix}.cardinality: must be one of {sorted(VALID_CARDINALITIES)}"
        )
    if rel.source not in concepts:
        errors.append(f"{prefix}.source: unknown concept {rel.source!r}")
    if rel.target not in concepts:
        errors.append(f"{prefix}.target: unknown concept {rel.target!r}")
    slot = attr_index.get(f"{rel.source}.{rel.name}")
    if slot is None:
        errors.append(
            f"{prefix}: source concept {rel.source!r} must declare a "
            f"type-ranged attribute named {rel.name!r}"
        )
        return errors
    if slot.kind != "relationship":
        errors.append(
            f"{prefix}: {rel.source}.{rel.name} is {slot.kind}, not relationship"
        )
    if slot.range_type != rel.target:
        errors.append(
            f"{prefix}: attribute range_type {slot.range_type!r} != "
            f"relationship target {rel.target!r}"
        )
    if slot.cardinality != rel.cardinality:
        errors.append(
            f"{prefix}: cardinality {rel.cardinality!r} != attribute "
            f"{slot.cardinality!r}"
        )
    return errors


def _url_embeds_credentials(url: str) -> bool:
    if _URL_USERINFO.search(url) or _URL_CRED_PARAM.search(url):
        return True
    parsed = urlparse(url)
    return bool(parsed.username or parsed.password)


def _source_errors(source, attr_index, rel_names, _source_ids) -> list[str]:
    errors: list[str] = []
    prefix = f"sources.{source.id}"
    if _url_embeds_credentials(source.url):
        errors.append(
            f"{prefix}.url: definition URLs must not embed credentials "
            "(userinfo or key/token query params)"
        )
    for i, mapping in enumerate(source.mappings):
        lands = mapping.lands_on
        slot = attr_index.get(lands)
        if slot is None and lands not in rel_names:
            errors.append(
                f"{prefix}.mappings[{i}].lands_on: {lands!r} is not a "
                "Type.attribute or relationship name"
            )
            continue
        expected_kind = slot.kind if slot is not None else "relationship"
        if mapping.kind != expected_kind:
            errors.append(
                f"{prefix}.mappings[{i}].kind: {mapping.kind!r} does not "
                f"match {lands} ({expected_kind})"
            )
    return errors


def _skill_errors(skill: BlueprintSkill, concepts) -> list[str]:
    errors: list[str] = []
    prefix = f"skills.{skill.slug}"
    if skill.type_name not in concepts:
        errors.append(f"{prefix}.type_name: unknown concept {skill.type_name!r}")
    if not SLUG_RE.match(skill.slug):
        errors.append(f"{prefix}.slug: must match skill SLUG_RE")
    if not TYPE_NAME_RE.match(skill.type_name):
        errors.append(f"{prefix}.type_name: must match skill TYPE_NAME_RE")
    if not (skill.body or "").strip():
        errors.append(f"{prefix}.body: must not be empty")
    elif len(skill.body) > MAX_SKILL_BODY_CHARS:
        errors.append(f"{prefix}.body: exceeds {MAX_SKILL_BODY_CHARS} chars")
    if len(skill.title) > MAX_SKILL_TITLE_CHARS:
        errors.append(f"{prefix}.title: exceeds {MAX_SKILL_TITLE_CHARS} chars")
    if len(skill.summary) > MAX_SKILL_SUMMARY_CHARS:
        errors.append(f"{prefix}.summary: exceeds {MAX_SKILL_SUMMARY_CHARS} chars")
    return errors


def _freshness_target_errors(
    target: str, source_ids: set[str], attr_index: dict[str, ConceptAttribute]
) -> list[str]:
    if target in source_ids or target in attr_index:
        return []
    return [
        f"freshness.policies: target {target!r} is not a source id or Type.attribute"
    ]


def _er_errors(cfg, concepts, attr_index) -> list[str]:
    errors: list[str] = []
    prefix = f"freshness.er.{cfg.type_name}"
    concept = concepts.get(cfg.type_name)
    if concept is None:
        errors.append(f"{prefix}: unknown concept")
        return errors
    attr_names = {a.name for a in concept.attributes}
    for key in (*cfg.identity, *cfg.blocking, *cfg.signals):
        if key not in attr_names:
            errors.append(f"{prefix}: {key!r} is not an attribute of {cfg.type_name}")
    return errors


def sample_subject_iri(concept, entity) -> str | None:
    """Mint the shared entity IRI from the concept identity keys.

    Same join ``sample_subject`` uses: ``entity_uri(type, "_".join(parts))``.
    ``None`` if an identity key is missing.
    """
    parts: list[str] = []
    for key in concept.identity:
        value = entity.attributes.get(key)
        if value is None:
            return None
        parts.append(str(value))
    if not parts:
        return None
    return entity_uri(entity.type, "_".join(parts))


def _sample_entity_iris(manifest: BlueprintManifest, type_name: str) -> set[str]:
    """Minted sample subject IRIs only — not identity parts, not join strings."""
    concept = next((c for c in manifest.concepts if c.name == type_name), None)
    sample = manifest.sample
    if concept is None or sample is None:
        return set()
    iris: set[str] = set()
    for entity in sample.entities:
        if entity.type != type_name:
            continue
        iri = sample_subject_iri(concept, entity)
        if iri:
            iris.add(iri)
    return iris


def resolve_sample_rel_target(
    manifest: BlueprintManifest, type_name: str, raw: str
) -> str | None:
    """Return the minted sample subject IRI, or ``None`` if ``raw`` is not one.

    Accepts that IRI, or the identity-join string that mints it. Never an
    identity *part*. Used by the validator and ``facts_for_sample``.
    """
    if not type_name or not isinstance(raw, str) or not raw.strip():
        return None
    token = raw.strip()
    iris = _sample_entity_iris(manifest, type_name)
    if token.startswith(ENTITY_URI_PREFIX):
        return token if token in iris else None
    minted = entity_uri(type_name, token)
    return minted if minted in iris else None


def _sample_errors(manifest: BlueprintManifest, attr_index) -> list[str]:
    errors: list[str] = []
    sample = manifest.sample
    assert sample is not None
    dumped = json.dumps(sample.model_dump(mode="json"), allow_nan=False)
    if len(dumped.encode("utf-8")) > SAMPLE_MAX_BYTES:
        errors.append(
            f"sample: serialized size exceeds {SAMPLE_MAX_BYTES} bytes"
        )
    concepts = {c.name for c in manifest.concepts}
    for i, entity in enumerate(sample.entities):
        prefix = f"sample.entities[{i}]"
        if entity.type not in concepts:
            errors.append(f"{prefix}.type: unknown concept {entity.type!r}")
        for leaf, value in entity.attributes.items():
            if leaf in _FORBIDDEN_SAMPLE_LEAVES:
                errors.append(
                    f"{prefix}.attributes.{leaf}: workspace-only leaf "
                    "(citation / provenance / freshness status / credential)"
                )
            slot = attr_index.get(f"{entity.type}.{leaf}")
            if slot is None:
                errors.append(
                    f"{prefix}.attributes.{leaf}: not declared on {entity.type}"
                )
            elif slot.kind == "relationship":
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"{prefix}.attributes.{leaf}: relationship sample "
                        "value must be a non-empty string"
                    )
                elif resolve_sample_rel_target(
                    manifest, slot.range_type or "", value
                ) is None:
                    errors.append(
                        f"{prefix}.attributes.{leaf}: does not resolve to "
                        f"a sample {slot.range_type}"
                    )
            elif slot.kind != "literal":
                errors.append(
                    f"{prefix}.attributes.{leaf}: sample values must be "
                    f"literal or relationship, not {slot.kind}"
                )
            if (
                sample.kind == "synthetic"
                and isinstance(value, str)
                and REAL_NCT_RE.match(value)
            ):
                errors.append(
                    f"{prefix}.attributes.{leaf}: synthetic samples must not "
                    "use a real NCT######## identifier"
                )
    return errors


__all__ = [
    "resolve_sample_rel_target",
    "sample_subject_iri",
    "validate_blueprint",
]
