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

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from cograph_client.api.deps import get_neptune_client
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.config import settings
from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.entitlement import is_entitled, layer_stack_for
from cograph_client.graph.global_ontology import fetch_ontology
from cograph_client.graph.aliases import (
    AliasStillReferencedError,
    backfill_aliases,
    fetch_alias_map,
)
from cograph_client.graph.ontology_changelog import fetch_ontology_changelog
from cograph_client.graph.ontology_commit import commit_ontology
from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri
from cograph_client.models.ontology import (
    AliasBackfill,
    AliasMapResponse,
    AliasRegister,
    AliasRename,
    AliasRetire,
    ApplyBatchRequest,
    OntologyChangelogEntry,
    OntologyChangelogResponse,
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
    WorkspaceTypeCount,
    WorkspaceTypeCountsResponse,
)
from cograph_client.nlp.pipeline import get_embedding_service
from cograph_client.resolver.ontology_resolver import OntologyResolver
from cograph_client.resolver.type_matcher import TypeMatcher
from cograph_client.resolver.verdict_cache import JsonVerdictCache

router = APIRouter(prefix="/graphs/{tenant}/ontology")

# Verdict cache lives alongside the app data (same path the ingest route uses);
# for ECS/Fargate this should be on an EFS mount or replaced with DynamoDB.
_VERDICT_CACHE_PATH = Path("/tmp/omnix-verdict-cache.json")


async def _workspace_catalog(tenant_id: str):
    """Tenant-aware API-source catalog for the workspace browser overlay.

    Loads the caller's ``tenant_custom`` entries from the durable store and
    merges them onto the global catalog (highest precedence for THIS tenant
    only). Degrades to ``None`` (global-only / empty overlay) on any failure
    so a broken source store never sinks the ontology read. Operator
    ``fetch_global_ontology`` deliberately never takes a tenant catalog —
    private entries must not leak onto the cross-tenant route.
    """
    try:
        from cograph_client.api_registry.catalog import load_tenant_custom_catalog
        from cograph_client.api_registry.store import make_tenant_api_source_store

        return await load_tenant_custom_catalog(
            tenant_id, make_tenant_api_source_store()
        )
    except Exception:
        return None


async def _workspace_ontology(
    tenant: TenantContext, client: NeptuneClient
) -> WorkspaceOntologyResponse:
    """Effective (shadowed) ontology for ``tenant`` — single LayerStack read.

    Full browser payload (ONTA-408): layered types + tenant-custom sources +
    tenant-layer skills overlay. Writes never go through this path.
    """
    stack = layer_stack_for(tenant)
    catalog = await _workspace_catalog(tenant.tenant_id)
    return await fetch_ontology(
        client,
        layers=stack.layer_pairs(),
        catalog=catalog,
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


@router.get("/type-counts", response_model=WorkspaceTypeCountsResponse)
async def workspace_type_counts(
    tenant: TenantContext = Depends(get_tenant),
):
    """Workspace-wide union of per-type entity counts (ONTA-409).

    Unions ``KgStats.type_breakdown`` across every knowledge graph in this
    tenant's durable stats store — one relational read, no Neptune. Types with
    zero instances in every KG are omitted (so the response IS the Active set).

    Freshness is write-path best-effort (the same stats path that powers the
    dashboard), not the live SPARQL of per-KG ``GET /kgs/{kg}/type-counts``.
    Empty is 200 with ``types: []`` — the Ontology viewer's Active pill falls
    back to All rather than showing a bare empty tree.

    Isolation: the store is keyed by ``tenant_id``; a peer tenant's rows never
    appear here.
    """
    from cograph_client.graph.kg_stats_store import (
        get_kg_stats_store,
        union_type_breakdowns,
    )

    store = get_kg_stats_store()
    try:
        rows = await store.list_for_tenant(tenant.tenant_id)
    except Exception:  # noqa: BLE001 — degrade to empty Active, never 500
        rows = []

    aggregated = union_type_breakdowns(rows)
    return WorkspaceTypeCountsResponse(
        tenant_id=tenant.tenant_id,
        types=[
            WorkspaceTypeCount(name=name, entity_count=total, by_kg=by_kg)
            for name, total, by_kg in aggregated
        ],
        kg_names=sorted({r.kg_name for r in rows}),
    )


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


# ── Ontology changelog reader (ONTA-401) ──────────────────────────────────────
#
# Append-only workspace changelog written by commit_ontology. Modeled on
# GET /graphs/{tenant}/history (value history). Tenant isolation is by named
# graph: we only ever FROM the caller's `{tenant_graph}/changelog` companion.
# Entries carry a full ChangeRecord delta so the reader never needs the live
# ontology graph to describe what changed.

# Absolute http(s) IRI for subject narrowing — same belt as history.py so a
# crafted `>` cannot inject GRAPH <other-tenant> into the query builder.
_ABS_IRI_RE = re.compile(r'^https?://[^\s<>"{}|\^`\\\x00-\x20]+$')
# Action is a short vocabulary token (commit_ontology, add_type, …) — reject
# anything that could break a SPARQL string literal or smuggle a FILTER.
_ACTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


@router.get("/changelog", response_model=OntologyChangelogResponse)
async def get_ontology_changelog(
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
    since: str | None = Query(
        None,
        description=(
            "ISO-8601 date/dateTime cutoff; returns only entries STRICTLY AFTER it"
        ),
    ),
    subject: str | None = Query(
        None,
        description=(
            "Narrow to one gov:subject IRI (target ontology graph URI for "
            "workspace commits, or a type/shape URI for governance-shaped rows)"
        ),
    ),
    action: str | None = Query(
        None,
        description="Exact gov:action match (e.g. commit_ontology, add_type)",
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0, le=100_000),
):
    """Return the workspace ontology changelog, newest first (ONTA-401).

    Each entry's ``changes`` list is the ChangeRecord delta written at commit
    time — enough to describe the mutation without consulting the live graph.
    Scoped exclusively to this tenant's companion changelog graph.
    """
    if subject is not None and not _ABS_IRI_RE.match(subject):
        raise HTTPException(
            status_code=422,
            detail="subject must be a well-formed absolute http(s) IRI",
        )
    if action is not None and not _ACTION_RE.match(action):
        raise HTTPException(
            status_code=422,
            detail="action must be a short alphanumeric token (e.g. commit_ontology)",
        )
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    entries = await fetch_ontology_changelog(
        client,
        graph_uri,
        since=since,
        subject=subject,
        action=action,
        limit=limit,
        offset=offset,
    )
    return OntologyChangelogResponse(
        tenant_id=tenant.tenant_id,
        graph_uri=graph_uri,
        count=len(entries),
        offset=offset,
        limit=limit,
        entries=[
            OntologyChangelogEntry(
                entry_uri=e.entry_uri,
                action=e.action,
                subject=e.subject,
                timestamp=e.timestamp,
                tenant_id=e.tenant_id,
                actor=e.actor,
                message=e.message,
                version_before=e.version_before,
                version_after=e.version_after,
                revision=e.revision,
                changes=list(e.changes),
            )
            for e in entries
        ],
    )


# ── Attribute aliases (ONTA-407a / 407b / ADR 0002 §7) ────────────────────────
#
# Full rename lifecycle on the TENANT ontology graph only — never a global layer:
#   rename (always creates alias) → query via rewrite_query_attrs → backfill
#   instance triples → retire (refuses while old-predicate refs remain).
# alignedTo is a separate mechanism (ONTA-402a stop-writing decision). Type
# renames remain a documented gap (entity URIs embed the type leaf).


@router.post("/aliases", status_code=201)
async def register_attribute_alias(
    body: AliasRegister,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Register an attribute alias (old → new) on the tenant ontology graph.

    Alias-edge only. Prefer ``POST /aliases/rename`` for a full rename that
    also updates the schema declaration (always creates the alias).
    """
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    try:
        result = await commit_ontology(
            client,
            graph_uri,
            [
                OntologyMutation(
                    op=OntologyOpKind.REGISTER_ALIAS,
                    type_name=body.type_name,
                    alias_from=body.from_slot,
                    alias_to=body.to_slot,
                    target_type=body.to_type,
                )
            ],
            actor=tenant.tenant_id,
            message=f"register_alias {body.from_slot} → {body.to_slot}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    old_uri = new_uri = None
    if result.change_records:
        rec = result.change_records[0]
        old_uri, new_uri = rec.old_value, rec.new_value
    return {
        "type": body.type_name,
        "from_slot": body.from_slot,
        "to_slot": body.to_slot,
        "to_type": body.to_type or body.type_name,
        "old_attr_uri": old_uri,
        "new_attr_uri": new_uri,
        "version_before": result.version_before,
        "version_after": result.version_after,
    }


@router.post("/aliases/rename", status_code=201)
async def rename_attribute_with_alias(
    body: AliasRename,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Full attribute rename — ALWAYS creates an alias (ONTA-407b).

    Ensures the new attribute declaration, records ``old aliasOf new``, and
    drops the old schema declaration. Instance triples keep the old predicate
    until ``POST /aliases/backfill``; retirement refuses while refs remain.
    """
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    try:
        result = await commit_ontology(
            client,
            graph_uri,
            [
                OntologyMutation(
                    op=OntologyOpKind.RENAME_ATTRIBUTE,
                    type_name=body.type_name,
                    alias_from=body.from_slot,
                    alias_to=body.to_slot,
                    target_type=body.to_type,
                    datatype=body.datatype,
                    description=body.description,
                )
            ],
            actor=tenant.tenant_id,
            message=f"rename_attribute {body.from_slot} → {body.to_slot}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rename_rec = next(
        (c for c in result.change_records if c.kind.value == "rename_with_alias"),
        result.change_records[0] if result.change_records else None,
    )
    return {
        "type": body.type_name,
        "from_slot": body.from_slot,
        "to_slot": body.to_slot,
        "to_type": body.to_type or body.type_name,
        "old_attr_uri": rename_rec.old_value if rename_rec else None,
        "new_attr_uri": rename_rec.new_value if rename_rec else None,
        "version_before": result.version_before,
        "version_after": result.version_after,
        "change_records": [c.model_dump() for c in result.change_records],
    }


@router.post("/aliases/backfill")
async def backfill_attribute_aliases(
    body: AliasBackfill,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Rewrite old-predicate instance triples onto their alias targets.

    After a clean backfill (zero remaining refs), call
    ``DELETE /aliases`` (retire) to drop the alias edge.
    """
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    data_graph = kg_graph_uri(tenant.tenant_id, body.kg_name)
    alias_map = await fetch_alias_map(client, graph_uri)
    if body.old_attr_uri:
        if body.old_attr_uri not in alias_map:
            raise HTTPException(
                status_code=404,
                detail=f"no alias registered for {body.old_attr_uri!r}",
            )
        alias_map = {body.old_attr_uri: alias_map[body.old_attr_uri]}
    rewritten = await backfill_aliases(
        client, data_graph, alias_map, batch_size=body.batch_size,
    )
    return {
        "kg_name": body.kg_name,
        "data_graph_uri": data_graph,
        "rewritten": rewritten,
        "aliases": alias_map,
    }


@router.delete("/aliases")
async def retire_attribute_alias(
    body: AliasRetire,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Retire an alias after backfill — 409 while instance refs remain."""
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    data_graph = kg_graph_uri(tenant.tenant_id, body.kg_name)
    try:
        result = await commit_ontology(
            client,
            graph_uri,
            [
                OntologyMutation(
                    op=OntologyOpKind.RETIRE_ALIAS,
                    type_name=body.type_name,
                    alias_from=body.from_slot,
                    data_graph_uri=data_graph,
                )
            ],
            actor=tenant.tenant_id,
            message=f"retire_alias {body.from_slot}",
        )
    except AliasStillReferencedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "alias_still_referenced",
                "old_attr_uri": exc.old_attr_uri,
                "remaining": exc.remaining,
                "data_graph_uri": exc.data_graph_uri,
                "message": str(exc),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "type": body.type_name,
        "from_slot": body.from_slot,
        "kg_name": body.kg_name,
        "retired": True,
        "version_before": result.version_before,
        "version_after": result.version_after,
    }


@router.get("/aliases", response_model=AliasMapResponse)
async def list_attribute_aliases(
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Return the flattened old→new attribute alias map for this tenant graph.

    Chains (``a → b → c``) collapse to one hop (``a → c``, ``b → c``). Empty
    when no aliases are registered. Cyclic chains are dropped (never hang).
    """
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    aliases = await fetch_alias_map(client, graph_uri)
    return AliasMapResponse(aliases=aliases)


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
