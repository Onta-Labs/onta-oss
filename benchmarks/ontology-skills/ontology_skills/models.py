"""Skill, type, relation, and neighborhood types for capability compilation."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Mapping

SkillKind = Literal["type", "relation"]
CompileMode = Literal["none", "flat", "routed", "rag"]


class OntologyError(ValueError):
    """Malformed ontology or neighborhood (unknown id, cycle, kind mismatch)."""


@dataclass(frozen=True, slots=True)
class Skill:
    """Procedural skill attached to one type or one relation.

    ``skill_id`` is the shadowing key: a more-specific attachment with the same
    id replaces an ancestor. ``provenance`` is recorded, never used as a
    retrieval rank — compilation is a deterministic walk, not search.
    """

    skill_id: str
    title: str
    body: str
    attached_to: str
    kind: SkillKind
    provenance: str = "curated"
    enabled: bool = True
    version: int = 1

    def __post_init__(self) -> None:
        if not self.skill_id or not self.skill_id.strip():
            raise OntologyError("skill_id must be non-empty")
        if self.kind not in ("type", "relation"):
            raise OntologyError(f"unknown skill kind {self.kind!r}")
        if not (self.body or "").strip():
            raise OntologyError(f"skill {self.skill_id!r} body must be non-empty")


@dataclass(frozen=True, slots=True)
class EntityType:
    type_id: str
    label: str
    parent_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.type_id:
            raise OntologyError("type_id must be non-empty")


@dataclass(frozen=True, slots=True)
class Relation:
    relation_id: str
    label: str
    source_type: str
    target_type: str
    parent_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.relation_id:
            raise OntologyError("relation_id must be non-empty")


@dataclass(frozen=True, slots=True)
class Neighborhood:
    """Local ontology neighborhood fed to the compiler.

    Seed order is significant: it is the tie-break for lineage. Two neighborhoods
    with the same ids in different order are different inputs and may legally
    differ in lineage order; the *set* of compiled skill ids stays the same.
    """

    type_ids: tuple[str, ...] = ()
    relation_ids: tuple[str, ...] = ()
    include_ancestors: bool = True
    include_incident_relations: bool = True


@dataclass(frozen=True, slots=True)
class CompiledSkillSet:
    """Deterministic compiler output. Equality is the eval's identity test."""

    mode: CompileMode
    skills: tuple[Skill, ...]
    type_lineage: tuple[str, ...]
    relation_ids: tuple[str, ...]
    suppressed_skill_ids: tuple[str, ...] = ()

    @property
    def skill_ids(self) -> tuple[str, ...]:
        return tuple(s.skill_id for s in self.skills)

    def fingerprint(self) -> str:
        payload = "|".join(
            (
                self.mode,
                ",".join(self.skill_ids),
                ",".join(self.type_lineage),
                ",".join(self.relation_ids),
                ",".join(self.suppressed_skill_ids),
            )
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "skill_ids": list(self.skill_ids),
            "type_lineage": list(self.type_lineage),
            "relation_ids": list(self.relation_ids),
            "suppressed_skill_ids": list(self.suppressed_skill_ids),
            "fingerprint": self.fingerprint(),
        }


@dataclass(frozen=True)
class Ontology:
    """Closed world of types, relations, and attached skills."""

    types: Mapping[str, EntityType]
    relations: Mapping[str, Relation]
    skills: tuple[Skill, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "types", dict(self.types))
        object.__setattr__(self, "relations", dict(self.relations))
        _validate_ontology(self)

    def skills_for(self, attached_to: str) -> tuple[Skill, ...]:
        matched = [s for s in self.skills if s.attached_to == attached_to]
        matched.sort(key=lambda s: (s.skill_id, s.version))
        return tuple(matched)


def _validate_ontology(onto: Ontology) -> None:
    type_ids = set(onto.types)
    rel_ids = set(onto.relations)
    if type_ids & rel_ids:
        clash = sorted(type_ids & rel_ids)
        raise OntologyError(f"type/relation id collision: {clash}")
    for typ in onto.types.values():
        for parent in typ.parent_ids:
            if parent not in onto.types:
                raise OntologyError(
                    f"type {typ.type_id!r} parent {parent!r} is not a type"
                )
    for rel in onto.relations.values():
        if rel.source_type not in onto.types:
            raise OntologyError(
                f"relation {rel.relation_id!r} source {rel.source_type!r} "
                "is not a type"
            )
        if rel.target_type not in onto.types:
            raise OntologyError(
                f"relation {rel.relation_id!r} target {rel.target_type!r} "
                "is not a type"
            )
        for parent in rel.parent_ids:
            if parent not in onto.relations:
                raise OntologyError(
                    f"relation {rel.relation_id!r} parent {parent!r} "
                    "is not a relation"
                )
    for skill in onto.skills:
        if skill.kind == "type" and skill.attached_to not in onto.types:
            raise OntologyError(
                f"skill {skill.skill_id!r} attached to unknown type "
                f"{skill.attached_to!r}"
            )
        if skill.kind == "relation" and skill.attached_to not in onto.relations:
            raise OntologyError(
                f"skill {skill.skill_id!r} attached to unknown relation "
                f"{skill.attached_to!r}"
            )
        if skill.kind == "type" and skill.attached_to in onto.relations:
            raise OntologyError(
                f"skill {skill.skill_id!r} kind=type but attached_to is a relation"
            )
        if skill.kind == "relation" and skill.attached_to in onto.types:
            raise OntologyError(
                f"skill {skill.skill_id!r} kind=relation but attached_to is a type"
            )
    _assert_unique_attachment_skill_ids(onto)
    _assert_acyclic(onto.types, "type")
    _assert_acyclic(onto.relations, "relation")


def _assert_unique_attachment_skill_ids(onto: Ontology) -> None:
    seen: set[tuple[str, str]] = set()
    for skill in onto.skills:
        key = (skill.attached_to, skill.skill_id)
        if key in seen:
            raise OntologyError(
                f"duplicate skill_id {skill.skill_id!r} on {skill.attached_to!r}"
            )
        seen.add(key)


def _assert_acyclic(nodes: Mapping[str, EntityType | Relation], kind: str) -> None:
    visiting: set[str] = set()
    done: set[str] = set()

    def walk(nid: str) -> None:
        if nid in done:
            return
        if nid in visiting:
            raise OntologyError(f"{kind} inheritance cycle at {nid!r}")
        visiting.add(nid)
        for parent in nodes[nid].parent_ids:
            walk(parent)
        visiting.remove(nid)
        done.add(nid)

    for nid in nodes:
        walk(nid)
