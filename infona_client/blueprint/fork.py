"""Fork a Blueprint package into a new identity with lineage (INF-579).

Copies the *package* (ontology slice, source definitions, sample section,
attribution). Does not copy instance data and does not write a graph.
The source package and any source install pin stay intact.

A seed has no parent. A fork is invalid without ``lineage.parent``
``{id, version}`` and a chain that keeps ancestors (INF-560 C2).
Attribution survives. Concept names stay; only the package id changes.

The new document is stored in the tenant-local catalog so inspect works
without installing. Install of the fork is a separate pin.

Boundary: OSS. No ``from infona.*``. Not a hosted registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from infona_client.blueprint.catalog import (
    CatalogedPackage,
    make_blueprint_package_store,
    shipped_seed_path,
)
from infona_client.blueprint.lock import _now
from infona_client.blueprint.models import (
    PACKAGE_ID_RE,
    BlueprintManifest,
    Lineage,
    LineageEntry,
    parse_blueprint,
)
from infona_client.blueprint.plan import (
    BlueprintError,
    BlueprintForkConflict,
    BlueprintNotFound,
    load_and_validate,
    manifest_content_hash,
)
from infona_client.blueprint.validate import validate_blueprint

_NS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class BlueprintInvalidForkId(BlueprintError):
    status_code = 400


@dataclass
class ForkResult:
    status: str
    tenant_id: str
    blueprint_id: str
    parent_id: str
    parent_version: str
    version: str
    attribution: str
    lineage: dict[str, Any]
    content_hash: str
    forked_at: str
    manifest: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tenant_id": self.tenant_id,
            "blueprint_id": self.blueprint_id,
            "parent_id": self.parent_id,
            "parent_version": self.parent_version,
            "version": self.version,
            "attribution": self.attribution,
            "lineage": self.lineage,
            "content_hash": self.content_hash,
            "forked_at": self.forked_at,
            "manifest": self.manifest,
            "sample_is_current": False,
        }


def kebab_namespace(raw: str) -> str:
    """Tenant id → legal package namespace. Fail closed if nothing remains."""
    slug = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    if not slug:
        slug = "workspace"
    slug = slug[:63]
    if not _NS_RE.match(slug):
        slug = f"ws-{slug}"[:63]
        if not _NS_RE.match(slug):
            raise BlueprintInvalidForkId(
                f"cannot derive a package namespace from {raw!r}"
            )
    return slug


def default_fork_id(tenant_id: str, parent_id: str) -> str:
    """``{tenant}/{parent-name}``, or ``…-fork`` when that would clobber."""
    leaf = parent_id.rsplit("/", 1)[-1]
    candidate = f"{kebab_namespace(tenant_id)}/{leaf}"
    if candidate == parent_id:
        candidate = f"{kebab_namespace(tenant_id)}/{leaf}-fork"
    return candidate


def _require_package_id(value: str) -> str:
    if not PACKAGE_ID_RE.match(value):
        raise BlueprintInvalidForkId(
            "fork target must be namespace/name in lowercase kebab, "
            f"got {value!r}"
        )
    return value


async def resolve_package(tenant_id: str, blueprint_id: str) -> BlueprintManifest:
    """Catalog first, then shipped seed. 404 if this tenant cannot see it."""
    store = make_blueprint_package_store()
    row = await store.get(tenant_id, blueprint_id)
    if row is not None:
        return row.manifest
    seed = shipped_seed_path(blueprint_id)
    if seed is not None:
        return load_and_validate(seed)
    raise BlueprintNotFound(
        f"blueprint {blueprint_id!r} is not in this workspace and is not "
        "a shipped seed"
    )


def copy_as_fork(
    parent: BlueprintManifest,
    new_id: str,
    *,
    forked_at: date,
) -> BlueprintManifest:
    """New identity + lineage. Attribution and concepts are unchanged."""
    _require_package_id(new_id)
    if new_id == parent.id:
        raise BlueprintForkConflict(
            "fork would clobber the source package",
            details={"blueprint_id": new_id},
        )
    namespace, _leaf = new_id.split("/", 1)
    parent_entry = LineageEntry(id=parent.id, version=parent.version)
    chain = [parent_entry, *list(parent.lineage.chain)]
    data = parent.model_dump(mode="json")
    data["id"] = new_id
    data["namespace"] = namespace
    data["published_at"] = forked_at.isoformat()
    data["lineage"] = Lineage(
        parent=parent_entry,
        chain=chain,
        forked_at=forked_at,
    ).model_dump(mode="json", exclude_none=True)
    return parse_blueprint(data)


def fork_card(manifest: BlueprintManifest) -> dict[str, Any]:
    """Inspectable package card for a catalogued fork (not an install pin)."""
    return {
        "blueprint_id": manifest.id,
        "name": manifest.name,
        "namespace": manifest.namespace,
        "version": manifest.version,
        "acquisition_revision": manifest.acquisition_revision,
        "content_hash": manifest_content_hash(manifest),
        "license": manifest.license,
        "attribution": manifest.attribution,
        "lineage": manifest.lineage.model_dump(mode="json", exclude_none=True),
        "types": [c.name for c in manifest.concepts],
        "sample_included": manifest.sample is not None,
        "sample_is_current": False,
        "sample_captured_at": (
            str(manifest.sample.captured_at) if manifest.sample is not None else None
        ),
        "installed": False,
        "status": "forked",
    }


async def fork_blueprint(
    tenant_id: str,
    blueprint_id: str,
    *,
    as_id: str | None = None,
) -> ForkResult:
    """Copy ``blueprint_id`` into a new catalog identity. No graph writes."""
    parent = await resolve_package(tenant_id, blueprint_id)
    target = _require_package_id(as_id) if as_id else default_fork_id(
        tenant_id, parent.id
    )
    store = make_blueprint_package_store()
    existing = await store.get(tenant_id, target)
    if existing is not None:
        raise BlueprintForkConflict(
            f"fork target {target!r} already exists in this workspace",
            details={"blueprint_id": target},
        )
    today = date.fromisoformat(_now()[:10])
    child = copy_as_fork(parent, target, forked_at=today)
    errors = validate_blueprint(child)
    if errors:
        raise BlueprintError(
            "forked package is invalid",
            details={"errors": errors},
        )
    await store.put(
        CatalogedPackage(
            tenant_id=tenant_id,
            manifest=child,
            origin="fork",
            stored_at=_now(),
        )
    )
    lineage = child.lineage.model_dump(mode="json", exclude_none=True)
    return ForkResult(
        status="forked",
        tenant_id=tenant_id,
        blueprint_id=child.id,
        parent_id=parent.id,
        parent_version=parent.version,
        version=child.version,
        attribution=child.attribution,
        lineage=lineage,
        content_hash=manifest_content_hash(child),
        forked_at=str(child.lineage.forked_at or today),
        manifest=child.model_dump(mode="json", exclude_none=True),
    )


__all__ = [
    "BlueprintInvalidForkId",
    "ForkResult",
    "copy_as_fork",
    "default_fork_id",
    "fork_blueprint",
    "fork_card",
    "kebab_namespace",
    "resolve_package",
]
