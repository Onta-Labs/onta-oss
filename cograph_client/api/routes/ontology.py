"""Tenant ontology routes — layered READS, tenant-graph WRITES (ONTA-397).

**Reads** (list / get / schema / workspace) go through
:func:`~cograph_client.graph.global_ontology.fetch_ontology` over the caller's
:class:`~cograph_client.graph.layers.LayerStack` (via
:func:`~cograph_client.graph.entitlement.layer_stack_for`). Empty tenant +
populated Public → Public types are visible. Same-name tenant definitions
shadow Public/Enhanced. Non-entitled stacks never see Enhanced.

**Writes** (create type, add attribute, add subtype, resolve-apply, …) always
target ``tenant_graph_uri(tenant)``. Ordinary mutations never write a global
layer graph. Isolation is by named graph: two tenants' type URIs may collide
byte-for-byte; they must never share a graph in a union.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from cograph_client.api.deps import get_neptune_client
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.config import settings
from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.entitlement import is_entitled, layer_stack_for
from cograph_client.graph.global_ontology import fetch_ontology
from cograph_client.graph.ontology_commit import commit_ontology
from cograph_client.graph.queries import tenant_graph_uri
from cograph_client.models.ontology import (
    ApplyBatchRequest,
    OntologyMutation,
    OntologyOpKind,
    ApplyBatchResult,
    ApplyChangeResult,
    AttributeAdd,
    AttributeDefinition,
    ResolutionResult,
    ResolvedChange,
    ResolveRequest,
    SubtypeAdd,
    TypeCreate,
    TypeResponse,
    WorkspaceOntologyResponse,
    WorkspaceOntologyType,
)
from cograph_client.nlp.pipeline import get_embedding_service
from cograph_client.resolver.ontology_resolver import OntologyResolver
from cograph_client.resolver.type_matcher import TypeMatcher
from cograph_client.resolver.verdict_cache import JsonVerdictCache

router = APIRouter(prefix="/graphs/{tenant}/ontology")

# Verdict cache lives alongside the app data (same path the ingest route uses);
# for ECS/Fargate this should be on an EFS mount or replaced with DynamoDB.
_VERDICT_CACHE_PATH = Path("/tmp/omnix-verdict-cache.json")


async def _workspace_ontology(
    tenant: TenantContext, client: NeptuneClient
) -> WorkspaceOntologyResponse:
    """Effective (shadowed) ontology for ``tenant`` — single LayerStack read."""
    stack = layer_stack_for(tenant)
    return await fetch_ontology(
        client,
        layers=stack.layer_pairs(),
        entitled=is_entitled(tenant),
        tenant_id=tenant.tenant_id,
        apply_shadowing=True,
    )


def _type_response(t: WorkspaceOntologyType) -> TypeResponse:
    """Map a layered type onto the legacy TypeResponse contract."""
    return TypeResponse(
        name=t.name,
        description=t.description or "",
        parent_type=t.parent_type,
        attributes=[
            AttributeDefinition(
                name=a.name,
                description=a.description or "",
                datatype=a.datatype,
            )
            for a in t.attributes
        ]
        + [
            # Relationships surface as attributes with the target type name as
            # datatype — matching the pre-layered TypeResponse shape used by
            # the CLI / Explorer (no separate relationships field).
            AttributeDefinition(
                name=r.name,
                description=r.description or "",
                datatype=r.target_type,
            )
            for r in t.relationships
        ],
        subtypes=list(t.subtypes),
        functions=[f.name for f in t.functions],
    )


@router.get("", response_model=WorkspaceOntologyResponse)
async def get_workspace_ontology(
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Effective layered ontology for this workspace (ONTA-397).

    Canonical full-payload read: layers status + shadowed types. Empty is 200
    with ``types: []``. Writes never go through this route.
    """
    return await _workspace_ontology(tenant, client)


@router.post("/types", status_code=201)
async def create_type(
    body: TypeCreate,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    # WRITE path: tenant graph only. Never a global layer.
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    muts = [
        OntologyMutation(
            op=OntologyOpKind.UPSERT_TYPE,
            type_name=body.name,
            description=body.description or None,
            parent_type=body.parent_type,
        )
    ]
    for attr in body.attributes:
        if attr.datatype and attr.datatype not in (
            "string", "integer", "float", "boolean", "datetime", "uri", "geo",
        ):
            muts.append(OntologyMutation(
                op=OntologyOpKind.UPSERT_RELATIONSHIP,
                type_name=body.name,
                slot_name=attr.name,
                target_type=attr.datatype,
                description=attr.description or "",
            ))
        else:
            muts.append(OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name=body.name,
                slot_name=attr.name,
                datatype=attr.datatype or "string",
                description=attr.description or "",
            ))
    await commit_ontology(client, graph_uri, muts, actor=tenant.tenant_id)
    return {"created": body.name, "attributes": len(body.attributes)}


@router.get("/types", response_model=list[TypeResponse])
async def list_types(
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """List effective types (tenant + visible global layers, shadowed)."""
    body = await _workspace_ontology(tenant, client)
    return [_type_response(t) for t in body.types]


@router.get("/types/{type_name}", response_model=TypeResponse)
async def get_type(
    type_name: str,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Type detail from the effective (shadowed) layered ontology."""
    body = await _workspace_ontology(tenant, client)
    for t in body.types:
        if t.name == type_name:
            return _type_response(t)
    raise HTTPException(status_code=404, detail=f"Type '{type_name}' not found")


@router.post("/types/{type_name}/attributes", status_code=201)
async def add_attributes(
    type_name: str,
    body: AttributeAdd,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    # WRITE path: tenant graph only.
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    muts = []
    for attr in body.attributes:
        if attr.datatype and attr.datatype not in (
            "string", "integer", "float", "boolean", "datetime", "uri", "geo",
        ):
            muts.append(OntologyMutation(
                op=OntologyOpKind.UPSERT_RELATIONSHIP,
                type_name=type_name,
                slot_name=attr.name,
                target_type=attr.datatype,
                description=attr.description or "",
            ))
        else:
            muts.append(OntologyMutation(
                op=OntologyOpKind.UPSERT_ATTRIBUTE,
                type_name=type_name,
                slot_name=attr.name,
                datatype=attr.datatype or "string",
                description=attr.description or "",
            ))
    await commit_ontology(client, graph_uri, muts, actor=tenant.tenant_id)
    return {"type": type_name, "attributes_added": len(body.attributes)}


@router.post("/types/{type_name}/subtypes", status_code=201)
async def add_subtype(
    type_name: str,
    body: SubtypeAdd,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    # WRITE path: tenant graph only.
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    await commit_ontology(
        client,
        graph_uri,
        [OntologyMutation(
            op=OntologyOpKind.SET_SUBCLASS,
            type_name=body.subtype,
            parent_type=type_name,
        )],
        actor=tenant.tenant_id,
    )
    return {"parent": type_name, "subtype": body.subtype}


@router.get("/schema")
async def get_full_schema(
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Complete effective schema (layered + shadowed). Used by the NL pipeline."""
    body = await _workspace_ontology(tenant, client)
    types: dict = {}
    for t in body.types:
        types[t.name] = {
            "attributes": [
                {"name": a.name, "datatype": a.datatype} for a in t.attributes
            ]
            + [
                {"name": r.name, "datatype": r.target_type} for r in t.relationships
            ],
            "functions": [f.name for f in t.functions],
            "layer": t.layer,
        }
    return {"types": types, "entitled": body.entitled, "tenant_id": body.tenant_id}


# ── Natural-language ontology evolution (COG-80) ──────────────────────────────
#
# `resolve` takes a fuzzy ask, resolves it against the current ontology, AUTO-
# APPLIES the high-confidence changes, and returns the rest as proposals. `apply`
# commits a single proposal the agent chose to confirm. Both write through the
# atomic upsert builders so retries are idempotent.


def _build_resolver(graph_uri: str) -> OntologyResolver:
    """Assemble an :class:`OntologyResolver` from the shared app primitives.

    Degrades gracefully: if the embedding service can't initialise (no key /
    offline) the resolver still runs on the TypeMatcher cascade's other layers.
    """
    try:
        embedding_service = get_embedding_service()
    except Exception:  # pragma: no cover - defensive: embeddings are optional
        embedding_service = None

    matcher = TypeMatcher(
        openrouter_key=settings.openrouter_api_key,
        cache=JsonVerdictCache(_VERDICT_CACHE_PATH),
        embedding_service=embedding_service,
        graph_uri=graph_uri,
    )
    return OntologyResolver(
        openrouter_key=settings.openrouter_api_key,
        type_matcher=matcher,
        embedding_service=embedding_service,
    )


async def _apply_change(change: ResolvedChange, graph_uri: str, client: NeptuneClient) -> list[str]:
    """Translate one resolved change into ontology mutations and commit them.

    Shared by `/resolve` (for confident `applied` changes) and `/apply` (for a
    confirmed proposal). All schema writes go through :func:`commit_ontology`
    (ONTA-403).
    """
    muts: list[OntologyMutation] = []

    # A `create` change means the subject type is newly minted — ensure it
    # exists first (idempotent on an existing type, never clobbers it).
    if change.action == "create":
        muts.append(OntologyMutation(
            op=OntologyOpKind.UPSERT_TYPE, type_name=change.subject_type,
        ))

    # A relationship's range points at another type; ensure that target type
    # exists before we point an object property at it.
    if change.kind == "relationship":
        muts.append(OntologyMutation(
            op=OntologyOpKind.UPSERT_TYPE, type_name=change.datatype_or_target,
        ))
        muts.append(OntologyMutation(
            op=OntologyOpKind.UPSERT_RELATIONSHIP,
            type_name=change.subject_type,
            slot_name=change.name,
            target_type=change.datatype_or_target,
            description="",
        ))
    else:
        # `reuse` is already satisfied, but the upsert is idempotent.
        muts.append(OntologyMutation(
            op=OntologyOpKind.UPSERT_ATTRIBUTE,
            type_name=change.subject_type,
            slot_name=change.name,
            datatype=change.datatype_or_target,
            description="",
        ))

    await commit_ontology(client, graph_uri, muts)
    return [m.op.value for m in muts]


@router.post("/resolve", response_model=ResolutionResult)
async def resolve_ontology(
    body: ResolveRequest,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
) -> ResolutionResult:
    """Resolve a fuzzy NL ask into ontology changes; auto-apply the confident
    ones, return ambiguous/new-type ones as proposals for the caller to confirm
    via `POST .../ontology/apply`.

    `dry_run=True` (the interactive Explorer path) is plan-only: the resolver
    runs exactly as below but NOTHING is written to Neptune — every change (what
    would have auto-applied plus the proposals) is returned under `proposals`,
    with `applied` empty, so the UI can render one uniform reviewable list."""
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    resolver = _build_resolver(graph_uri)
    result = await resolver.resolve(body.ask, graph_uri, client)

    if body.dry_run:
        # Plan-only: write nothing, fold the would-be-applied changes into the
        # proposals list so the caller reviews everything uniformly.
        return ResolutionResult(
            applied=[],
            proposals=result.applied + result.proposals,
            summary=result.summary,
            dry_run=True,
        )

    for change in result.applied:
        await _apply_change(change, graph_uri, client)

    return result


@router.post("/apply")
async def apply_ontology_change(
    body: ResolvedChange,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Commit a single proposal previously returned by `/resolve` (stateless —
    the caller passes the change object straight back). Idempotent.

    Kept for back-compat; to apply several proposals at once use `/apply/batch`
    (one round-trip instead of N)."""
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    operations = await _apply_change(body, graph_uri, client)
    return {
        "applied": body,
        "operations": len(operations),
        "summary": f"Applied {change_label(body)}",
    }


@router.post("/apply/batch", response_model=ApplyBatchResult)
async def apply_ontology_changes(
    body: ApplyBatchRequest,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
) -> ApplyBatchResult:
    """Commit MANY proposals from one `/resolve` call in a single round-trip.

    The canonical batch-apply route: every client (SDK `ontologyApplyBatch`,
    MCP `apply_ontology_changes`) rides THIS endpoint as a thin pass-through —
    none reimplements the loop client-side (interface convergence, CLAUDE.md).

    Semantics — identical, per change, to `/apply` (same `_apply_change`, same
    idempotent upserts), so N-in-one is equivalent to N single calls. Changes
    apply in the submitted order. Partial-failure is well defined: a change that
    raises is reported with `ok=False` + its error and does NOT abort the rest
    (each change's writes are independent + idempotent), so re-POSTing the whole
    batch safely retries only what failed.
    """
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    results: list[ApplyChangeResult] = []
    applied_count = 0
    failed_count = 0
    total_ops = 0
    for change in body.changes:
        try:
            operations = await _apply_change(change, graph_uri, client)
        except Exception as exc:  # noqa: BLE001 — isolate one change's failure
            failed_count += 1
            results.append(
                ApplyChangeResult(change=change, ok=False, operations=0, error=str(exc))
            )
            continue
        applied_count += 1
        total_ops += len(operations)
        results.append(
            ApplyChangeResult(change=change, ok=True, operations=len(operations))
        )

    summary = f"Applied {applied_count}/{len(body.changes)} change(s)"
    if failed_count:
        summary += f" ({failed_count} failed)"
    return ApplyBatchResult(
        results=results,
        applied_count=applied_count,
        failed_count=failed_count,
        operations=total_ops,
        summary=summary,
    )


def change_label(change: ResolvedChange) -> str:
    target = f" → {change.datatype_or_target}" if change.kind == "relationship" else f" ({change.datatype_or_target})"
    return f"{change.action} {change.kind} '{change.name}'{target} on {change.subject_type}"
