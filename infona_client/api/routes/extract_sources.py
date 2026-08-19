"""Persist 3rd-party extract sources (ONTA-554). Distinct from /api-sources.

  GET    /graphs/{tenant}/extract-sources
  GET    /graphs/{tenant}/extract-sources/catalog
  POST   /graphs/{tenant}/extract-sources
  GET    /graphs/{tenant}/extract-sources/{slug}
  PATCH  /graphs/{tenant}/extract-sources/{slug}
  DELETE /graphs/{tenant}/extract-sources/{slug}
  POST   /graphs/{tenant}/extract-sources/{slug}/run
  PUT    /graphs/{tenant}/extract-sources/{slug}/schedule
  DELETE /graphs/{tenant}/extract-sources/{slug}/schedule

Run builds the frozen ``POST /ingest/dlt`` body from the saved config and
calls the same :func:`run_dlt_ingest` handoff, through the shared
``ingestion/saved_run.py`` path the scheduler's ``extract`` action also uses —
a scheduled read and a pressed Run are the same work (ONTA-555). ``catalog``
returns the prefill templates every client picks from; ``schedule`` writes an
ordinary row into the shared schedule store. Secrets stay in the existing
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
from infona_client.api.deps import (
    get_enrichment_job_store,
    get_neptune_client,
    get_schedule_store,
)
from infona_client.api.rate_limit import limiter
from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.ingestion.catalog import (
    ConnectorTemplate,
    list_connectors,
    unresolved_placeholders,
)
from infona_client.ingestion.extract_source_store import (
    StoredExtractSource,
    make_extract_source_store,
)
from infona_client.ingestion.hosted import require_hosted_extract
from infona_client.ingestion.http import raise_dlt_http
from infona_client.ingestion.models import (
    CreateExtractSourceRequest,
    DltAuthSpec,
    DltExtractSource,
    DltSourceSpec,
    ExtractScheduleRequest,
    ExtractSourceSummary,
    RunExtractSourceRequest,
    UpdateExtractSourceRequest,
)
from infona_client.ingestion.saved_run import ExtractNotRunnable, run_saved_extract
from infona_client.ingestion.schedule import (
    delete_extract_schedule,
    find_extract_schedule,
    schedule_info,
    schedules_by_slug,
    upsert_extract_schedule,
)
from infona_client.ingestion.store_secrets import (
    delete_extract_secrets,
    extract_has_secret,
    put_extract_secrets,
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


async def _summary(
    tenant_id: str, stored: StoredExtractSource, schedule=None
) -> ExtractSourceSummary:
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
        schedule=schedule_info(schedule) if schedule is not None else None,
    )


#: ``/catalog`` is a sibling of ``/{slug}``; FastAPI matches in declaration
#: order, so the literal route below wins — and this name is refused as a slug
#: on create so no source can ever be shadowed by it.
RESERVED_SLUGS = frozenset({"catalog"})


@router.get("/catalog", response_model=list[ConnectorTemplate])
async def get_connector_catalog(
    tenant: TenantContext = Depends(get_tenant),
):
    """Prefill templates for the connect flow (ONTA-555).

    Static data — the same list for every workspace — served from the backend
    rather than each client shipping a copy, so the Explorer, the CLI and MCP
    offer the identical catalog. No secrets: a template names the credential
    the user must paste (BYOK), never one we hold.
    """
    require_hosted_extract(tenant)
    return list_connectors()


def _reject_unfinished_template(source: DltSourceSpec) -> None:
    """422 when a base URL still carries ``{placeholder}`` slots.

    Saving ``https://{store}.myshopify.com`` would fail later as an opaque DNS
    error; naming the unfilled slots here is the actionable version.
    """
    slots = unresolved_placeholders(source.base_url)
    if slots:
        raise HTTPException(
            status_code=422,
            detail=(
                "base_url still has unfilled placeholder(s): "
                + ", ".join(sorted(set(slots)))
            ),
        )


@router.get("", response_model=list[ExtractSourceSummary])
async def list_extract_sources(
    tenant: TenantContext = Depends(get_tenant),
    schedule_store=Depends(get_schedule_store),
):
    require_hosted_extract(tenant)
    rows = await _store().list_for_tenant(tenant.tenant_id)
    by_slug = await schedules_by_slug(schedule_store, tenant.tenant_id)
    return [
        await _summary(tenant.tenant_id, r, by_slug.get(r.slug)) for r in rows
    ]


@router.post("", response_model=ExtractSourceSummary)
async def create_extract_source(
    body: CreateExtractSourceRequest,
    tenant: TenantContext = Depends(require_tenant_write),
):
    require_hosted_extract(tenant)
    slug = body.slug.strip().lower()
    if slug in RESERVED_SLUGS:
        raise HTTPException(
            status_code=422, detail=f"slug {slug!r} is reserved — pick another name"
        )
    existing = await _store().get(tenant.tenant_id, slug)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"extract source {body.slug!r} already exists")
    _reject_unfinished_template(body.source)
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
    schedule_store=Depends(get_schedule_store),
):
    require_hosted_extract(tenant)
    row = await _store().get(tenant.tenant_id, slug)
    if row is None:
        raise HTTPException(status_code=404, detail="extract source not found")
    schedule = await find_extract_schedule(schedule_store, tenant.tenant_id, slug)
    summary = await _summary(tenant.tenant_id, row, schedule)
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
    schedule_store=Depends(get_schedule_store),
):
    require_hosted_extract(tenant)
    row = await _store().get(tenant.tenant_id, slug)
    if row is None:
        raise HTTPException(status_code=404, detail="extract source not found")
    current = row.source
    secrets = dict(body.secrets)
    source = current.source
    if body.source is not None:
        _reject_unfinished_template(body.source)
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
    schedule = await find_extract_schedule(schedule_store, tenant.tenant_id, slug)
    return await _summary(tenant.tenant_id, stored, schedule)


@router.delete("/{slug}", response_model=OkResponse)
async def delete_extract_source(
    slug: str,
    tenant: TenantContext = Depends(require_tenant_write),
    schedule_store=Depends(get_schedule_store),
):
    require_hosted_extract(tenant)
    deleted = await _store().delete(tenant.tenant_id, slug)
    if not deleted:
        raise HTTPException(status_code=404, detail="extract source not found")
    await delete_extract_secrets(tenant.tenant_id, slug)
    # A cadence pointing at a deleted source would fire forever against nothing.
    await delete_extract_schedule(schedule_store, tenant.tenant_id, slug)
    return OkResponse()


@router.post("/{slug}/run", response_model=IngestResult)
@limiter.limit("10/minute")
async def run_extract_source(
    request: Request,
    slug: str,
    body: Optional[RunExtractSourceRequest] = None,
    tenant: TenantContext = Depends(require_tenant_write),
    # Typed as Any on purpose: the SPARQL client is being removed (ONTA-527);
    # this route only hands it to the shared run path, which takes it as Any.
    client: Any = Depends(get_neptune_client),
    job_store=Depends(get_enrichment_job_store),
):
    require_hosted_extract(tenant)
    row = await _store().get(tenant.tenant_id, slug)
    if row is None:
        raise HTTPException(status_code=404, detail="extract source not found")
    if not row.source.enabled:
        raise HTTPException(status_code=409, detail="extract source is disabled")
    start = time.monotonic()
    try:
        result = await run_saved_extract(
            tenant_id=tenant.tenant_id,
            source=row.source,
            neptune=client,
            job_store=job_store,
            override=body,
        )
    except ExtractNotRunnable as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise_dlt_http(exc)
        raise
    emit(
        "ingestion_completed",
        distinct_id=distinct_id_for(tenant.subject, tenant.tenant_id),
        tenant=tenant.tenant_id,
        kg=body.kg if body and body.kg else (row.source.kg or ""),
        rows=result.rows_in,
        entities=result.entities_resolved or result.entities_extracted,
        triples=result.triples_inserted,
        duration_ms=round((time.monotonic() - start) * 1000, 1),
    )
    return result


# --- schedule (ONTA-555) ------------------------------------------------------
#
# A cadence is an ordinary row in the SHARED schedule store (action "extract"),
# fired by the SAME runner as enrich / dedupe / notify — see
# ``ingestion/schedule.py``. These two routes exist so a client sets a source's
# cadence in one call instead of hand-joining schedule rows to sources; they are
# not a second scheduler and write nothing the /schedules family cannot read.


@router.put("/{slug}/schedule", response_model=ExtractSourceSummary)
async def put_extract_schedule(
    slug: str,
    body: ExtractScheduleRequest,
    tenant: TenantContext = Depends(require_tenant_write),
    schedule_store=Depends(get_schedule_store),
):
    """Set (or replace) how often this source is re-read."""
    require_hosted_extract(tenant)
    row = await _store().get(tenant.tenant_id, slug)
    if row is None:
        raise HTTPException(status_code=404, detail="extract source not found")
    if not row.source.map:
        # An unmapped source cannot run, so a cadence would fail on every fire.
        raise HTTPException(
            status_code=422,
            detail="map the resources to ontology types before scheduling reads",
        )
    try:
        schedule = await upsert_extract_schedule(
            schedule_store,
            tenant_id=tenant.tenant_id,
            slug=slug,
            kg_name=row.source.kg or "",
            body=body,
        )
    except NotImplementedError as exc:
        # cron without the optional croniter package — same 501 /schedules gives.
        raise HTTPException(status_code=501, detail=str(exc))
    return await _summary(tenant.tenant_id, row, schedule)


@router.delete("/{slug}/schedule", response_model=ExtractSourceSummary)
async def clear_extract_schedule(
    slug: str,
    tenant: TenantContext = Depends(require_tenant_write),
    schedule_store=Depends(get_schedule_store),
):
    """Stop recurring reads. The source stays, runnable on demand."""
    require_hosted_extract(tenant)
    row = await _store().get(tenant.tenant_id, slug)
    if row is None:
        raise HTTPException(status_code=404, detail="extract source not found")
    await delete_extract_schedule(schedule_store, tenant.tenant_id, slug)
    return await _summary(tenant.tenant_id, row)
