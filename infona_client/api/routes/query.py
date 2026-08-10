"""Raw SPARQL passthrough (ONTA-412; E9 neo4j hard-break).

Both routes below hand a caller-supplied SPARQL string to the store. Resolving
the tenant from the ROUTE PATH authorizes WHO is asking; it says nothing about
WHICH graphs the submitted text touches. On Neptune the default graph is the
union of all named graphs, so before this module gained a guard an ordinary
``SELECT * WHERE { ?s ?p ?o }`` on any tenant's path returned every tenant's
triples, and ``DROP GRAPH <other tenant>`` on the update route was equally
reachable.

The two routes are treated differently because their exposure differs:

* READ is confined, not removed. ``infona_client.graph.sparql_scope`` requires
  the query to declare a tenant-owned dataset, which makes the STORE do the
  confinement (see that module for why a "reject bad clauses" rule cannot work
  here). The route keeps working for its real callers: both the published CLI's
  ``cograph clear`` loop and ``eval_diagnosis``'s probes already send
  ``FROM <tenant graph>``.
* WRITE is restricted to operators. No first-party client calls it, and no text
  rule can confine an arbitrary SPARQL Update: ``DROP ALL``, ``CLEAR DEFAULT``
  and a graph-less removal all name no graph yet act on everything, and an
  ``INSERT { GRAPH <mine> ... }`` driven by an unscoped ``WHERE`` would copy
  another tenant's rows into a graph the caller can then read back
  legitimately. Tenant-scoped writes have first-class routes already
  (``/triples``, ``/kgs``, ingest).

**Neo4j mode (ADR 0012 L2 / E9 partial):** when ``COGRAPH_GRAPH_BACKEND=neo4j``,
both routes return **410 Gone**. There is no SPARQL façade over Neo4j — clients
must use the agent, SDK, or high-level typed REST APIs. The SPARQL
implementation is **not deleted**; Neptune deployments keep the full surface.
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from infona_client.api.deps import get_neptune_client
from infona_client.api.rate_limit import limiter
from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext, auth_is_configured, get_tenant
from infona_client.graph.client import NeptuneClient
from infona_client.graph.kg_writer import graph_backend
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.sparql_scope import TenantScopeError, enforce_query_scope
from infona_client.models.query import SPARQLQuery, SPARQLResult, SPARQLUpdate

# Shared 410 body for neo4j-mode hard break (public SPARQL surfaces only).
_SPARQL_GONE_DETAIL = (
    "Raw SPARQL /query and /update are not available when "
    "COGRAPH_GRAPH_BACKEND=neo4j. Use the agent, SDK, or high-level APIs "
    "(/ask, /agent, /triples, /kgs, ingest, explore) instead."
)


def reject_raw_sparql_if_neo4j() -> None:
    """Hard-break public SPARQL HTTP surfaces under neo4j backend (E9 / ADR 0012 L2).

    Raises ``HTTPException(410)`` when ``COGRAPH_GRAPH_BACKEND=neo4j``. No-op for
    the default Neptune path. SPARQL code paths remain in-tree for Neptune and
    internal callers; only these public routes are gated.
    """
    if graph_backend() == "neo4j":
        raise HTTPException(status_code=410, detail=_SPARQL_GONE_DETAIL)


def require_raw_update_access(
    tenant: TenantContext = Depends(require_tenant_write),
) -> TenantContext:
    """Fail closed unless the caller is an operator (or auth is off entirely).

    The open-access carve-out keeps the documented escape hatch usable on a
    self-hosted install with no auth configured, where ``get_tenant`` already
    grants an anonymous caller any tenant in the URL and there is consequently
    no boundary this route could cross. In any deployment that HAS auth,
    including static API keys, this is a plain operator gate.

    Layered ON TOP of the workspace write capability (ONTA-452) rather than
    beside it: operator-ness is a platform role, so without this a staff account
    holding a READ-ONLY membership would still have had raw Update. Both checks
    must pass, and the write check runs first.
    """
    if tenant.is_operator or not auth_is_configured():
        return tenant
    raise HTTPException(
        status_code=403,
        detail=(
            "Raw SPARQL Update is operator only. Use the tenant-scoped write "
            "routes (/triples, /kgs, ingest) instead."
        ),
    )


router = APIRouter()


@router.post(
    "/graphs/{tenant}/query",
    response_model=SPARQLResult,
    responses={
        410: {
            "description": (
                "Gone — raw SPARQL is unavailable when "
                "COGRAPH_GRAPH_BACKEND=neo4j. Use agent/SDK/high-level APIs."
            )
        },
    },
)
@limiter.limit("500/minute")
async def execute_query(
    request: Request,
    body: SPARQLQuery,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Execute a scoped raw SPARQL SELECT/ASK/CONSTRUCT (Neptune backend only).

    The query must declare the graphs it reads (``FROM`` / ``FROM NAMED`` naming
    workspace-owned IRIs). Tenant confinement is enforced before the store is
    touched.

    **Neo4j:** returns **410 Gone** when ``COGRAPH_GRAPH_BACKEND=neo4j`` — there
    is no SPARQL façade. Prefer ``/ask``, ``/agent``, explore, or typed write
    routes. Unchanged on Neptune (default).
    """
    reject_raw_sparql_if_neo4j()
    try:
        enforce_query_scope(body.query, tenant.tenant_id)
    except TenantScopeError as exc:
        # Raised BEFORE the store is touched, so a rejected query never has a
        # chance to return another tenant's rows.
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    raw = await client.query(body.query)
    vars, bindings = parse_sparql_results(raw)
    return SPARQLResult(vars=vars, bindings=bindings)


@router.post(
    "/graphs/{tenant}/update",
    responses={
        410: {
            "description": (
                "Gone — raw SPARQL Update is unavailable when "
                "COGRAPH_GRAPH_BACKEND=neo4j. Use agent/SDK/high-level APIs."
            )
        },
    },
)
@limiter.limit("500/minute")
async def execute_update(
    request: Request,
    body: SPARQLUpdate,
    tenant: TenantContext = Depends(require_raw_update_access),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Execute raw SPARQL Update (operator-only; Neptune backend only).

    Operator-only wherever authentication is configured. Arbitrary SPARQL Update
    cannot be confined to one workspace by inspecting its text — use
    ``/triples``, ``/kgs``, or ingest for workspace-scoped writes.

    **Neo4j:** returns **410 Gone** when ``COGRAPH_GRAPH_BACKEND=neo4j``.
    Unchanged on Neptune (default).
    """
    reject_raw_sparql_if_neo4j()
    await client.update(body.update)
    return {"status": "ok"}
