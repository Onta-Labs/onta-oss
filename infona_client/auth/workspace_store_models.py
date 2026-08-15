"""Records, errors, and the WorkspaceStore protocol.

Implementation siblings: ``workspace_store_memory`` / ``workspace_store_postgres``.
Public names stay importable from :mod:`infona_client.auth.workspace_store`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol, Sequence

#: Env flag gating the ownership 403 on ``POST /v1/me/tenants`` (see
#: :func:`ownership_enforced`). Default off — flipped only after the premium
#: backfill has seeded the registry (rollout step 3).
OWNERSHIP_ENFORCE_ENV = "INFONA_WORKSPACE_ENFORCE_OWNERSHIP"

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
    #: ``owner`` | ``writer`` | ``reader`` (legacy ``member`` = writer).
    role: str
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

    async def set_workspace_label(self, tenant_id: str, label: str) -> bool:
        """Update the registry's display label. Returns False if unregistered.

        The label here is the one invite emails render; each user's own copy
        lives on their identity profile. Only an owner rename updates this.
        """
        ...

    # -- members --
    async def get_member(
        self, tenant_id: str, subject: str
    ) -> Optional[WorkspaceMember]: ...

    async def list_members(self, tenant_id: str) -> list[WorkspaceMember]: ...

    async def add_member(
        self, tenant_id: str, subject: str, role: str = "writer"
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
