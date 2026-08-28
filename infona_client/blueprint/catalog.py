"""Tenant-local Blueprint package catalog (INF-579).

Stores the *package document* — know-how, not instance data. Fork writes
a new identity here. Install also records what it applied so a later
fork does not need a hosted registry.

Keyed ``(tenant_id, blueprint_id)``. No cross-tenant read (INF-580).
In-memory in this PR (fork leftover). The *install lock* is not —
it lives on the tenant GraphStore (``:BlueprintInstallLock``).

Shipped OSS seeds are readable by id. They are protocol artifacts, not
a registry index.

Boundary: OSS. ``infona_client.*`` / stdlib only. No ``from infona.*``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Protocol

from infona_client.blueprint.models import BlueprintManifest
from infona_client.blueprint.seeds import CLINICAL_TRIALS

CatalogOrigin = Literal["fork", "install"]


@dataclass
class CatalogedPackage:
    """One package document owned by one workspace."""

    tenant_id: str
    manifest: BlueprintManifest
    origin: CatalogOrigin
    stored_at: str


class BlueprintPackageStore(Protocol):
    async def get(
        self, tenant_id: str, blueprint_id: str
    ) -> Optional[CatalogedPackage]: ...

    async def list_for_tenant(self, tenant_id: str) -> list[CatalogedPackage]: ...

    async def put(self, row: CatalogedPackage) -> CatalogedPackage: ...


class InMemoryBlueprintPackageStore:
    """Zero-config default. Tenant-confined — no cross-tenant read."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], CatalogedPackage] = {}

    async def get(
        self, tenant_id: str, blueprint_id: str
    ) -> Optional[CatalogedPackage]:
        return self._rows.get((tenant_id, blueprint_id))

    async def list_for_tenant(self, tenant_id: str) -> list[CatalogedPackage]:
        return [row for (tid, _), row in self._rows.items() if tid == tenant_id]

    async def put(self, row: CatalogedPackage) -> CatalogedPackage:
        self._rows[(row.tenant_id, row.manifest.id)] = row
        return row


_store: Optional[BlueprintPackageStore] = None

#: Frozen seed id → directory. Not a registry; the package lives in-tree.
SHIPPED_SEEDS: dict[str, Path] = {
    "infona/clinical-trials": CLINICAL_TRIALS,
}


def shipped_seed_path(blueprint_id: str) -> Path | None:
    """Return the in-tree seed directory, or None. Not a hosted lookup."""
    return SHIPPED_SEEDS.get(blueprint_id)


def make_blueprint_package_store() -> BlueprintPackageStore:
    global _store
    if _store is None:
        _store = InMemoryBlueprintPackageStore()
    return _store


def reset_blueprint_package_store() -> None:
    """Test helper — drop the memoized catalog."""
    global _store
    _store = None


__all__ = [
    "SHIPPED_SEEDS",
    "BlueprintPackageStore",
    "CatalogedPackage",
    "InMemoryBlueprintPackageStore",
    "make_blueprint_package_store",
    "reset_blueprint_package_store",
    "shipped_seed_path",
]
