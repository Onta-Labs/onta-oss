"""POST /graphs/{tenant}/corrections — the A10 user-correction write path (ONTA-281).

The ONE canonical route every client (Explorer webapp, CLI, MCP) uses to fix a
wrong fact. A human corrects an attribute value in the Explorer; this route turns
it into an A10 :class:`~cograph_client.pipeline.corrections.UserAssertion` and
applies it via the shared P6 writer, which supersedes the wrong value and stamps
the corrected fact at the TOP ``user_assertion`` provenance authority — so a later
refresh can never clobber it (ONTA-276 conflict policy ranks it highest).

Tenant-scoped + auth'd exactly like the other ``/graphs/{tenant}/...`` routes
(``get_tenant`` resolves + authorizes the path tenant). The correction's
``actor`` is taken from the AUTHENTICATED subject, never from the request body —
a client cannot spoof who made a correction.

The correction affordance sits on literal-attribute rows, so the route mints the
literal predicate (``types/<Type>/attrs/<attribute>``) via the shared write-side
helper; the entity's type is derived from the subject IRI. No backend logic is
reimplemented client-side — the interface sends (subject, attribute, value) and
the canonical route does the rest (interface-convergence rule).
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from cograph_client.api.deps import get_neptune_client
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.queries import kg_graph_uri
from cograph_client.pipeline.corrections import (
    USER_ASSERTION_AUTHORITY,
    UserAssertion,
    UserAssertionError,
    apply_user_assertion,
    literal_attribute_predicate,
)

logger = structlog.stdlib.get_logger("cograph.api.corrections")

router = APIRouter(prefix="/graphs/{tenant}")


class CorrectionRequest(BaseModel):
    """Body for a user correction of one literal attribute value.

    ``subject`` is the entity IRI the Explorer record row carries (``rec.id``);
    ``attribute`` is the attribute leaf (the ``FieldStat.key`` on the drawer
    field); ``value`` is the corrected value. ``type_name`` is optional — the
    route derives it from the subject IRI (``…/entities/<Type>/<slug>``) when the
    caller omits it. ``reason`` is an optional free-text note.
    """

    kg_name: str = Field(..., description="KG the entity lives in")
    subject: str = Field(..., description="Canonical entity IRI (the record's rec.id)")
    attribute: str = Field(..., description="Attribute leaf name (the field's key)")
    value: str = Field(..., description="The corrected value")
    type_name: str = Field("", description="Entity type; derived from subject when omitted")
    reason: str = Field("", description="Optional note explaining the correction")


class CorrectionResponse(BaseModel):
    """The applied correction's receipt: what became current, what it retired,
    and the top authority it was stamped at."""

    status: str
    subject: str
    predicate: str
    value: str
    authority: str
    superseded: list[str]
    run_id: str


@router.post("/corrections", response_model=CorrectionResponse)
async def create_correction(
    req: CorrectionRequest,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Apply an A10 user correction and return its A6 receipt.

    Validates the body, derives the entity type + literal predicate on the write
    side, stamps ``actor`` from the authenticated subject, and calls the shared
    :func:`apply_user_assertion` writer (supersede + top-authority provenance,
    all through kg_writer). Returns the corrected value now current, the wrong
    value(s) retired, and the ``user_assertion`` authority the fix carries.
    """
    if not req.kg_name.strip():
        raise HTTPException(status_code=422, detail="kg_name must be non-empty")
    if not req.subject.strip():
        raise HTTPException(status_code=422, detail="subject must be non-empty")
    if not req.attribute.strip():
        raise HTTPException(status_code=422, detail="attribute must be non-empty")
    if not str(req.value).strip():
        raise HTTPException(status_code=422, detail="value must be non-empty")

    assertion = UserAssertion(
        predicate="",  # filled below once the type resolves
        value=req.value,
        subject=req.subject.strip(),
        type_name=req.type_name.strip(),
        # actor is the AUTHENTICATED identity — never a client-supplied field.
        actor=tenant.subject or "",
        reason=req.reason.strip(),
    )

    # Resolve the entity type (explicit, else parsed from the subject IRI), then
    # mint the literal predicate on the write side.
    try:
        type_name = assertion.resolved_type()
    except UserAssertionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    predicate = literal_attribute_predicate(type_name, req.attribute.strip())
    # UserAssertion is frozen; rebuild it with the resolved predicate + type.
    assertion = UserAssertion(
        predicate=predicate,
        value=req.value,
        subject=req.subject.strip(),
        type_name=type_name,
        actor=tenant.subject or "",
        reason=req.reason.strip(),
    )

    run_id = f"correction-{uuid.uuid4()}"
    instance_graph = kg_graph_uri(tenant.tenant_id, req.kg_name)

    try:
        receipt = await apply_user_assertion(
            client,
            instance_graph,
            assertion,
            run_id=run_id,
            tenant_id=tenant.tenant_id,
            kg_name=req.kg_name,
        )
    except UserAssertionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    logger.info(
        "correction_applied",
        tenant=tenant.tenant_id,
        kg=req.kg_name,
        subject=assertion.subject,
        predicate=predicate,
        superseded=len(receipt.superseded),
    )
    return CorrectionResponse(
        status="ok",
        subject=assertion.subject,
        predicate=predicate,
        value=req.value,
        authority=USER_ASSERTION_AUTHORITY.value,
        superseded=[o for _s, _p, o in receipt.superseded],
        run_id=run_id,
    )
