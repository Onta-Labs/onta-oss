"""Type-attached FUNCTION endpoint-URL registry (canonical HTTP surface).

POST /graphs/{tenant}/functions — attach an endpoint URL to a type
GET  /graphs/{tenant}/functions — list attachments visible in the tenant graph

**Attachment identity (ONTA-399).** The SPARQL writer
(``register_function_triple``) mints a layer-qualified type URI via
``layer_type_uri`` and writes into the correct named graph:

* Tenant (default) → ``types/<T>`` in the tenant graph
* Enhanced → ``types/x/<T>`` in ``graphs/global/enhanced`` (operator-only
  over HTTP; ordinary workspace writes stay on Tenant — plan §5)
* Public → refused (ONTA-400 / ``LAYER_CONTENT_MATRIX``)

This route is the **endpoint-URL registry**, not the execution engine. Runtime
still goes through ``functions/executor.py`` (HTTP tier-2 + platform lambdas)
and ``api/routes/lambda_functions.py`` (invoke + materialize). Layer-B work is
attachment identity only — no new runtime.

Boundary: OSS. Imports only ``cograph_client.*`` / stdlib.
"""

from fastapi import APIRouter, Depends, HTTPException

from cograph_client.api.deps import get_neptune_client
from cograph_client.auth.access import require_tenant_write
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.layers import Layer, enhanced_graph_uri
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import (
    list_functions_query,
    register_function_triple,
    resolve_function_attachment,
    tenant_graph_uri,
)
from cograph_client.models.function import FunctionRef, FunctionRegister, FunctionTier

router = APIRouter()


def _layer_from_body(body: FunctionRegister) -> Layer | None:
    if body.layer is None:
        return None
    try:
        return Layer(body.layer)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"unknown layer {body.layer!r}"
        ) from exc


@router.post("/graphs/{tenant}/functions", status_code=201)
async def register_function(
    body: FunctionRegister,
    tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Attach a function endpoint URL to a type.

    Tenant attachments are the ordinary workspace write path. Enhanced
    attachments are operator-only (global-layer authoring); Public is refused
    by the writer (ONTA-400).

    Mutating: ``require_tenant_write`` refuses a ``reader`` member with 403
    (ONTA-451). The ``GET`` listing below stays on plain ``get_tenant``.
    """
    layer = _layer_from_body(body)
    try:
        resolved_layer, type_uri = resolve_function_attachment(
            body.entity_type, layer=layer
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if resolved_layer is Layer.PUBLIC:
        # Writer also refuses; fail at the route with a clear 422.
        from cograph_client.graph.layer_content import (
            ContentKind,
            LayerContentError,
            assert_permits,
        )

        try:
            assert_permits(
                Layer.PUBLIC,
                ContentKind.FUNCTIONS,
                what=f"register_function entity_type={body.entity_type!r}",
            )
        except LayerContentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if resolved_layer is Layer.ENHANCED and not tenant.is_operator:
        raise HTTPException(
            status_code=403,
            detail=(
                "attaching a function to the Enhanced global layer is "
                "operator-only; workspace writes stay on the Tenant layer"
            ),
        )

    graph_uri = (
        enhanced_graph_uri()
        if resolved_layer is Layer.ENHANCED
        else tenant_graph_uri(tenant.tenant_id)
    )
    try:
        sparql = register_function_triple(
            graph_uri,
            entity_type=body.entity_type,
            function_name=body.name,
            endpoint_url=body.endpoint_url,
            description=body.description,
            layer=resolved_layer,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await client.update(sparql)
    bare_type = type_uri.rsplit("/", 1)[-1]
    return {
        "registered": body.name,
        "entity_type": bare_type,
        "layer": resolved_layer.value,
        "type_uri": type_uri,
        "graph_uri": graph_uri,
    }


@router.get("/graphs/{tenant}/functions", response_model=list[FunctionRef])
async def list_functions(
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
    entity_type: str | None = None,
):
    """List functions in the tenant graph (workspace layer).

    Enhanced global functions live in ``graphs/global/enhanced`` and are read
    via the operator Global Ontology browser / layered reads (ONTA-397) — they
    are not mixed into this tenant-scoped list so a non-entitled workspace
    cannot discover them by listing functions.
    """
    graph_uri = tenant_graph_uri(tenant.tenant_id)
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
            layer=Layer.TENANT.value,
        )
        for row in bindings
    ]
