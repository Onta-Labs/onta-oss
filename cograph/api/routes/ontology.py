from fastapi import APIRouter, Depends, HTTPException

from cograph.api.deps import get_neptune_client
from cograph.auth.api_keys import TenantContext, get_tenant
from cograph.graph.client import NeptuneClient
from cograph.graph.ontology_queries import (
    get_full_ontology_query,
    get_subtypes_query,
    get_type_attributes_query,
    get_type_detail_query,
    get_type_functions_query,
    insert_attribute,
    insert_subtype,
    insert_type,
    list_types_query,
)
from cograph.graph.parser import parse_sparql_results
from cograph.graph.queries import tenant_graph_uri
from cograph.models.ontology import (
    AttributeAdd,
    AttributeDefinition,
    SubtypeAdd,
    TypeCreate,
    TypeResponse,
)

router = APIRouter(prefix="/graphs/{tenant}/ontology")


@router.post("/types", status_code=201)
async def create_type(
    body: TypeCreate,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    sparql = insert_type(graph_uri, body.name, body.description, body.parent_type)
    await client.update(sparql)

    for attr in body.attributes:
        attr_sparql = insert_attribute(graph_uri, body.name, attr.name, attr.description, attr.datatype)
        await client.update(attr_sparql)

    return {"created": body.name, "attributes": len(body.attributes)}


@router.get("/types", response_model=list[TypeResponse])
async def list_types(
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    raw = await client.query(list_types_query(graph_uri))
    _, bindings = parse_sparql_results(raw)

    types = {}
    for row in bindings:
        label = row.get("label", "")
        if label not in types:
            types[label] = TypeResponse(
                name=label,
                description=row.get("comment", ""),
                parent_type=_extract_name(row.get("parent")) if row.get("parent") else None,
            )
        elif row.get("parent"):
            types[label].parent_type = _extract_name(row["parent"])

    return list(types.values())


@router.get("/types/{type_name}", response_model=TypeResponse)
async def get_type(
    type_name: str,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    graph_uri = tenant_graph_uri(tenant.tenant_id)

    raw = await client.query(get_type_detail_query(graph_uri, type_name))
    _, bindings = parse_sparql_results(raw)
    if not bindings:
        raise HTTPException(status_code=404, detail=f"Type '{type_name}' not found")

    row = bindings[0]
    result = TypeResponse(
        name=row.get("label", type_name),
        description=row.get("comment", ""),
        parent_type=_extract_name(row.get("parent")) if row.get("parent") else None,
    )

    attr_raw = await client.query(get_type_attributes_query(graph_uri, type_name))
    _, attr_bindings = parse_sparql_results(attr_raw)
    result.attributes = [
        AttributeDefinition(
            name=r.get("attrLabel", ""),
            description=r.get("attrComment", ""),
            datatype=_xsd_to_datatype(r.get("range", "")),
        )
        for r in attr_bindings
    ]

    sub_raw = await client.query(get_subtypes_query(graph_uri, type_name))
    _, sub_bindings = parse_sparql_results(sub_raw)
    result.subtypes = [r.get("label", "") for r in sub_bindings]

    func_raw = await client.query(get_type_functions_query(graph_uri, type_name))
    _, func_bindings = parse_sparql_results(func_raw)
    result.functions = [r.get("name", "") for r in func_bindings]

    return result


@router.post("/types/{type_name}/attributes", status_code=201)
async def add_attributes(
    type_name: str,
    body: AttributeAdd,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    for attr in body.attributes:
        sparql = insert_attribute(graph_uri, type_name, attr.name, attr.description, attr.datatype)
        await client.update(sparql)
    return {"type": type_name, "attributes_added": len(body.attributes)}


@router.post("/types/{type_name}/subtypes", status_code=201)
async def add_subtype(
    type_name: str,
    body: SubtypeAdd,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    sparql = insert_subtype(graph_uri, type_name, body.subtype)
    await client.update(sparql)
    return {"parent": type_name, "subtype": body.subtype}


@router.get("/schema")
async def get_full_schema(
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Get the complete ontology schema. Used by the NL pipeline."""
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    raw = await client.query(get_full_ontology_query(graph_uri))
    _, bindings = parse_sparql_results(raw)

    types = {}
    for row in bindings:
        type_label = row.get("typeLabel", "")
        if not type_label:
            continue
        if type_label not in types:
            types[type_label] = {"attributes": [], "functions": []}
        if row.get("attrLabel") and row["attrLabel"] not in [a["name"] for a in types[type_label]["attributes"]]:
            types[type_label]["attributes"].append({
                "name": row["attrLabel"],
                "datatype": _xsd_to_datatype(row.get("range", "")),
            })
        if row.get("funcName") and row["funcName"] not in types[type_label]["functions"]:
            types[type_label]["functions"].append(row["funcName"])

    return {"types": types}


def _extract_name(uri: str | None) -> str | None:
    if not uri:
        return None
    return uri.rstrip("/").split("/")[-1]


TYPE_URI_PREFIX = "https://omnix.dev/types/"


def _xsd_to_datatype(xsd_uri: str) -> str:
    if not xsd_uri:
        return "string"
    # Check if it's a reference to another ontology type
    if xsd_uri.startswith(TYPE_URI_PREFIX):
        return xsd_uri[len(TYPE_URI_PREFIX):]
    mapping = {
        "string": "string",
        "integer": "integer",
        "float": "float",
        "boolean": "boolean",
        "dateTime": "datetime",
        "Resource": "uri",
    }
    last = xsd_uri.split("#")[-1] if "#" in xsd_uri else xsd_uri.split("/")[-1]
    return mapping.get(last, "string")
