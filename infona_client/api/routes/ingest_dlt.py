"""POST /graphs/{tenant}/ingest/dlt — dlt extract then ingest (ONTA-553).

Canonical execute route for 3rd-party REST/SQL sources. Persistence of
workspace configs is ``extract_sources``; this module only runs the frozen
``{source, map, kg}`` body through ``run_dlt_ingest`` (``insert_facts`` +
``refresh_after_write``). ``env:`` BYOK is ungated; ``store:`` secrets require
hosted entitlement when a premium checker is registered (ONTA-554).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request

from infona_client.analytics import distinct_id_for, emit
from infona_client.api.deps import get_enrichment_job_store, get_neptune_client
from infona_client.api.rate_limit import limiter
from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext
from infona_client.config import settings
from infona_client.ingestion.hosted import require_hosted_if_store_secret
from infona_client.ingestion.http import raise_dlt_http
from infona_client.ingestion.models import DltIngestRequest
from infona_client.ingestion.run import run_dlt_ingest
from infona_client.ingestion.store_secrets import tenant_store_getter
from infona_client.resolver.models import IngestResult

router = APIRouter(prefix="/graphs/{tenant}")


@router.post("/ingest/dlt", response_model=IngestResult)
@limiter.limit("10/minute")
async def ingest_dlt(
    request: Request,
    body: DltIngestRequest,
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
    job_store=Depends(get_enrichment_job_store),
):
    require_hosted_if_store_secret(tenant, body.source)
    start = time.monotonic()
    try:
        result = await run_dlt_ingest(
            tenant_id=tenant.tenant_id,
            body=body,
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
        kg=body.kg or "",
        rows=result.rows_in,
        entities=result.entities_resolved or result.entities_extracted,
        triples=result.triples_inserted,
        duration_ms=round((time.monotonic() - start) * 1000, 1),
    )
    return result
