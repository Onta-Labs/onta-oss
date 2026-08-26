"""Type-attached FUNCTION endpoint-URL registry (canonical HTTP surface).

POST   /graphs/{tenant}/functions — attach an endpoint URL to a type
GET    /graphs/{tenant}/functions — list attachments visible in the tenant graph
DELETE /graphs/{tenant}/functions/{function_name}?entity_type= — detach a TENANT attachment

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

Boundary: OSS. Imports only ``infona_client.*`` / stdlib.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from infona_client.api.deps import get_neptune_client
from infona_client.auth.access import require_tenant_write
from infona_client.auth.api_keys import TenantContext, get_tenant
from infona_client.graph.client import NeptuneClient
from infona_client.graph.iri import IRI_BASE
from infona_client.graph.layers import Layer, enhanced_graph_uri
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import (
    delete_subjects_query,
    list_functions_query,
    register_function_triple,
    resolve_function_attachment,
    tenant_graph_uri,
)
from infona_client.functions.store import StoredFunction, make_function_store
from infona_client.models.function import FunctionRef, FunctionRegister, FunctionTier

router = APIRouter()


def _stored_to_ref(rec: StoredFunction) -> FunctionRef:
    return FunctionRef(
        name=rec.name,
        entity_type=rec.entity_type,
        endpoint_url=rec.endpoint_url,
        description=rec.description,
        tier=FunctionTier.CUSTOM,
        layer=rec.layer or "tenant",
    )


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
        from infona_client.graph.layer_content import (
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

    # Neo4j has no SPARQL update. Persist via the swappable function store
    # first so a register is never a 500 on the only product backend; then
    # best-effort SPARQL for residual / hermetic FakeNeptune tests.
    bare_type = type_uri.rsplit("/", 1)[-1]
    await make_function_store().upsert(
        StoredFunction(
            tenant_id=tenant.tenant_id,
            name=body.name,
            entity_type=bare_type,
            endpoint_url=body.endpoint_url,
            description=body.description,
            layer=resolved_layer.value,
        )
    )
    try:
        await client.update(sparql)
    except Exception:
        pass
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
    stored = await make_function_store().list_for_tenant(
        tenant.tenant_id, entity_type=entity_type
    )
    by_key: dict[tuple[str, str], FunctionRef] = {
        (rec.entity_type.casefold(), rec.name.casefold()): _stored_to_ref(rec)
        for rec in stored
    }
    try:
        sparql = list_functions_query(graph_uri, entity_type)
        raw = await client.query(sparql)
        _, bindings = parse_sparql_results(raw)
        for row in bindings:
            ref = FunctionRef(
                name=row.get("name", ""),
                entity_type=row.get("type", "").split("/")[-1],
                endpoint_url=row.get("endpoint"),
                description=row.get("desc", ""),
                tier=FunctionTier.CUSTOM,
                layer=Layer.TENANT.value,
            )
            by_key.setdefault((ref.entity_type.casefold(), ref.name.casefold()), ref)
    except Exception:
        pass
    return sorted(by_key.values(), key=lambda r: (r.entity_type.casefold(), r.name.casefold()))


_NON_TENANT_DELETE_DETAIL = (
    "function attachments on the Enhanced or Public layer cannot be "
    "deleted over this tenant route"
)


def _bare_type(type_uri: str) -> str:
    return type_uri.rsplit("/", 1)[-1]


async def _sparql_has_attachment(
    client: NeptuneClient, graph_uri: str, entity_type: str, function_name: str
) -> bool:
    try:
        raw = await client.query(list_functions_query(graph_uri, entity_type))
        _, bindings = parse_sparql_results(raw)
    except Exception:
        return False
    want = function_name.casefold()
    return any(row.get("name", "").casefold() == want for row in bindings)


async def _sparql_delete_attachment(
    client: NeptuneClient, graph_uri: str, function_name: str
) -> None:
    # Residual SPARQL writer (same subject mint as register_function_triple).
    # Neo4j has no SPARQL update; best-effort like register. Builder lives in
    # queries.py so this route does not hand-roll a graph DELETE.
    try:
        await client.update(
            delete_subjects_query(
                graph_uri, [f"{IRI_BASE}/functions/{function_name}"]
            )
        )
    except Exception:
        pass


@router.delete("/graphs/{tenant}/functions/{function_name}")
async def delete_function(
    function_name: str,
    entity_type: str = Query(
        ...,
        description=(
            "Type the function is attached to. Required because attachment "
            "identity is (tenant_id, entity_type, name)."
        ),
    ),
    tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Detach a TENANT-layer function attachment.

    Identity is ``(tenant_id, entity_type, name)`` — the same key as upsert —
    so ``entity_type`` is required. Enhanced and Public attachments are
    refused (403); a missing tenant attachment is 404.

    Mutating: ``require_tenant_write`` refuses a ``reader`` member with 403.
    """
    try:
        resolved_layer, type_uri = resolve_function_attachment(entity_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if resolved_layer is not Layer.TENANT:
        raise HTTPException(status_code=403, detail=_NON_TENANT_DELETE_DETAIL)

    bare_type = _bare_type(type_uri)
    store = make_function_store()
    rows = await store.list_for_tenant(tenant.tenant_id, entity_type=bare_type)
    rec = next(
        (r for r in rows if r.name.casefold() == function_name.casefold()),
        None,
    )
    if rec is not None and (rec.layer or Layer.TENANT.value) != Layer.TENANT.value:
        raise HTTPException(status_code=403, detail=_NON_TENANT_DELETE_DETAIL)

    deleted = await store.delete(tenant.tenant_id, bare_type, function_name)
    graph_uri = tenant_graph_uri(tenant.tenant_id)
    if not deleted and not await _sparql_has_attachment(
        client, graph_uri, bare_type, function_name
    ):
        raise HTTPException(
            status_code=404,
            detail=(
                f"function {function_name!r} is not attached to type {bare_type!r}"
            ),
        )

    await _sparql_delete_attachment(client, graph_uri, function_name)
    return {"deleted": function_name, "entity_type": bare_type}
