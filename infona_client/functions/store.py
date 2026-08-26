"""Durable TENANT-layer function attachments for the Neo4j graph backend.

SPARQL ``register_function_triple`` is the Neptune/SPARQL writer. Local and
hosted Neo4j have no SPARQL update endpoint, so attachments live here — same
swappable store pattern as ``skills/store.py``. Keyed by
``(tenant_id, entity_type, name)``.

Boundary: OSS. ``infona_client.*`` / stdlib only.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

@dataclass
class StoredFunction:
    tenant_id: str
    name: str
    entity_type: str
    endpoint_url: str
    description: str = ""
    layer: str = "tenant"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class FunctionStore(Protocol):
    async def list_for_tenant(
        self, tenant_id: str, entity_type: Optional[str] = None
    ) -> list[StoredFunction]: ...

    async def upsert(self, rec: StoredFunction) -> StoredFunction: ...

    async def delete(
        self, tenant_id: str, entity_type: str, name: str
    ) -> bool: ...


class InMemoryFunctionStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], StoredFunction] = {}
        self._lock = asyncio.Lock()

    async def list_for_tenant(
        self, tenant_id: str, entity_type: Optional[str] = None
    ) -> list[StoredFunction]:
        async with self._lock:
            rows = [r for r in self._rows.values() if r.tenant_id == tenant_id]
        if entity_type:
            want = entity_type.casefold()
            rows = [r for r in rows if r.entity_type.casefold() == want]
        rows.sort(key=lambda r: (r.entity_type.casefold(), r.name.casefold()))
        return [copy.deepcopy(r) for r in rows]

    async def upsert(self, rec: StoredFunction) -> StoredFunction:
        now = datetime.now(timezone.utc)
        key = (rec.tenant_id, rec.entity_type.casefold(), rec.name.casefold())
        async with self._lock:
            existing = self._rows.get(key)
            stored = StoredFunction(
                tenant_id=rec.tenant_id,
                name=rec.name,
                entity_type=rec.entity_type,
                endpoint_url=rec.endpoint_url,
                description=rec.description,
                layer=rec.layer or "tenant",
                created_at=rec.created_at or (existing.created_at if existing else now),
                updated_at=now,
            )
            self._rows[key] = stored
            return copy.deepcopy(stored)

    async def delete(self, tenant_id: str, entity_type: str, name: str) -> bool:
        key = (tenant_id, entity_type.casefold(), name.casefold())
        async with self._lock:
            return self._rows.pop(key, None) is not None


_store: FunctionStore | None = None


def make_function_store() -> FunctionStore:
    global _store
    if _store is None:
        _store = InMemoryFunctionStore()
    return _store


def reset_function_store() -> None:
    global _store
    _store = None


__all__ = [
    "StoredFunction",
    "FunctionStore",
    "InMemoryFunctionStore",
    "make_function_store",
    "reset_function_store",
]
