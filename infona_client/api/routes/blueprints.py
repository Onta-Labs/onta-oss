"""Canonical Blueprint routes — one family, every client (INF-575 / INF-579 / INF-578).

Workspace mutations stay on ``/graphs/{tenant}/…`` so ``get_tenant`` confines
the install to the path tenant (INF-580). INF-575's ``/v1/blueprints/{id}``
sketch is the same operations (inspect / install / fork / extend / update),
not a second identity-scoped family.

  POST   /graphs/{tenant}/blueprints/validate
  POST   /graphs/{tenant}/blueprints/install
  GET    /graphs/{tenant}/blueprints
  GET    /graphs/{tenant}/blueprints/{namespace}/{name}
  DELETE /graphs/{tenant}/blueprints/{namespace}/{name}
  POST   /graphs/{tenant}/blueprints/{namespace}/{name}/fork
  POST   /graphs/{tenant}/blueprints/{namespace}/{name}/extend
  POST   /graphs/{tenant}/blueprints/{namespace}/{name}/update

Explorer, CLI, and MCP reach these through the shared SDK. No per-interface
path strings.

Boundary: OSS. ``infona_client.*`` / stdlib only.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.blueprint.install import (
    BlueprintError,
    fork_blueprint,
    inspect_blueprint,
    install_blueprint,
    list_installed_blueprints,
    load_and_validate,
    uninstall_blueprint,
)
from infona_client.blueprint.layer import extend_blueprint, update_blueprint
from infona_client.blueprint.validate import validate_blueprint as validate_document

router = APIRouter(prefix="/graphs/{tenant}/blueprints")


def _package_id(namespace: str, name: str) -> str:
    return f"{namespace}/{name}"


def _raise(exc: BlueprintError) -> None:
    payload: dict[str, Any] = {"error": str(exc)}
    payload.update(exc.details)
    raise HTTPException(status_code=exc.status_code, detail=payload)


class ValidateRequest(BaseModel):
    manifest: Optional[dict[str, Any]] = None
    manifest_yaml: Optional[str] = None


class InstallRequest(BaseModel):
    kg: str = Field(min_length=1)
    include_sample: bool = True
    manifest: Optional[dict[str, Any]] = None
    manifest_yaml: Optional[str] = None


class ForkRequest(BaseModel):
    """Optional new identity. Default is ``{tenant}/{parent-name}``."""

    as_id: Optional[str] = Field(default=None, alias="as")

    model_config = {"populate_by_name": True}


class ExtendRequest(BaseModel):
    """Private overlay delta. Same pin. Not a new package identity."""

    overlay: Optional[dict[str, Any]] = None
    overlay_yaml: Optional[str] = None


class UpdateRequest(BaseModel):
    """New public base of the same id. Overlay is reapplied."""

    include_sample: Optional[bool] = None
    manifest: Optional[dict[str, Any]] = None
    manifest_yaml: Optional[str] = None


def _manifest_from_body(body: ValidateRequest | InstallRequest | UpdateRequest):
    if body.manifest is not None:
        return body.manifest
    if body.manifest_yaml:
        return body.manifest_yaml
    raise HTTPException(
        status_code=400,
        detail="provide manifest or manifest_yaml",
    )


@router.post("/validate")
async def validate_blueprint_route(
    body: ValidateRequest,
    tenant: TenantContext = Depends(get_tenant),
):
    """Validate a v1 document. Writes nothing. Tenant is authorized, unused."""
    _ = tenant
    try:
        manifest = load_and_validate(_manifest_from_body(body))
        errors = validate_document(manifest)
    except BlueprintError as exc:
        _raise(exc)
        return
    return {"valid": not errors, "errors": errors}


@router.post("/install")
async def install_blueprint_route(
    body: InstallRequest,
    tenant: TenantContext = Depends(require_tenant_write),
):
    try:
        result = await install_blueprint(
            _manifest_from_body(body),
            tenant_id=tenant.tenant_id,
            kg=body.kg,
            include_sample=body.include_sample,
        )
    except BlueprintError as exc:
        _raise(exc)
        return
    return result.to_dict()


@router.get("")
async def list_blueprints_route(
    tenant: TenantContext = Depends(get_tenant),
):
    return {"blueprints": await list_installed_blueprints(tenant.tenant_id)}


@router.get("/{namespace}/{name}")
async def inspect_blueprint_route(
    namespace: str,
    name: str,
    tenant: TenantContext = Depends(get_tenant),
):
    try:
        return await inspect_blueprint(tenant.tenant_id, _package_id(namespace, name))
    except BlueprintError as exc:
        _raise(exc)
        return


@router.delete("/{namespace}/{name}")
async def uninstall_blueprint_route(
    namespace: str,
    name: str,
    tenant: TenantContext = Depends(require_tenant_write),
):
    try:
        return await uninstall_blueprint(tenant.tenant_id, _package_id(namespace, name))
    except BlueprintError as exc:
        _raise(exc)
        return


@router.post("/{namespace}/{name}/fork")
async def fork_blueprint_route(
    namespace: str,
    name: str,
    body: ForkRequest | None = None,
    tenant: TenantContext = Depends(require_tenant_write),
):
    req = body or ForkRequest()
    try:
        result = await fork_blueprint(
            tenant.tenant_id,
            _package_id(namespace, name),
            as_id=req.as_id,
        )
    except BlueprintError as exc:
        _raise(exc)
        return
    return result.to_dict()


def _overlay_from_body(body: ExtendRequest) -> dict[str, Any]:
    if body.overlay is not None:
        return body.overlay
    if body.overlay_yaml:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise HTTPException(status_code=400, detail="YAML overlay needs PyYAML") from exc
        loaded = yaml.safe_load(body.overlay_yaml)
        if not isinstance(loaded, dict):
            raise HTTPException(status_code=400, detail="overlay must be an object")
        return loaded
    raise HTTPException(status_code=400, detail="provide overlay or overlay_yaml")


@router.post("/{namespace}/{name}/extend")
async def extend_blueprint_route(
    namespace: str,
    name: str,
    body: ExtendRequest,
    tenant: TenantContext = Depends(require_tenant_write),
):
    try:
        return await extend_blueprint(
            tenant.tenant_id, _package_id(namespace, name), _overlay_from_body(body)
        )
    except BlueprintError as exc:
        _raise(exc)
        return


@router.post("/{namespace}/{name}/update")
async def update_blueprint_route(
    namespace: str,
    name: str,
    body: UpdateRequest,
    tenant: TenantContext = Depends(require_tenant_write),
):
    try:
        return await update_blueprint(
            _manifest_from_body(body),
            tenant_id=tenant.tenant_id,
            blueprint_id=_package_id(namespace, name),
            include_sample=body.include_sample,
        )
    except BlueprintError as exc:
        _raise(exc)
        return
