"""Workspace membership + invite routes (ONTA-227) — the CANONICAL endpoints.

These are the *single* backend routes the web Explorer, the CLI, and any future
interface use for the whole invite lifecycle (interface-convergence rule): the
owner creates/lists/revokes invites and manages members under
``/v1/me/tenants/{tenant_id}/…``; the invitee sees and settles their invites
under ``/v1/me/invites…`` or accepts by one-time token at
``POST /v1/invites/accept``.

Auth: every route resolves the caller's *subject* (user id) from their own
``X-API-Key`` via the registered verifier's ``AuthVerdict.subject`` — keys that
carry no subject (static keys, no-auth dev mode) get 403. Owner-only checks are
enforced server-side against the workspace registry (``workspaces.owner_subject``
is authoritative), never client-side.

Dual-write order (accept, member-removal): the identity-provider grant — the
AUTH truth — goes first, the registry row second; both steps are idempotent so
a 5xx mid-way is healed by retry. Member-removal deliberately tolerates a
missing membership row (revoke the grant regardless, 200 either way) — that
makes the owner's remove button the manual repair for accept limbo too.

Degradation is per-provider, mirroring the ``tenant_directory`` 501 precedent:
no ``TenantGrantProvider`` → accept/removal 501; no ``InviteDeliveryProvider``
→ ``GET /v1/me/invites`` and in-app accept/decline 501, invite creation is
link-only, and token accept is token-possession semantics (the link IS the
credential — single-use, expiring, revocable).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException, Security
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from cograph_client.auth.api_keys import api_key_header
from cograph_client.auth.workspace_store import (
    PENDING_INVITE_CAP,
    DuplicatePendingInviteError,
    InviteDeliveryProvider,
    TenantGrantProvider,
    Workspace,
    WorkspaceError,
    WorkspaceInvite,
    WorkspaceStore,
    effective_status,
    get_invite_delivery_provider,
    get_tenant_grant_provider,
    make_workspace_store,
    require_subject,
)
from cograph_client.config import settings

logger = structlog.stdlib.get_logger("cograph.workspace_invites")

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class InviteCreate(BaseModel):
    email: str = Field(..., description="Invitee email address.")
    role: str = Field("member", description="Invited role (v1: 'member' only).")


class InviteOut(BaseModel):
    id: str
    tenant_id: str
    email: str
    role: str
    status: str  # read-time status: a pending row past expiry reads "expired"
    invited_by: str
    created_at: datetime
    expires_at: datetime
    email_sent: bool


class InviteCreateOut(BaseModel):
    invite: InviteOut
    # The raw one-time accept token — returned ONCE here, never retrievable
    # again (only its sha256 is stored).
    accept_token: str
    accept_url: Optional[str]
    delivery: str  # "email_sent" | "in_app" | "link_only"


class MyInviteOut(BaseModel):
    id: str
    tenant_id: str
    workspace_label: str
    email: str
    role: str
    created_at: datetime
    expires_at: datetime


class MemberOut(BaseModel):
    subject: str
    role: str
    joined_at: datetime
    email: Optional[str] = None
    name: Optional[str] = None


class AcceptOut(BaseModel):
    tenant_id: str
    label: str
    role: str
    status: str


class TokenAccept(BaseModel):
    token: str = Field(..., description="The one-time accept token.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _subject(api_key: Optional[str]) -> str:
    try:
        return require_subject(api_key)
    except WorkspaceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


async def _registered_workspace(store: WorkspaceStore, tenant_id: str) -> Workspace:
    """The registry row for ``tenant_id`` — 404 when absent.

    Requiring the row (rather than the identity provider's grant list) is what
    makes reserved ids safe by construction: no self-serve create ever
    registers them, so invites INTO a reserved id only work once it has been
    manually seeded. For ordinary ids the owner re-adds their workspace via
    ``POST /v1/me/tenants`` to (re)claim the row.
    """
    ws = await store.get_workspace(tenant_id)
    if ws is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f'Workspace "{tenant_id}" is not registered. Re-add it via '
                "POST /v1/me/tenants to claim it, then invite."
            ),
        )
    return ws


async def _require_owner(
    store: WorkspaceStore, tenant_id: str, subject: str
) -> Workspace:
    ws = await _registered_workspace(store, tenant_id)
    if ws.owner_subject != subject:
        raise HTTPException(
            status_code=403, detail="Only the workspace owner can do this."
        )
    return ws


def _require_grant_provider() -> TenantGrantProvider:
    provider = get_tenant_grant_provider()
    if provider is None:
        raise HTTPException(
            status_code=501,
            detail="Tenant grants are not configured for this deployment.",
        )
    return provider


def _require_delivery_provider() -> InviteDeliveryProvider:
    provider = get_invite_delivery_provider()
    if provider is None:
        raise HTTPException(
            status_code=501,
            detail="Invite delivery is not configured for this deployment.",
        )
    return provider


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _accept_url(token: str) -> Optional[str]:
    base = (settings.invite_accept_url_base or "").strip()
    if not base:
        return None
    return f"{base.rstrip('/')}/{token}"


def _invite_out(invite: WorkspaceInvite) -> InviteOut:
    return InviteOut(
        id=invite.id,
        tenant_id=invite.tenant_id,
        email=invite.email,
        role=invite.role,
        status=effective_status(invite),
        invited_by=invite.invited_by,
        created_at=invite.created_at,
        expires_at=invite.expires_at,
        email_sent=invite.signup_invitation_id is not None,
    )


async def _email_matches(
    delivery: InviteDeliveryProvider, subject: str, invite_email: str
) -> bool:
    emails = await run_in_threadpool(delivery.emails_for_subject, subject)
    return invite_email in {e.strip().lower() for e in (emails or [])}


def _reject_settled(invite: WorkspaceInvite, subject: str) -> Optional[AcceptOut]:
    """Pre-accept status gate. Returns an idempotent-success payload for a
    same-subject re-accept, raises for every other settled state, and returns
    None when the invite is genuinely pending."""
    status = effective_status(invite)
    if status == "pending":
        return None
    if status == "accepted" and invite.accepted_by == subject:
        # Idempotent re-accept: grant + membership + status all already done.
        return AcceptOut(
            tenant_id=invite.tenant_id,
            label=invite.tenant_id,
            role=invite.role,
            status="accepted",
        )
    if status == "accepted":
        raise HTTPException(
            status_code=409, detail="This invite was already accepted."
        )
    raise HTTPException(status_code=410, detail=f"This invite is {status}.")


async def _accept(
    store: WorkspaceStore, invite: WorkspaceInvite, subject: str
) -> AcceptOut:
    """The ONE accept code path (in-app and token accept both land here).

    Order per the design's dual-write rule: grant (auth truth) → membership
    row → mark accepted. Every step is idempotent, so a failure part-way
    returns 5xx and the client's retry is the repair.
    """
    settled = _reject_settled(invite, subject)
    if settled is not None:
        ws = await store.get_workspace(invite.tenant_id)
        if ws is not None:
            settled.label = ws.label
        return settled
    grant = _require_grant_provider()
    ws = await store.get_workspace(invite.tenant_id)
    label = ws.label if ws is not None else invite.tenant_id
    try:
        await run_in_threadpool(grant.grant, subject, invite.tenant_id, label)
    except Exception as exc:  # noqa: BLE001 — surface as retryable 5xx
        logger.warning(
            "workspace_grant_failed", tenant=invite.tenant_id, error=str(exc)
        )
        raise HTTPException(
            status_code=502, detail="Granting workspace access failed; retry."
        )
    await store.add_member(invite.tenant_id, subject, invite.role)
    if not await store.mark_accepted(invite.id, subject):
        # Lost a race with a concurrent settle — re-read and re-gate. A
        # same-subject concurrent accept still returns 200 here.
        latest = await store.get_invite(invite.id)
        if latest is not None:
            settled = _reject_settled(latest, subject)
            if settled is not None:
                settled.label = label
                return settled
        raise HTTPException(
            status_code=409, detail="This invite is no longer pending."
        )
    logger.info(
        "workspace_invite_accepted", tenant=invite.tenant_id, invite_id=invite.id
    )
    return AcceptOut(
        tenant_id=invite.tenant_id, label=label, role=invite.role, status="accepted"
    )


# ---------------------------------------------------------------------------
# Owner routes — invites
# ---------------------------------------------------------------------------


@router.post(
    "/v1/me/tenants/{tenant_id}/invites",
    response_model=InviteCreateOut,
    status_code=201,
)
async def create_invite(
    tenant_id: str,
    body: InviteCreate,
    api_key: Optional[str] = Security(api_key_header),
):
    subject = _subject(api_key)
    store = make_workspace_store()
    ws = await _require_owner(store, tenant_id, subject)

    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Invalid email address.")
    role = (body.role or "member").strip().lower()
    if role != "member":
        # v1 has exactly one owner (the registry row) — invited roles are
        # 'member' only until ownership transfer exists.
        raise HTTPException(
            status_code=400, detail="Only the 'member' role can be invited in v1."
        )
    if await store.count_pending(tenant_id) >= PENDING_INVITE_CAP:
        raise HTTPException(
            status_code=429,
            detail=(
                f"This workspace already has {PENDING_INVITE_CAP} pending "
                "invites; revoke some before inviting more."
            ),
        )

    token = secrets.token_urlsafe(32)  # 256-bit; sha256 at rest, raw never stored
    invite = WorkspaceInvite(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        email=email,
        role=role,
        status="pending",
        token_hash=_hash_token(token),
        invited_by=subject,
    )
    try:
        invite = await store.create_invite(invite)
    except DuplicatePendingInviteError as exc:
        # "Expired" is computed at read time; a stored-pending row past expiry
        # is NOT a real pending invite, so persist the computed expiry and
        # retry once. A second collision is a true duplicate → 409 carrying
        # the existing invite id.
        existing = await store.get_invite(exc.invite_id) if exc.invite_id else None
        if existing is not None and effective_status(existing) == "expired":
            await store.mark_expired(existing.id)
            try:
                invite = await store.create_invite(invite)
            except DuplicatePendingInviteError as exc2:
                raise _duplicate_409(exc2)
        else:
            raise _duplicate_409(exc)

    delivery_mode = "link_only"
    accept_url = _accept_url(token)
    delivery = get_invite_delivery_provider()
    if delivery is not None:
        existing_subject = await run_in_threadpool(
            delivery.lookup_subject_by_email, email
        )
        if existing_subject is not None:
            # Existing users are surfaced in-app (GET /v1/me/invites) — no
            # sign-up email for an account that already exists.
            delivery_mode = "in_app"
        elif accept_url:
            try:
                invitation_id = await run_in_threadpool(
                    delivery.send_signup_invitation,
                    email,
                    accept_url,
                    {
                        "invite_id": invite.id,
                        "tenant_id": tenant_id,
                        "workspace_label": ws.label,
                    },
                )
            except Exception as exc:  # noqa: BLE001 — email is best-effort
                invitation_id = None
                logger.warning(
                    "workspace_invite_email_failed",
                    invite_id=invite.id,
                    error=str(exc),
                )
            if invitation_id:
                await store.set_signup_invitation_id(invite.id, invitation_id)
                invite.signup_invitation_id = invitation_id
                delivery_mode = "email_sent"
        else:
            logger.warning(
                "workspace_invite_no_redirect_base",
                hint=(
                    "set OMNIX_INVITE_ACCEPT_URL_BASE to enable sign-up "
                    "invitation emails; returning link-only"
                ),
            )
    logger.info(
        "workspace_invite_created",
        tenant=tenant_id,
        invite_id=invite.id,
        delivery=delivery_mode,
    )
    return InviteCreateOut(
        invite=_invite_out(invite),
        accept_token=token,
        accept_url=accept_url,
        delivery=delivery_mode,
    )


def _duplicate_409(exc: DuplicatePendingInviteError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": "An invite for this email is already pending.",
            "invite_id": exc.invite_id,
        },
    )


@router.get("/v1/me/tenants/{tenant_id}/invites", response_model=list[InviteOut])
async def list_invites(
    tenant_id: str, api_key: Optional[str] = Security(api_key_header)
):
    subject = _subject(api_key)
    store = make_workspace_store()
    await _require_owner(store, tenant_id, subject)
    return [_invite_out(i) for i in await store.list_invites(tenant_id)]


@router.delete("/v1/me/tenants/{tenant_id}/invites/{invite_id}")
async def revoke_invite(
    tenant_id: str,
    invite_id: str,
    api_key: Optional[str] = Security(api_key_header),
):
    subject = _subject(api_key)
    store = make_workspace_store()
    await _require_owner(store, tenant_id, subject)
    invite = await store.get_invite(invite_id)
    if invite is None or invite.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Unknown invite.")
    if not await store.mark_revoked(invite_id):
        latest = await store.get_invite(invite_id)
        if latest is not None and latest.status == "accepted":
            raise HTTPException(
                status_code=409,
                detail="This invite was already accepted; remove the member instead.",
            )
        # Already revoked/declined/expired — revoke is idempotent.
        return {"revoked": invite_id}
    # Close the "revoked in the app but the email still works" gap:
    # best-effort revoke of the identity provider's sign-up invitation.
    delivery = get_invite_delivery_provider()
    if delivery is not None and invite.signup_invitation_id:
        try:
            await run_in_threadpool(
                delivery.revoke_signup_invitation, invite.signup_invitation_id
            )
        except Exception as exc:  # noqa: BLE001 — best-effort by design
            logger.warning(
                "workspace_signup_invitation_revoke_failed",
                invite_id=invite_id,
                error=str(exc),
            )
    logger.info("workspace_invite_revoked", tenant=tenant_id, invite_id=invite_id)
    return {"revoked": invite_id}


# ---------------------------------------------------------------------------
# Member routes
# ---------------------------------------------------------------------------


@router.get("/v1/me/tenants/{tenant_id}/members", response_model=list[MemberOut])
async def list_members(
    tenant_id: str, api_key: Optional[str] = Security(api_key_header)
):
    subject = _subject(api_key)
    store = make_workspace_store()
    ws = await _registered_workspace(store, tenant_id)
    is_member = (
        ws.owner_subject == subject
        or await store.get_member(tenant_id, subject) is not None
    )
    if not is_member:
        raise HTTPException(
            status_code=403, detail="Only workspace members can list members."
        )
    members = await store.list_members(tenant_id)
    delivery = get_invite_delivery_provider()
    out: list[MemberOut] = []
    for m in members:
        email = name = None
        if delivery is not None:
            try:
                profile = await run_in_threadpool(delivery.display_profile, m.subject)
            except Exception:  # noqa: BLE001 — decoration is best-effort
                profile = None
            if profile:
                email = profile.get("email")
                name = profile.get("name")
        out.append(
            MemberOut(
                subject=m.subject,
                role=m.role,
                joined_at=m.joined_at,
                email=email,
                name=name,
            )
        )
    return out


@router.delete("/v1/me/tenants/{tenant_id}/members/{member_subject}")
async def remove_member(
    tenant_id: str,
    member_subject: str,
    api_key: Optional[str] = Security(api_key_header),
):
    subject = _subject(api_key)
    store = make_workspace_store()
    await _require_owner(store, tenant_id, subject)
    if member_subject == subject:
        raise HTTPException(
            status_code=400, detail="The workspace owner cannot remove themselves."
        )
    grant = _require_grant_provider()
    # Grant revoke (auth truth) FIRST; then the registry row. Tolerate a
    # missing membership row — a half-completed accept leaves a user WITH
    # access but in no member list, and this route is the manual repair.
    try:
        await run_in_threadpool(grant.revoke, member_subject, tenant_id)
    except Exception as exc:  # noqa: BLE001 — fail closed, retry is repair
        logger.warning(
            "workspace_revoke_failed", tenant=tenant_id, error=str(exc)
        )
        raise HTTPException(
            status_code=502, detail="Revoking workspace access failed; retry."
        )
    removed_row = await store.remove_member(tenant_id, member_subject)
    logger.info(
        "workspace_member_removed",
        tenant=tenant_id,
        had_membership_row=removed_row,
    )
    return {"removed": member_subject}


# ---------------------------------------------------------------------------
# Invitee routes
# ---------------------------------------------------------------------------


@router.get("/v1/me/invites", response_model=list[MyInviteOut])
async def my_invites(api_key: Optional[str] = Security(api_key_header)):
    subject = _subject(api_key)
    delivery = _require_delivery_provider()  # the email oracle — 501 without
    store = make_workspace_store()
    emails = await run_in_threadpool(delivery.emails_for_subject, subject)
    invites = await store.list_invites_for_emails(emails or [])
    out: list[MyInviteOut] = []
    for inv in invites:
        ws = await store.get_workspace(inv.tenant_id)
        out.append(
            MyInviteOut(
                id=inv.id,
                tenant_id=inv.tenant_id,
                workspace_label=ws.label if ws else inv.tenant_id,
                email=inv.email,
                role=inv.role,
                created_at=inv.created_at,
                expires_at=inv.expires_at,
            )
        )
    return out


async def _authorized_invite(
    store: WorkspaceStore, invite_id: str, subject: str
) -> WorkspaceInvite:
    """Load an invite and authorize the caller as its addressee (email match
    via the delivery provider). Used by in-app accept AND decline — an invite
    id alone must not let a stranger settle someone else's invite."""
    delivery = _require_delivery_provider()
    invite = await store.get_invite(invite_id)
    if invite is None:
        raise HTTPException(status_code=404, detail="Unknown invite.")
    if not await _email_matches(delivery, subject, invite.email):
        raise HTTPException(
            status_code=403,
            detail=(
                f"This invite was sent to {invite.email}. Sign in with that "
                "email, or ask the owner to re-invite the one you're using."
            ),
        )
    return invite


@router.post("/v1/me/invites/{invite_id}/accept", response_model=AcceptOut)
async def accept_invite(
    invite_id: str, api_key: Optional[str] = Security(api_key_header)
):
    subject = _subject(api_key)
    store = make_workspace_store()
    invite = await _authorized_invite(store, invite_id, subject)
    return await _accept(store, invite, subject)


@router.post("/v1/me/invites/{invite_id}/decline")
async def decline_invite(
    invite_id: str, api_key: Optional[str] = Security(api_key_header)
):
    subject = _subject(api_key)
    store = make_workspace_store()
    invite = await _authorized_invite(store, invite_id, subject)
    if not await store.mark_declined(invite_id):
        latest = await store.get_invite(invite_id)
        if latest is not None and latest.status == "accepted":
            raise HTTPException(
                status_code=409, detail="This invite was already accepted."
            )
        # Already declined/revoked/expired — decline is idempotent.
    logger.info(
        "workspace_invite_declined", tenant=invite.tenant_id, invite_id=invite_id
    )
    return {"declined": invite_id}


@router.post("/v1/invites/accept", response_model=AcceptOut)
async def accept_invite_by_token(
    body: TokenAccept, api_key: Optional[str] = Security(api_key_header)
):
    subject = _subject(api_key)
    store = make_workspace_store()
    invite = await store.get_invite_by_token_hash(_hash_token(body.token.strip()))
    if invite is None:
        raise HTTPException(status_code=404, detail="Invalid invite link.")
    # Email match when an email oracle exists (the hosted product). Without a
    # delivery provider this is deliberately token-possession semantics — the
    # link IS the credential (single-use, expiring, revocable); see the
    # workspace_store module docstring. A settled invite (revoked/expired/
    # declined/accepted) skips the match and falls through to _accept's status
    # gate: a dead link is dead for everyone — 410 beats 403 there, and the
    # idempotent same-subject re-accept was email-authorized when it first
    # landed.
    delivery = get_invite_delivery_provider()
    if (
        delivery is not None
        and effective_status(invite) == "pending"
        and not await _email_matches(delivery, subject, invite.email)
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"This invite was sent to {invite.email}. Sign in with that "
                "email, or ask the owner to re-invite the one you're using."
            ),
        )
    return await _accept(store, invite, subject)
