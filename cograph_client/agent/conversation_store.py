"""Conversation stores for the unified Ask-AI agent (COG-130).

The agent classifies one message per turn. For multi-turn dialogues — most
importantly a ``clarify`` round followed by the user's answer — the classifier
and the capabilities must see the WHOLE conversation, not just the latest
message in isolation. Without that, an under-specified answer like "I wanna do
both" looks ambiguous on its own and the agent re-asks the same question
forever (the COG-130 infinite-clarify loop).

The frontend already threads a stable ``session_id`` into every request, so the
backend owns conversation state keyed by that id — keeping the clients thin (the
webapp/CLI/MCP all converge for free, no per-client transcript plumbing; see
the interface-convergence note in CLAUDE.md / COG-128).

This mirrors :mod:`cograph_client.agent.plan_store` exactly:

- ``ConversationStore`` — an async Protocol so the backend is swappable.
- ``InMemoryConversationStore`` — the zero-config default; non-durable.
- ``PostgresConversationStore`` — durable + shared across ECS tasks over a
  generic Postgres DSN (``settings.database_url``), so a clarify on one task and
  the answer on another still share history. Vendor-neutral: a plain DSN, no
  cloud-provider identifiers.
- ``make_conversation_store()`` — Postgres when ``settings.database_url`` is set,
  else in-memory.

Only the rolling tail of a conversation is kept (``_MAX_TURNS``) — enough to
ground classification without unbounded growth.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from cograph_client.config import settings

# Keep the rolling tail of a dialogue: enough turns to carry a clarify→answer
# exchange (and a little more) without the prompt — or the stored payload —
# growing without bound.
_MAX_TURNS = 20


@dataclass
class Turn:
    """One conversational turn — a user message or an assistant response.

    ``kind``/``intent`` are recorded for assistant turns so the convergence
    guard can count prior ``clarify`` rounds and the classifier prompt can avoid
    re-asking an already-answered dimension.
    """

    role: str  # "user" | "assistant"
    text: str
    kind: Optional[str] = None  # assistant: answer | clarify | plan | result
    intent: Optional[str] = None  # assistant: the chosen intent(s), joined

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Turn":
        return cls(
            role=d.get("role", ""),
            text=d.get("text", ""),
            kind=d.get("kind"),
            intent=d.get("intent"),
        )


@dataclass
class Conversation:
    """A persisted, tenant-scoped rolling transcript keyed by ``session_id``."""

    session_id: str
    tenant_id: str
    turns: list[Turn] = field(default_factory=list)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_json(self) -> str:
        return json.dumps(
            {
                "session_id": self.session_id,
                "tenant_id": self.tenant_id,
                "turns": [t.to_dict() for t in self.turns],
                "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            }
        )

    @classmethod
    def from_payload(cls, payload: Any) -> "Conversation":
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode()
        data = json.loads(payload) if isinstance(payload, str) else payload
        updated_raw = data.get("updated_at")
        updated = (
            datetime.fromisoformat(updated_raw)
            if updated_raw
            else datetime.now(timezone.utc)
        )
        return cls(
            session_id=data["session_id"],
            tenant_id=data.get("tenant_id", ""),
            turns=[Turn.from_dict(t) for t in data.get("turns", [])],
            updated_at=updated,
        )


class ConversationStore(Protocol):
    async def load(self, session_id: str, tenant_id: str) -> list[Turn]: ...
    async def append(
        self, session_id: str, tenant_id: str, turns: list[Turn]
    ) -> None: ...


def _trim(turns: list[Turn]) -> list[Turn]:
    """Keep only the most recent ``_MAX_TURNS`` (oldest-first ordering)."""
    return turns[-_MAX_TURNS:] if len(turns) > _MAX_TURNS else turns


class InMemoryConversationStore:
    """Tenant-scoped in-memory transcript store — the zero-config default.

    Mirrors :class:`~cograph_client.agent.plan_store.InMemoryPlanStore`: an
    ``asyncio.Lock`` guards the dict and reads return copies so a caller can't
    mutate stored state by reference. Transcripts do not survive a process
    restart; use :class:`PostgresConversationStore` for durability.
    """

    def __init__(self) -> None:
        self._convos: dict[tuple[str, str], list[Turn]] = {}
        self._lock = asyncio.Lock()

    async def load(self, session_id: str, tenant_id: str) -> list[Turn]:
        if not session_id:
            return []
        async with self._lock:
            turns = self._convos.get((tenant_id, session_id), [])
            return [Turn.from_dict(t.to_dict()) for t in turns]

    async def append(
        self, session_id: str, tenant_id: str, turns: list[Turn]
    ) -> None:
        if not session_id or not turns:
            return
        async with self._lock:
            existing = self._convos.get((tenant_id, session_id), [])
            self._convos[(tenant_id, session_id)] = _trim([*existing, *turns])


class PostgresConversationStore:
    """Durable ``ConversationStore`` over a generic Postgres DSN via asyncpg.

    The full rolling transcript is serialized to a ``payload`` jsonb column; the
    pool + table are created lazily on first use (idempotent
    ``CREATE TABLE IF NOT EXISTS``) so importing/constructing never touches the
    network. Vendor-neutral by construction — the only configuration is a plain
    DSN; no cloud-provider ARNs, account ids, or hostnames live here.
    """

    _TABLE = "cograph_conversations"

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn if dsn is not None else settings.database_url
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is not None:
                return self._pool
            import asyncpg  # imported lazily so the dependency is optional

            pool = await asyncpg.create_pool(dsn=self._dsn)
            async with pool.acquire() as conn:
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._TABLE} (
                        session_id text NOT NULL,
                        tenant_id text NOT NULL,
                        updated_at timestamptz,
                        payload jsonb NOT NULL,
                        PRIMARY KEY (tenant_id, session_id)
                    )
                    """
                )
            self._pool = pool
            return self._pool

    async def load(self, session_id: str, tenant_id: str) -> list[Turn]:
        if not session_id:
            return []
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT payload FROM {self._TABLE} "
                f"WHERE tenant_id = $1 AND session_id = $2",
                tenant_id,
                session_id,
            )
        if row is None:
            return []
        return Conversation.from_payload(row["payload"]).turns

    async def append(
        self, session_id: str, tenant_id: str, turns: list[Turn]
    ) -> None:
        if not session_id or not turns:
            return
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            # Read-modify-write under a row lock so concurrent turns on the same
            # session don't clobber each other's appends.
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"SELECT payload FROM {self._TABLE} "
                    f"WHERE tenant_id = $1 AND session_id = $2 FOR UPDATE",
                    tenant_id,
                    session_id,
                )
                existing = (
                    Conversation.from_payload(row["payload"]).turns if row else []
                )
                convo = Conversation(
                    session_id=session_id,
                    tenant_id=tenant_id,
                    turns=_trim([*existing, *turns]),
                    updated_at=datetime.now(timezone.utc),
                )
                await conn.execute(
                    f"""
                    INSERT INTO {self._TABLE}
                        (session_id, tenant_id, updated_at, payload)
                    VALUES ($1, $2, $3, $4::jsonb)
                    ON CONFLICT (tenant_id, session_id) DO UPDATE SET
                        updated_at = EXCLUDED.updated_at,
                        payload = EXCLUDED.payload
                    """,
                    session_id,
                    tenant_id,
                    convo.updated_at,
                    convo.to_json(),
                )


_store: Optional[InMemoryConversationStore] = None
_durable_store: Optional[PostgresConversationStore] = None


def make_conversation_store() -> ConversationStore:
    """Select the conversation-store backend from configuration.

    Returns a :class:`PostgresConversationStore` when ``settings.database_url``
    is set (durable, shared across ECS tasks), else an
    :class:`InMemoryConversationStore`. Both are process-level singletons so the
    durable backend owns one asyncpg pool per process (created lazily — calling
    this never touches the network). Mirrors
    :func:`cograph_client.agent.plan_store.make_plan_store`.
    """
    global _store, _durable_store
    if settings.database_url:
        if _durable_store is None:
            _durable_store = PostgresConversationStore()
        return _durable_store
    if _store is None:
        _store = InMemoryConversationStore()
    return _store


def reset_conversation_store() -> None:
    """Test helper — clear both singletons."""
    global _store, _durable_store
    _store = None
    _durable_store = None


__all__ = [
    "Conversation",
    "ConversationStore",
    "InMemoryConversationStore",
    "PostgresConversationStore",
    "Turn",
    "make_conversation_store",
    "reset_conversation_store",
]
