"""POST /graphs/{tenant}/ingest — raw data ingestion with schema resolution."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request

from omnix.api.deps import get_neptune_client
from omnix.api.rate_limit import limiter
from omnix.auth.api_keys import TenantContext, get_tenant
from omnix.config import settings
from omnix.graph.client import NeptuneClient
from omnix.resolver.models import CSVRowsRequest, CSVSchemaMapping, CSVSchemaRequest, IngestRequest, IngestResult
from omnix.graph.queries import kg_graph_uri, tenant_graph_uri
from omnix.nlp.pipeline import NLQueryPipeline
from omnix.resolver.schema_resolver import SchemaResolver
from omnix.resolver.verdict_cache import JsonVerdictCache

router = APIRouter(prefix="/graphs/{tenant}")

# Verdict cache lives alongside the app data. For ECS/Fargate deployments,
# this should be on an EFS mount or replaced with DynamoDB.
_CACHE_PATH = Path("/tmp/omnix-verdict-cache.json")


def _get_verdict_cache() -> JsonVerdictCache:
    return JsonVerdictCache(_CACHE_PATH)


@router.post("/ingest", response_model=IngestResult)
@limiter.limit("10/minute")
async def ingest(
    request: Request,
    body: IngestRequest,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Ingest raw content into the knowledge graph.

    Runs LLM extraction, schema resolution (type matching, attribute
    resolution, validation), and inserts validated triples into Neptune.
    """
    cache = _get_verdict_cache()
    resolver = SchemaResolver(
        neptune=client,
        anthropic_key=settings.anthropic_api_key,
        verdict_cache=cache,
    )
    # Use KG-specific graph for instance data if specified
    instance_graph = kg_graph_uri(tenant.tenant_id, body.kg_name) if body.kg_name else None
    result = await resolver.ingest(
        content=body.content,
        tenant_id=tenant.tenant_id,
        content_type=body.content_type,
        source=body.source,
        instance_graph=instance_graph,
    )
    # Invalidate ontology cache so queries pick up new types/relationships
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    NLQueryPipeline.invalidate_cache(graph_uri)
    # Re-embed all affected types (new types + types with new attributes)
    # so semantic retrieval never serves stale embeddings
    affected_types = set(result.types_created)
    for attr_added in result.attributes_added:
        type_name = attr_added.split(".")[0]
        affected_types.add(type_name)
    if affected_types:
        from omnix.nlp.pipeline import get_embedding_service
        svc = get_embedding_service()
        if svc:
            try:
                await svc.embed_types(graph_uri, list(affected_types), client)
            except Exception:
                pass  # non-blocking
    return result


@router.post("/ingest/csv/schema", response_model=CSVSchemaMapping)
@limiter.limit("10/minute")
async def infer_csv_schema(
    request: Request,
    body: CSVSchemaRequest,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Step 1: Infer column mapping from CSV headers + sample rows. Single LLM call."""
    import anthropic
    from omnix.resolver.csv_resolver import CSVResolver
    from omnix.resolver.schema_resolver import SchemaResolver

    graph_uri = tenant_graph_uri(tenant.tenant_id)
    cache = _get_verdict_cache()
    resolver = SchemaResolver(neptune=client, anthropic_key=settings.anthropic_api_key, verdict_cache=cache)
    existing_types, _ = await resolver._fetch_ontology(graph_uri)

    csv_resolver = CSVResolver(
        anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
        settings.openrouter_api_key,
    )
    return await csv_resolver.infer_schema(body.headers, body.sample_rows, existing_types, body.total_rows)


@router.post("/ingest/csv/rows", response_model=IngestResult)
@limiter.limit("30/minute")
async def ingest_csv_rows(
    request: Request,
    body: CSVRowsRequest,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Step 2: Insert rows using a pre-inferred mapping. No LLM call."""
    from omnix.resolver.csv_resolver import CSVResolver
    from omnix.resolver.models import ExtractionResult

    graph_uri = tenant_graph_uri(tenant.tenant_id)
    instance_graph = kg_graph_uri(tenant.tenant_id, body.kg_name) if body.kg_name else graph_uri
    cache = _get_verdict_cache()
    resolver = SchemaResolver(neptune=client, anthropic_key=settings.anthropic_api_key, verdict_cache=cache)
    resolver._instance_graph = instance_graph
    existing_types, existing_attrs = await resolver._fetch_ontology(graph_uri)

    entities, relationships = CSVResolver.apply_mapping(body.mapping, body.rows)

    extraction = ExtractionResult(entities=entities, relationships=relationships)
    result = IngestResult(entities_extracted=len(entities))
    entity_uri_map: dict[str, str] = {}
    entity_type_map: dict[str, str] = {}
    batch_id = ""

    result = await resolver._resolve_and_insert(
        extraction, graph_uri, existing_types, existing_attrs,
        body.source, result, entity_uri_map, entity_type_map, batch_id,
    )

    NLQueryPipeline.invalidate_cache(graph_uri)
    # Incrementally embed new/changed types
    affected_types = set(result.types_created)
    for attr_added in result.attributes_added:
        if "." in attr_added:
            affected_types.add(attr_added.split(".")[0])
    if affected_types:
        from omnix.nlp.pipeline import get_embedding_service
        svc = get_embedding_service()
        if svc:
            try:
                await svc.embed_types(graph_uri, list(affected_types), client)
            except Exception:
                pass  # non-blocking
    return result


@router.post("/embeddings/build")
@limiter.limit("5/minute")
async def build_embeddings(
    request: Request,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Trigger a full embedding build for all ontology types in this tenant."""
    from omnix.nlp.pipeline import get_embedding_service
    svc = get_embedding_service()
    if not svc:
        return {"status": "embeddings_not_configured"}
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    count = await svc.build_from_ontology(graph_uri, client)
    return {"status": "ok", "types_embedded": count}
