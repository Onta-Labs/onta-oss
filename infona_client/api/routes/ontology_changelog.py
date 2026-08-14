"""Ontology changelog + grouped history readers (ONTA-401 / ONTA-410).

Patched ``fetch_ontology_changelog`` / ``_current_revision_counter`` are
looked up on the facade via ``_host()``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Query

from infona_client.api.deps import get_neptune_client
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.graph.queries import tenant_graph_uri
from infona_client.models.ontology import (
    OntologyChangelogEntry,
    OntologyChangelogResponse,
    OntologyHistoryGroup,
    OntologyHistoryResponse,
)
from infona_client.api.routes.ontology_common import (
    _ABS_IRI_RE,
    _ACTION_RE,
    _changelog_entry_model,
    _host,
)


async def get_ontology_changelog(
    tenant: TenantContext = Depends(get_tenant),
    client: Any = Depends(get_neptune_client),
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
    h = _host()
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    entries = await h.fetch_ontology_changelog(
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


async def get_ontology_history(
    tenant: TenantContext = Depends(get_tenant),
    client: Any = Depends(get_neptune_client),
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
    h = _host()
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    entries = await h.fetch_ontology_changelog(
        client,
        graph_uri,
        since=since,
        subject=subject,
        action=action,
        limit=limit,
        offset=offset,
    )
    rev = await h._current_revision_counter(client, graph_uri)
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

    groups = h.group_changelog_entries(entries)
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
