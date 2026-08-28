"""Semver rules for a v1-frozen Blueprint (INF-563, INF-560 C4).

``version`` answers one question: does this break an installed consumer's
schema? Acquisition and freshness *instruction* changes are a different
signal — ``acquisition_revision`` — so a workspace can refuse a silent
source swap. ``latest`` is a listing decoration, never what install writes.

Removing a concept or narrowing an attribute range is breaking. Adding an
optional attribute is not.
"""

from __future__ import annotations

from enum import Enum

from infona_client.blueprints.schema import (
    Attribute,
    BlueprintManifest,
    Concept,
    Relationship,
)


class VersionBump(str, Enum):
    major = "major"
    minor = "minor"
    patch = "patch"


class ChangeReport:
    """The strongest schema bump plus whether acquisition_revision must move."""

    def __init__(
        self,
        version_bump: VersionBump,
        acquisition_revision_bump: bool,
        reasons: list[str],
    ) -> None:
        self.version_bump = version_bump
        self.acquisition_revision_bump = acquisition_revision_bump
        self.reasons = reasons

    def __repr__(self) -> str:
        return (
            f"ChangeReport({self.version_bump.value}, "
            f"acquisition_revision_bump={self.acquisition_revision_bump}, "
            f"reasons={self.reasons!r})"
        )


def _concepts_by_name(manifest: BlueprintManifest) -> dict[str, Concept]:
    return {c.name: c for c in manifest.concepts}


def _attrs_by_name(concept: Concept) -> dict[str, Attribute]:
    return {a.name: a for a in concept.attributes}


def _rel_key(rel: Relationship) -> tuple[str, str, str]:
    return (rel.name, rel.source, rel.target)


def _range_narrowed(old: Attribute, new: Attribute) -> bool:
    if old.kind != new.kind:
        return True
    if old.range != new.range:
        return True
    old_vals = set(old.allowed_values or [])
    new_vals = set(new.allowed_values or [])
    if old_vals and new_vals and not old_vals <= new_vals and new_vals < old_vals:
        return True
    if old_vals and new_vals and new_vals < old_vals:
        return True
    if old_vals and new_vals == set() and old.allowed_values is not None:
        # Dropping an enum widens, not narrows.
        return False
    if (not old_vals) and new_vals:
        return True
    return False


def classify_change(
    old: BlueprintManifest, new: BlueprintManifest
) -> ChangeReport:
    """Compare two valid manifests and return the consumer-break signal.

    Strength order for ``version``: major > minor > patch. An acquisition
    instruction change never becomes a silent MINOR; it sets
    ``acquisition_revision_bump`` instead.
    """

    reasons: list[str] = []
    bump = VersionBump.patch
    acq_bump = False

    def raise_to(next_bump: VersionBump, reason: str) -> None:
        nonlocal bump
        reasons.append(reason)
        order = {VersionBump.patch: 0, VersionBump.minor: 1, VersionBump.major: 2}
        if order[next_bump] > order[bump]:
            bump = next_bump

    old_concepts = _concepts_by_name(old)
    new_concepts = _concepts_by_name(new)

    for name in old_concepts:
        if name not in new_concepts:
            raise_to(VersionBump.major, f"removed concept {name}")

    for name, new_c in new_concepts.items():
        if name not in old_concepts:
            raise_to(VersionBump.minor, f"added concept {name}")
            continue
        old_c = old_concepts[name]
        if old_c.identity_keys != new_c.identity_keys:
            raise_to(
                VersionBump.major,
                f"changed identity keys on {name}",
            )
        old_attrs = _attrs_by_name(old_c)
        new_attrs = _attrs_by_name(new_c)
        for attr_name, old_a in old_attrs.items():
            if attr_name not in new_attrs:
                raise_to(VersionBump.major, f"removed attribute {name}.{attr_name}")
                continue
            new_a = new_attrs[attr_name]
            if old_a.kind != new_a.kind:
                raise_to(
                    VersionBump.major,
                    f"changed {name}.{attr_name} from {old_a.kind.value} "
                    f"to {new_a.kind.value}",
                )
            elif _range_narrowed(old_a, new_a):
                raise_to(
                    VersionBump.major,
                    f"narrowed range of {name}.{attr_name}",
                )
            if (not old_a.required) and new_a.required:
                raise_to(
                    VersionBump.major,
                    f"made {name}.{attr_name} required",
                )
        for attr_name, new_a in new_attrs.items():
            if attr_name not in old_attrs:
                if new_a.required:
                    raise_to(
                        VersionBump.major,
                        f"added required attribute {name}.{attr_name}",
                    )
                else:
                    raise_to(
                        VersionBump.minor,
                        f"added optional attribute {name}.{attr_name}",
                    )

    old_rels = {_rel_key(r) for r in old.relationships}
    new_rels = {_rel_key(r) for r in new.relationships}
    for key in old_rels - new_rels:
        raise_to(VersionBump.major, f"removed relationship {key[0]}")
    for key in new_rels - old_rels:
        raise_to(VersionBump.minor, f"added relationship {key[0]}")

    old_sources = {s.id for s in old.sources}
    new_sources = {s.id for s in new.sources}
    for sid in old_sources - new_sources:
        raise_to(VersionBump.major, f"removed source {sid}")
    for sid in new_sources - old_sources:
        raise_to(VersionBump.minor, f"added source {sid}")

    old_tasks = {t.name for t in old.tasks}
    new_tasks = {t.name for t in new.tasks}
    if new_tasks - old_tasks:
        raise_to(VersionBump.minor, "added task")
    if old_tasks - new_tasks:
        raise_to(VersionBump.major, "removed task")

    if old.skills.mcp_tools != new.skills.mcp_tools or len(new.skills.prose) > len(
        old.skills.prose
    ) or len(new.skills.functions) > len(old.skills.functions):
        raise_to(VersionBump.minor, "added skill or function")

    if len(new.examples.questions) > len(old.examples.questions) or len(
        new.evals.items
    ) > len(old.evals.items):
        raise_to(VersionBump.minor, "added question or eval")

    old_er = {e.type: e for e in old.validation.entity_resolution}
    new_er = {e.type: e for e in new.validation.entity_resolution}
    for t, new_e in new_er.items():
        old_e = old_er.get(t)
        if old_e is None:
            raise_to(VersionBump.minor, f"added ER config for {t}")
        elif (
            old_e.auto_merge != new_e.auto_merge
            or old_e.review != new_e.review
            or old_e.blocking != new_e.blocking
        ):
            raise_to(VersionBump.minor, f"changed ER threshold on {t}")
        elif old_e.identity != new_e.identity:
            raise_to(VersionBump.major, f"changed ER identity on {t}")

    if old.acquisition.model_dump() != new.acquisition.model_dump():
        acq_bump = True
        reasons.append("changed acquisition instruction")
    if old.freshness.model_dump() != new.freshness.model_dump():
        acq_bump = True
        reasons.append("changed freshness policy")

    if not reasons:
        reasons.append("no schema or acquisition change")

    return ChangeReport(bump, acq_bump, reasons)
