"""Tenant-local Blueprint overlay (INF-578).

A workspace keeps the public pin (e.g. ``infona/clinical-trials``) and
adds a private delta: types, attributes, source overrides, tighter
rules, skills. The delta is not a new package identity — that is fork
(INF-579). Overlay rows never enter the catalogued public document, so
they cannot flow upstream or to another tenant (INF-580).

The store is process-local in-memory (lost on restart and across ECS
tasks). The install lock is not — it lives on the tenant GraphStore.

Boundary: OSS protocol. ``infona_client.*`` / stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from infona_client.blueprint.models import (
    BlueprintSkill,
    ConceptAttribute,
    ConflictRule,
    SourceMapping,
    TombstoneRule,
)
from infona_client.blueprint.plan import BlueprintError
from infona_client.models.ontology import OntologyMutation, OntologyOpKind

_Strict = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OverlayConcept(BaseModel):
    """New type, or extra attributes on a base type. Not a full package."""

    model_config = _Strict
    name: str = Field(min_length=1, max_length=200)
    label: str = ""
    description: str = ""
    identity: list[str] = Field(default_factory=list)
    parent_type: str | None = None
    attributes: list[ConceptAttribute] = Field(default_factory=list)


class OverlaySourceOverride(BaseModel):
    """Partial source rebind. Only set fields override the public base."""

    model_config = _Strict
    id: str = Field(min_length=1)
    title: str | None = None
    url: str | None = None
    declared_cadence: str | None = None
    description: str | None = None
    mappings: list[SourceMapping] | None = None


class OverlayRuleTighten(BaseModel):
    """Stricter maintenance rules. Absent fields stay on the public base."""

    model_config = _Strict
    conflict: list[ConflictRule] | None = None
    tombstones: TombstoneRule | None = None


class OverlayDocument(BaseModel):
    """Tenant-local delta against one installed pin. Not a Blueprint package."""

    model_config = _Strict
    concepts: list[OverlayConcept] = Field(default_factory=list)
    skills: list[BlueprintSkill] = Field(default_factory=list)
    sources: list[OverlaySourceOverride] = Field(default_factory=list)
    rules: OverlayRuleTighten | None = None


class BlueprintOverlayError(BlueprintError):
    status_code = 400


class BlueprintIdMismatch(BlueprintError):
    status_code = 400


@dataclass
class OverlayConflict:
    """Recorded for INF-595. This ticket does not render a UI."""

    kind: str
    path: str
    message: str
    base_from: Any = None
    base_to: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "message": self.message,
            "base_from": self.base_from,
            "base_to": self.base_to,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OverlayConflict":
        return cls(
            kind=str(raw["kind"]),
            path=str(raw["path"]),
            message=str(raw.get("message") or ""),
            base_from=raw.get("base_from"),
            base_to=raw.get("base_to"),
        )


@dataclass
class StoredOverlay:
    """One tenant's private layer on one installed pin."""

    tenant_id: str
    blueprint_id: str
    document: OverlayDocument
    conflicts: list[OverlayConflict] = field(default_factory=list)
    updated_at: str = ""
    base_version: str = ""
    base_content_hash: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "concepts": [c.model_dump(mode="json") for c in self.document.concepts],
            "skills": [s.model_dump(mode="json") for s in self.document.skills],
            "sources": [s.model_dump(mode="json") for s in self.document.sources],
            "rules": (
                self.document.rules.model_dump(mode="json")
                if self.document.rules is not None
                else None
            ),
            "base_version": self.base_version,
            "base_content_hash": self.base_content_hash,
            "updated_at": self.updated_at,
        }


class BlueprintOverlayStore(Protocol):
    async def get(self, tenant_id: str, blueprint_id: str) -> Optional[StoredOverlay]: ...

    async def put(self, row: StoredOverlay) -> StoredOverlay: ...

    async def delete(self, tenant_id: str, blueprint_id: str) -> bool: ...


class InMemoryBlueprintOverlayStore:
    """Tenant-confined. No cross-tenant read."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], StoredOverlay] = {}

    async def get(self, tenant_id: str, blueprint_id: str) -> Optional[StoredOverlay]:
        return self._rows.get((tenant_id, blueprint_id))

    async def put(self, row: StoredOverlay) -> StoredOverlay:
        self._rows[(row.tenant_id, row.blueprint_id)] = row
        return row

    async def delete(self, tenant_id: str, blueprint_id: str) -> bool:
        return self._rows.pop((tenant_id, blueprint_id), None) is not None


_store: Optional[BlueprintOverlayStore] = None


def make_blueprint_overlay_store() -> BlueprintOverlayStore:
    global _store
    if _store is None:
        _store = InMemoryBlueprintOverlayStore()
    return _store


def reset_blueprint_overlay_store() -> None:
    global _store
    _store = None


def parse_overlay(raw: dict[str, Any] | OverlayDocument) -> OverlayDocument:
    if isinstance(raw, OverlayDocument):
        return raw
    return OverlayDocument.model_validate(raw)


def merge_overlay(base: OverlayDocument, incoming: OverlayDocument) -> OverlayDocument:
    """Accumulate extends. Same name/id last-write-wins per field."""
    concepts = {c.name: c for c in base.concepts}
    for item in incoming.concepts:
        prev = concepts.get(item.name)
        if prev is None:
            concepts[item.name] = item
            continue
        attrs = {a.name: a for a in prev.attributes}
        attrs.update({a.name: a for a in item.attributes})
        concepts[item.name] = prev.model_copy(
            update={
                "label": item.label or prev.label,
                "description": item.description or prev.description,
                "identity": item.identity or prev.identity,
                "parent_type": item.parent_type or prev.parent_type,
                "attributes": list(attrs.values()),
            }
        )
    skills = {(s.type_name, s.slug): s for s in base.skills}
    skills.update({(s.type_name, s.slug): s for s in incoming.skills})
    sources = {s.id: s for s in base.sources}
    for item in incoming.sources:
        prev = sources.get(item.id)
        if prev is None:
            sources[item.id] = item
            continue
        patch = item.model_dump(exclude_none=True)
        sources[item.id] = prev.model_copy(update=patch)
    rules = incoming.rules if incoming.rules is not None else base.rules
    return OverlayDocument(
        concepts=list(concepts.values()),
        skills=list(skills.values()),
        sources=list(sources.values()),
        rules=rules,
    )


def _attr_map(concept: Any) -> dict[str, ConceptAttribute]:
    return {a.name: a for a in getattr(concept, "attributes", [])}


def _attr_conflict(old: ConceptAttribute, new: ConceptAttribute) -> str | None:
    if old.kind != new.kind:
        return (
            "literal_to_type_ranged"
            if new.kind == "relationship"
            else "type_ranged_to_literal"
        )
    if old.kind == "relationship" and old.range_type != new.range_type:
        return "narrowed_range"
    if old.kind == "literal" and old.datatype != new.datatype:
        return "narrowed_range"
    if old.optional and not new.optional:
        return "make_optional_required"
    return None


def _source_override_fields(override: OverlaySourceOverride) -> set[str]:
    return {k for k, v in override.model_dump(exclude_none=True).items() if k != "id"}


def detect_conflicts(
    old_base: Any,
    new_base: Any,
    overlay: OverlayDocument,
) -> list[OverlayConflict]:
    """3-way: old public pin, new public pin, tenant overlay."""
    old_c = {c.name: c for c in old_base.concepts}
    new_c = {c.name: c for c in new_base.concepts}
    old_s = {s.id: s for s in old_base.sources}
    new_s = {s.id: s for s in new_base.sources}
    old_skills = {(s.type_name, s.slug) for s in old_base.skills}
    new_skills = {(s.type_name, s.slug) for s in new_base.skills}
    out: list[OverlayConflict] = []

    for item in overlay.concepts:
        if item.name in old_c:
            if item.name not in new_c:
                out.append(
                    OverlayConflict(
                        kind="removed_extended_type",
                        path=f"concepts.{item.name}",
                        message=(
                            f"upstream removed {item.name}, which this "
                            "workspace extended"
                        ),
                        base_from=item.name,
                        base_to=None,
                    )
                )
                continue
            old_attrs, new_attrs = _attr_map(old_c[item.name]), _attr_map(new_c[item.name])
            for name, prev in old_attrs.items():
                if name not in new_attrs:
                    out.append(
                        OverlayConflict(
                            kind="removed_attribute",
                            path=f"concepts.{item.name}.attributes.{name}",
                            message=f"upstream removed {item.name}.{name}",
                            base_from=name,
                            base_to=None,
                        )
                    )
                    continue
                kind = _attr_conflict(prev, new_attrs[name])
                if kind:
                    out.append(
                        OverlayConflict(
                            kind=kind,
                            path=f"concepts.{item.name}.attributes.{name}",
                            message=f"upstream {kind.replace('_', ' ')} on {item.name}.{name}",
                            base_from=prev.model_dump(mode="json"),
                            base_to=new_attrs[name].model_dump(mode="json"),
                        )
                    )
            for attr in item.attributes:
                if attr.name in new_attrs and attr.name not in old_attrs:
                    out.append(
                        OverlayConflict(
                            kind="attribute_collision",
                            path=f"concepts.{item.name}.attributes.{attr.name}",
                            message=f"upstream and overlay both added {item.name}.{attr.name}",
                            base_from=None,
                            base_to=new_attrs[attr.name].model_dump(mode="json"),
                        )
                    )
        elif item.name in new_c:
            out.append(
                OverlayConflict(
                    kind="type_collision",
                    path=f"concepts.{item.name}",
                    message=f"upstream and overlay both added type {item.name}",
                    base_from=None,
                    base_to=item.name,
                )
            )

    for override in overlay.sources:
        if override.id in old_s and override.id not in new_s:
            out.append(
                OverlayConflict(
                    kind="removed_overridden_source",
                    path=f"sources.{override.id}",
                    message=f"upstream removed source {override.id} that this workspace rebound",
                    base_from=override.id,
                    base_to=None,
                )
            )
            continue
        if override.id not in old_s or override.id not in new_s:
            continue
        prev, nxt = old_s[override.id], new_s[override.id]
        tenant_fields = _source_override_fields(override)
        changed = {
            name
            for name in tenant_fields
            if getattr(prev, name, None) != getattr(nxt, name, None)
        }
        if changed:
            out.append(
                OverlayConflict(
                    kind="source_changed",
                    path=f"sources.{override.id}",
                    message=(
                        f"upstream changed {sorted(changed)} on source "
                        f"{override.id}, which this workspace also overrode"
                    ),
                    base_from={n: getattr(prev, n, None) for n in changed},
                    base_to={n: getattr(nxt, n, None) for n in changed},
                )
            )

    for skill in overlay.skills:
        key = (skill.type_name, skill.slug)
        if key in new_skills and key not in old_skills:
            out.append(
                OverlayConflict(
                    kind="skill_collision",
                    path=f"skills.{skill.type_name}/{skill.slug}",
                    message=f"upstream and overlay both added skill {skill.slug}",
                    base_from=None,
                    base_to=skill.slug,
                )
            )

    if overlay.rules is not None and old_base.rules != new_base.rules:
        out.append(
            OverlayConflict(
                kind="rules_changed",
                path="rules",
                message="upstream changed maintenance rules this workspace tightened",
                base_from=old_base.rules.model_dump(mode="json"),
                base_to=new_base.rules.model_dump(mode="json"),
            )
        )
    return out


def mutations_from_overlay(
    overlay: OverlayDocument, *, base_type_names: set[str]
) -> list[OntologyMutation]:
    """Tenant-layer schema writes for the private delta (ADR 0002)."""
    out: list[OntologyMutation] = []
    for concept in overlay.concepts:
        if concept.name not in base_type_names:
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


def validate_overlay(overlay: OverlayDocument, *, base_type_names: set[str]) -> None:
    for concept in overlay.concepts:
        if concept.name in base_type_names:
            if not concept.attributes:
                raise BlueprintOverlayError(
                    f"overlay extension of {concept.name} must add attributes"
                )
            continue
        # A type that left the public base is still an extension if the
        # overlay never declared identity — keep it on the tenant layer.
        if not concept.label and not concept.identity:
            if not concept.attributes:
                raise BlueprintOverlayError(
                    f"overlay extension of {concept.name} must add attributes"
                )
            continue
        if not concept.label or not concept.identity or not concept.attributes:
            raise BlueprintOverlayError(
                f"new overlay type {concept.name} needs label, identity, and attributes"
            )


__all__ = [
    "BlueprintIdMismatch",
    "BlueprintOverlayError",
    "BlueprintOverlayStore",
    "InMemoryBlueprintOverlayStore",
    "OverlayConcept",
    "OverlayConflict",
    "OverlayDocument",
    "OverlayRuleTighten",
    "OverlaySourceOverride",
    "StoredOverlay",
    "detect_conflicts",
    "make_blueprint_overlay_store",
    "merge_overlay",
    "mutations_from_overlay",
    "parse_overlay",
    "reset_blueprint_overlay_store",
    "validate_overlay",
]
