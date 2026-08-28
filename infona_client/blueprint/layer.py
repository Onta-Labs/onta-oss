"""Apply a private overlay and a non-clobbering upstream update (INF-578).

Same installed pin. Overlay is the tenant layer (ADR 0002 writes go to
the tenant named graph). Update reapplies the public base, then the
overlay. Conflicts are recorded, never silently dropped. Conflict UI
is INF-595.

Boundary: OSS. No ``from infona.*``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from infona_client.blueprint.lock import _now, make_blueprint_lock_store
from infona_client.blueprint.models import BlueprintManifest, BlueprintSkill
from infona_client.blueprint.overlay import (
    BlueprintIdMismatch,
    OverlayConflict,
    OverlayDocument,
    StoredOverlay,
    detect_conflicts,
    make_blueprint_overlay_store,
    merge_overlay,
    mutations_from_overlay,
    parse_overlay,
    validate_overlay,
)
from infona_client.blueprint.plan import (
    BlueprintNotInstalled,
    load_and_validate,
)
from infona_client.graph.ontology_commit import commit_ontology
from infona_client.graph.queries import tenant_graph_uri
from infona_client.skills import TypeSkill, make_type_skill_store


def overlay_public(row: StoredOverlay | None) -> dict[str, Any] | None:
    return None if row is None else row.to_public()


def overlay_conflicts(row: StoredOverlay | None) -> list[dict[str, Any]]:
    if row is None:
        return []
    return [c.to_dict() for c in row.conflicts]


async def _write_overlay_skills(tenant_id: str, skills: list[BlueprintSkill]) -> None:
    store = make_type_skill_store()
    for skill in skills:
        await store.upsert(
            TypeSkill(
                slug=skill.slug,
                type_name=skill.type_name,
                body=skill.body,
                title=skill.title,
                summary=skill.summary,
                tenant_id=tenant_id,
                metadata={"blueprint_overlay": True},
            )
        )


async def apply_overlay(
    tenant_id: str,
    overlay: OverlayDocument,
    *,
    base_type_names: set[str],
    neptune: Any = None,
) -> None:
    validate_overlay(overlay, base_type_names=base_type_names)
    mutations = mutations_from_overlay(overlay, base_type_names=base_type_names)
    if mutations:
        await commit_ontology(
            neptune,
            tenant_graph_uri(tenant_id),
            mutations,
            actor="blueprint_overlay",
            message="apply private Blueprint overlay",
        )
    await _write_overlay_skills(tenant_id, overlay.skills)


async def reconcile_overlay_after_base_write(
    *,
    tenant_id: str,
    blueprint_id: str,
    new_base: BlueprintManifest,
    old_base: BlueprintManifest | None,
    content_hash: str,
    neptune: Any = None,
) -> dict[str, Any]:
    """Re-apply the overlay after an upstream pin write. Record conflicts."""
    store = make_blueprint_overlay_store()
    row = await store.get(tenant_id, blueprint_id)
    if row is None:
        return {"overlay": None, "conflicts": []}
    conflicts: list[OverlayConflict] = []
    if old_base is not None:
        conflicts = detect_conflicts(old_base, new_base, row.document)
    await apply_overlay(
        tenant_id,
        row.document,
        base_type_names={c.name for c in new_base.concepts},
        neptune=neptune,
    )
    updated = StoredOverlay(
        tenant_id=tenant_id,
        blueprint_id=blueprint_id,
        document=row.document,
        conflicts=conflicts,
        updated_at=_now(),
        base_version=new_base.version,
        base_content_hash=content_hash,
    )
    await store.put(updated)
    return {"overlay": updated.to_public(), "conflicts": [c.to_dict() for c in conflicts]}


async def extend_blueprint(
    tenant_id: str,
    blueprint_id: str,
    overlay: Mapping[str, Any] | OverlayDocument,
    *,
    neptune: Any = None,
) -> dict[str, Any]:
    """Add a private delta on the installed pin. Does not change the pin."""
    lock = await make_blueprint_lock_store().get(tenant_id, blueprint_id)
    if lock is None:
        raise BlueprintNotInstalled(
            f"blueprint {blueprint_id!r} is not installed in this workspace"
        )
    incoming = parse_overlay(overlay)
    store = make_blueprint_overlay_store()
    existing = await store.get(tenant_id, blueprint_id)
    document = (
        merge_overlay(existing.document, incoming)
        if existing is not None
        else incoming
    )
    await apply_overlay(
        tenant_id,
        document,
        base_type_names=set(lock.owned_types),
        neptune=neptune,
    )
    row = StoredOverlay(
        tenant_id=tenant_id,
        blueprint_id=blueprint_id,
        document=document,
        conflicts=existing.conflicts if existing is not None else [],
        updated_at=_now(),
        base_version=lock.version,
        base_content_hash=lock.content_hash,
    )
    await store.put(row)
    return {
        "status": "extended",
        "tenant_id": tenant_id,
        "blueprint_id": blueprint_id,
        "version": lock.version,
        "overlay": row.to_public(),
        "conflicts": [c.to_dict() for c in row.conflicts],
    }


async def update_blueprint(
    source: str | Path | Mapping[str, Any] | BlueprintManifest,
    *,
    tenant_id: str,
    blueprint_id: str,
    include_sample: bool | None = None,
    neptune: Any = None,
) -> dict[str, Any]:
    """Pull a new public base onto the same pin. Overlay is reapplied."""
    from infona_client.blueprint.install import install_blueprint

    lock = await make_blueprint_lock_store().get(tenant_id, blueprint_id)
    if lock is None:
        raise BlueprintNotInstalled(
            f"blueprint {blueprint_id!r} is not installed in this workspace"
        )
    manifest = load_and_validate(source)
    if manifest.id != blueprint_id:
        raise BlueprintIdMismatch(
            f"update target {blueprint_id!r} does not match package {manifest.id!r}",
            details={"expected": blueprint_id, "got": manifest.id},
        )
    keep_sample = lock.sample_included if include_sample is None else include_sample
    result = await install_blueprint(
        manifest,
        tenant_id=tenant_id,
        kg=lock.kg,
        include_sample=keep_sample,
        neptune=neptune,
    )
    row = await make_blueprint_overlay_store().get(tenant_id, blueprint_id)
    body = result.to_dict()
    body["conflicts"] = overlay_conflicts(row)
    body["overlay"] = overlay_public(row)
    if body["conflicts"] and body.get("status") == "updated":
        body["status"] = "updated_with_conflicts"
    return body


__all__ = [
    "apply_overlay",
    "extend_blueprint",
    "overlay_conflicts",
    "overlay_public",
    "reconcile_overlay_after_base_write",
    "update_blueprint",
]
