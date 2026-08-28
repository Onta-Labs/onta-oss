"""Install, inspect, and uninstall a Blueprint package (INF-575 / INF-577).

One engine. HTTP routes, ``python -m infona_client.blueprint``, the npm
CLI, and MCP all call these functions. Writes go through the shared
paths: ``commit_ontology`` for schema, ``insert_facts`` /
``delete_facts`` / ``refresh_after_write`` for the optional sample.

Install yields an empty graph plus an optional bounded sample (INF-564).
The sample is marked as sample and is never current. Re-install of the
same pin is a no-op. Uninstall removes what this install wrote and
leaves the rest of the workspace. If another KG in the tenant still
holds non-sample rows typed against owned types, uninstall refuses.

Boundary: OSS. No ``from infona.*``. BYOK — credentials stay workspace-side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

from infona_client.blueprint.lock import (
    BlueprintLock,
    _now,
    make_blueprint_lock_store,
)
from infona_client.blueprint.models import BlueprintManifest
from infona_client.blueprint.plan import (
    BlueprintError,
    BlueprintForkNotImplemented,
    BlueprintNotInstalled,
    BlueprintUninstallRefused,
    BlueprintValidationError,
    facts_for_sample,
    load_and_validate,
    manifest_content_hash,
    mutations_from_manifest,
    sample_subject,
)
from infona_client.graph.kg_writer import delete_facts, insert_facts, refresh_after_write
from infona_client.graph.ontology_catalog import list_attributes, list_types
from infona_client.graph.ontology_commit import commit_ontology
from infona_client.graph.ontology_queries import type_uri
from infona_client.graph.queries import kg_graph_uri, tenant_graph_uri
from infona_client.graph.rdfs_helpers import session_entities_of_type
from infona_client.graph.scope import GraphScope
from infona_client.graph.store import get_graph_store
from infona_client.models.ontology import OntologyMutation, OntologyOpKind
from infona_client.skills import TypeSkill, make_type_skill_store


@dataclass
class InstallResult:
    status: Literal["installed", "already_installed", "updated"]
    tenant_id: str
    blueprint_id: str
    name: str
    version: str
    acquisition_revision: int
    content_hash: str
    kg: str
    types: list[str]
    sample_included: bool
    sample_is_current: bool
    sample_captured_at: str | None
    sample_subjects: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tenant_id": self.tenant_id,
            "blueprint_id": self.blueprint_id,
            "name": self.name,
            "version": self.version,
            "acquisition_revision": self.acquisition_revision,
            "content_hash": self.content_hash,
            "kg": self.kg,
            "types": list(self.types),
            "sample_included": self.sample_included,
            "sample_is_current": False,
            "sample_captured_at": self.sample_captured_at,
            "sample_subjects": list(self.sample_subjects),
            "skills": list(self.skills),
        }


def _result_from_lock(
    lock: BlueprintLock,
    status: Literal["installed", "already_installed", "updated"],
) -> InstallResult:
    return InstallResult(
        status=status,
        tenant_id=lock.tenant_id,
        blueprint_id=lock.blueprint_id,
        name=lock.name,
        version=lock.version,
        acquisition_revision=lock.acquisition_revision,
        content_hash=lock.content_hash,
        kg=lock.kg,
        types=list(lock.owned_types),
        sample_included=lock.sample_included,
        sample_is_current=False,
        sample_captured_at=lock.sample_captured_at,
        sample_subjects=list(lock.sample_subjects),
        skills=[f"{t}/{s}" for t, s in lock.owned_skills],
    )


async def _existing_type_names(tenant_id: str) -> set[str]:
    return {t.name for t in await list_types(tenant_id=tenant_id)}


async def _existing_attr_pairs(tenant_id: str) -> set[tuple[str, str]]:
    return {(a.domain, a.name) for a in await list_attributes(tenant_id=tenant_id)}


async def _write_skills(tenant_id: str, manifest: BlueprintManifest) -> list[tuple[str, str]]:
    store = make_type_skill_store()
    owned: list[tuple[str, str]] = []
    for skill in manifest.skills:
        await store.upsert(
            TypeSkill(
                slug=skill.slug,
                type_name=skill.type_name,
                body=skill.body,
                title=skill.title,
                summary=skill.summary,
                tenant_id=tenant_id,
                metadata={
                    "blueprint_id": manifest.id,
                    "blueprint_version": manifest.version,
                },
            )
        )
        owned.append((skill.type_name, skill.slug))
    return owned


async def _remove_skills(tenant_id: str, owned: list[tuple[str, str]]) -> list[str]:
    store = make_type_skill_store()
    removed: list[str] = []
    for type_name, slug in owned:
        if await store.delete(tenant_id, type_name, slug):
            removed.append(f"{type_name}/{slug}")
    return removed


async def _dependent_instances(
    tenant_id: str,
    types: list[str],
    *,
    ignore_subjects: set[str],
    extra_kgs: list[str],
) -> list[dict[str, str]]:
    """Non-sample instance rows of owned types, across tenant KGs."""
    kgs = list(dict.fromkeys(extra_kgs))
    try:
        from infona_client.graph.kg_registry import list_registered_kgs

        for row in await list_registered_kgs(tenant_id):
            name = row.get("name") if isinstance(row, dict) else None
            if name:
                kgs.append(str(name))
    except Exception:  # noqa: BLE001 — registry is best-effort for refuse
        pass
    kgs = list(dict.fromkeys(kgs))
    store = get_graph_store()
    hits: list[dict[str, str]] = []
    for kg in kgs:
        session = store.session(GraphScope.for_instance(tenant_id, kg))
        for type_name in types:
            try:
                uris = await session_entities_of_type(
                    session, type_uri(type_name), include_subclasses=False
                )
            except Exception:  # noqa: BLE001 — missing type is not a dependent
                continue
            for uri in uris:
                if uri in ignore_subjects:
                    continue
                hits.append({"kg": kg, "type": type_name, "subject": uri})
    return hits


async def install_blueprint(
    source: str | Path | Mapping[str, Any] | BlueprintManifest,
    *,
    tenant_id: str,
    kg: str,
    include_sample: bool = True,
    neptune: Any = None,
) -> InstallResult:
    """Apply a Blueprint to ``tenant_id`` / ``kg``. Idempotent on the same pin."""
    manifest = load_and_validate(source)
    content_hash = manifest_content_hash(manifest)
    store = make_blueprint_lock_store()
    existing = await store.get(tenant_id, manifest.id)
    if (
        existing is not None
        and existing.version == manifest.version
        and existing.acquisition_revision == manifest.acquisition_revision
        and existing.content_hash == content_hash
        and existing.kg == kg
        and existing.sample_included == bool(include_sample and manifest.sample)
    ):
        return _result_from_lock(existing, "already_installed")

    prior_types = await _existing_type_names(tenant_id)
    prior_attrs = await _existing_attr_pairs(tenant_id)
    mutations = mutations_from_manifest(manifest)
    await commit_ontology(
        neptune,
        tenant_graph_uri(tenant_id),
        mutations,
        actor="blueprint_install",
        message=f"install {manifest.id}@{manifest.version}",
    )

    owned_types = [c.name for c in manifest.concepts]
    owned_attrs = [
        (c.name, a.name) for c in manifest.concepts for a in c.attributes
    ]
    created_types = [n for n in owned_types if n not in prior_types]
    # Attrs that already lived on a pre-existing type stay on uninstall.
    created_attrs = [p for p in owned_attrs if p not in prior_attrs]

    sample_subjects: list[str] = []
    if existing is not None and existing.sample_subjects:
        await delete_facts(
            neptune,
            kg_graph_uri(tenant_id, existing.kg),
            subjects=list(existing.sample_subjects),
            touched_types=existing.owned_types,
            reason="blueprint_reinstall_replace_sample",
        )
        await refresh_after_write(
            neptune,
            tenant_id=tenant_id,
            kg_name=existing.kg,
            affected_types=existing.owned_types,
            deleted_subjects=existing.sample_subjects,
        )

    if include_sample and manifest.sample is not None:
        facts, sample_subjects = facts_for_sample(manifest)
        if facts:
            await insert_facts(
                neptune,
                kg_graph_uri(tenant_id, kg),
                facts=facts,
            )
            await refresh_after_write(
                neptune,
                tenant_id=tenant_id,
                kg_name=kg,
                affected_types=owned_types,
            )

    owned_skills = await _write_skills(tenant_id, manifest)
    captured = (
        str(manifest.sample.captured_at)
        if include_sample and manifest.sample is not None
        else None
    )
    lock = BlueprintLock(
        tenant_id=tenant_id,
        blueprint_id=manifest.id,
        name=manifest.name,
        version=manifest.version,
        acquisition_revision=manifest.acquisition_revision,
        content_hash=content_hash,
        kg=kg,
        installed_at=_now(),
        sample_included=bool(include_sample and manifest.sample),
        sample_captured_at=captured,
        sample_subjects=sample_subjects,
        created_types=created_types,
        owned_types=owned_types,
        owned_attributes=created_attrs,
        owned_skills=owned_skills,
    )
    await store.put(lock)
    status: Literal["installed", "updated"] = (
        "updated" if existing is not None else "installed"
    )
    return _result_from_lock(lock, status)


def _card(manifest: BlueprintManifest | None, lock: BlueprintLock) -> dict[str, Any]:
    card: dict[str, Any] = {
        "blueprint_id": lock.blueprint_id,
        "name": lock.name,
        "version": lock.version,
        "acquisition_revision": lock.acquisition_revision,
        "content_hash": lock.content_hash,
        "kg": lock.kg,
        "installed_at": lock.installed_at,
        "types": list(lock.owned_types),
        "sample_included": lock.sample_included,
        "sample_is_current": False,
        "sample_captured_at": lock.sample_captured_at,
        "sample_subject_count": len(lock.sample_subjects),
        "skills": [f"{t}/{s}" for t, s in lock.owned_skills],
    }
    if manifest is not None:
        card["license"] = manifest.license
        card["attribution"] = manifest.attribution
        card["namespace"] = manifest.namespace
        card["sources"] = [
            {
                "id": s.id,
                "title": s.title,
                "credential": s.credential,
                "key_env": s.key_env,
            }
            for s in manifest.sources
        ]
        card["tasks"] = [t.id for t in manifest.tasks]
    return card


async def inspect_blueprint(
    tenant_id: str,
    blueprint_id: str,
    *,
    source: str | Path | Mapping[str, Any] | BlueprintManifest | None = None,
) -> dict[str, Any]:
    """Return the workspace lock + card. 404 if this tenant has no install."""
    store = make_blueprint_lock_store()
    lock = await store.get(tenant_id, blueprint_id)
    if lock is None:
        raise BlueprintNotInstalled(
            f"blueprint {blueprint_id!r} is not installed in this workspace"
        )
    manifest = load_and_validate(source) if source is not None else None
    return _card(manifest, lock)


async def list_installed_blueprints(tenant_id: str) -> list[dict[str, Any]]:
    store = make_blueprint_lock_store()
    locks = await store.list_for_tenant(tenant_id)
    return [_card(None, lock) for lock in locks]


async def uninstall_blueprint(
    tenant_id: str,
    blueprint_id: str,
    *,
    neptune: Any = None,
) -> dict[str, Any]:
    """Remove what this install wrote. Refuse if non-sample typed data remains."""
    store = make_blueprint_lock_store()
    lock = await store.get(tenant_id, blueprint_id)
    if lock is None:
        raise BlueprintNotInstalled(
            f"blueprint {blueprint_id!r} is not installed in this workspace"
        )
    dependents = await _dependent_instances(
        tenant_id,
        lock.owned_types,
        ignore_subjects=set(lock.sample_subjects),
        extra_kgs=[lock.kg],
    )
    if dependents:
        raise BlueprintUninstallRefused(
            "uninstall refused: other instance data is typed against "
            "this Blueprint's types",
            details={"dependents": dependents},
        )

    if lock.sample_subjects:
        await delete_facts(
            neptune,
            kg_graph_uri(tenant_id, lock.kg),
            subjects=list(lock.sample_subjects),
            touched_types=lock.owned_types,
            reason="blueprint_uninstall",
        )

    removed_skills = await _remove_skills(tenant_id, lock.owned_skills)

    undo: list[OntologyMutation] = []
    for type_name, slot in lock.owned_attributes:
        undo.append(
            OntologyMutation(
                op=OntologyOpKind.DELETE_ATTRIBUTE,
                type_name=type_name,
                slot_name=slot,
            )
        )
    for type_name in lock.created_types:
        undo.append(
            OntologyMutation(
                op=OntologyOpKind.DELETE_TYPE,
                type_name=type_name,
            )
        )
    if undo:
        await commit_ontology(
            neptune,
            tenant_graph_uri(tenant_id),
            undo,
            actor="blueprint_uninstall",
            message=f"uninstall {blueprint_id}",
        )

    await refresh_after_write(
        neptune,
        tenant_id=tenant_id,
        kg_name=lock.kg,
        affected_types=lock.owned_types,
        deleted_subjects=lock.sample_subjects,
    )
    await store.delete(tenant_id, blueprint_id)
    return {
        "status": "uninstalled",
        "blueprint_id": blueprint_id,
        "removed_types": list(lock.created_types),
        "removed_sample": list(lock.sample_subjects),
        "removed_skills": removed_skills,
        "left_in_place": {
            "pre_existing_types": [
                t for t in lock.owned_types if t not in lock.created_types
            ],
        },
    }


def fork_blueprint(*_args: Any, **_kwargs: Any) -> None:
    """INF-579 lineage is not in this PR. Same route family, honest 501."""
    raise BlueprintForkNotImplemented(
        "Blueprint fork/lineage is not implemented (INF-579). "
        "Copy the inspectable package directory; do not invent a second endpoint."
    )


__all__ = [
    "BlueprintError",
    "BlueprintForkNotImplemented",
    "BlueprintNotInstalled",
    "BlueprintUninstallRefused",
    "BlueprintValidationError",
    "InstallResult",
    "fork_blueprint",
    "inspect_blueprint",
    "install_blueprint",
    "list_installed_blueprints",
    "load_and_validate",
    "manifest_content_hash",
    "mutations_from_manifest",
    "sample_subject",
    "uninstall_blueprint",
]
