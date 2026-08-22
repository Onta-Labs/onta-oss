"""Hosted-extract entitlement (ONTA-554).

``POST /ingest/dlt`` with ``env:`` BYOK is OSS and ungated — the CLI on a Cloud
tenant still works. Persist + ``store:`` secrets are the hosted path: when a
premium entitlement checker is registered, those require Enhanced.

OSS (no checker) stays open so self-host persist works.
"""

from __future__ import annotations

from fastapi import HTTPException

from infona_client.auth.api_keys import TenantContext
from infona_client.graph.entitlement import get_entitlement_checker, is_entitled
from infona_client.ingestion.models import DltSourceSpec
from infona_client.ingestion.secrets import is_hosted_secret_ref


_DENIED = (
    "Hosted 3rd-party extract requires an Enhanced entitlement. "
    "Use the OSS CLI with env-var BYOK (POST /ingest/dlt, secret_ref=env:VAR) "
    "or upgrade this workspace."
)


def require_hosted_extract(tenant: TenantContext) -> None:
    """403 when a Cloud entitlement checker is on and this workspace is not entitled."""
    if get_entitlement_checker() is None:
        return
    if is_entitled(tenant):
        return
    raise HTTPException(status_code=403, detail=_DENIED)


def require_hosted_if_store_secret(tenant: TenantContext, spec: DltSourceSpec) -> None:
    """Gate ``store:`` secret_refs on /ingest/dlt; leave ``env:`` BYOK ungated."""
    refs: list[str] = []
    if spec.auth and spec.auth.secret_ref:
        refs.append(spec.auth.secret_ref)
    if spec.dsn:
        refs.append(spec.dsn)
    if any(is_hosted_secret_ref(r) for r in refs):
        require_hosted_extract(tenant)
