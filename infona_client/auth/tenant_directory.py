"""Tenant directory plugin protocol — user-owned tenant management.

Tenants belong to USERS (see auth/api_keys.py): a user owns N tenants and every
API key they create works for all of them. *Reading and mutating* that ownership
list (list/add/remove tenants) is identity-provider specific — it lives in the
user's Clerk/WorkOS/... profile — so infona-oss does not implement it directly.
Instead a deployment registers a provider here, exactly as it registers an API
key verifier via ``register_external_verifier``. The premium Clerk integration
(``infona.auth.clerk``) registers one; without a provider the ``/v1/me/tenants``
routes report 501.

The provider authenticates the caller from their own API key (the same key used
for ``X-API-Key`` auth) — no admin/identity-provider secret ever leaves the
backend. This is what lets the CLI and the Explorer manage tenants over one
shared backend route instead of each holding the Clerk secret.

Validation rules (slug shape, reserved ids, label length, per-user label
uniqueness) and the auto-naming scheme for one-click workspace creation are
product rules, not provider specifics, so they live here as the single source of
truth shared by the route and any caller. Clients (Explorer, CLI, MCP) do NOT
re-implement them: they call ``POST /v1/me/tenants`` — with no body at all for
the auto-named "Untitled workspace N" case — and surface whatever this module
decides.
"""

import re
import secrets
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol, runtime_checkable

# Slug rule: lowercase alphanumeric + interior dashes, 3–40 chars.
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")

# Ids that must never be self-served: shared/env tenants, the backend fallback
# tenant, and the disposable benchmark tenant.
RESERVED_TENANT_IDS = frozenset(
    {"demo-tenant", "hotel-design-partner", "default", "spider-bench"}
)

MAX_LABEL_LEN = 64

# Auto-minted workspaces (the Explorer's one-click "Add workspace") are labelled
# "Untitled workspace N", counting up per user.
UNTITLED_LABEL_PREFIX = "Untitled workspace"
_UNTITLED_LABEL_RE = re.compile(rf"^{UNTITLED_LABEL_PREFIX} (\d+)$", re.IGNORECASE)

# Slug prefix for auto-minted ids. The id is NOT derived from the label: tenant
# ids are claimed in a GLOBAL registry (workspace_store), so a derived
# "untitled-workspace-1" would be contended across every user of the deployment
# — the first ever caller would own it and everyone else would 403 once
# ownership enforcement is on. A random suffix keeps auto-create collision-free.
# Ids are opaque and permanent (they key the graph IRIs); the LABEL is the
# renameable, human-facing name.
_UNTITLED_ID_PREFIX = "untitled-workspace"
_ID_SUFFIX_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_ID_SUFFIX_LEN = 6


@dataclass
class Tenant:
    id: str
    label: str


class TenantProviderError(Exception):
    """A client-facing failure from a provider, carrying an HTTP status.

    Providers raise this for auth/conflict/not-found conditions so the route can
    translate them into the right status without knowing provider internals.
    """

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@runtime_checkable
class TenantProvider(Protocol):
    """Manages the caller's owned tenants, authenticated by their API key.

    Implementations should raise ``TenantProviderError`` for caller-facing
    failures (401 invalid key, 404 unknown tenant, 409 already exists) and fail
    closed on identity-provider outages.
    """

    def list_tenants(self, api_key: str) -> list[Tenant]: ...

    def add_tenant(self, api_key: str, tenant_id: str, label: str) -> Tenant: ...

    def remove_tenant(self, api_key: str, tenant_id: str) -> None: ...

    def rename_tenant(self, api_key: str, tenant_id: str, label: str) -> Tenant:
        """Change a tenant's human-facing label. The id is immutable.

        Optional for back-compat: a provider written before renaming existed
        simply won't have this, and ``PATCH /v1/me/tenants/{id}`` reports 501
        rather than failing at import time.
        """
        ...


_provider: Optional[TenantProvider] = None


def register_tenant_provider(provider: Optional[TenantProvider]) -> None:
    """Register (or clear) the tenant directory provider. Pass None to clear."""
    global _provider
    _provider = provider


def get_tenant_provider() -> Optional[TenantProvider]:
    return _provider


def validate_label(label: str) -> str:
    """Validate + trim a workspace label. Raises ``TenantProviderError(400)``."""
    lbl = label.strip()
    if not lbl:
        raise TenantProviderError(400, "Label is required.")
    if len(lbl) > MAX_LABEL_LEN:
        raise TenantProviderError(
            400, f"Label must be {MAX_LABEL_LEN} characters or fewer."
        )
    return lbl


def label_key(label: str) -> str:
    """Comparison key for label uniqueness: case- and whitespace-insensitive.

    "My Workspace", "my workspace" and "My  Workspace " are the same name to a
    person, so they collide here too.
    """
    return " ".join(label.split()).casefold()


def ensure_label_available(
    owned: Iterable[Tenant], label: str, *, exclude_id: Optional[str] = None
) -> None:
    """Enforce "no two of MY workspaces share a name".

    Labels live per-user (each user's identity profile carries their own copy),
    so uniqueness is scoped to the caller's own list — two different users may
    each have a "Research" workspace. Pass ``exclude_id`` when renaming so a
    tenant doesn't collide with itself. Raises ``TenantProviderError(409)``.

    This is read-then-write, so it is ADVISORY, not an invariant: two concurrent
    creates from two tabs can both pass the check and both land. Closing that
    would need a transactional store, and the identity profile (where labels
    live) is not one. The rule exists to stop a user accidentally ending up with
    two identically-named workspaces, which it does; it is not a constraint any
    other code may assume holds.
    """
    key = label_key(label)
    for t in owned:
        if exclude_id is not None and t.id == exclude_id:
            continue
        if label_key(t.label) == key:
            raise TenantProviderError(
                409, f'You already have a workspace named "{t.label}".'
            )


def next_untitled_label(owned: Iterable[Tenant]) -> str:
    """The next "Untitled workspace N" for this user: highest existing N, plus 1.

    Interior gaps are not filled — with 1 and 3 present the next is 4, not 2 —
    so a rename in the middle of the list doesn't make the next create land on a
    number the user just walked past. Deleting the HIGHEST one does free its
    number for reuse; that's the same "New Folder" behaviour every file manager
    has, and tracking retired numbers would mean persisting a counter for a name
    the user is expected to replace anyway.
    """
    taken = {label_key(t.label) for t in owned}
    highest = 0
    for t in owned:
        m = _UNTITLED_LABEL_RE.match(" ".join(t.label.split()))
        if m:
            highest = max(highest, int(m.group(1)))
    candidate = f"{UNTITLED_LABEL_PREFIX} {highest + 1}"
    if len(candidate) <= MAX_LABEL_LEN and label_key(candidate) not in taken:
        return candidate
    # Pathological input only: a hand-typed "Untitled workspace <45 digits>" is
    # a legal 64-char label whose successor is 65. Never 400 the one-click
    # button over it — fall back to the lowest free number instead.
    n = 1
    while True:
        candidate = f"{UNTITLED_LABEL_PREFIX} {n}"
        if label_key(candidate) not in taken:
            return candidate
        n += 1


def mint_untitled_tenant_id() -> str:
    """A fresh, globally collision-resistant slug for an auto-created workspace."""
    suffix = "".join(secrets.choice(_ID_SUFFIX_ALPHABET) for _ in range(_ID_SUFFIX_LEN))
    return f"{_UNTITLED_ID_PREFIX}-{suffix}"


def validate_new_tenant(tenant_id: str, label: str) -> tuple[str, str]:
    """Validate + normalize a tenant id/label for creation.

    Returns the trimmed (id, label). Raises ``TenantProviderError(400, ...)`` on
    a bad slug, a reserved id, or a missing/over-long label — mirroring the web
    Explorer's createTenant checks so both surfaces enforce identical rules.
    """
    tid = tenant_id.strip()
    if not TENANT_ID_RE.match(tid):
        raise TenantProviderError(
            400,
            "Tenant id must be 3–40 characters: lowercase letters, numbers, "
            "and interior dashes.",
        )
    if tid in RESERVED_TENANT_IDS:
        raise TenantProviderError(400, f'"{tid}" is reserved.')
    return tid, validate_label(label)
