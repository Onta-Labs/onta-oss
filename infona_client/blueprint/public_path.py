"""INF-605 — public / outsider install target is a new workspace.

The signed-out public card signs in, then posts the existing
``/graphs/{tenant}/blueprints`` install or fork route. Default target
for that outsider path is a newly minted workspace, never an existing
tenant that already holds a KG. Optional later: install into an
existing workspace as an explicit warned action (``target=existing``).

Install writes ontology-layer content only (INF-564). It registers an
empty KG slot so later first-run has somewhere to land. It does not
acquire, enrich, ingest, or kick ``run_first_run``. First-run stays
``POST …/first-run`` / ``infona blueprint first-run``.

Public install defaults to no package sample either — copying sample
rows is still instance data. CLI ``target=existing`` may still ask for
the bounded sample. No Install-vs-Fork chooser.

Boundary: OSS. ``infona_client.*`` / stdlib only. No ``from infona.*``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Mapping

from infona_client.blueprint.catalog import (
    make_blueprint_package_store,
    shipped_seed_path,
)
from infona_client.blueprint.fork import fork_blueprint, resolve_package
from infona_client.blueprint.install import install_blueprint
from infona_client.blueprint.lock import make_blueprint_lock_store
from infona_client.blueprint.plan import (
    BlueprintNewWorkspaceUnavailable,
    BlueprintNotFound,
    BlueprintValidationError,
    load_and_validate,
)
from infona_client.graph.kg_registry import (
    ensure_kg_registered_store,
    list_registered_kgs,
)
from infona_client.graph.kg_writer import ensure_kg_registered
from infona_client.graph.queries import is_valid_kg_name

TARGET_EXISTING = "existing"
TARGET_NEW_WORKSPACE = "new_workspace"
InstallTarget = Literal["existing", "new_workspace"]


def resolve_shipped_seed(seed: str) -> Path:
    """Load an in-tree seed by package id or catalog leaf. Not a registry."""
    key = (seed or "").strip()
    if not key:
        raise BlueprintNotFound("seed is required")
    path = shipped_seed_path(key)
    if path is not None:
        return path
    if "/" not in key:
        path = shipped_seed_path(f"infona/{key}")
        if path is not None:
            return path
    raise BlueprintNotFound(f"no shipped Blueprint seed {key!r}")


def default_kg_for_source(source: str | Path | Mapping[str, Any]) -> str:
    """KG name for a seed / manifest: the package id leaf."""
    return load_and_validate(source).id.rsplit("/", 1)[-1]


async def tenant_holds_graph(tenant_id: str) -> bool:
    """True when the tenant already has a registered KG or a Blueprint pin."""
    if await list_registered_kgs(tenant_id):
        return True
    if await make_blueprint_lock_store().list_for_tenant(tenant_id):
        return True
    return bool(await make_blueprint_package_store().list_for_tenant(tenant_id))


async def path_holds_blueprint(tenant_id: str, blueprint_id: str) -> bool:
    """Same-pin leftover (re-install / retry) must stay here, not mint again."""
    if await make_blueprint_lock_store().get(tenant_id, blueprint_id):
        return True
    return await make_blueprint_package_store().get(tenant_id, blueprint_id) is not None


async def mint_new_workspace(api_key: str) -> str:
    """Same empty-body mint as ``POST /v1/me/tenants``."""
    from fastapi import HTTPException

    from infona_client.api.routes.tenants import create_untitled_workspace
    from infona_client.auth.tenant_directory import (
        TenantProviderError,
        get_tenant_provider,
    )
    if get_tenant_provider() is None:
        raise BlueprintNewWorkspaceUnavailable(
            "public install target is a new workspace; this tenant already "
            "holds a knowledge graph and no tenant directory is configured"
        )
    try:
        created = await create_untitled_workspace(api_key)
    except HTTPException as exc:
        raise BlueprintNewWorkspaceUnavailable(str(exc.detail)) from exc
    except TenantProviderError as exc:
        raise BlueprintNewWorkspaceUnavailable(exc.detail) from exc
    return created.id


async def resolve_install_tenant(
    *,
    api_key: str,
    path_tenant: str,
    target: str,
    reuse_blueprint_id: str | None = None,
) -> str:
    """INF-605: ``new_workspace`` never writes into a tenant that already
    holds a *different* graph. An empty path tenant (just minted) is
    reused. A leftover pin of the same Blueprint is also reused so a
    second install click does not mint another workspace."""
    if target == TARGET_EXISTING:
        return path_tenant
    if target != TARGET_NEW_WORKSPACE:
        raise BlueprintValidationError(f"unknown install target {target!r}")
    if not await tenant_holds_graph(path_tenant):
        return path_tenant
    if reuse_blueprint_id and await path_holds_blueprint(
        path_tenant, reuse_blueprint_id
    ):
        return path_tenant
    return await mint_new_workspace(api_key)


async def ensure_install_kg(
    tenant_id: str, kg: str, *, neptune: Any = None
) -> None:
    """Register an empty KG slot. Does not fetch or ingest instance data."""
    if not kg or not is_valid_kg_name(kg):
        raise BlueprintValidationError(f"invalid knowledge graph name {kg!r}")
    await ensure_kg_registered_store(tenant_id, kg)
    await ensure_kg_registered(neptune, tenant_id, kg)


async def install_with_target(
    source: str | Path | Mapping[str, Any],
    *,
    tenant_id: str,
    api_key: str,
    kg: str | None = None,
    include_sample: bool | None = None,
    target: str = TARGET_EXISTING,
    neptune: Any = None,
) -> dict[str, Any]:
    """Install ontology-layer content, optionally into a new workspace.

    Does not acquire, enrich, or kick first-run. Public
    ``target=new_workspace`` defaults to no package sample. CLI
    ``target=existing`` keeps the optional bounded sample unless the
    caller passes ``include_sample=False``.
    """
    loaded = load_and_validate(source)
    if include_sample is None:
        include_sample = target != TARGET_NEW_WORKSPACE
    dest = await resolve_install_tenant(
        api_key=api_key,
        path_tenant=tenant_id,
        target=target,
        reuse_blueprint_id=loaded.id,
    )
    kg_name = kg or loaded.id.rsplit("/", 1)[-1]
    await ensure_install_kg(dest, kg_name, neptune=neptune)
    result = await install_blueprint(
        source,
        tenant_id=dest,
        kg=kg_name,
        include_sample=include_sample,
        neptune=neptune,
    )
    payload = result.to_dict()
    payload["target"] = (
        TARGET_NEW_WORKSPACE if dest != tenant_id or target == TARGET_NEW_WORKSPACE
        else TARGET_EXISTING
    )
    payload["tenant_id"] = dest
    return payload


async def fork_with_target(
    tenant_id: str,
    blueprint_id: str,
    *,
    api_key: str,
    as_id: str | None = None,
    target: str = TARGET_EXISTING,
) -> dict[str, Any]:
    """Fork with lineage. Public path uses ``target=new_workspace``. No install."""
    parent = await resolve_package(tenant_id, blueprint_id)
    dest = await resolve_install_tenant(
        api_key=api_key,
        path_tenant=tenant_id,
        target=target,
    )
    result = await fork_blueprint(
        dest, blueprint_id, as_id=as_id, parent=parent
    )
    payload = result.to_dict()
    payload["target"] = (
        TARGET_NEW_WORKSPACE if dest != tenant_id or target == TARGET_NEW_WORKSPACE
        else TARGET_EXISTING
    )
    payload["tenant_id"] = dest
    return payload


__all__ = [
    "TARGET_EXISTING",
    "TARGET_NEW_WORKSPACE",
    "default_kg_for_source",
    "ensure_install_kg",
    "fork_with_target",
    "install_with_target",
    "mint_new_workspace",
    "path_holds_blueprint",
    "resolve_install_tenant",
    "resolve_shipped_seed",
    "tenant_holds_graph",
]
