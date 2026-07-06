"""Workspace registry + invite store and provider protocols (ONTA-227).

Workspaces (API term: tenants) have historically been single-user by
construction: a slug in one user's identity-provider profile, shared by telling
someone the slug so they self-add it via ``POST /v1/me/tenants``. That mechanism
is also a security hole — there is no global registry of workspace ownership, so
any user can add any non-reserved workspace id and silently gain full access.
This module introduces the durable registry that closes the hole and carries
workspace membership + email invites:

- ``workspaces`` — who owns a slug (``owner_subject`` is authoritative).
- ``workspace_members`` — who belongs to it (the ``role='owner'`` row is a
  projection of ``owner_subject``, written only by :meth:`claim_workspace`,
  never independently).
- ``workspace_invites`` — pending/settled email invites. The accept token is
  stored as a sha256 hash only; the raw token is returned once at create time
  and never persisted. ``expired`` is computed at read time from ``expires_at``
  (no sweeper in v1); the partial unique index on ``(tenant_id, email) WHERE
  status = 'pending'`` — not app-level checks — is the guard against two
  concurrent creates both landing.

Auth stays on the identity provider (Clerk/WorkOS/...) exactly as today: the
registry is a *second* book beside it (design "Approach A"). Accept/removal
dual-write both; the grant (auth truth) goes first so a half-completed write
fails closed, and every step is idempotent so retry is the repair path.

Two plugin protocols keep OSS importable standalone with zero identity-vendor
dependency, mirroring ``tenant_directory``/``register_external_verifier``:

- :class:`TenantGrantProvider` — subject-scoped grant/revoke. The existing
  ``TenantProvider`` is strictly caller-key-scoped (it edits the CALLER's own
  tenant list); invite accept and owner-removal must edit ANOTHER user's
  grants, which only an identity integration can do.
- :class:`InviteDeliveryProvider` — email→subject lookup, verified emails,
  display profiles, and sign-up invitation email delivery. Without one, invite
  creation still works (the owner copy-pastes the accept link) but email
  matching is absent and token accept degrades to token-possession semantics —
  the link IS the credential (still single-use, expiring, revocable).

OSS ships NO implementations of either protocol; the premium identity
integration registers both.

Ownership enforcement (the 403 on someone else's slug) is deliberately gated on
BOTH a durable store and ``COGRAPH_WORKSPACE_ENFORCE_OWNERSHIP=1``: an
in-memory registry that forgets owners on restart would silently re-run
first-claim-wins, which is worse than not pretending. Rollout is deploy
(writes on, flag off) → backfill → flip the flag, so lazy-claim only ever
applies to genuinely new ids.
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

import structlog

from cograph_client.auth.api_keys import AuthVerdict, get_external_verifier
from cograph_client.config import settings
from cograph_client.db.pool import get_pg_pool

logger = structlog.stdlib.get_logger("cograph.auth.workspace_store")

#: Env flag gating the ownership 403 on ``POST /v1/me/tenants`` (see
#: :func:`ownership_enforced`). Default off — flipped only after the premium
#: backfill has seeded the registry (rollout step 3).
OWNERSHIP_ENFORCE_ENV = "COGRAPH_WORKSPACE_ENFORCE_OWNERSHIP"

#: Invite validity window. 30 days, matching Clerk sign-up invitation validity —
#: a live email link pointing at an expired row is a support ticket.
INVITE_TTL_DAYS = 30

#: Cheap abuse brake: max stored-``pending`` invites per workspace.
PENDING_INVITE_CAP = 50

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class Workspace:
    tenant_id: str
    owner_subject: str
    label: str
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class WorkspaceMember:
    tenant_id: str
    subject: str
    role: str  # "owner" | "member"
    joined_at: datetime = field(default_factory=_utcnow)


@dataclass
class WorkspaceInvite:
    id: str
    tenant_id: str
    email: str  # lowercased at write
    role: str
    status: str  # stored status; see effective_status() for read-time expiry
    token_hash: str  # sha256 hex of the accept token; raw token never stored
    invited_by: str
    created_at: datetime = field(default_factory=_utcnow)
    expires_at: datetime = field(
        default_factory=lambda: _utcnow() + timedelta(days=INVITE_TTL_DAYS)
    )
    # Vendor-neutral name for the identity provider's sign-up invitation id
    # (Clerk invitation id in the hosted product); set when an email was sent,
    # used for best-effort revoke.
    signup_invitation_id: Optional[str] = None
    accepted_by: Optional[str] = None


def effective_status(invite: WorkspaceInvite, now: Optional[datetime] = None) -> str:
    """The read-time status: a stored-``pending`` invite past ``expires_at`` is
    ``expired``. No sweeper mutates rows in v1; every reader goes through this."""
    now = now or _utcnow()
    if invite.status == "pending" and invite.expires_at <= now:
        return "expired"
    return invite.status


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkspaceError(Exception):
    """A client-facing failure carrying an HTTP status, mirroring
    ``TenantProviderError`` so routes can translate without knowing internals.
    ``detail`` may be a dict (e.g. the duplicate-invite 409 carries the
    existing invite id)."""

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


class DuplicatePendingInviteError(Exception):
    """A pending invite already exists for (tenant_id, email).

    Raised by the store when the pending-uniqueness constraint fires;
    ``invite_id`` is the existing pending invite's id when it could be
    resolved (the route surfaces it in the 409 body).
    """

    def __init__(self, invite_id: Optional[str]):
        self.invite_id = invite_id
        super().__init__("an invite for this email is already pending")


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------


class WorkspaceStore(Protocol):
    """Async workspace registry / membership / invite store.

    ``durable`` distinguishes the Postgres backend from the in-memory dev
    fallback — ownership enforcement refuses to run on a non-durable store
    (see :func:`ownership_enforced`).
    """

    durable: bool

    # -- workspaces --
    async def get_workspace(self, tenant_id: str) -> Optional[Workspace]: ...

    async def claim_workspace(
        self, tenant_id: str, owner_subject: str, label: str
    ) -> Optional[Workspace]:
        """Atomically claim an unregistered id (INSERT .. ON CONFLICT DO
        NOTHING + returned-row check). Returns the new row iff THIS call won
        the claim; None when the id was already registered (by anyone,
        including the caller). Also writes the owner's membership-row
        projection — the single write path for ``role='owner'`` rows."""
        ...

    # -- members --
    async def get_member(
        self, tenant_id: str, subject: str
    ) -> Optional[WorkspaceMember]: ...

    async def list_members(self, tenant_id: str) -> list[WorkspaceMember]: ...

    async def add_member(
        self, tenant_id: str, subject: str, role: str = "member"
    ) -> None:
        """Idempotent membership upsert (an existing row keeps its role)."""
        ...

    async def remove_member(self, tenant_id: str, subject: str) -> bool:
        """Delete the membership row. Returns False when no row existed —
        callers deliberately tolerate that (accept-limbo repair)."""
        ...

    # -- invites --
    async def create_invite(self, invite: WorkspaceInvite) -> WorkspaceInvite:
        """Insert a pending invite. Raises :class:`DuplicatePendingInviteError`
        when a pending invite for (tenant_id, email) already exists — the
        uniqueness constraint, not an app-level pre-check, is the guard."""
        ...

    async def get_invite(self, invite_id: str) -> Optional[WorkspaceInvite]: ...

    async def get_invite_by_token_hash(
        self, token_hash: str
    ) -> Optional[WorkspaceInvite]: ...

    async def list_invites(self, tenant_id: str) -> list[WorkspaceInvite]:
        """Stored-``pending`` invites for a workspace, newest first (rows past
        expiry included — readers render them via :func:`effective_status`)."""
        ...

    async def list_invites_for_emails(
        self, emails: Sequence[str]
    ) -> list[WorkspaceInvite]:
        """Pending, unexpired invites addressed to any of ``emails``."""
        ...

    async def count_pending(self, tenant_id: str) -> int:
        """Stored-``pending`` rows that are still unexpired at read time.
        Expired-at-read rows are excluded so the per-workspace invite cap
        cannot be bricked by 50 stale invites (they still hold the
        pending-uniqueness slot until :meth:`mark_expired` frees it)."""
        ...

    async def mark_accepted(self, invite_id: str, subject: str) -> bool:
        """Single-use compare-and-set: pending + unexpired → accepted.
        Returns False when the invite was not in that state."""
        ...

    async def mark_declined(self, invite_id: str) -> bool: ...

    async def mark_revoked(self, invite_id: str) -> bool: ...

    async def mark_expired(self, invite_id: str) -> bool:
        """Persist a read-time-computed expiry (pending → expired), freeing the
        pending-uniqueness slot so the owner can re-invite the same email."""
        ...

    async def set_signup_invitation_id(
        self, invite_id: str, invitation_id: str
    ) -> None: ...


# ---------------------------------------------------------------------------
# In-memory store (zero-config dev default; non-durable)
# ---------------------------------------------------------------------------


class InMemoryWorkspaceStore:
    """Dict-backed :class:`WorkspaceStore` — dev/test only, forgets on restart.

    Uses a ``threading.Lock`` (not ``asyncio.Lock``) on purpose: the critical
    sections are pure dict operations with no awaits, and a threading lock is
    loop-agnostic — the singleton survives being touched from different event
    loops (TestClient portals, ``asyncio.run`` seeding in tests) where an
    asyncio primitive would bind to its first loop and raise.
    """

    durable = False

    def __init__(self) -> None:
        self._workspaces: dict[str, Workspace] = {}
        self._members: dict[tuple[str, str], WorkspaceMember] = {}
        self._invites: dict[str, WorkspaceInvite] = {}
        self._lock = threading.Lock()

    # -- workspaces --

    async def get_workspace(self, tenant_id: str) -> Optional[Workspace]:
        with self._lock:
            ws = self._workspaces.get(tenant_id)
            return replace(ws) if ws else None

    async def claim_workspace(
        self, tenant_id: str, owner_subject: str, label: str
    ) -> Optional[Workspace]:
        with self._lock:
            if tenant_id in self._workspaces:
                return None
            ws = Workspace(
                tenant_id=tenant_id, owner_subject=owner_subject, label=label
            )
            self._workspaces[tenant_id] = ws
            self._members.setdefault(
                (tenant_id, owner_subject),
                WorkspaceMember(
                    tenant_id=tenant_id, subject=owner_subject, role="owner"
                ),
            )
            return replace(ws)

    # -- members --

    async def get_member(
        self, tenant_id: str, subject: str
    ) -> Optional[WorkspaceMember]:
        with self._lock:
            m = self._members.get((tenant_id, subject))
            return replace(m) if m else None

    async def list_members(self, tenant_id: str) -> list[WorkspaceMember]:
        with self._lock:
            members = [
                replace(m) for m in self._members.values() if m.tenant_id == tenant_id
            ]
        return sorted(members, key=lambda m: m.joined_at)

    async def add_member(
        self, tenant_id: str, subject: str, role: str = "member"
    ) -> None:
        with self._lock:
            self._members.setdefault(
                (tenant_id, subject),
                WorkspaceMember(tenant_id=tenant_id, subject=subject, role=role),
            )

    async def remove_member(self, tenant_id: str, subject: str) -> bool:
        with self._lock:
            return self._members.pop((tenant_id, subject), None) is not None

    # -- invites --

    async def create_invite(self, invite: WorkspaceInvite) -> WorkspaceInvite:
        with self._lock:
            for existing in self._invites.values():
                if (
                    existing.tenant_id == invite.tenant_id
                    and existing.email == invite.email
                    and existing.status == "pending"
                ):
                    raise DuplicatePendingInviteError(existing.id)
            self._invites[invite.id] = replace(invite)
            return replace(invite)

    async def get_invite(self, invite_id: str) -> Optional[WorkspaceInvite]:
        with self._lock:
            inv = self._invites.get(invite_id)
            return replace(inv) if inv else None

    async def get_invite_by_token_hash(
        self, token_hash: str
    ) -> Optional[WorkspaceInvite]:
        with self._lock:
            for inv in self._invites.values():
                if inv.token_hash == token_hash:
                    return replace(inv)
        return None

    async def list_invites(self, tenant_id: str) -> list[WorkspaceInvite]:
        with self._lock:
            invites = [
                replace(i)
                for i in self._invites.values()
                if i.tenant_id == tenant_id and i.status == "pending"
            ]
        return sorted(invites, key=lambda i: i.created_at, reverse=True)

    async def list_invites_for_emails(
        self, emails: Sequence[str]
    ) -> list[WorkspaceInvite]:
        wanted = {e.strip().lower() for e in emails if e}
        now = _utcnow()
        with self._lock:
            invites = [
                replace(i)
                for i in self._invites.values()
                if i.email in wanted
                and i.status == "pending"
                and i.expires_at > now
            ]
        return sorted(invites, key=lambda i: i.created_at, reverse=True)

    async def count_pending(self, tenant_id: str) -> int:
        now = _utcnow()
        with self._lock:
            return sum(
                1
                for i in self._invites.values()
                if i.tenant_id == tenant_id
                and i.status == "pending"
                and i.expires_at > now
            )

    async def mark_accepted(self, invite_id: str, subject: str) -> bool:
        now = _utcnow()
        with self._lock:
            inv = self._invites.get(invite_id)
            if inv is None or inv.status != "pending" or inv.expires_at <= now:
                return False
            inv.status = "accepted"
            inv.accepted_by = subject
            return True

    async def mark_declined(self, invite_id: str) -> bool:
        return self._transition(invite_id, "declined")

    async def mark_revoked(self, invite_id: str) -> bool:
        return self._transition(invite_id, "revoked")

    async def mark_expired(self, invite_id: str) -> bool:
        return self._transition(invite_id, "expired")

    def _transition(self, invite_id: str, to_status: str) -> bool:
        with self._lock:
            inv = self._invites.get(invite_id)
            if inv is None or inv.status != "pending":
                return False
            inv.status = to_status
            return True

    async def set_signup_invitation_id(
        self, invite_id: str, invitation_id: str
    ) -> None:
        with self._lock:
            inv = self._invites.get(invite_id)
            if inv is not None:
                inv.signup_invitation_id = invitation_id


# ---------------------------------------------------------------------------
# Postgres store (durable; DDL on first use via the shared pool)
# ---------------------------------------------------------------------------


class PostgresWorkspaceStore:
    """Durable :class:`WorkspaceStore` over the shared asyncpg pool
    (``db/pool.py`` — never a private pool). Tables are created lazily on
    first use, matching the other durable stores (no migration step).
    Vendor-neutral by construction: a plain DSN, no cloud identifiers.
    """

    durable = True

    _WORKSPACES = "cograph_workspaces"
    _MEMBERS = "cograph_workspace_members"
    _INVITES = "cograph_workspace_invites"

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
                    role text NOT NULL CHECK (role IN ('owner','member')),
                    joined_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, subject)
                )
                """
            )
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._INVITES} (
                    id uuid PRIMARY KEY,
                    tenant_id text NOT NULL,
                    email text NOT NULL,
                    role text NOT NULL DEFAULT 'member',
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
        self, tenant_id: str, subject: str, role: str = "member"
    ) -> None:
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


# ---------------------------------------------------------------------------
# Factory (mirrors make_conversation_store)
# ---------------------------------------------------------------------------


_memory_store: Optional[InMemoryWorkspaceStore] = None
_durable_store: Optional[PostgresWorkspaceStore] = None


def make_workspace_store() -> WorkspaceStore:
    """Select the workspace-store backend from configuration.

    :class:`PostgresWorkspaceStore` when ``settings.database_url`` is set
    (durable, shared across tasks), else :class:`InMemoryWorkspaceStore`
    (dev-only: rows are forgotten on restart — the owner re-adds their
    workspace and re-invites, and ownership enforcement stays off). Both are
    process-level singletons; construction never touches the network.
    """
    global _memory_store, _durable_store
    if settings.database_url:
        if _durable_store is None:
            _durable_store = PostgresWorkspaceStore()
        return _durable_store
    if _memory_store is None:
        _memory_store = InMemoryWorkspaceStore()
    return _memory_store


def reset_workspace_store() -> None:
    """Test helper — clear both singletons."""
    global _memory_store, _durable_store
    _memory_store = None
    _durable_store = None


# ---------------------------------------------------------------------------
# Ownership enforcement gate
# ---------------------------------------------------------------------------


def _enforce_flag() -> bool:
    return os.environ.get(OWNERSHIP_ENFORCE_ENV, "0") == "1"


def ownership_enforced(store: WorkspaceStore) -> bool:
    """Whether ``POST /v1/me/tenants`` should 403 on someone else's slug.

    Requires BOTH the env flag and a durable store: an in-memory registry that
    forgets owners on restart would silently re-run first-claim-wins, which is
    worse than not pretending. Read per-request so the flag can be flipped
    without code changes (rollout step 3).
    """
    return _enforce_flag() and bool(getattr(store, "durable", False))


def log_workspace_registry_mode() -> None:
    """Log the registry's operating mode once at app startup — the degraded
    modes are deliberate, but they must be visible, not silent."""
    durable = bool(settings.database_url)
    if _enforce_flag() and durable:
        logger.info("workspace_ownership_enforced")
    elif _enforce_flag() and not durable:
        logger.warning(
            "workspace_ownership_degraded",
            reason=(
                f"{OWNERSHIP_ENFORCE_ENV}=1 but no durable store "
                "(OMNIX_DATABASE_URL unset) — the ownership 403 is OFF and the "
                "in-memory registry forgets owners on restart"
            ),
        )
    else:
        logger.info(
            "workspace_ownership_not_enforced",
            hint=(
                f"set {OWNERSHIP_ENFORCE_ENV}=1 with a durable store to close "
                "the workspace self-add hole (after the registry backfill)"
            ),
        )


# ---------------------------------------------------------------------------
# Provider protocols + registration (OSS ships NO implementations)
# ---------------------------------------------------------------------------


@runtime_checkable
class TenantGrantProvider(Protocol):
    """Subject-scoped tenant grant/revoke — the piece ``TenantProvider`` cannot do.

    The existing ``TenantProvider`` is strictly caller-key-scoped (it resolves
    the CALLER's subject and edits the CALLER's list). Invite accept and
    owner-removal must edit ANOTHER user's grants, so they need these. The
    premium impl wraps the identity provider's metadata write path, so cache
    invalidation comes free. Both methods MUST be idempotent — retry after a
    half-completed dual-write is the repair path.
    """

    def grant(self, subject: str, tenant_id: str, label: str) -> None: ...

    def revoke(self, subject: str, tenant_id: str) -> None: ...


@runtime_checkable
class InviteDeliveryProvider(Protocol):
    """Email↔subject resolution + sign-up invitation delivery.

    Registered by the premium identity integration. Without one, invite
    creation still works (link-only) but ``GET /v1/me/invites`` and in-app
    accept/decline report 501, and token accept degrades to token-possession
    semantics (documented in the module docstring).
    """

    def lookup_subject_by_email(self, email: str) -> Optional[str]: ...

    def emails_for_subject(self, subject: str) -> list[str]:
        """The subject's VERIFIED emails only — this is the accept/decline
        authorization oracle, so unverified addresses must not appear."""
        ...

    def display_profile(self, subject: str) -> Optional[dict]:
        """``{"email": ..., "name": ...}`` for members-list decoration."""
        ...

    def send_signup_invitation(
        self, email: str, redirect_url: str, metadata: dict
    ) -> Optional[str]:
        """Send a sign-up invitation email via the identity provider. Returns
        the provider's invitation id (stored for best-effort revoke), or None
        when no email was sent."""
        ...

    def revoke_signup_invitation(self, invitation_id: str) -> bool:
        """Best-effort revoke of a previously sent sign-up invitation."""
        ...


_grant_provider: Optional[TenantGrantProvider] = None
_delivery_provider: Optional[InviteDeliveryProvider] = None


def register_tenant_grant_provider(
    provider: Optional[TenantGrantProvider],
) -> None:
    """Register (or clear) the tenant-grant provider. Pass None to clear."""
    global _grant_provider
    _grant_provider = provider


def get_tenant_grant_provider() -> Optional[TenantGrantProvider]:
    return _grant_provider


def register_invite_delivery_provider(
    provider: Optional[InviteDeliveryProvider],
) -> None:
    """Register (or clear) the invite-delivery provider. Pass None to clear."""
    global _delivery_provider
    _delivery_provider = provider


def get_invite_delivery_provider() -> Optional[InviteDeliveryProvider]:
    return _delivery_provider


# ---------------------------------------------------------------------------
# Subject resolution
# ---------------------------------------------------------------------------


def require_subject(api_key: Optional[str]) -> str:
    """Resolve the auth subject (user id) behind ``api_key``, or raise.

    Deliberately independent of :func:`~cograph_client.auth.api_keys.get_tenant`:
    that path 403/401s on tenant grants, but a brand-new user accepting their
    first workspace invite has ZERO tenants yet — their key must still resolve
    a subject. Keys that carry no subject (static ``OMNIX_API_KEYS`` entries,
    legacy verdicts, no-auth dev mode) get 403 — invites require user-scoped
    auth. Unknown keys stay 401.
    """
    verifier = get_external_verifier()
    if verifier is None:
        # No identity integration ⇒ no subjects exist in this deployment.
        raise WorkspaceError(403, "invites require user-scoped auth")
    if not api_key:
        raise WorkspaceError(401, "Not authenticated")
    keys_map = settings.get_api_keys_map()
    if keys_map.get(api_key) is not None:
        # Static keys are valid but anonymous — they cannot own or accept.
        raise WorkspaceError(403, "invites require user-scoped auth")
    try:
        verdict = verifier(api_key)
    except Exception:  # noqa: BLE001 — fail closed, same as get_tenant
        logger.warning("workspace_subject_verifier_failed", exc_info=True)
        verdict = None
    if isinstance(verdict, AuthVerdict) and verdict.subject:
        return verdict.subject
    if verdict is not None:
        raise WorkspaceError(403, "invites require user-scoped auth")
    raise WorkspaceError(401, "Invalid API key")


def resolve_subject(api_key: Optional[str]) -> Optional[str]:
    """Quiet variant of :func:`require_subject` — None instead of raising.

    Used where a missing subject must NOT fail the request (the tenant create
    route keeps today's behavior for static/anonymous keys).
    """
    try:
        return require_subject(api_key)
    except WorkspaceError:
        return None


__all__ = [
    "DuplicatePendingInviteError",
    "INVITE_TTL_DAYS",
    "InMemoryWorkspaceStore",
    "InviteDeliveryProvider",
    "OWNERSHIP_ENFORCE_ENV",
    "PENDING_INVITE_CAP",
    "PostgresWorkspaceStore",
    "TenantGrantProvider",
    "Workspace",
    "WorkspaceError",
    "WorkspaceInvite",
    "WorkspaceMember",
    "WorkspaceStore",
    "effective_status",
    "get_invite_delivery_provider",
    "get_tenant_grant_provider",
    "log_workspace_registry_mode",
    "make_workspace_store",
    "ownership_enforced",
    "register_invite_delivery_provider",
    "register_tenant_grant_provider",
    "require_subject",
    "resolve_subject",
    "reset_workspace_store",
]
