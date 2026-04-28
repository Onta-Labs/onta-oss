"""Knowledge graph management — list, create, delete named graphs within a tenant.

All KGs share the tenant's ontology but have separate instance data.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from cograph_client.api.deps import get_neptune_client
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri

router = APIRouter(prefix="/graphs/{tenant}/kgs")

OMNIX_ONTO = "https://cograph.tech/onto"


class KGCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = ""


class KGInfo(BaseModel):
    name: str
    description: str = ""
    triple_count: int = 0


@router.get("", response_model=list[KGInfo])
async def list_kgs(
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """List all knowledge graphs for a tenant."""
    base = tenant_graph_uri(tenant.tenant_id)

    # Query the metadata graph for KG registrations
    sparql = (
        f"SELECT ?name ?desc FROM <{base}> WHERE {{"
        f"  ?kg <{OMNIX_ONTO}/kg_name> ?name ."
        f"  OPTIONAL {{ ?kg <{OMNIX_ONTO}/kg_description> ?desc }}"
        f"}}"
    )
    raw = await client.query(sparql)
    _, bindings = parse_sparql_results(raw)

    kgs = []
    for row in bindings:
        name = row.get("name", "")
        if not name:
            continue

        # Get triple count for this KG
        graph = kg_graph_uri(tenant.tenant_id, name)
        count_sparql = f"SELECT (COUNT(*) as ?c) FROM <{graph}> WHERE {{ ?s ?p ?o }}"
        try:
            count_raw = await client.query(count_sparql)
            _, count_bindings = parse_sparql_results(count_raw)
            count = int(count_bindings[0].get("c", "0")) if count_bindings else 0
        except Exception:
            count = 0

        kgs.append(KGInfo(name=name, description=row.get("desc", ""), triple_count=count))

    return kgs


@router.post("", response_model=KGInfo, status_code=201)
async def create_kg(
    body: KGCreate,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Create a new knowledge graph for a tenant."""
    base = tenant_graph_uri(tenant.tenant_id)
    kg_uri = f"https://cograph.tech/kgs/{tenant.tenant_id}/{body.name}"

    sparql = (
        f"INSERT DATA {{\n"
        f"  GRAPH <{base}> {{\n"
        f'    <{kg_uri}> <{OMNIX_ONTO}/kg_name> "{body.name}" .\n'
    )
    if body.description:
        sparql += f'    <{kg_uri}> <{OMNIX_ONTO}/kg_description> "{body.description}" .\n'
    sparql += f"  }}\n}}"

    await client.update(sparql)
    return KGInfo(name=body.name, description=body.description, triple_count=0)


@router.delete("/{kg_name}")
async def delete_kg(
    kg_name: str,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Delete a knowledge graph and all its data."""
    base = tenant_graph_uri(tenant.tenant_id)
    graph = kg_graph_uri(tenant.tenant_id, kg_name)
    kg_uri = f"https://cograph.tech/kgs/{tenant.tenant_id}/{kg_name}"

    # Drop all triples in the KG graph
    await client.update(f"DROP SILENT GRAPH <{graph}>")

    # Remove KG metadata
    await client.update(
        f"DELETE WHERE {{\n"
        f"  GRAPH <{base}> {{\n"
        f"    <{kg_uri}> ?p ?o .\n"
        f"  }}\n"
        f"}}"
    )

    # Purge stale examples from the example bank for this KG
    try:
        from cograph_client.nlp.example_bank import get_example_bank
        bank = get_example_bank()
        if bank and bank._examples:
            before = len(bank._examples)
            bank._examples = [e for e in bank._examples if e.kg_name != kg_name]
            removed = before - len(bank._examples)
            if removed > 0:
                bank.save()
                import structlog
                structlog.get_logger("cograph.kg").info(
                    "example_bank_purged", kg=kg_name, removed=removed,
                    remaining=len(bank._examples),
                )
    except Exception:
        pass  # Bank purge is best-effort, don't fail the delete

    return {"deleted": kg_name}
