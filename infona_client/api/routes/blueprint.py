"""Canonical Blueprint routes — validate + export (INF-565).

``POST /graphs/{tenant}/kgs/{kg_name}/blueprint/export`` turns a live KG
into a schema-valid package (directory files + manifest).
``POST /graphs/{tenant}/blueprint/validate`` runs the INF-563 validator
on a submitted document or package files.

CLI, SDK, Explorer, and MCP must call these routes — no per-interface
endpoint (COG-128). Instance dump stays on ``GET …/kgs/{kg}/export``.

Boundary: OSS. ``infona_client.*`` / stdlib only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.blueprint.export import ExportOptions, export_blueprint
from infona_client.blueprint.redact import ExportRedactionError
from infona_client.blueprint.validate import validate_blueprint
from infona_client.graph.queries import is_valid_kg_name
from infona_client.graph.store import get_graph_store

export_router = APIRouter(prefix="/graphs/{tenant}/kgs")
validate_router = APIRouter(prefix="/graphs/{tenant}/blueprint")


class BlueprintExportRequest(BaseModel):
    namespace: str = "infona"
    version: str = "0.1.0"
    license: str = "Apache-2.0"
    attribution: str | None = None
    name: str | None = None
    package_id: str | None = None
    acquisition_revision: int = Field(default=1, ge=1)


class BlueprintExportResponse(BaseModel):
    kg: str
    manifest: dict[str, Any]
    files: dict[str, str]


class BlueprintValidateRequest(BaseModel):
    """Either ``manifest`` or ``files`` (must include blueprint.yaml / json)."""

    manifest: dict[str, Any] | None = None
    files: dict[str, str] | None = None


class BlueprintValidateResponse(BaseModel):
    errors: list[str]


@export_router.post(
    "/{kg_name}/blueprint/export",
    response_model=BlueprintExportResponse,
)
async def export_kg_blueprint(
    tenant: str,
    kg_name: str,
    body: BlueprintExportRequest = Body(default_factory=BlueprintExportRequest),
    ctx: TenantContext = Depends(get_tenant),
) -> BlueprintExportResponse:
    if ctx.tenant_id != tenant:
        raise HTTPException(status_code=403, detail="tenant mismatch")
    if not is_valid_kg_name(kg_name):
        raise HTTPException(status_code=400, detail=f"invalid kg name {kg_name!r}")
    req = body or BlueprintExportRequest()
    options = ExportOptions(
        namespace=req.namespace,
        version=req.version,
        license=req.license,
        attribution=req.attribution,
        name=req.name,
        package_id=req.package_id,
        acquisition_revision=req.acquisition_revision,
    )
    try:
        result = await export_blueprint(
            tenant_id=tenant,
            kg=kg_name,
            store=get_graph_store(),
            options=options,
        )
    except ExportRedactionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return BlueprintExportResponse(
        kg=kg_name,
        manifest=result.as_response()["manifest"],
        files=result.files,
    )


@validate_router.post("/validate", response_model=BlueprintValidateResponse)
async def validate_kg_blueprint(
    tenant: str,
    body: BlueprintValidateRequest,
    ctx: TenantContext = Depends(get_tenant),
) -> BlueprintValidateResponse:
    if ctx.tenant_id != tenant:
        raise HTTPException(status_code=403, detail="tenant mismatch")
    if body.manifest is not None:
        return BlueprintValidateResponse(errors=validate_blueprint(body.manifest))
    if body.files:
        yaml_text = body.files.get("blueprint.yaml") or body.files.get("blueprint.yml")
        json_text = body.files.get("blueprint.json")
        if yaml_text and json_text:
            return BlueprintValidateResponse(
                errors=["package must not ship both YAML and JSON roots"]
            )
        raw = yaml_text or json_text
        if raw is None:
            return BlueprintValidateResponse(
                errors=["files must include blueprint.yaml or blueprint.json"]
            )
        try:
            from infona_client.blueprint.yamlutil import load_yaml

            doc = load_yaml(raw) if yaml_text else None
            if doc is None:
                import json

                doc = json.loads(json_text or "")
            return BlueprintValidateResponse(errors=validate_blueprint(doc))
        except Exception as exc:  # noqa: BLE001 — validator contract is a list
            return BlueprintValidateResponse(errors=[str(exc)])
    return BlueprintValidateResponse(errors=["submit manifest or files"])


__all__ = ["export_router", "validate_router"]
