"""Durable Postgres WorkspaceStore over the shared asyncpg pool."""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from typing import Any, Optional, Sequence

from infona_client.auth.capabilities import normalize_role
from infona_client.auth.workspace_store_models import (
    DuplicatePendingInviteError,
    Workspace,
    WorkspaceInvite,
    WorkspaceMember,
)
from infona_client.config import settings
from infona_client.db.pool import get_pg_pool


class PostgresWorkspaceStore:
    """Durable :class:`WorkspaceStore` over the shared asyncpg pool
    (``db/pool.py`` — never a private pool). Tables are created lazily on
    first use, matching the other durable stores (no migration step).
    Vendor-neutral by construction: a plain DSN, no cloud identifiers.
    """

    durable = True

    _WORKSPACES = "infona_workspaces"
    _MEMBERS = "infona_workspace_members"
    _INVITES = "infona_workspace_invites"

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn if dsn is not None else settings.database_url
        self._schema_ready = False
        self._schema_lock = threading.Lock()  # cheap re-entry guard; DDL is idempotent

    async def _conn_pool(self) -> Any:
        pool = await get_pg_pool(self._dsn)
        if not self._schema_ready:
            await self._ensure_schema(pool)
        return pool

    async def _ensure_schema(self, pool: Any) -> None:
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._WORKSPACES} (
                    tenant_id text PRIMARY KEY,
                    owner_subject text NOT NULL,
                    label text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._MEMBERS} (
                    tenant_id text NOT NULL,
                    subject text NOT NULL,
                    role text NOT NULL,
                    joined_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, subject)
                )
                """
            )
            # Widen legacy CHECK (owner|member) so writer/reader may land.
            # CREATE TABLE IF NOT EXISTS never rewrites an existing constraint.
            await conn.execute(
                f"ALTER TABLE {self._MEMBERS} "
                f"DROP CONSTRAINT IF EXISTS {self._MEMBERS}_role_check"
            )
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._INVITES} (
                    id uuid PRIMARY KEY,
                    tenant_id text NOT NULL,
                    email text NOT NULL,
                    role text NOT NULL DEFAULT 'writer',
                    status text NOT NULL CHECK (status IN
                        ('pending','accepted','revoked','declined','expired')),
                    token_hash text NOT NULL,
                    invited_by text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    expires_at timestamptz NOT NULL,
                    signup_invitation_id text,
                    accepted_by text
                )
                """
            )
            # Default for brand-new tables was already 'writer'; older DBs still
            # default to 'member' which normalize_role maps to writer.
            # Duplicate-pending prevention at the constraint level — two
            # concurrent creates must not both land.
            await conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {self._INVITES}_pending_uq "
                f"ON {self._INVITES} (tenant_id, email) WHERE status = 'pending'"
            )
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self._INVITES}_token_idx "
                f"ON {self._INVITES} (token_hash)"
            )
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self._INVITES}_email_idx "
                f"ON {self._INVITES} (email) WHERE status = 'pending'"
            )
        with self._schema_lock:
            self._schema_ready = True

    @staticmethod
    def _ws(row: Any) -> Workspace:
        return Workspace(
            tenant_id=row["tenant_id"],
            owner_subject=row["owner_subject"],
            label=row["label"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _member(row: Any) -> WorkspaceMember:
        return WorkspaceMember(
            tenant_id=row["tenant_id"],
            subject=row["subject"],
            role=row["role"],
            joined_at=row["joined_at"],
        )

    @staticmethod
    def _invite(row: Any) -> WorkspaceInvite:
        return WorkspaceInvite(
            id=str(row["id"]),
            tenant_id=row["tenant_id"],
            email=row["email"],
            role=row["role"],
            status=row["status"],
            token_hash=row["token_hash"],
            invited_by=row["invited_by"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            signup_invitation_id=row["signup_invitation_id"],
            accepted_by=row["accepted_by"],
        )

    # -- workspaces --

    async def get_workspace(self, tenant_id: str) -> Optional[Workspace]:
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {self._WORKSPACES} WHERE tenant_id = $1", tenant_id
            )
        return self._ws(row) if row else None

    async def claim_workspace(
        self, tenant_id: str, owner_subject: str, label: str
    ) -> Optional[Workspace]:
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO {self._WORKSPACES} (tenant_id, owner_subject, label)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (tenant_id) DO NOTHING
                    RETURNING tenant_id, owner_subject, label, created_at
                    """,
                    tenant_id,
                    owner_subject,
                    label,
                )
                if row is None:
                    return None  # already registered — the returned-row check
                await conn.execute(
                    f"""
                    INSERT INTO {self._MEMBERS} (tenant_id, subject, role)
                    VALUES ($1, $2, 'owner')
                    ON CONFLICT (tenant_id, subject) DO NOTHING
                    """,
                    tenant_id,
                    owner_subject,
                )
        return self._ws(row)

    async def set_workspace_label(self, tenant_id: str, label: str) -> bool:
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE {self._WORKSPACES} SET label = $2 WHERE tenant_id = $1 "
                "RETURNING tenant_id",
                tenant_id,
                label,
            )
        return row is not None

    # -- members --

    async def get_member(
        self, tenant_id: str, subject: str
    ) -> Optional[WorkspaceMember]:
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {self._MEMBERS} WHERE tenant_id = $1 AND subject = $2",
                tenant_id,
                subject,
            )
        return self._member(row) if row else None

    async def list_members(self, tenant_id: str) -> list[WorkspaceMember]:
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {self._MEMBERS} WHERE tenant_id = $1 "
                f"ORDER BY joined_at",
                tenant_id,
            )
        return [self._member(r) for r in rows]

    async def add_member(
        self, tenant_id: str, subject: str, role: str = "writer"
    ) -> None:
        role = normalize_role(role)
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._MEMBERS} (tenant_id, subject, role)
                VALUES ($1, $2, $3)
                ON CONFLICT (tenant_id, subject) DO NOTHING
                """,
                tenant_id,
                subject,
                role,
            )

    async def remove_member(self, tenant_id: str, subject: str) -> bool:
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"DELETE FROM {self._MEMBERS} WHERE tenant_id = $1 AND subject = $2",
                tenant_id,
                subject,
            )
        return result == "DELETE 1"

    # -- invites --

    async def create_invite(self, invite: WorkspaceInvite) -> WorkspaceInvite:
        import asyncpg  # lazy — optional dependency, only needed with a DSN

        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    f"""
                    INSERT INTO {self._INVITES}
                        (id, tenant_id, email, role, status, token_hash,
                         invited_by, created_at, expires_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    uuid.UUID(invite.id),
                    invite.tenant_id,
                    invite.email,
                    invite.role,
                    invite.status,
                    invite.token_hash,
                    invite.invited_by,
                    invite.created_at,
                    invite.expires_at,
                )
            except asyncpg.UniqueViolationError:
                row = await conn.fetchrow(
                    f"SELECT id FROM {self._INVITES} "
                    f"WHERE tenant_id = $1 AND email = $2 AND status = 'pending'",
                    invite.tenant_id,
                    invite.email,
                )
                raise DuplicatePendingInviteError(str(row["id"]) if row else None)
        return replace(invite)

    async def get_invite(self, invite_id: str) -> Optional[WorkspaceInvite]:
        try:
            uid = uuid.UUID(invite_id)
        except (TypeError, ValueError):
            return None
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {self._INVITES} WHERE id = $1", uid
            )
        return self._invite(row) if row else None

    async def get_invite_by_token_hash(
        self, token_hash: str
    ) -> Optional[WorkspaceInvite]:
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {self._INVITES} WHERE token_hash = $1", token_hash
            )
        return self._invite(row) if row else None

    async def list_invites(self, tenant_id: str) -> list[WorkspaceInvite]:
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {self._INVITES} "
                f"WHERE tenant_id = $1 AND status = 'pending' "
                f"ORDER BY created_at DESC",
                tenant_id,
            )
        return [self._invite(r) for r in rows]

    async def list_invites_for_emails(
        self, emails: Sequence[str]
    ) -> list[WorkspaceInvite]:
        wanted = [e.strip().lower() for e in emails if e]
        if not wanted:
            return []
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {self._INVITES} "
                f"WHERE email = ANY($1::text[]) AND status = 'pending' "
                f"AND expires_at > now() "
                f"ORDER BY created_at DESC",
                wanted,
            )
        return [self._invite(r) for r in rows]

    async def count_pending(self, tenant_id: str) -> int:
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            n = await conn.fetchval(
                f"SELECT count(*) FROM {self._INVITES} "
                f"WHERE tenant_id = $1 AND status = 'pending' "
                f"AND expires_at > now()",
                tenant_id,
            )
        return int(n or 0)

    async def mark_accepted(self, invite_id: str, subject: str) -> bool:
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE {self._INVITES}
                SET status = 'accepted', accepted_by = $2
                WHERE id = $1 AND status = 'pending' AND expires_at > now()
                RETURNING id
                """,
                uuid.UUID(invite_id),
                subject,
            )
        return row is not None

    async def mark_declined(self, invite_id: str) -> bool:
        return await self._transition(invite_id, "declined")

    async def mark_revoked(self, invite_id: str) -> bool:
        return await self._transition(invite_id, "revoked")

    async def mark_expired(self, invite_id: str) -> bool:
        return await self._transition(invite_id, "expired")

    async def _transition(self, invite_id: str, to_status: str) -> bool:
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE {self._INVITES} SET status = $2 "
                f"WHERE id = $1 AND status = 'pending' RETURNING id",
                uuid.UUID(invite_id),
                to_status,
            )
        return row is not None

    async def set_signup_invitation_id(
        self, invite_id: str, invitation_id: str
    ) -> None:
        pool = await self._conn_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {self._INVITES} SET signup_invitation_id = $2 WHERE id = $1",
                uuid.UUID(invite_id),
                invitation_id,
            )

