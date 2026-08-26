"""THE single conversational surface for the unified Ask-AI agent (COG-118).

``POST /graphs/{tenant}/agent`` is the ONLY endpoint. Everything the agent can do
is a capability behind the registry — there is no per-task conversational
endpoint. The legacy ``/ask``, ``/enrich/*`` and ``/normalize/*`` routes stay for
back-compat (existing dialogs), but the agent does NOT call them: it drives the
underlying engines directly through the capability registry.

Request/response contract:

  POST body {message, context:{kg_name, type_name, selection?}, session_id?,
             confirm?:{plan_id}}
    - confirm.plan_id present → execute_plan → {kind:"result", steps:[...]}
      (execute is the only mutating path; long work runs as background jobs)
    - else → planner.handle → {kind: "answer"|"clarify"|"plan"}

Capabilities are registered at import time via
:func:`infona_client.agent.planner.register_default_capabilities` (also invoked
from ``app.py`` at startup, import-safe + idempotent).
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from infona_client.agent import planner
from infona_client.agent.planner import register_default_capabilities
from infona_client.agent.registry import AgentContext, ReadOnlyMembershipError
from infona_client.api.deps import (
    get_enrichment_job_store,
    get_executor,
    get_neptune_client,
    get_schedule_store,
)
from infona_client.auth.access import get_tenant_with_capability
from infona_client.auth.api_keys import TenantContext
from infona_client.config import settings
from infona_client.enrichment.executor import EnrichmentExecutor
from infona_client.graph.client import NeptuneClient

router = APIRouter(prefix="/graphs/{tenant}/agent")

# Register the default OSS capabilities on import so the single endpoint works
# even if app.py's explicit startup call is bypassed (e.g. a test mounting only
# this router). Idempotent — last-write-wins.
register_default_capabilities()


class AgentRequestContext(BaseModel):
    # ONTA-414: same pattern the /ask body enforces and the same one create
    # enforces (KGCreate.name). This value reaches kg_graph_uri and is
    # interpolated into a graph IRI inside generated SPARQL, so a ">" in it
    # would close the IRI early and permit a second FROM naming another
    # tenant's graph. "*" keeps the existing "" default ("no KG selected") legal.
    kg_name: str = Field("", pattern=r"^[a-zA-Z0-9_-]*$")
    type_name: str | None = None
    selection: dict | None = None
    # Explicit URLs the user attached for this turn (the Explorer's "paste links"
    # affordance). Optional + defaulted so existing clients are unaffected; the
    # planner routes a URL-bearing turn and capabilities extract records from
    # these pages via the premium URL-targeted seam.
    urls: list[str] = []
    # Which interface is calling — "explorer" / "cli" / "mcp" / "sdk". Optional +
    # defaulted so existing clients are unaffected. ONE canonical field on the ONE
    # canonical route (never a per-interface endpoint or header convention) —
    # capabilities tag per-stage cost/latency telemetry with it.
    medium: str = ""


class Confirm(BaseModel):
    plan_id: str


class AgentRequest(BaseModel):
    message: str = Field("", description="The user's message to the agent")
    context: AgentRequestContext = Field(default_factory=AgentRequestContext)
    session_id: str | None = None
    confirm: Confirm | None = None
    # Optional HARD per-run spend ceiling (USD) for any enrichment/discovery job
    # this turn kicks off (ONTA-282/ONTA-378). Default None → deployment default
    # (unchanged behavior). Threaded onto AgentContext so the enrich/discovery
    # capability stamps it onto the job it creates, where the executor's
    # ``resolve_spend_ceiling(...)`` lets it WIN over the global default and bound
    # that single job — so a caller (e.g. persona-eval's disposable tenant) can
    # cap one run without touching the global/production ceiling.
    spend_ceiling_usd: float | None = None


def _build_ctx(
    tenant: TenantContext,
    body: AgentRequest,
    client: NeptuneClient,
    executor: EnrichmentExecutor,
    job_store,
    schedule_store=None,
) -> AgentContext:
    return AgentContext(
        tenant_id=tenant.tenant_id,
        kg_name=body.context.kg_name,
        neptune=client,
        type_name=body.context.type_name,
        selection=body.context.selection,
        urls=body.context.urls,
        medium=body.context.medium,
        # Thread the conversation id through so a capability can stamp it onto any
        # job it creates (chat → job provenance). Covers both the classify/handle
        # and the confirm/execute-plan paths since ctx is built once from body.
        session_id=body.session_id,
        # Per-run HARD spend ceiling (ONTA-378): threaded so the enrich/discovery
        # capability bounds the single job it creates. None → deployment default.
        spend_ceiling_usd=body.spend_ceiling_usd,
        # Tenant-level membership capability ("write" | "read") resolved by
        # get_tenant_with_capability. The planner refuses to COMMIT a mutating
        # plan when this is "read" (ONTA-451) — see agent_turn's docstring for
        # why the gate lives at capability dispatch, not on the route.
        capability=tenant.capability,
        openrouter_key=settings.openrouter_api_key
        or os.environ.get("OPENROUTER_API_KEY", ""),
        anthropic_key=settings.anthropic_api_key,
        extras={
            "tenant": tenant,
            "enrichment_executor": executor,
            "enrichment_job_store": job_store,
            # The subscribe capability persists a ``notify`` Schedule through the
            # SAME schedule store the canonical /schedules route uses (no bespoke
            # endpoint / logic — interface convergence). Built lazily on app.state.
            "schedule_store": schedule_store,
        },
    )


@router.post("")
async def agent_turn(
    body: AgentRequest,
    tenant: TenantContext = Depends(get_tenant_with_capability),
    client: NeptuneClient = Depends(get_neptune_client),
    executor: EnrichmentExecutor = Depends(get_executor),
    job_store=Depends(get_enrichment_job_store),
    schedule_store=Depends(get_schedule_store),
):
    """One agent turn: confirm→execute a plan, or classify+respond to a message.

    **Write authorization (ONTA-451).** This is the one READ/WRITE MIXED route in
    the API: the same endpoint answers a question and ingests a dataset. A
    blanket ``Depends(require_tenant_write)`` — the gate every single-purpose
    mutating route uses — would therefore 403 a read-only member out of the
    read-only turns their role explicitly permits (query / ask / research /
    ontology inspection), which is the wrong product behavior, not just a
    stricter one.

    So the gate sits at CAPABILITY DISPATCH instead: ``get_tenant_with_capability``
    resolves the membership capability, ``_build_ctx`` threads it onto
    :class:`AgentContext`, and the planner refuses at the two points where a
    mutation is actually committed — persisting a mutating plan, and
    ``execute_plan`` (the only path that runs one). Capability classification is
    deny-by-default, so a capability that does not declare ``writes = False`` is
    treated as mutating. The resulting
    :class:`~infona_client.agent.registry.ReadOnlyMembershipError` is translated
    to HTTP 403 here, with the same wording ``require_tenant_write`` uses.
    """
    ctx = _build_ctx(tenant, body, client, executor, job_store, schedule_store)
    try:
        if body.confirm is not None:
            return await planner.execute_plan(ctx, body.confirm.plan_id)
        # Tag the thread with the auth subject (the signed-in user) so it shows up
        # in their conversation history (COG-131). Ownerless (static/demo key)
        # sessions carry owner=None and never appear in anyone's list.
        return await planner.handle(
            ctx,
            body.message,
            session={"id": body.session_id, "owner": tenant.subject},
        )
    except ReadOnlyMembershipError as exc:
        raise HTTPException(status_code=403, detail=exc.detail) from exc
