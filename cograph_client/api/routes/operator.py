"""Operator-only routes (ONTA staff / ``@onta.team``).

Visibility is gated on :attr:`TenantContext.is_operator`, which the auth
provider sets from a *verified* ``@onta.team`` email (or allowlist /
``public_metadata.is_operator``) — never from a client-supplied flag
(ONTA-234). Non-operators receive 403; missing jobs 404.

The Job Stage Trace endpoint is deliberately **not** under
``/graphs/{tenant}/…`` so an operator can look up any job by id without
knowing its tenant first (cross-tenant debugging of prod regressions).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from cograph_client.api.deps import get_enrichment_job_store
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.pipeline.stage_trace import JobStageTrace, resolve_trace

router = APIRouter(prefix="/operator", tags=["operator"])


def require_operator(
    tenant: TenantContext = Depends(get_tenant),
) -> TenantContext:
    """Fail closed unless the authenticated identity is an ONTA operator."""
    if not tenant.is_operator:
        raise HTTPException(status_code=403, detail="operator only")
    return tenant


@router.get("/jobs/{job_id}/trace", response_model=JobStageTrace)
async def get_job_stage_trace(
    job_id: str,
    _operator: TenantContext = Depends(require_operator),
    job_store=Depends(get_enrichment_job_store),
):
    """Return the P0–P9 contract-level stage trace for a job.

    Cross-tenant: any job id the store knows about is visible to operators.
    Prefer live ``job.stage_trace`` when present; otherwise reconstruct from
    manifest / provider_logs / progress so pre-instrumentation jobs still
    render.

    **Ask/agent answer runs (ONTA-389):** a completed ``/ask`` or agent
    ``kind:answer`` turn mints a job with ``category=answer`` and returns
    ``run_id`` (= this ``job_id``) on the answer payload. Open this endpoint with
    that id to see live **P7 Answer (A7)** + **P0/A9** coverage on Job Trace.
    """
    job = await job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    # Keep job status / category current on the envelope even if the stored
    # live trace is slightly stale mid-run.
    trace = resolve_trace(job)
    status = getattr(getattr(job, "status", None), "value", None) or str(
        getattr(job, "status", "") or ""
    )
    category = getattr(getattr(job, "category", None), "value", None) or str(
        getattr(job, "category", "") or ""
    )
    trace.status = status or trace.status
    trace.category = category or trace.category
    trace.tenant_id = job.tenant_id or trace.tenant_id
    trace.kg_name = job.kg_name or trace.kg_name
    return trace
