"""Tenant-local Blueprint package catalog (INF-579).

Stores the *package document* — know-how, not instance data. Fork writes
a new identity here. Install also records what it applied so a later
fork does not need a hosted registry.

Keyed ``(tenant_id, blueprint_id)``. No cross-tenant read (INF-580).
Persistence is the same tenant-confined GraphStore the install lock
uses — a ``:BlueprintPackage`` node keyed ``(tenant_id, blueprint_id)``.
Process restart and multi-task ECS share that store. Not a hosted
registry. Not Postgres.

Shipped OSS seeds are readable by id. They are protocol artifacts, not
a registry index.

Boundary: OSS. ``infona_client.*`` / stdlib only. No ``from infona.*``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Protocol

from infona_client.blueprint.models import BlueprintManifest, parse_blueprint
from infona_client.blueprint.seeds import CLINICAL_TRIALS
from infona_client.graph.store import get_graph_store

CatalogOrigin = Literal["fork", "install"]


@dataclass
class CatalogedPackage:
    """One package document owned by one workspace."""

    tenant_id: str
    manifest: BlueprintManifest
    origin: CatalogOrigin
    stored_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "origin": self.origin,
            "stored_at": self.stored_at,
            "manifest": self.manifest.model_dump(mode="json"),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CatalogedPackage":
        origin = raw.get("origin") or "install"
        if origin not in ("fork", "install"):
            origin = "install"
        return cls(
            tenant_id=raw["tenant_id"],
            manifest=parse_blueprint(raw["manifest"]),
            origin=origin,
            stored_at=str(raw.get("stored_at") or ""),
        )


class BlueprintPackageStore(Protocol):
    async def get(
        self, tenant_id: str, blueprint_id: str
    ) -> Optional[CatalogedPackage]: ...

    async def list_for_tenant(self, tenant_id: str) -> list[CatalogedPackage]: ...

    async def put(self, row: CatalogedPackage) -> CatalogedPackage: ...

    async def delete(self, tenant_id: str, blueprint_id: str) -> bool: ...


class InMemoryBlueprintPackageStore:
    """Process-local map. Tests that do not want a GraphStore can use this.

    Production install / fork / inspect use
    :class:`GraphStoreBlueprintPackageStore`.
    """

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

    async def delete(self, tenant_id: str, blueprint_id: str) -> bool:
        return self._rows.pop((tenant_id, blueprint_id), None) is not None


def _package_from_payload(raw: Any) -> Optional[CatalogedPackage]:
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        return None
    return CatalogedPackage.from_dict(raw)


# Store-level Cypher (same privilege as :BlueprintInstallLock — tenant-scoped
# metadata, not session Cypher). Isolation is the MATCH key.
_GET_CYPHER = """
MATCH (p:BlueprintPackage {tenant_id: $tenant_id, blueprint_id: $blueprint_id})
RETURN p.payload AS payload
"""

_LIST_CYPHER = """
MATCH (p:BlueprintPackage {tenant_id: $tenant_id})
RETURN p.payload AS payload
"""

_PUT_CYPHER = """
MERGE (p:BlueprintPackage {tenant_id: $tenant_id, blueprint_id: $blueprint_id})
SET p.payload = $payload
RETURN p.payload AS payload
"""

_DELETE_CYPHER = """
MATCH (p:BlueprintPackage {tenant_id: $tenant_id, blueprint_id: $blueprint_id})
WITH p, 1 AS n
DETACH DELETE p
RETURN n
"""


class GraphStoreBlueprintPackageStore:
    """Package catalog over the process GraphStore (Memory or Neo4j).

    Native ``blueprint_package_*`` methods win (MemoryGraphStore). Neo4j uses
    store-level ``_run`` Cypher, same as :mod:`infona_client.blueprint.lock`.
    Every call resolves :func:`get_graph_store` so a new wrapper after
    process reload still sees packages written before the bounce.
    """

    def _store(self) -> Any:
        return get_graph_store()

    async def get(
        self, tenant_id: str, blueprint_id: str
    ) -> Optional[CatalogedPackage]:
        store = self._store()
        native = getattr(store, "blueprint_package_get", None)
        if callable(native):
            return _package_from_payload(await native(tenant_id, blueprint_id))
        run = getattr(store, "_run", None)
        if not callable(run):
            raise RuntimeError("GraphStore cannot read Blueprint packages")
        rows = await run(
            _GET_CYPHER,
            {"tenant_id": tenant_id, "blueprint_id": blueprint_id},
            writing=False,
            database=None,
        )
        if not rows:
            return None
        return _package_from_payload(rows[0].get("payload"))

    async def list_for_tenant(self, tenant_id: str) -> list[CatalogedPackage]:
        store = self._store()
        native = getattr(store, "blueprint_package_list", None)
        if callable(native):
            out: list[CatalogedPackage] = []
            for raw in await native(tenant_id):
                row = _package_from_payload(raw)
                if row is not None:
                    out.append(row)
            return out
        run = getattr(store, "_run", None)
        if not callable(run):
            raise RuntimeError("GraphStore cannot list Blueprint packages")
        rows = await run(
            _LIST_CYPHER,
            {"tenant_id": tenant_id},
            writing=False,
            database=None,
        )
        out: list[CatalogedPackage] = []
        for row in rows:
            pkg = _package_from_payload(row.get("payload"))
            if pkg is not None:
                out.append(pkg)
        return out

    async def put(self, row: CatalogedPackage) -> CatalogedPackage:
        store = self._store()
        payload = row.to_dict()
        native = getattr(store, "blueprint_package_put", None)
        if callable(native):
            await native(row.tenant_id, row.manifest.id, payload)
            return row
        run = getattr(store, "_run", None)
        if not callable(run):
            raise RuntimeError("GraphStore cannot persist Blueprint packages")
        await run(
            _PUT_CYPHER,
            {
                "tenant_id": row.tenant_id,
                "blueprint_id": row.manifest.id,
                "payload": json.dumps(payload),
            },
            writing=True,
            database=None,
        )
        return row

    async def delete(self, tenant_id: str, blueprint_id: str) -> bool:
        store = self._store()
        native = getattr(store, "blueprint_package_delete", None)
        if callable(native):
            return bool(await native(tenant_id, blueprint_id))
        run = getattr(store, "_run", None)
        if not callable(run):
            raise RuntimeError("GraphStore cannot delete Blueprint packages")
        rows = await run(
            _DELETE_CYPHER,
            {"tenant_id": tenant_id, "blueprint_id": blueprint_id},
            writing=True,
            database=None,
        )
        if not rows:
            return False
        return int(rows[0].get("n") or 0) > 0


_store: Optional[BlueprintPackageStore] = None

#: Frozen seed id → directory. Not a registry; the package lives in-tree.
SHIPPED_SEEDS: dict[str, Path] = {
    "infona/clinical-trials": CLINICAL_TRIALS,
}


def shipped_seed_path(blueprint_id: str) -> Path | None:
    """Return the in-tree seed directory, or None. Not a hosted lookup."""
    return SHIPPED_SEEDS.get(blueprint_id)


def make_blueprint_package_store() -> BlueprintPackageStore:
    """Process wrapper over the tenant-confined GraphStore.

    Reloading the wrapper (``reset_blueprint_package_store``) does not drop
    packages — they live on the GraphStore. A new process / ECS task that
    shares the store still sees a forked package.
    """
    global _store
    if _store is None:
        _store = GraphStoreBlueprintPackageStore()
    return _store


def reset_blueprint_package_store() -> None:
    """Test helper — drop the memoized wrapper. Does not wipe GraphStore rows."""
    global _store
    _store = None


__all__ = [
    "SHIPPED_SEEDS",
    "BlueprintPackageStore",
    "CatalogedPackage",
    "GraphStoreBlueprintPackageStore",
    "InMemoryBlueprintPackageStore",
    "make_blueprint_package_store",
    "reset_blueprint_package_store",
    "shipped_seed_path",
]
