"""In-memory WorkspaceStore (zero-config dev default; non-durable)."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Optional, Sequence

from infona_client.auth.capabilities import normalize_role
from infona_client.auth.workspace_store_models import (
    DuplicatePendingInviteError,
    Workspace,
    WorkspaceInvite,
    WorkspaceMember,
    _utcnow,
)


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

    async def set_workspace_label(self, tenant_id: str, label: str) -> bool:
        with self._lock:
            ws = self._workspaces.get(tenant_id)
            if ws is None:
                return False
            ws.label = label
            return True

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
        self, tenant_id: str, subject: str, role: str = "writer"
    ) -> None:
        role = normalize_role(role)
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

