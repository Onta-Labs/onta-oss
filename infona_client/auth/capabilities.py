"""Tenant-level access capability (read | write) — Infona membership v1.

Product model (tenant-scoped first; graph-level grants are a later phase):

* **owner** — full write + membership admin (invite/remove). Exactly one per
  workspace registry row (``workspaces.owner_subject``).
* **writer** — may mutate instance data and schema for the tenant; may not
  manage members. Legacy role name ``member`` normalizes to ``writer``.
* **reader** — may read / query / browse only; mutating routes return 403.

Capability is derived from role (``write`` ⊃ ``read``). Graph-level grants are
intentionally out of scope here.
"""

from __future__ import annotations

from typing import Literal

Capability = Literal["read", "write"]
MemberRole = Literal["owner", "writer", "reader"]

#: Roles that may appear on membership / invite rows after normalization.
MEMBER_ROLES: frozenset[str] = frozenset({"owner", "writer", "reader"})

#: Roles an owner may invite (never ``owner`` until ownership transfer exists).
INVITABLE_ROLES: frozenset[str] = frozenset({"writer", "reader"})


def normalize_role(role: str | None) -> MemberRole:
    """Map a free-form role string onto the canonical set.

    * ``member`` (legacy invite/membership value) → ``writer``
    * unknown / empty → ``writer`` (fail open for capability; invite routes
      still validate against :data:`INVITABLE_ROLES` before insert)
    """
    r = (role or "").strip().lower()
    if r == "member":
        return "writer"
    if r in MEMBER_ROLES:
        return r  # type: ignore[return-value]
    return "writer"


def capability_for_role(role: str | None) -> Capability:
    """``owner`` / ``writer`` / legacy ``member`` → write; ``reader`` → read."""
    n = normalize_role(role)
    if n == "reader":
        return "read"
    return "write"


def can_admin_members(role: str | None) -> bool:
    """Only the workspace owner manages invites and membership."""
    return normalize_role(role) == "owner"


def can_write(role: str | None) -> bool:
    return capability_for_role(role) == "write"


#: The message a read-only member gets from ANY refused mutation. Lives here so
#: the two enforcement points — :func:`infona_client.auth.access.require_tenant_write`
#: (single-purpose mutating routes) and
#: :class:`infona_client.agent.registry.ReadOnlyMembershipError` (the read/write
#: mixed ``/agent`` surface, ONTA-451) — cannot drift into two different wordings
#: for the same refusal.
READ_ONLY_DETAIL = (
    "This workspace membership is read-only. "
    "Ask the owner for write access to make changes."
)
