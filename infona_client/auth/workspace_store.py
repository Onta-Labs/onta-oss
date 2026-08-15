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
BOTH a durable store and ``INFONA_WORKSPACE_ENFORCE_OWNERSHIP=1``: an
in-memory registry that forgets owners on restart would silently re-run
first-claim-wins, which is worse than not pretending. Rollout is deploy
(writes on, flag off) → backfill → flip the flag, so lazy-claim only ever
applies to genuinely new ids.
"""


from __future__ import annotations

import os
from typing import Optional, Protocol, runtime_checkable

import structlog

from infona_client.auth.api_keys import AuthVerdict, get_external_verifier
from infona_client.config import settings
from infona_client.auth.workspace_store_models import (  # noqa: F401
    DuplicatePendingInviteError,
    INVITE_TTL_DAYS,
    OWNERSHIP_ENFORCE_ENV,
    PENDING_INVITE_CAP,
    Workspace,
    WorkspaceError,
    WorkspaceInvite,
    WorkspaceMember,
    WorkspaceStore,
    _utcnow,
    effective_status,
)
from infona_client.auth.workspace_store_memory import (  # noqa: F401
    InMemoryWorkspaceStore,
)
from infona_client.auth.workspace_store_postgres import (  # noqa: F401
    PostgresWorkspaceStore,
)

logger = structlog.stdlib.get_logger("infona.auth.workspace_store")


def _host():
    """Call-time lookup of this module (monkeypatch surface)."""
    from infona_client.auth import workspace_store as _mod

    return _mod



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
    host = _host()
    if settings.database_url:
        if _durable_store is None:
            _durable_store = host.PostgresWorkspaceStore()
        return _durable_store
    if _memory_store is None:
        _memory_store = host.InMemoryWorkspaceStore()
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
                "(INFONA_DATABASE_URL unset) — the ownership 403 is OFF and the "
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

    Deliberately independent of :func:`~infona_client.auth.api_keys.get_tenant`:
    that path 403/401s on tenant grants, but a brand-new user accepting their
    first workspace invite has ZERO tenants yet — their key must still resolve
    a subject. Keys that carry no subject (static ``INFONA_API_KEYS`` entries,
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
