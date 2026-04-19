from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import list_functions_query
from cograph_client.models.function import FunctionRef, FunctionTier


async def get_functions_for_entity(
    client: NeptuneClient,
    graph_uri: str,
    entity_type: str,
) -> list[FunctionRef]:
    sparql = list_functions_query(graph_uri, entity_type)
    raw = await client.query(sparql)
    _, bindings = parse_sparql_results(raw)
    return [
        FunctionRef(
            name=row.get("name", ""),
            entity_type=row.get("type", "").split("/")[-1],
            endpoint_url=row.get("endpoint"),
            description=row.get("desc", ""),
            tier=FunctionTier.CUSTOM,
        )
        for row in bindings
    ]
