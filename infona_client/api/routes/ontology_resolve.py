"""NL ontology evolution — resolve + apply (COG-80).

Schema writes go through ``commit_ontology`` on the facade. Tests patch
``_build_resolver`` on ``ontology``; call it via ``_host()``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends

from infona_client.api.deps import get_neptune_client
from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext
from infona_client.graph.queries import tenant_graph_uri
from infona_client.models.ontology import (
    ApplyBatchRequest,
    ApplyBatchResult,
    ApplyChangeResult,
    OntologyMutation,
    OntologyOpKind,
    ResolutionResult,
    ResolvedChange,
    ResolveRequest,
)
from infona_client.resolver.ontology_resolver import OntologyResolver
from infona_client.resolver.type_matcher import TypeMatcher
from infona_client.resolver.verdict_cache import JsonVerdictCache
from infona_client.api.routes.ontology_common import _VERDICT_CACHE_PATH, _host


def _build_resolver(graph_uri: str) -> OntologyResolver:
    """Assemble an :class:`OntologyResolver` from the shared app primitives.

    Degrades gracefully: if the embedding service can't initialise (no key /
    offline) the resolver still runs on the TypeMatcher cascade's other layers.
    """
    h = _host()
    try:
        embedding_service = h.get_embedding_service()
    except Exception:  # pragma: no cover - defensive: embeddings are optional
        embedding_service = None

    matcher = TypeMatcher(
        openrouter_key=h.settings.openrouter_api_key,
        cache=JsonVerdictCache(_VERDICT_CACHE_PATH),
        embedding_service=embedding_service,
        graph_uri=graph_uri,
    )
    return OntologyResolver(
        openrouter_key=h.settings.openrouter_api_key,
        type_matcher=matcher,
        embedding_service=embedding_service,
    )


async def _apply_change(change: ResolvedChange, graph_uri: str, client: Any) -> list[str]:
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

    await _host().commit_ontology(client, graph_uri, muts)
    return [m.op.value for m in muts]


async def resolve_ontology(
    body: ResolveRequest,
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
) -> ResolutionResult:
    """Resolve a fuzzy NL ask into ontology changes; auto-apply the confident
    ones, return ambiguous/new-type ones as proposals for the caller to confirm
    via `POST .../ontology/apply`.

    `dry_run=True` (the interactive Explorer path) is plan-only: the resolver
    runs exactly as below but NOTHING is written — every change (what
    would have auto-applied plus the proposals) is returned under `proposals`,
    with `applied` empty, so the UI can render one uniform reviewable list."""
    h = _host()
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    resolver = h._build_resolver(graph_uri)
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
        await h._apply_change(change, graph_uri, client)

    return result


async def apply_ontology_change(
    body: ResolvedChange,
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
):
    """Commit a single proposal previously returned by `/resolve` (stateless —
    the caller passes the change object straight back). Idempotent.

    Kept for back-compat; to apply several proposals at once use `/apply/batch`
    (one round-trip instead of N)."""
    h = _host()
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    operations = await h._apply_change(body, graph_uri, client)
    return {
        "applied": body,
        "operations": len(operations),
        "summary": f"Applied {h.change_label(body)}",
    }


async def apply_ontology_changes(
    body: ApplyBatchRequest,
    tenant: TenantContext = Depends(require_tenant_write),
    client: Any = Depends(get_neptune_client),
) -> ApplyBatchResult:
    """Commit MANY proposals from one `/resolve` call in a single round-trip.

    The canonical batch-apply route: every client (SDK `ontologyApplyBatch`,
    MCP `apply_ontology_changes`) rides THIS endpoint as a thin pass-through —
    none reimplements the loop client-side (interface convergence, CLAUDE.md).

    Semantics — identical, per change, to `/apply` (same `_apply_change`, same
    idempotent upserts), so N-in-one is equivalent to N single calls. Changes
    apply in the submitted order. Partial-failure is well defined: a change that
    raises is reported with `ok=False` + its error and does NOT abort the rest.
    """
    h = _host()
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    results: list[ApplyChangeResult] = []
    applied_count = 0
    failed_count = 0
    total_ops = 0
    for change in body.changes:
        try:
            operations = await h._apply_change(change, graph_uri, client)
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
    target = (
        f" → {change.datatype_or_target}"
        if change.kind == "relationship"
        else f" ({change.datatype_or_target})"
    )
    return f"{change.action} {change.kind} '{change.name}'{target} on {change.subject_type}"
