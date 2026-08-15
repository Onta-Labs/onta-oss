"""On-demand semantic instance reindex for one KG (ONTA-181)."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from infona_client.api.deps import get_neptune_client, get_schedule_store
from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext


class ReindexAccepted(BaseModel):
    """202 body for the on-demand semantic reindex trigger."""

    status: str = "accepted"
    kg_name: str
    schedule_id: str
    # "scheduled"       → a due-now schedule row was seeded; the claim-based
    #                     runner fires it (multi-task safe via SKIP LOCKED).
    # "background-task" → no runner in this deployment (zero-config OSS);
    #                     the reconcile was fired as an in-process task.
    mode: str


async def reindex_kg_semantic(
    kg_name: str,
    request: Request,
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
    schedule_store=Depends(get_schedule_store),
):
    """Trigger an on-demand semantic reconcile (= backfill) for one KG.

    THE entry point for indexing an already-ingested KG without re-ingesting
    (ONTA-181's parliamentary-speeches scenario): the reconciler's first run
    against a KG is the backfill. Deliberately NOT an inline long-running
    request — it seeds the KG's recurring reconcile schedule row with
    ``next_run=now`` and returns 202 immediately; the claim-based schedule
    runner picks it up within one poll interval, so overlapping ECS tasks never
    double-scan. Deployments without a runner (no DSN, scheduler off) fall back
    to a fire-and-forget in-process task — single process, so no claim needed.

    503 when the semantic index is disabled (``INFONA_SEMANTIC_INDEX_ENABLED``
    is the master gate for the write hook AND the reconciler): accepting the
    request would acknowledge work that can never run.
    """
    from infona_client.semantic.reconciler import (
        ensure_reconcile_schedule,
        schedule_reconcile_task,
        semantic_index_enabled,
    )

    if not semantic_index_enabled():
        raise HTTPException(
            status_code=503,
            detail=(
                "Semantic indexing is disabled for this deployment "
                "(set INFONA_SEMANTIC_INDEX_ENABLED=true to enable it)."
            ),
        )

    schedule = await ensure_reconcile_schedule(
        schedule_store, tenant.tenant_id, kg_name, due_now=True
    )
    runner = getattr(request.app.state, "schedule_runner", None)
    if runner is None:
        schedule_reconcile_task(client, tenant.tenant_id, kg_name)
        mode = "background-task"
    else:
        mode = "scheduled"
    return ReindexAccepted(kg_name=kg_name, schedule_id=schedule.id, mode=mode)
