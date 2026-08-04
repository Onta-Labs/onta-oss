"""Raw SPARQL passthrough (ONTA-412).

Both routes below hand a caller-supplied SPARQL string to the store. Resolving
the tenant from the ROUTE PATH authorizes WHO is asking; it says nothing about
WHICH graphs the submitted text touches. On Neptune the default graph is the
union of all named graphs, so before this module gained a guard an ordinary
``SELECT * WHERE { ?s ?p ?o }`` on any tenant's path returned every tenant's
triples, and ``DROP GRAPH <other tenant>`` on the update route was equally
reachable.

The two routes are treated differently because their exposure differs:

* READ is confined, not removed. ``cograph_client.graph.sparql_scope`` requires
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
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from cograph_client.api.deps import get_neptune_client
from cograph_client.api.rate_limit import limiter
from cograph_client.auth.access import require_tenant_write
from cograph_client.auth.api_keys import TenantContext, auth_is_configured, get_tenant
from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.sparql_scope import TenantScopeError, enforce_query_scope
from cograph_client.models.query import SPARQLQuery, SPARQLResult, SPARQLUpdate


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


@router.post("/graphs/{tenant}/query", response_model=SPARQLResult)
@limiter.limit("500/minute")
async def execute_query(
    request: Request,
    body: SPARQLQuery,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    try:
        enforce_query_scope(body.query, tenant.tenant_id)
    except TenantScopeError as exc:
        # Raised BEFORE the store is touched, so a rejected query never has a
        # chance to return another tenant's rows.
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    raw = await client.query(body.query)
    vars, bindings = parse_sparql_results(raw)
    return SPARQLResult(vars=vars, bindings=bindings)


@router.post("/graphs/{tenant}/update")
@limiter.limit("500/minute")
async def execute_update(
    request: Request,
    body: SPARQLUpdate,
    tenant: TenantContext = Depends(require_raw_update_access),
    client: NeptuneClient = Depends(get_neptune_client),
):
    await client.update(body.update)
    return {"status": "ok"}
