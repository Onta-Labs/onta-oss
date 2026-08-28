"""Workspace lock for an installed Blueprint (ADR 0014 F5 / INF-577).

Records what install wrote so re-install can no-op and uninstall can
remove only Blueprint-owned schema, skills, and sample rows.

The lock is tenant-scoped (ontology is tenant-scoped). Sample subjects
are KG-scoped and listed explicitly. Persistence is the same
tenant-confined GraphStore the rest of the graph uses — a
``:BlueprintInstallLock`` node keyed ``(tenant_id, blueprint_id)``,
the :class:`KnowledgeGraph` registry pattern. Process restart and
multi-task ECS share that store. Not a hosted registry. Not Postgres.

Boundary: OSS. ``infona_client.*`` / stdlib only.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from infona_client.graph.store import get_graph_store


@dataclass
class BlueprintLock:
    """Pinned install record for one Blueprint in one workspace."""

    tenant_id: str
    blueprint_id: str
    name: str
    version: str
    acquisition_revision: int
    content_hash: str
    kg: str
    installed_at: str
    sample_included: bool
    sample_captured_at: str | None
    sample_subjects: list[str] = field(default_factory=list)
    created_types: list[str] = field(default_factory=list)
    owned_types: list[str] = field(default_factory=list)
    owned_attributes: list[tuple[str, str]] = field(default_factory=list)
    owned_skills: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["owned_attributes"] = [list(p) for p in self.owned_attributes]
        data["owned_skills"] = [list(p) for p in self.owned_skills]
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BlueprintLock":
        attrs = raw.get("owned_attributes") or []
        skills = raw.get("owned_skills") or []
        return cls(
            tenant_id=raw["tenant_id"],
            blueprint_id=raw["blueprint_id"],
            name=raw.get("name") or raw["blueprint_id"],
            version=raw["version"],
            acquisition_revision=int(raw["acquisition_revision"]),
            content_hash=raw["content_hash"],
            kg=raw["kg"],
            installed_at=raw["installed_at"],
            sample_included=bool(raw.get("sample_included")),
            sample_captured_at=raw.get("sample_captured_at"),
            sample_subjects=list(raw.get("sample_subjects") or []),
            created_types=list(raw.get("created_types") or []),
            owned_types=list(raw.get("owned_types") or []),
            owned_attributes=[(str(a[0]), str(a[1])) for a in attrs],
            owned_skills=[(str(a[0]), str(a[1])) for a in skills],
        )


class BlueprintLockStore(Protocol):
    async def get(self, tenant_id: str, blueprint_id: str) -> Optional[BlueprintLock]: ...

    async def list_for_tenant(self, tenant_id: str) -> list[BlueprintLock]: ...

    async def put(self, lock: BlueprintLock) -> BlueprintLock: ...

    async def delete(self, tenant_id: str, blueprint_id: str) -> bool: ...


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _lock_from_payload(raw: Any) -> Optional[BlueprintLock]:
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        return None
    return BlueprintLock.from_dict(raw)


# Store-level Cypher (same privilege as kg_registry — tenant-scoped metadata,
# not session Cypher, which would require a kg). Isolation is the MATCH key.
_GET_CYPHER = """
MATCH (l:BlueprintInstallLock {tenant_id: $tenant_id, blueprint_id: $blueprint_id})
RETURN l.payload AS payload
"""

_LIST_CYPHER = """
MATCH (l:BlueprintInstallLock {tenant_id: $tenant_id})
RETURN l.payload AS payload
"""

_PUT_CYPHER = """
MERGE (l:BlueprintInstallLock {tenant_id: $tenant_id, blueprint_id: $blueprint_id})
SET l.payload = $payload
RETURN l.payload AS payload
"""

_DELETE_CYPHER = """
MATCH (l:BlueprintInstallLock {tenant_id: $tenant_id, blueprint_id: $blueprint_id})
WITH l, 1 AS n
DETACH DELETE l
RETURN n
"""


class InMemoryBlueprintLockStore:
    """Process-local map. Tests that do not want a GraphStore can use this.

    Production install / inspect / uninstall use
    :class:`GraphStoreBlueprintLockStore`.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], BlueprintLock] = {}

    async def get(self, tenant_id: str, blueprint_id: str) -> Optional[BlueprintLock]:
        return self._rows.get((tenant_id, blueprint_id))

    async def list_for_tenant(self, tenant_id: str) -> list[BlueprintLock]:
        return [lock for (tid, _), lock in self._rows.items() if tid == tenant_id]

    async def put(self, lock: BlueprintLock) -> BlueprintLock:
        self._rows[(lock.tenant_id, lock.blueprint_id)] = lock
        return lock

    async def delete(self, tenant_id: str, blueprint_id: str) -> bool:
        return self._rows.pop((tenant_id, blueprint_id), None) is not None


class GraphStoreBlueprintLockStore:
    """Lock backend over the process GraphStore (Memory or Neo4j).

    Native ``blueprint_lock_*`` methods win (MemoryGraphStore). Neo4j uses
    store-level ``_run`` Cypher, same as :mod:`infona_client.graph.kg_registry`.
    Every call resolves :func:`get_graph_store` so a new wrapper after
    process reload still sees pins written before the bounce.
    """

    def _store(self) -> Any:
        return get_graph_store()

    async def get(self, tenant_id: str, blueprint_id: str) -> Optional[BlueprintLock]:
        store = self._store()
        native = getattr(store, "blueprint_lock_get", None)
        if callable(native):
            return _lock_from_payload(await native(tenant_id, blueprint_id))
        run = getattr(store, "_run", None)
        if not callable(run):
            raise RuntimeError("GraphStore cannot read Blueprint locks")
        rows = await run(
            _GET_CYPHER,
            {"tenant_id": tenant_id, "blueprint_id": blueprint_id},
            writing=False,
            database=None,
        )
        if not rows:
            return None
        return _lock_from_payload(rows[0].get("payload"))

    async def list_for_tenant(self, tenant_id: str) -> list[BlueprintLock]:
        store = self._store()
        native = getattr(store, "blueprint_lock_list", None)
        if callable(native):
            out: list[BlueprintLock] = []
            for raw in await native(tenant_id):
                lock = _lock_from_payload(raw)
                if lock is not None:
                    out.append(lock)
            return out
        run = getattr(store, "_run", None)
        if not callable(run):
            raise RuntimeError("GraphStore cannot list Blueprint locks")
        rows = await run(
            _LIST_CYPHER,
            {"tenant_id": tenant_id},
            writing=False,
            database=None,
        )
        locks: list[BlueprintLock] = []
        for row in rows:
            lock = _lock_from_payload(row.get("payload"))
            if lock is not None:
                locks.append(lock)
        return locks

    async def put(self, lock: BlueprintLock) -> BlueprintLock:
        store = self._store()
        payload = lock.to_dict()
        native = getattr(store, "blueprint_lock_put", None)
        if callable(native):
            await native(lock.tenant_id, lock.blueprint_id, payload)
            return lock
        run = getattr(store, "_run", None)
        if not callable(run):
            raise RuntimeError("GraphStore cannot persist Blueprint locks")
        await run(
            _PUT_CYPHER,
            {
                "tenant_id": lock.tenant_id,
                "blueprint_id": lock.blueprint_id,
                "payload": json.dumps(payload),
            },
            writing=True,
            database=None,
        )
        return lock

    async def delete(self, tenant_id: str, blueprint_id: str) -> bool:
        store = self._store()
        native = getattr(store, "blueprint_lock_delete", None)
        if callable(native):
            return bool(await native(tenant_id, blueprint_id))
        run = getattr(store, "_run", None)
        if not callable(run):
            raise RuntimeError("GraphStore cannot delete Blueprint locks")
        rows = await run(
            _DELETE_CYPHER,
            {"tenant_id": tenant_id, "blueprint_id": blueprint_id},
            writing=True,
            database=None,
        )
        if not rows:
            return False
        return int(rows[0].get("n") or 0) > 0


_store: Optional[BlueprintLockStore] = None


def make_blueprint_lock_store() -> BlueprintLockStore:
    """Process wrapper over the tenant-confined GraphStore.

    Reloading the wrapper (``reset_blueprint_lock_store``) does not drop
    pins — they live on the GraphStore. A new process / ECS task that
    shares the store still sees the install.
    """
    global _store
    if _store is None:
        _store = GraphStoreBlueprintLockStore()
    return _store


def reset_blueprint_lock_store() -> None:
    """Test helper — drop the memoized wrapper. Does not wipe GraphStore pins."""
    global _store
    _store = None


__all__ = [
    "BlueprintLock",
    "BlueprintLockStore",
    "GraphStoreBlueprintLockStore",
    "InMemoryBlueprintLockStore",
    "make_blueprint_lock_store",
    "reset_blueprint_lock_store",
    "_now",
]
