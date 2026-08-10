import logging
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Union

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from infona_client.config import settings
from infona_client.graph.queries import is_valid_tenant_id

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass
class TenantContext:
    tenant_id: str
    api_key: str
    # The auth subject (the user id behind the key) when the provider exposes
    # one — None for anonymous/static keys. Used to scope per-user resources
    # (e.g. Ask-AI conversation history, COG-131) without the OSS layer knowing
    # anything provider-specific: "subject" is a generic auth concept.
    subject: Optional[str] = None
    # Whether the authenticated identity is an ONTA *operator* (a first-party
    # staff/admin account), as decided by the auth PROVIDER — never by anything
    # the client sends. Generic like ``subject``: the OSS layer only carries the
    # bit; the *determination* (email domain / allowlist / provider role) lives in
    # the premium provider that populates :class:`AuthVerdict.is_operator`
    # (ONTA-234). Static/anonymous keys have no identity → ``False`` by default,
    # so they can never see the operator-only view. Routes gate operator-only
    # visibility (e.g. the global API-source catalog) on this flag.
    is_operator: bool = False
    # Whether THIS workspace may see the Global-Enhanced ontology layer
    # (ONTA-398). Set by the auth PROVIDER from verified identity metadata
    # (Clerk ``public_metadata``) — never from a client header, query param, or
    # deep link. Static/anonymous keys default ``False``. The layered-ontology
    # seam (:func:`infona_client.graph.entitlement.is_entitled`) is the single
    # predicate that consumes this bit (plus an env allowlist on the premium
    # side); callers must not invent a second path.
    enhanced_entitled: bool = False
    # Tenant-level capability for this subject on this workspace (``read`` |
    # ``write``). Defaults to ``write`` so static keys and pre-capability
    # membership keep today's behavior. Resolved at request time by
    # :func:`infona_client.auth.access.require_tenant_write` / membership
    # lookup — not trusted from the client.
    capability: str = "write"
    # Membership role when known: ``owner`` | ``writer`` | ``reader``. Empty
    # when static/anonymous keys have no membership row.
    role: str = ""


@dataclass
class AuthVerdict:
    """A richer verifier result: the tenants a key may access plus the auth
    subject (the user id behind the key, when the provider exposes one),
    whether that identity is an ONTA operator, and which of its workspaces
    are Enhanced-entitled.

    Verifiers may keep returning a bare ``str``/``Sequence[str]`` (no subject,
    non-operator, no entitlement); returning an :class:`AuthVerdict`
    additionally carries the subject through to :class:`TenantContext.subject`,
    the operator bit through to :class:`TenantContext.is_operator`, and
    per-workspace Enhanced entitlement through to
    :class:`TenantContext.enhanced_entitled` for the resolved tenant. The
    DETERMINATION for both bits is the provider's job (premium Clerk:
    email domain / allowlist / ``public_metadata``) — the OSS seam only
    threads the resulting bits, so no provider-specific logic leaks into
    OSS (ONTA-234, ONTA-398).
    """

    tenants: Sequence[str]
    subject: Optional[str] = None
    is_operator: bool = False
    # Workspace ids the verified identity may see Enhanced for. Resolved
    # against the requested path tenant into ``TenantContext.enhanced_entitled``.
    # Empty by default — fail closed.
    entitled_tenants: Sequence[str] = ()


# A verifier takes a raw API key and returns either:
#   - a single tenant_id (legacy single-tenant keys), or
#   - a sequence of tenant_ids the key may access (user-scoped keys: a user
#     owns N tenants and every key they create works for all of them), or
#   - an AuthVerdict (tenants + the auth subject), or
#   - None if the key is not recognized.
# Implementations are expected to fail closed (return None) on network or
# timeout errors rather than raising — raising would turn an auth provider
# outage into a 500.
ExternalVerifier = Callable[
    [str], Optional[Union[str, Sequence[str], "AuthVerdict"]]
]

_external_verifier: Optional[ExternalVerifier] = None


def register_external_verifier(verifier: Optional[ExternalVerifier]) -> None:
    """Register (or clear) an external API key verifier.

    Downstream deployments can use this to plug in a third-party auth
    provider (Clerk, WorkOS, a custom keystore, etc.) without forking
    infona-oss. Pass None to clear.
    """
    global _external_verifier
    _external_verifier = verifier


def get_external_verifier() -> Optional[ExternalVerifier]:
    """The registered external verifier, if any.

    Exposed for callers that need the raw verdict rather than a resolved
    ``TenantContext`` — e.g. workspace-invite subject resolution, which must
    work for a key whose user has ZERO tenants yet (``get_tenant`` would 401
    on the empty grant list).
    """
    return _external_verifier


def _resolve_allowed(
    allowed: Sequence[str],
    requested: Optional[str],
    api_key: str,
    subject: Optional[str] = None,
    is_operator: bool = False,
    entitled_tenants: Sequence[str] = (),
) -> TenantContext:
    """Pick the tenant for a key that may access several.

    The requested tenant comes from the route path (/graphs/{tenant}/...).
    No request → the key's first tenant; a request outside the allowed set
    is a 403 (the key is valid, the tenant grant is not).

    ``is_operator`` (from the provider's :class:`AuthVerdict`) rides through
    onto the resolved :class:`TenantContext` unchanged — it describes the
    identity, not the tenant, so it is the same for every tenant the key
    grants. ``entitled_tenants`` is resolved against the *selected* tenant
    into ``enhanced_entitled`` (workspace-scoped, ONTA-398).
    """
    allowed = [t for t in allowed if t]
    if not allowed:
        raise HTTPException(status_code=401, detail="Invalid API key")
    entitled = {t for t in entitled_tenants if t}
    if requested is None or requested == "":
        tenant_id = allowed[0]
        return TenantContext(
            tenant_id=tenant_id, api_key=api_key, subject=subject,
            is_operator=is_operator,
            enhanced_entitled=tenant_id in entitled,
        )
    if requested in allowed:
        return TenantContext(
            tenant_id=requested, api_key=api_key, subject=subject,
            is_operator=is_operator,
            enhanced_entitled=requested in entitled,
        )
    allowed_hint = ", ".join(allowed) if allowed else "(none)"
    raise HTTPException(
        status_code=403,
        detail=(
            f"API key does not grant access to tenant '{requested}'. "
            f"This key can access: {allowed_hint}. "
            f"Set INFONA_TENANT to one of those workspace ids "
            f"(or create an unscoped key for all workspaces)."
        ),
    )


def get_tenant(
    tenant: Optional[str] = None,
    api_key: Optional[str] = Security(api_key_header),
    request: Request = None,  # injected by FastAPI; None in direct calls/tests
) -> TenantContext:
    """Resolve the tenant for a request.

    `tenant` is injected from the route path (/graphs/{tenant}/...) when
    present. Static keys map 1:1 to a tenant: path omitted → key's tenant
    (back-compat); path present and equal → ok; path present and different
    → 403 (never a silent reroute to the key's data). Multi-tenant keys
    (verifier returned a sequence / AuthVerdict) are authorized against the
    requested path tenant the same way. Legacy single-tenant str verdicts
    (claims.tenant) still route to THEIR tenant regardless of the path.

    On success the AUTHENTICATED tenant id (and the auth subject, when the
    provider exposes one) is stashed on ``request.state`` so the usage-metering
    middleware and the analytics seam (ONTA-323) attribute the request to the
    identity auth actually resolved — never to the raw URL path (which
    unauthenticated 404/405 traffic could otherwise abuse). Failed auth raises
    before the stash, so unauthenticated requests are never attributed to anyone.
    """
    ctx = _resolve_tenant(tenant, api_key)
    if request is not None:
        request.state.usage_tenant = ctx.tenant_id
        # The auth subject (Clerk user id) when the provider exposes one — used
        # as the analytics distinct_id so frontend + backend land on one person.
        # None for static/anonymous keys; the analytics seam falls back to a
        # stable system:<tenant> id in that case.
        request.state.auth_subject = ctx.subject
    return ctx


def _has_static_keys() -> bool:
    keys_map = settings.get_api_keys_map()
    return bool(keys_map) and keys_map != {"": ""}


def auth_is_configured() -> bool:
    """Whether ANY authentication is wired up (static key map or a verifier).

    When this is False the deployment is in open-access mode: ``_resolve_tenant``
    below hands an anonymous caller whatever tenant the URL names, so there is no
    tenant boundary for anything to cross. Routes that are otherwise restricted
    to operators use this to stay usable for a self-hosted single-user install
    without weakening anything in a deployment that DOES have auth.
    """
    return _has_static_keys() or _external_verifier is not None


def _resolve_tenant(tenant: Optional[str], api_key: Optional[str]) -> TenantContext:
    keys_map = settings.get_api_keys_map()
    has_static_keys = _has_static_keys()
    has_external = _external_verifier is not None

    # No auth configured at all — open access; honor the requested tenant.
    #
    # ONTA-422: "honor the requested tenant" means the caller's raw URL segment
    # becomes ``tenant_id``, which every route interpolates into a graph IRI
    # (``tenant_graph_uri`` / ``kg_graph_uri``). This is the ONE mode where that
    # value is unconstrained — a static key maps to a fixed tenant and a verifier
    # authorizes the path tenant against a grant list, so neither can be crafted.
    # Reject a tenant that cannot legally sit inside an IRI before it becomes
    # one. The check is the shared structural predicate (no shape rule), so a
    # self-hosted deployment's existing workspace id keeps working; only ids that
    # could not have produced a valid query anyway are refused.
    #
    # 400 rather than the 422 ``tenant_graph_uri`` raises: this is a rejection at
    # the auth boundary, before any route body runs, and it sits beside the 401 /
    # 403 this function already raises. The 422 stays as the backstop for any
    # caller that reaches a URI builder some other way.
    if not has_static_keys and not has_external:
        requested = tenant or "default"
        if not is_valid_tenant_id(requested):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid workspace id {requested!r}: must be a single "
                    "path segment with no whitespace, control character, '/', "
                    "'%', or any of <>\"{}|^`\\"
                ),
            )
        return TenantContext(tenant_id=requested, api_key="")

    if not api_key:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Static keys take precedence: cheap dict lookup, no network round-trip.
    # Path tenant present and different from the key's tenant is a 403 — never
    # silently reroute to the key's data under a foreign path (dogfood S3).
    # Path omitted (or empty) keeps back-compat: operate on the key's tenant.
    if has_static_keys:
        tenant_id = keys_map.get(api_key)
        if tenant_id is not None:
            if tenant is not None and tenant != "" and tenant != tenant_id:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"API key does not grant access to tenant '{tenant}'. "
                        f"This key can access: {tenant_id}."
                    ),
                )
            return TenantContext(tenant_id=tenant_id, api_key=api_key)

    # Fall back to the external verifier, if one is registered.
    if has_external:
        try:
            verdict = _external_verifier(api_key)  # type: ignore[misc]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("external verifier raised: %s", exc)
            verdict = None
        if isinstance(verdict, AuthVerdict):
            return _resolve_allowed(
                verdict.tenants, tenant, api_key, subject=verdict.subject,
                is_operator=verdict.is_operator,
                entitled_tenants=verdict.entitled_tenants,
            )
        if isinstance(verdict, str):
            return TenantContext(tenant_id=verdict, api_key=api_key)
        if verdict is not None:
            return _resolve_allowed(verdict, tenant, api_key)

    raise HTTPException(status_code=401, detail="Invalid API key")
