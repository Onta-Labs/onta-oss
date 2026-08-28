"""Workspace lock for an installed Blueprint (ADR 0014 F5 / INF-577).

Records what install wrote so re-install can no-op and uninstall can
remove only Blueprint-owned schema, skills, and sample rows.

The lock is tenant-scoped (ontology is tenant-scoped). Sample subjects
are KG-scoped and listed explicitly. This PR ships an in-memory store
only — process restart and multi-task ECS lose the pin (inspect 404s,
re-install is not a no-op, uninstall cannot find what install wrote).
A durable backend (same pattern as the skill store) is a leftover.

Boundary: OSS. ``infona_client.*`` / stdlib only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

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


class InMemoryBlueprintLockStore:
    """Zero-config default. Keyed ``(tenant_id, blueprint_id)`` — no cross-tenant read."""

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


_store: Optional[BlueprintLockStore] = None


def make_blueprint_lock_store() -> BlueprintLockStore:
    """Process singleton. Always in-memory in this PR (durable store leftover)."""
    global _store
    if _store is None:
        _store = InMemoryBlueprintLockStore()
    return _store


def reset_blueprint_lock_store() -> None:
    """Test helper — drop the memoized store."""
    global _store
    _store = None


__all__ = [
    "BlueprintLock",
    "BlueprintLockStore",
    "InMemoryBlueprintLockStore",
    "make_blueprint_lock_store",
    "reset_blueprint_lock_store",
    "_now",
]
