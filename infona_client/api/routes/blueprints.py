"""Canonical Blueprint routes — one family, every client (INF-575 / INF-579 / INF-578).

Workspace mutations stay on ``/graphs/{tenant}/…`` so ``get_tenant`` confines
the install to the path tenant (INF-580). INF-575's ``/v1/blueprints/{id}``
sketch is the same operations (inspect / install / fork / extend / update),
not a second identity-scoped family.

  POST   /graphs/{tenant}/blueprints/validate
  POST   /graphs/{tenant}/blueprints/install
         body may include seed=infona/clinical-trials, target=new_workspace
         (INF-605: outsider default is a new workspace; install does not
         acquire — first-run is a separate command)
  GET    /graphs/{tenant}/blueprints
  GET    /graphs/{tenant}/blueprints/{namespace}/{name}
  DELETE /graphs/{tenant}/blueprints/{namespace}/{name}
  POST   /graphs/{tenant}/blueprints/{namespace}/{name}/fork
  POST   /graphs/{tenant}/blueprints/{namespace}/{name}/extend
  POST   /graphs/{tenant}/blueprints/{namespace}/{name}/update
  POST   /graphs/{tenant}/blueprints/{namespace}/{name}/first-run

Explorer, CLI, and MCP reach these through the shared SDK. No per-interface
path strings.

Boundary: OSS. ``infona_client.*`` / stdlib only.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.blueprint.first_run import FIRST_RUN_MAX_ROWS, run_first_run
from infona_client.blueprint.install import (
    BlueprintError,
    inspect_blueprint,
    list_installed_blueprints,
    load_and_validate,
    uninstall_blueprint,
)
from infona_client.blueprint.layer import extend_blueprint, update_blueprint
from infona_client.blueprint.public_path import (
    TARGET_EXISTING,
    fork_with_target,
    install_with_target,
    resolve_shipped_seed,
)
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
    kg: Optional[str] = Field(default=None, min_length=1)
    include_sample: Optional[bool] = None
    manifest: Optional[dict[str, Any]] = None
    manifest_yaml: Optional[str] = None
    seed: Optional[str] = None
    target: Literal["existing", "new_workspace"] = TARGET_EXISTING


class ForkRequest(BaseModel):
    """Optional new identity. Default is ``{tenant}/{parent-name}``."""

    as_id: Optional[str] = Field(default=None, alias="as")
    target: Literal["existing", "new_workspace"] = TARGET_EXISTING

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


class FirstRunRequest(BaseModel):
    """Workspace-side credentials + optional question. Never persisted."""

    credentials: Optional[dict[str, str]] = None
    question: Optional[str] = None
    max_rows: int = Field(default=FIRST_RUN_MAX_ROWS, ge=1, le=5000)


def _manifest_from_body(body: ValidateRequest | InstallRequest | UpdateRequest):
    if body.manifest is not None:
        return body.manifest
    if body.manifest_yaml:
        return body.manifest_yaml
    raise HTTPException(
        status_code=400,
        detail="provide manifest or manifest_yaml",
    )


def _install_source(body: InstallRequest):
    if body.manifest is not None:
        return body.manifest
    if body.manifest_yaml:
        return body.manifest_yaml
    if body.seed:
        return resolve_shipped_seed(body.seed)
    raise HTTPException(
        status_code=400,
        detail="provide manifest, manifest_yaml, or seed",
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
        return await install_with_target(
            _install_source(body),
            tenant_id=tenant.tenant_id,
            api_key=tenant.api_key,
            kg=body.kg,
            include_sample=body.include_sample,
            target=body.target,
        )
    except BlueprintError as exc:
        _raise(exc)
        return


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
        return await fork_with_target(
            tenant.tenant_id,
            _package_id(namespace, name),
            api_key=tenant.api_key,
            as_id=req.as_id,
            target=req.target,
        )
    except BlueprintError as exc:
        _raise(exc)
        return


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


@router.post("/{namespace}/{name}/first-run")
async def first_run_blueprint_route(
    namespace: str,
    name: str,
    body: FirstRunRequest | None = None,
    tenant: TenantContext = Depends(require_tenant_write),
):
    """INF-593 — credentials → acquire_condition_set → first supported answer."""
    req = body or FirstRunRequest()
    try:
        result = await run_first_run(
            tenant.tenant_id,
            _package_id(namespace, name),
            credentials=req.credentials,
            question=req.question,
            max_rows=req.max_rows,
        )
    except BlueprintError as exc:
        _raise(exc)
        return
    return result.to_dict()
