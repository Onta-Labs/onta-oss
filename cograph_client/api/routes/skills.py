"""HTTP routes for type-attached SKILLS — the canonical CRUD + resolution
surface every client (Explorer webapp, CLI, MCP) reaches through the shared SDK.

  GET    /graphs/{tenant}/skills                          list resolved skills (all visible layers)
  POST   /graphs/{tenant}/skills                          create/replace a TENANT skill
  POST   /graphs/{tenant}/skills/validate                 validate a skill body (no write)
  GET    /graphs/{tenant}/skills/prompt-block             the exact text an agent is handed
  GET    /graphs/{tenant}/skills/{type_name}/{slug}       read one (full body)
  PATCH  /graphs/{tenant}/skills/{type_name}/{slug}       edit a TENANT skill
  DELETE /graphs/{tenant}/skills/{type_name}/{slug}       delete a TENANT skill

One canonical route per operation. ``prompt-block`` exists so a client never
re-implements the render/budget rules locally — that would be exactly the
per-interface drift the convergence rule forbids; the MCP server and the CLI ask
the backend for the same bytes the backend's own planner would inject.

Authorization: ``get_tenant`` authorizes ``{tenant}`` against the caller's key
(403 on an unowned tenant); the three mutating routes (create / update / delete)
additionally require ``require_tenant_write``, so a ``reader`` member is refused
with 403 (ONTA-451). ``validate`` writes nothing and stays on plain
``get_tenant``. The two GLOBAL layers are curated canon and are **read-only over
HTTP**: any mutation targeting a non-tenant layer returns 403, mirroring the
API-source registry's treatment of its global catalog.

Boundary: OSS. Imports only ``cograph_client.*`` / stdlib.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from cograph_client.auth.access import require_tenant_write
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.graph.entitlement import is_entitled, layer_stack_for
from cograph_client.graph.layers import Layer
from cograph_client.skills import (
    DEFAULT_PROMPT_BUDGET,
    TypeSkill,
    global_skills_by_layer,
    make_type_skill_store,
    merge_layers,
    resolve_skills,
    skills_prompt_block,
    validate_skill,
)

logger = structlog.stdlib.get_logger("cograph.skills.routes")

router = APIRouter(prefix="/graphs/{tenant}/skills")


# --------------------------------------------------------------------------- #
# Entitlement
# --------------------------------------------------------------------------- #
def _entitled(tenant: TenantContext) -> bool:
    """Does this caller see the Global-ENHANCED layer?

    Thin route-local wrapper over the frozen seam
    :func:`cograph_client.graph.entitlement.is_entitled` (Wave 0 / ONTA-396 /
    ONTA-398). OSS default remains False; premium determination plugs in via
    :func:`~cograph_client.graph.entitlement.register_entitlement_checker`.
    Resolution degrades to ``Tenant > Public``, never errors. Never consults
    client headers, ``?layer=…``, or deep links.
    """
    return is_entitled(tenant)


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
class SkillSummary(BaseModel):
    slug: str
    type_name: str
    title: str = ""
    summary: str = ""
    layer: str
    enabled: bool = True
    version: int = 1
    body_chars: int = 0
    #: Only tenant-layer skills can be edited through this API.
    editable: bool = False


class SkillDetail(SkillSummary):
    body: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateSkillRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=64)
    type_name: str = Field(min_length=1, max_length=128)
    body: str = Field(description="The markdown skill body — this IS the skill")
    title: str = ""
    summary: str = ""
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateSkillRequest(BaseModel):
    body: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    enabled: Optional[bool] = None
    metadata: Optional[dict[str, Any]] = None


class ValidationIssue(BaseModel):
    message: str


class ValidateResponse(BaseModel):
    valid: bool
    errors: list[ValidationIssue] = Field(default_factory=list)


class PromptBlockResponse(BaseModel):
    text: str
    skill_count: int
    chars: int


class OkResponse(BaseModel):
    ok: bool = True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _store():
    return make_type_skill_store()


def _summary(skill: TypeSkill) -> SkillSummary:
    return SkillSummary(
        slug=skill.slug,
        type_name=skill.type_name,
        title=skill.title,
        summary=skill.summary,
        layer=skill.layer.value,
        enabled=skill.enabled,
        version=skill.version,
        body_chars=len(skill.body or ""),
        editable=skill.layer is Layer.TENANT,
    )


def _detail(skill: TypeSkill) -> SkillDetail:
    return SkillDetail(
        **_summary(skill).model_dump(),
        body=skill.body,
        metadata=dict(skill.metadata or {}),
    )


def _raise_if_invalid(skill: TypeSkill) -> None:
    errors = validate_skill(skill)
    if errors:
        raise HTTPException(status_code=422, detail=errors)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("", response_model=list[SkillSummary])
async def list_skills(
    type_name: Optional[str] = Query(
        None, description="Restrict to one entity type (case-insensitive)."
    ),
    tenant: TenantContext = Depends(get_tenant),
):
    """List the skills visible to this workspace, in precedence order.

    Returns the RESOLVED union across every visible layer — tenant-authored
    skills plus the curated global ones — because that union is what an agent
    actually sees. Global rows come back with ``editable: false``.

    Unlike the API-source catalog, the global layers are NOT operator-gated
    here: a curated skill is content the workspace is meant to benefit from
    (and Global-Enhanced is already gated by entitlement), not a disclosure of
    our vendor stack.
    """
    if type_name:
        skills = await resolve_skills(
            type_name,
            tenant_id=tenant.tenant_id,
            entitled=_entitled(tenant),
            store=_store(),
            include_disabled=True,
        )
        return [_summary(s) for s in skills]

    # No type filter: every type the tenant has authored for, plus every global
    # type. ONE store read for the whole tenant layer, then merge per type in
    # memory — resolving per type through the store would be an N+1 across a
    # workspace's types.
    tenant_rows = await _store().list_for_tenant(tenant.tenant_id)
    by_layer: dict[Layer, list[TypeSkill]] = dict(global_skills_by_layer())
    by_layer[Layer.TENANT] = tenant_rows

    stack = layer_stack_for(tenant)
    names: dict[str, str] = {}
    for layer in stack.layers:  # entitlement already applied by the stack
        for s in by_layer.get(layer, []):
            names.setdefault(s.type_name.casefold(), s.type_name)

    out: list[SkillSummary] = []
    for name in sorted(names.values(), key=str.casefold):
        for s in merge_layers(
            by_layer, stack, type_name=name, include_disabled=True
        ):
            out.append(_summary(s))
    return out


@router.post("", response_model=SkillDetail, status_code=201)
async def create_skill(
    req: CreateSkillRequest,
    tenant: TenantContext = Depends(require_tenant_write),
):
    """Create (or replace) a TENANT-layer skill.

    Idempotent on ``(type_name, slug)``: posting the same slug again replaces the
    body and bumps ``version``, so a client that retries cannot fork the skill.
    """
    skill = TypeSkill(
        slug=req.slug,
        type_name=req.type_name,
        body=req.body,
        title=req.title,
        summary=req.summary,
        layer=Layer.TENANT,
        tenant_id=tenant.tenant_id,
        enabled=req.enabled,
        metadata=dict(req.metadata or {}),
    )
    _raise_if_invalid(skill)
    stored = await _store().upsert(skill)
    logger.info(
        "skill_upserted",
        tenant_id=tenant.tenant_id,
        type_name=stored.type_name,
        slug=stored.slug,
        version=stored.version,
    )
    return _detail(stored)


@router.post("/validate", response_model=ValidateResponse)
async def validate_skill_route(
    req: CreateSkillRequest,
    tenant: TenantContext = Depends(get_tenant),
):
    """Validate a skill without writing it (the authoring pre-flight)."""
    skill = TypeSkill(
        slug=req.slug,
        type_name=req.type_name,
        body=req.body,
        title=req.title,
        summary=req.summary,
        layer=Layer.TENANT,
        tenant_id=tenant.tenant_id,
        enabled=req.enabled,
    )
    errors = validate_skill(skill)
    return ValidateResponse(
        valid=not errors, errors=[ValidationIssue(message=e) for e in errors]
    )


@router.get("/prompt-block", response_model=PromptBlockResponse)
async def get_prompt_block(
    type_name: list[str] = Query(
        default_factory=list,
        description="Entity type(s) to resolve. Repeat for several.",
    ),
    max_chars: int = Query(DEFAULT_PROMPT_BUDGET, ge=100, le=100_000),
    tenant: TenantContext = Depends(get_tenant),
):
    """Return the EXACT text an LM agent would be handed for these types.

    The canonical read of the injection seam
    (``skills.resolve.skills_prompt_block``): clients render nothing themselves,
    so the block a CLI or MCP agent sees is byte-identical to the one the
    backend's own planner would inject. Empty ``type_name`` → empty text.
    """
    text = await skills_prompt_block(
        type_name,
        tenant_id=tenant.tenant_id,
        entitled=_entitled(tenant),
        store=_store(),
        max_chars=max_chars,
    )
    count = 0
    for name in dict.fromkeys(type_name):
        count += len(
            await resolve_skills(
                name,
                tenant_id=tenant.tenant_id,
                entitled=_entitled(tenant),
                store=_store(),
            )
        )
    return PromptBlockResponse(text=text, skill_count=count, chars=len(text))


@router.get("/{type_name}/{slug}", response_model=SkillDetail)
async def get_skill(
    type_name: str,
    slug: str,
    tenant: TenantContext = Depends(get_tenant),
):
    """Read one resolved skill (full body), whichever layer wins."""
    for skill in await resolve_skills(
        type_name,
        tenant_id=tenant.tenant_id,
        entitled=_entitled(tenant),
        store=_store(),
        include_disabled=True,
    ):
        if skill.slug == slug:
            return _detail(skill)
    raise HTTPException(
        status_code=404, detail=f"no skill '{slug}' on type '{type_name}'"
    )


@router.patch("/{type_name}/{slug}", response_model=SkillDetail)
async def update_skill(
    type_name: str,
    slug: str,
    req: UpdateSkillRequest,
    tenant: TenantContext = Depends(require_tenant_write),
):
    """Partially update a TENANT skill. 403 on a curated global skill.

    A tenant cannot edit curated content in place; the sanctioned override is to
    POST a tenant skill with the SAME slug, which shadows the global one for
    this workspace only.
    """
    existing = await _store().get(tenant.tenant_id, type_name, slug)
    if existing is None:
        # Distinguish "curated, not yours to edit" from "does not exist".
        for skill in await resolve_skills(
            type_name,
            tenant_id=tenant.tenant_id,
            entitled=_entitled(tenant),
            store=_store(),
            include_disabled=True,
        ):
            if skill.slug == slug:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"skill '{slug}' is curated ({skill.layer.value} layer) and "
                        "read-only; POST a tenant skill with the same slug to "
                        "override it for this workspace"
                    ),
                )
        raise HTTPException(
            status_code=404, detail=f"no skill '{slug}' on type '{type_name}'"
        )

    if req.body is not None:
        existing.body = req.body
    if req.title is not None:
        existing.title = req.title
    if req.summary is not None:
        existing.summary = req.summary
    if req.enabled is not None:
        existing.enabled = req.enabled
    if req.metadata is not None:
        existing.metadata = dict(req.metadata)

    _raise_if_invalid(existing)
    stored = await _store().upsert(existing)
    return _detail(stored)


@router.delete("/{type_name}/{slug}", response_model=OkResponse)
async def delete_skill(
    type_name: str,
    slug: str,
    tenant: TenantContext = Depends(require_tenant_write),
):
    """Delete a TENANT skill. 403 on a curated global skill, 404 if unknown."""
    deleted = await _store().delete(tenant.tenant_id, type_name, slug)
    if deleted:
        logger.info(
            "skill_deleted",
            tenant_id=tenant.tenant_id,
            type_name=type_name,
            slug=slug,
        )
        return OkResponse()

    for skill in await resolve_skills(
        type_name,
        tenant_id=tenant.tenant_id,
        entitled=_entitled(tenant),
        store=_store(),
        include_disabled=True,
    ):
        if skill.slug == slug:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"skill '{slug}' is curated ({skill.layer.value} layer) and "
                    "cannot be deleted by a workspace"
                ),
            )
    raise HTTPException(
        status_code=404, detail=f"no skill '{slug}' on type '{type_name}'"
    )
