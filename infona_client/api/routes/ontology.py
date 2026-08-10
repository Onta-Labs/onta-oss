"""Tenant ontology routes — layered READS, tenant-graph WRITES (ONTA-397).

**Reads** (list / get / schema / workspace) go through
:func:`~infona_client.graph.global_ontology.fetch_ontology` over the caller's
:class:`~infona_client.graph.layers.LayerStack` (via
:func:`~infona_client.graph.entitlement.layer_stack_for_tenant`, which applies
the workspace base pin from ONTA-405). Empty tenant +
populated Public → Public types are visible. Same-name tenant definitions
shadow Public/Enhanced. Non-entitled stacks never see Enhanced.

**Writes** (create type, add attribute, add subtype, resolve-apply, …) always
target ``tenant_graph_uri(tenant)``. Ordinary mutations never write a global
layer graph. Isolation is by named graph: two tenants' type URIs may collide
byte-for-byte; they must never share a graph in a union.
"""

from infona_client.graph.iri import IRI_BASE
import re
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from infona_client.api.deps import get_neptune_client
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.auth.access import get_tenant_with_capability, require_tenant_write
from infona_client.auth.capabilities import can_write
from infona_client.config import settings
from infona_client.graph.client import NeptuneClient
from infona_client.graph.entitlement import is_entitled, layer_stack_for_tenant
from infona_client.graph.global_ontology import fetch_ontology
from infona_client.graph.aliases import (
    AliasStillReferencedError,
    backfill_aliases,
    fetch_alias_map,
)
from infona_client.graph.ontology_base_pin import (
    BasePin,
    BasePinReadError,
    ensure_workspace_base_pin,
    get_base_pin,
    latest_base_release_version,
    preview_base_upgrade,
    rollback_base_pin,
    upgrade_base_pin,
)
from infona_client.graph.ontology_changelog import (
    fetch_ontology_changelog,
    group_changelog_entries,
)
from infona_client.graph.ontology_commit import (
    commit_ontology,
    release_graph_uri,
    revision_graph_uri,
)
from infona_client.graph.ontology_compat import classify_diff
from infona_client.graph.ontology_snapshots import (
    _current_revision_counter,
    diff_graphs,
)
from infona_client.graph.queries import kg_graph_uri, tenant_graph_uri
from infona_client.models.ontology import (
    AliasBackfill,
    AliasMapResponse,
    AliasRegister,
    AliasRename,
    AliasRetire,
    ApplyBatchRequest,
    BasePinResponse,
    BasePinUpgradeRequest,
    CollisionRecordResponse,
    OntologyChangelogEntry,
    OntologyChangelogResponse,
    OntologyDiffResponse,
    OntologyHistoryGroup,
    OntologyHistoryResponse,
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
    UpgradePreviewResponse,
    WorkspaceOntologyResponse,
    WorkspaceOntologyType,
    WorkspaceTypeCount,
    WorkspaceTypeCountsResponse,
)
from infona_client.nlp.pipeline import get_embedding_service
from infona_client.resolver.ontology_resolver import OntologyResolver
from infona_client.resolver.type_matcher import TypeMatcher
from infona_client.resolver.verdict_cache import JsonVerdictCache

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
        from infona_client.api_registry.catalog import load_tenant_custom_catalog
        from infona_client.api_registry.store import make_tenant_api_source_store

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

    Base layer is resolved from the workspace pin (ONTA-405) so a global
    release does not silently change the effective ontology until upgrade.
    """
    stack = await layer_stack_for_tenant(client, tenant, auto_ensure=True)
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
    tenant: TenantContext = Depends(get_tenant_with_capability),
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
    from infona_client.graph.kg_stats_store import (
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
    tenant: TenantContext = Depends(require_tenant_write),
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
    tenant: TenantContext = Depends(get_tenant_with_capability),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """List effective types (tenant + visible global layers, shadowed)."""
    body = await _workspace_ontology(tenant, client)
    return [_type_response(t) for t in body.types]


@router.get("/types/{type_name}", response_model=TypeResponse)
async def get_type(
    type_name: str,
    tenant: TenantContext = Depends(get_tenant_with_capability),
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
    tenant: TenantContext = Depends(require_tenant_write),
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
    tenant: TenantContext = Depends(require_tenant_write),
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
    tenant: TenantContext = Depends(get_tenant_with_capability),
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


# ── Base pin + history + diff (ONTA-410) ──────────────────────────────────────
#
# Version strip / upgrade / history groups / structural diff for the workspace
# ontology viewer. Thin wrappers over ontology_base_pin + changelog grouping +
# diff_shapes. Tenant isolation is by named graph throughout.


def _changelog_entry_model(e) -> OntologyChangelogEntry:
    return OntologyChangelogEntry(
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


def _base_pin_response(
    pin: BasePin,
    *,
    workspace_revision: int,
    latest_available: int | None,
) -> BasePinResponse:
    upgrade_available = False
    if latest_available is not None:
        if pin.base_version is None:
            upgrade_available = True  # live pin, a release exists
        elif latest_available > pin.base_version:
            upgrade_available = True
    return BasePinResponse(
        tenant_id=pin.tenant_id,
        base_layer=pin.base_layer,
        base_version=pin.base_version,
        is_live=pin.is_live,
        auto_upgrade=pin.auto_upgrade,
        previous_version=pin.previous_version,
        has_previous=pin.has_previous,
        updated_at=pin.updated_at,
        workspace_revision=workspace_revision,
        latest_available=latest_available,
        upgrade_available=upgrade_available,
    )


@router.get("/base-pin", response_model=BasePinResponse)
async def get_workspace_base_pin(
    tenant: TenantContext = Depends(get_tenant_with_capability),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Current workspace base pin + revision + upgrade affordance (ONTA-410).

    Ensures a pin when missing (soft backfill to latest) so the version strip
    always has a defined state. Pin **read** infrastructure failures → 503
    (never silent re-pin to latest).

    The backfill is a WRITE, so it is gated on the caller's write capability
    (ONTA-452): a read-only member sees the same pin, computed ephemerally, and
    opening the version strip no longer pins or auto-upgrades their workspace.
    """
    entitled = is_entitled(tenant)
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    try:
        pin = await ensure_workspace_base_pin(
            client,
            tenant.tenant_id,
            entitled=entitled,
            persist=can_write(tenant.role),
        )
    except BasePinReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    latest = await latest_base_release_version(client, pin.base_layer)
    rev = await _current_revision_counter(client, graph_uri)
    return _base_pin_response(
        pin,
        workspace_revision=rev,
        latest_available=latest,
    )


@router.get("/base-pin/preview", response_model=UpgradePreviewResponse)
async def preview_workspace_base_upgrade(
    tenant: TenantContext = Depends(get_tenant_with_capability),
    client: NeptuneClient = Depends(get_neptune_client),
    to_version: int | None = Query(
        None,
        ge=1,
        description="Target base release; omit for latest available",
    ),
):
    """Preview upgrading the workspace base pin (structural ChangeRecords)."""
    entitled = is_entitled(tenant)
    try:
        preview = await preview_base_upgrade(
            client,
            tenant.tenant_id,
            entitled=entitled,
            to_version=to_version,
            # A preview is a read; its internal ensure must not write for a
            # read-only member (ONTA-452).
            persist=can_write(tenant.role),
        )
    except BasePinReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return UpgradePreviewResponse(
        from_version=preview.from_version,
        to_version=preview.to_version,
        base_layer=preview.base_layer,
        changes=list(preview.changes),
        collisions=[
            CollisionRecordResponse(
                type_name=c.type_name,
                slot_name=c.slot_name,
                kind=c.kind,
                detail=c.detail,
            )
            for c in preview.collisions
        ],
        deprecated_used=list(preview.deprecated_used),
        summary=list(preview.summary),
        from_fingerprint=preview.from_fingerprint,
        to_fingerprint=preview.to_fingerprint,
    )


@router.post("/base-pin/upgrade", response_model=BasePinResponse)
async def post_workspace_base_upgrade(
    body: BasePinUpgradeRequest = Body(default_factory=BasePinUpgradeRequest),
    tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Upgrade the workspace base pin to ``to_version`` (or latest)."""
    entitled = is_entitled(tenant)
    to_version = body.to_version if body is not None else None
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    try:
        pin = await upgrade_base_pin(
            client,
            tenant.tenant_id,
            entitled=entitled,
            to_version=to_version,
        )
    except BasePinReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    latest = await latest_base_release_version(client, pin.base_layer)
    rev = await _current_revision_counter(client, graph_uri)
    return _base_pin_response(
        pin,
        workspace_revision=rev,
        latest_available=latest,
    )


@router.post("/base-pin/rollback", response_model=BasePinResponse)
async def post_workspace_base_rollback(
    tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Roll the workspace base pin back to its previous version."""
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    try:
        pin = await rollback_base_pin(client, tenant.tenant_id)
    except BasePinReadError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    latest = await latest_base_release_version(client, pin.base_layer)
    rev = await _current_revision_counter(client, graph_uri)
    return _base_pin_response(
        pin,
        workspace_revision=rev,
        latest_available=latest,
    )


@router.get("/history", response_model=OntologyHistoryResponse)
async def get_ontology_history(
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
    since: str | None = Query(
        None,
        description="ISO-8601 cutoff; only entries STRICTLY AFTER it",
    ),
    subject: str | None = Query(
        None,
        description="Narrow to one gov:subject IRI",
    ),
    action: str | None = Query(
        None,
        description="Exact gov:action match",
    ),
    grouped: bool = Query(
        True,
        description="Collapse consecutive mid-ingest commits (default true)",
    ),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0, le=100_000),
):
    """Grouped (or flat) workspace ontology history (ONTA-410).

    Default ``grouped=true`` collapses consecutive ``commit_ontology`` bursts
    that share a job identity or fall within a 60s window — hundreds of
    automatic mid-ingest revisions become a few history rows. Empty changelog
    → 200 with empty groups/entries, never an error.
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
    rev = await _current_revision_counter(client, graph_uri)
    if not grouped:
        return OntologyHistoryResponse(
            tenant_id=tenant.tenant_id,
            graph_uri=graph_uri,
            grouped=False,
            count=len(entries),
            offset=offset,
            limit=limit,
            workspace_revision=rev,
            groups=[],
            entries=[_changelog_entry_model(e) for e in entries],
        )

    groups = group_changelog_entries(entries)
    return OntologyHistoryResponse(
        tenant_id=tenant.tenant_id,
        graph_uri=graph_uri,
        grouped=True,
        count=len(groups),
        offset=offset,
        limit=limit,
        workspace_revision=rev,
        groups=[
            OntologyHistoryGroup(
                id=g.id,
                start=g.start,
                end=g.end,
                count=g.count,
                actor=g.actor,
                message=g.message,
                sample_actions=list(g.sample_actions),
                change_summary_counts=dict(g.change_summary_counts),
                entries=[_changelog_entry_model(e) for e in g.entries],
            )
            for g in groups
        ],
        entries=[],
    )


def _resolve_ontology_ref(
    ref: str,
    *,
    tenant_id: str,
    base_layer: str = "public",
) -> tuple[str, str]:
    """Map a version/revision ref string to ``(canonical_ref, graph_uri)``.

    Accepted forms:
    * ``current`` / ``live`` — tenant live ontology graph
    * bare integer / ``rN`` / ``revision:N`` / ``revision/N`` — workspace revision
    * ``release:N`` / ``vN`` — base-layer release snapshot for the pin's layer
    * absolute ``https://…`` graph URI — must stay under this tenant's graphs/
      or a global public/enhanced release path
    f"""
    raw = (ref or "").strip()
    if not raw:
        raise ValueError("version ref must be non-empty")

    live = tenant_graph_uri(tenant_id)
    lower = raw.lower()

    if lower in ("current", "live"):
        return ("current", live)

    # Absolute graph URI — tenant isolation + allowed global release graphs.
    if _ABS_IRI_RE.match(raw):
        g = raw.rstrip("/")
        tenant_prefix = f"{IRI_BASE}/graphs/{tenant_id}"
        global_ok = (
            g.startswith(f"{IRI_BASE}/graphs/global/public")
            or g.startswith(f"{IRI_BASE}/graphs/global/enhanced")
        )
        if not (g == live or g.startswith(tenant_prefix + "/") or global_ok):
            raise ValueError(
                "graph URI must be this tenant's ontology graph (or a global release)"
            )
        return (raw, g)

    # release:N / vN — base layer release
    m_rel = re.match(r"^(?:release:|v)(\d+)$", lower)
    if m_rel:
        n = int(m_rel.group(1))
        if n < 1:
            raise ValueError(f"release version must be >= 1, got {n}")
        from infona_client.graph.layers import enhanced_graph_uri, public_graph_uri

        base_live = (
            enhanced_graph_uri() if base_layer == "enhanced" else public_graph_uri()
        )
        uri = release_graph_uri(base_live, n)
        return (f"release:{n}", uri)

    # revision:N / rN / bare integer
    m_rev = re.match(r"^(?:revision[:/]|r)?(\d+)$", lower)
    if m_rev:
        n = int(m_rev.group(1))
        if n < 1:
            raise ValueError(f"revision must be >= 1, got {n}")
        uri = revision_graph_uri(live, n)
        return (f"revision:{n}", uri)

    raise ValueError(
        f"unrecognized version ref {ref!r}; use current, revision:N, release:N, or a graph URI"
    )


@router.get("/diff", response_model=OntologyDiffResponse)
async def get_ontology_diff(
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
    from_ref: str | None = Query(
        None,
        alias="from",
        description="Source ref: current | revision:N | release:N | graph URI",
    ),
    to_ref: str | None = Query(
        None,
        alias="to",
        description="Target ref: current | revision:N | release:N | graph URI",
    ),
    from_revision: int | None = Query(
        None, ge=1, description="Shorthand for from=revision:N"
    ),
    to_revision: int | None = Query(
        None, ge=1, description="Shorthand for to=revision:N"
    ),
):
    """Structural ontology diff as ChangeRecords (ONTA-410).

    Reuses ``diff_graphs`` / ``diff_shapes`` so the viewer and the pure
    classifier see the same records. Missing snapshot graphs resolve to empty
    shapes (clear empty), never a 500. Deep-link a version/revision that does
    not exist → empty change list for that scope.
    """
    # Resolve shorthand query params into refs.
    src = from_ref
    dst = to_ref
    if from_revision is not None:
        src = f"revision:{from_revision}"
    if to_revision is not None:
        dst = f"revision:{to_revision}"
    if not src or not dst:
        raise HTTPException(
            status_code=422,
            detail="provide from+to (or from_revision+to_revision) version refs",
        )

    # Base layer from pin so release:N maps correctly (soft; live public on miss).
    base_layer = "public"
    try:
        pin = await get_base_pin(client, tenant.tenant_id)
        if pin is not None:
            base_layer = pin.base_layer
    except BasePinReadError:
        base_layer = "public"

    try:
        from_label, from_uri = _resolve_ontology_ref(
            src, tenant_id=tenant.tenant_id, base_layer=base_layer
        )
        to_label, to_uri = _resolve_ontology_ref(
            dst, tenant_id=tenant.tenant_id, base_layer=base_layer
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    changes = await diff_graphs(client, from_uri, to_uri)
    verdict = classify_diff(changes)
    return OntologyDiffResponse(
        tenant_id=tenant.tenant_id,
        from_ref=from_label,
        to_ref=to_label,
        from_graph_uri=from_uri,
        to_graph_uri=to_uri,
        changes=list(changes),
        count=len(changes),
        compat_class=verdict.overall.value,
        requires_major=verdict.requires_major,
        summary=list(verdict.summary),
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
    tenant: TenantContext = Depends(require_tenant_write),
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
    tenant: TenantContext = Depends(require_tenant_write),
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
    tenant: TenantContext = Depends(require_tenant_write),
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
    tenant: TenantContext = Depends(require_tenant_write),
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
    tenant: TenantContext = Depends(require_tenant_write),
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
    tenant: TenantContext = Depends(require_tenant_write),
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
    tenant: TenantContext = Depends(require_tenant_write),
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
