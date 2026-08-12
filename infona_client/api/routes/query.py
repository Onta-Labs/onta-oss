"""Raw SPARQL passthrough — **410 Gone** (ONTA-527).

``POST /graphs/{tenant}/query`` and ``POST /graphs/{tenant}/update`` handed a
caller-supplied SPARQL string to Amazon Neptune. There is no SPARQL façade over
the Neo4j property graph, the Neptune cluster is decommissioned, and the SPARQL
execution path is deleted — so both routes now return 410 unconditionally and
the implementations are gone.

The routes are kept (rather than removed from the app) so an old client gets a
410 that names the replacement instead of a bare 404 it would read as "wrong
URL". Both known first-party callers — the published CLI's ``infona clear``
loop and ``eval_diagnosis``'s probes — need the typed replacements
(``/kgs``, ``/ask``, ``/agent``), not a scoped SPARQL string.

Historical note worth keeping: on Neptune the default graph was the UNION of
all named graphs, so an unscoped ``SELECT * WHERE { ?s ?p ?o }`` on any
tenant's path returned every tenant's triples. That is why
:mod:`infona_client.graph.sparql_scope` exists and why the READ route required
a tenant-owned dataset clause. That module is still live for the NL layer's
generated-query confinement (ONTA-424) — it is the *guard*, not the executor,
so it does not come out with the routes.
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from infona_client.api.rate_limit import limiter
from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext, auth_is_configured, get_tenant
from infona_client.models.query import SPARQLQuery, SPARQLResult, SPARQLUpdate

_SPARQL_GONE_DETAIL = (
    "Raw SPARQL /query and /update are no longer available: Infona stores a "
    "property graph (Neo4j), not RDF. Use the agent, SDK, or high-level APIs "
    "(/ask, /agent, /kgs, ingest, explore) instead."
)

_GONE_RESPONSES = {
    410: {
        "description": (
            "Gone — raw SPARQL was removed with the Neptune backend. Use "
            "agent / SDK / high-level APIs."
        )
    },
}


def reject_raw_sparql() -> None:
    """Hard-break the public SPARQL HTTP surfaces (ONTA-527)."""
    raise HTTPException(status_code=410, detail=_SPARQL_GONE_DETAIL)


def require_raw_update_access(
    tenant: TenantContext = Depends(require_tenant_write),
) -> TenantContext:
    """Fail closed unless the caller is an operator (or auth is off entirely).

    Retained ahead of the 410 so the route's authorization semantics do not
    silently loosen while it is a tombstone: a non-operator still gets 403, not
    a 410 that reads as "this used to be yours". The open-access carve-out keeps
    the documented escape hatch coherent on a self-hosted install with no auth
    configured, where ``get_tenant`` already grants an anonymous caller any
    tenant in the URL.
    """
    if tenant.is_operator or not auth_is_configured():
        return tenant
    raise HTTPException(
        status_code=403,
        detail=(
            "Raw SPARQL Update is operator only. Use the tenant-scoped write "
            "routes (/kgs, ingest) instead."
        ),
    )


router = APIRouter()


@router.post(
    "/graphs/{tenant}/query",
    response_model=SPARQLResult,
    responses=_GONE_RESPONSES,
)
@limiter.limit("500/minute")
async def execute_query(
    request: Request,
    body: SPARQLQuery,
    tenant: TenantContext = Depends(get_tenant),
):
    """Gone. Use ``/ask``, ``/agent``, or the explore APIs."""
    reject_raw_sparql()


@router.post("/graphs/{tenant}/update", responses=_GONE_RESPONSES)
@limiter.limit("500/minute")
async def execute_update(
    request: Request,
    body: SPARQLUpdate,
    tenant: TenantContext = Depends(require_raw_update_access),
):
    """Gone. Use ``/kgs`` or ingest for workspace-scoped writes."""
    reject_raw_sparql()
