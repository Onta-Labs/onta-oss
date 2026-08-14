"""Unified jobs list (COG-101).

A single endpoint that lists ALL tracked jobs for a tenant — dedupe,
enrichment, reconciliation, discovery, and file ingest — from the configured
job store (in-memory or Postgres). This complements, and does not replace, the
enrichment-specific ``/graphs/{tenant}/enrich/jobs`` routes: those remain the
place to create, inspect conflicts for, apply, and cancel enrichment jobs.

``DELETE /graphs/{tenant}/jobs`` hard-removes every row for the tenant (demo
reset / tenant wipe). ``DELETE /graphs/{tenant}/enrich/jobs/{id}`` stays
cancel-only — that is a run-control verb, not a store purge.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from infona_client.api.deps import get_enrichment_job_store
from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.enrichment.models import JobCategory, JobSummary

router = APIRouter(prefix="/graphs/{tenant}/jobs")


class PurgeJobsResponse(BaseModel):
    deleted: int


class DeleteJobResponse(BaseModel):
    deleted: bool
    job_id: str


@router.get("", response_model=list[JobSummary])
async def list_jobs(
    category: Optional[JobCategory] = Query(
        None, description="Filter to a single job category."
    ),
    tenant: TenantContext = Depends(get_tenant),
    job_store=Depends(get_enrichment_job_store),
):
    """List a tenant's jobs across all categories, newest first.

    Pass ``?category=dedupe|enrichment|reconciliation|discovery|ingest`` to
    filter. Each item is a ``JobSummary`` carrying the unified fields the Jobs
    page renders: category, trigger, last_run, next_run, cost (+ note), status,
    and the derived ``progress_pct``.
    """
    summaries = await job_store.list_for_tenant(tenant.tenant_id)
    if category is not None:
        summaries = [s for s in summaries if s.category == category]
    return summaries


@router.delete("", response_model=PurgeJobsResponse)
async def purge_jobs(
    tenant: TenantContext = Depends(require_tenant_write),
    job_store=Depends(get_enrichment_job_store),
):
    """Hard-delete every job for this tenant.

    Used by demo reset / tenant wipe. Does not touch instance data or the
    ontology — only the job store (enrichment, ingest, ask, dedupe, …).
    Idempotent: an empty tenant returns ``deleted=0``.
    """
    deleted = await job_store.delete_for_tenant(tenant.tenant_id)
    return PurgeJobsResponse(deleted=deleted)


@router.delete("/{job_id}", response_model=DeleteJobResponse)
async def delete_job(
    job_id: str,
    tenant: TenantContext = Depends(require_tenant_write),
    job_store=Depends(get_enrichment_job_store),
):
    """Hard-delete one job. 404 if it is missing or belongs to another tenant."""
    job = await job_store.get(job_id)
    if not job or job.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=404, detail="job not found")
    await job_store.delete(job_id)
    return DeleteJobResponse(deleted=True, job_id=job_id)
