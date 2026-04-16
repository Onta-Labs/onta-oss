import logging
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from cograph.config import settings

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass
class TenantContext:
    tenant_id: str
    api_key: str


# A verifier takes a raw API key and returns the tenant_id the key should
# be routed to, or None if the key is not recognized. Implementations are
# expected to fail closed (return None) on network/timeout errors rather
# than raising — raising would turn an auth provider outage into a 500.
ExternalVerifier = Callable[[str], Optional[str]]

_external_verifier: Optional[ExternalVerifier] = None


def register_external_verifier(verifier: Optional[ExternalVerifier]) -> None:
    """Register (or clear) an external API key verifier.

    Downstream deployments can use this to plug in a third-party auth
    provider (Clerk, WorkOS, a custom keystore, etc.) without forking
    omnix-oss. Pass None to clear.
    """
    global _external_verifier
    _external_verifier = verifier


def get_tenant(api_key: Optional[str] = Security(api_key_header)) -> TenantContext:
    keys_map = settings.get_api_keys_map()
    has_static_keys = bool(keys_map) and keys_map != {"": ""}
    has_external = _external_verifier is not None

    # No auth configured at all — open access, default tenant.
    if not has_static_keys and not has_external:
        return TenantContext(tenant_id="default", api_key="")

    if not api_key:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Static keys take precedence: cheap dict lookup, no network round-trip.
    if has_static_keys:
        tenant_id = keys_map.get(api_key)
        if tenant_id is not None:
            return TenantContext(tenant_id=tenant_id, api_key=api_key)

    # Fall back to the external verifier, if one is registered.
    if has_external:
        try:
            tenant_id = _external_verifier(api_key)  # type: ignore[misc]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("external verifier raised: %s", exc)
            tenant_id = None
        if tenant_id is not None:
            return TenantContext(tenant_id=tenant_id, api_key=api_key)

    raise HTTPException(status_code=401, detail="Invalid API key")
