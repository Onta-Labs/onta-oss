"""Raw triple CRUD — **410 Gone** (ONTA-527).

Public raw-triple SPO was a SPARQL surface over the RDF named-graph model. It
is not a faithful surface over the Assertion / Entity property graph — there is
no kg scope on these routes, and free-form predicates do not map 1:1 to property
keys — so the Neo4j cutover hard-broke it (E9) and the SPARQL implementation is
now deleted with the rest of the legacy backend.

The routes are kept (rather than removed from the app) so an old client gets a
410 that names the replacement instead of a bare 404 it would read as "wrong
URL". Callers use the high-level typed write paths: ingest, enrich, agent, or
KG-scoped APIs, all of which land on ``kg_writer.insert_facts``.
"""

from fastapi import APIRouter, Depends, HTTPException

from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.auth.access import require_tenant_write
from infona_client.models.triple import TripleBatch, TripleCreate, TripleDelete

router = APIRouter()

_TRIPLES_GONE_DETAIL = (
    "Raw /triples SPO is no longer available: Infona stores a property graph "
    "(Neo4j), not RDF triples. Use the high-level APIs (ingest, enrich, agent, "
    "explore, KG-scoped writes) instead of free-form triples."
)

_GONE_RESPONSES = {
    410: {
        "description": (
            "Gone — raw triple SPO was removed with the Neptune/SPARQL "
            "backend. Use ingest / enrich / agent / KG-scoped writes."
        )
    },
}


def reject_raw_triples() -> None:
    """Hard-break the public raw-triple routes (ONTA-527)."""
    raise HTTPException(status_code=410, detail=_TRIPLES_GONE_DETAIL)


@router.post(
    "/graphs/{tenant}/triples",
    response_model=TripleBatch,
    responses=_GONE_RESPONSES,
)
async def create_triples(
    body: TripleCreate,
    tenant: TenantContext = Depends(require_tenant_write),
):
    reject_raw_triples()


@router.get("/graphs/{tenant}/triples", responses=_GONE_RESPONSES)
async def get_triples(
    tenant: TenantContext = Depends(get_tenant),
    subject: str | None = None,
    predicate: str | None = None,
    object: str | None = None,
    limit: int = 100,
):
    reject_raw_triples()


@router.delete(
    "/graphs/{tenant}/triples",
    response_model=TripleBatch,
    responses=_GONE_RESPONSES,
)
async def remove_triples(
    body: TripleDelete,
    tenant: TenantContext = Depends(require_tenant_write),
):
    reject_raw_triples()
