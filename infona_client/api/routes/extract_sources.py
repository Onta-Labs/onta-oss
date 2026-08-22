"""Persist 3rd-party extract sources (ONTA-554). Distinct from /api-sources.

  GET    /graphs/{tenant}/extract-sources
  POST   /graphs/{tenant}/extract-sources
  GET    /graphs/{tenant}/extract-sources/{slug}
  PATCH  /graphs/{tenant}/extract-sources/{slug}
  DELETE /graphs/{tenant}/extract-sources/{slug}
  POST   /graphs/{tenant}/extract-sources/{slug}/run

Run builds the frozen ``POST /ingest/dlt`` body from the saved config and
calls the same :func:`run_dlt_ingest` handoff. Secrets stay in the existing
cipher store (slug ``dlt:{extract_slug}``).

Hosted entitlement applies when a premium checker is registered. OSS CLI
``POST /ingest/dlt`` with ``env:`` BYOK is a different route and stays ungated.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from infona_client.analytics import distinct_id_for, emit
from infona_client.api.deps import get_enrichment_job_store, get_neptune_client
from infona_client.api.rate_limit import limiter
from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.config import settings
from infona_client.ingestion.extract_source_store import (
    StoredExtractSource,
    make_extract_source_store,
    secret_store_slug,
)
from infona_client.ingestion.hosted import require_hosted_extract
from infona_client.ingestion.http import raise_dlt_http
from infona_client.ingestion.models import (
    CreateExtractSourceRequest,
    DltAuthSpec,
    DltExtractSource,
    DltIngestRequest,
    DltSourceSpec,
    ExtractSourceSummary,
    RunExtractSourceRequest,
    UpdateExtractSourceRequest,
)
from infona_client.ingestion.store_secrets import (
    delete_extract_secrets,
    extract_has_secret,
    put_extract_secrets,
    tenant_store_getter,
)
from infona_client.resolver.models import IngestResult

router = APIRouter(prefix="/graphs/{tenant}/extract-sources")
_log = structlog.stdlib.get_logger("infona.api.extract_sources")


class OkResponse(BaseModel):
    ok: bool = True


def _store():
    return make_extract_source_store()


def _strip_inline_secrets(source: DltSourceSpec, secrets: dict[str, str]) -> DltSourceSpec:
    """Move inline token / literal DSN into the write-only secrets map."""
    merged = dict(secrets)
    auth = source.auth
    if auth and auth.token:
        merged.setdefault("token", auth.token)
        logical = (
            auth.secret_ref
            if auth.secret_ref and not auth.secret_ref.startswith(("env:", "store:"))
            else "token"
        )
        auth = auth.model_copy(update={"token": None, "secret_ref": logical})
    dsn = source.dsn
    if dsn and not dsn.startswith(("env:", "store:")) and "://" in dsn:
        merged.setdefault("dsn", dsn)
        dsn = "dsn"
        if auth is None:
            auth = DltAuthSpec(type="none", secret_ref="dsn")
    secrets.clear()
    secrets.update(merged)
    from infona_client.ingestion.models import CREDENTIAL_HEADER_NAMES

    headers = {
        k: v
        for k, v in (source.headers or {}).items()
        if str(k).lower() not in CREDENTIAL_HEADER_NAMES
    }
    return source.model_copy(update={"auth": auth, "dsn": dsn, "headers": headers})


def _to_extract(body: CreateExtractSourceRequest) -> DltExtractSource:
    secrets = dict(body.secrets)
    source = _strip_inline_secrets(body.source, secrets)
    body.secrets.clear()
    body.secrets.update(secrets)
    return DltExtractSource(
        slug=body.slug,
        title=body.title or body.slug,
        kind=source.kind,
        source=source,
        map=body.map,
        kg=body.kg,
        enabled=body.enabled,
    )


async def _summary(tenant_id: str, stored: StoredExtractSource) -> ExtractSourceSummary:
    src = stored.source
    return ExtractSourceSummary(
        slug=src.slug,
        title=src.title or src.slug,
        kind=src.kind,
        enabled=src.enabled,
        has_secret=await extract_has_secret(tenant_id, src.slug),
        resources=list(src.source.resources),
        mapped=list(src.map.keys()),
        kg=src.kg,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
    )


def _run_request(stored: DltExtractSource, override: Optional[RunExtractSourceRequest]) -> DltIngestRequest:
    spec = stored.source.model_copy(deep=True)
    if override and override.limit is not None:
        spec = spec.model_copy(update={"limit": override.limit})
    auth = spec.auth
    if auth and auth.secret_ref and not auth.secret_ref.startswith(("env:", "store:")):
        spec = spec.model_copy(
            update={
                "auth": auth.model_copy(
                    update={
                        "secret_ref": f"store:{secret_store_slug(stored.slug)}/{auth.secret_ref}",
                        "token": None,
                    }
                )
            }
        )
    if spec.dsn and spec.dsn == "dsn":
        spec = spec.model_copy(
            update={"dsn": f"store:{secret_store_slug(stored.slug)}/dsn"}
        )
    kg = stored.kg
    if override and override.kg:
        kg = override.kg
    if not stored.map:
        raise HTTPException(
            status_code=422,
            detail="map is required before run — set each resource's ontology type + id_field",
        )
    return DltIngestRequest(source=spec, map=stored.map, kg=kg)


@router.get("", response_model=list[ExtractSourceSummary])
async def list_extract_sources(
    tenant: TenantContext = Depends(get_tenant),
):
    require_hosted_extract(tenant)
    rows = await _store().list_for_tenant(tenant.tenant_id)
    return [await _summary(tenant.tenant_id, r) for r in rows]


@router.post("", response_model=ExtractSourceSummary)
async def create_extract_source(
    body: CreateExtractSourceRequest,
    tenant: TenantContext = Depends(require_tenant_write),
):
    require_hosted_extract(tenant)
    existing = await _store().get(tenant.tenant_id, body.slug.strip().lower())
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"extract source {body.slug!r} already exists")
    extract = _to_extract(body)
    if body.secrets:
        try:
            await put_extract_secrets(tenant.tenant_id, extract.slug, body.secrets)
        except Exception as exc:
            raise_dlt_http(exc)
            raise
    stored = await _store().upsert(
        StoredExtractSource(tenant_id=tenant.tenant_id, source=extract)
    )
    return await _summary(tenant.tenant_id, stored)


@router.get("/{slug}", response_model=dict[str, Any])
async def get_extract_source(
    slug: str,
    tenant: TenantContext = Depends(get_tenant),
):
    require_hosted_extract(tenant)
    row = await _store().get(tenant.tenant_id, slug)
    if row is None:
        raise HTTPException(status_code=404, detail="extract source not found")
    summary = await _summary(tenant.tenant_id, row)
    spec = row.source.source.redacted()
    return {
        **summary.model_dump(mode="json"),
        "source": spec,
        "map": {k: v.model_dump() for k, v in row.source.map.items()},
    }


@router.patch("/{slug}", response_model=ExtractSourceSummary)
async def update_extract_source(
    slug: str,
    body: UpdateExtractSourceRequest,
    tenant: TenantContext = Depends(require_tenant_write),
):
    require_hosted_extract(tenant)
    row = await _store().get(tenant.tenant_id, slug)
    if row is None:
        raise HTTPException(status_code=404, detail="extract source not found")
    current = row.source
    secrets = dict(body.secrets)
    source = current.source
    if body.source is not None:
        source = _strip_inline_secrets(body.source, secrets)
    if secrets:
        try:
            await put_extract_secrets(tenant.tenant_id, current.slug, secrets)
        except Exception as exc:
            raise_dlt_http(exc)
            raise
    updated = current.model_copy(
        update={
            "title": current.title if body.title is None else body.title,
            "source": source,
            "kind": source.kind,
            "map": current.map if body.map is None else body.map,
            "kg": current.kg if body.kg is None else body.kg,
            "enabled": current.enabled if body.enabled is None else body.enabled,
        }
    )
    stored = await _store().upsert(
        StoredExtractSource(tenant_id=tenant.tenant_id, source=updated)
    )
    return await _summary(tenant.tenant_id, stored)


@router.delete("/{slug}", response_model=OkResponse)
async def delete_extract_source(
    slug: str,
    tenant: TenantContext = Depends(require_tenant_write),
):
    require_hosted_extract(tenant)
    deleted = await _store().delete(tenant.tenant_id, slug)
    if not deleted:
        raise HTTPException(status_code=404, detail="extract source not found")
    await delete_extract_secrets(tenant.tenant_id, slug)
    return OkResponse()


@router.post("/{slug}/run", response_model=IngestResult)
@limiter.limit("10/minute")
async def run_extract_source(
    request: Request,
    slug: str,
    body: Optional[RunExtractSourceRequest] = None,
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
    job_store=Depends(get_enrichment_job_store),
):
    require_hosted_extract(tenant)
    row = await _store().get(tenant.tenant_id, slug)
    if row is None:
        raise HTTPException(status_code=404, detail="extract source not found")
    if not row.source.enabled:
        raise HTTPException(status_code=409, detail="extract source is disabled")
    from infona_client.ingestion.run import run_dlt_ingest

    dlt_body = _run_request(row.source, body)
    start = time.monotonic()
    try:
        result = await run_dlt_ingest(
            tenant_id=tenant.tenant_id,
            body=dlt_body,
            neptune=client,
            anthropic_key=settings.anthropic_api_key,
            job_store=job_store,
            store_get=tenant_store_getter(tenant.tenant_id),
        )
    except Exception as exc:
        raise_dlt_http(exc)
        raise
    emit(
        "ingestion_completed",
        distinct_id=distinct_id_for(tenant.subject, tenant.tenant_id),
        tenant=tenant.tenant_id,
        kg=dlt_body.kg or "",
        rows=result.rows_in,
        entities=result.entities_resolved or result.entities_extracted,
        triples=result.triples_inserted,
        duration_ms=round((time.monotonic() - start) * 1000, 1),
    )
    return result
