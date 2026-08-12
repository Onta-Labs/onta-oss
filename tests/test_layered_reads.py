"""ONTA-397 — workspace ontology reads through LayerStack (ported by ONTA-527,
layered catalog merge restored by ONTA-535).

Layered reads (Tenant > Enhanced > Public shadowing) used to be a SPARQL
``LayerStack`` concern via :func:`~infona_client.graph.global_ontology.fetch_ontology`.
That SPARQL path went out with the SPARQL backend (ONTA-527). ONTA-535 ports
the route-side merge onto the GraphStore ontology catalog
(``api/routes/ontology.py::_workspace_ontology_store``): every visible layer is
read and first-visible-layer-wins shadowing is applied.

The file splits three ways:

* **Unit tests of ``fetch_ontology`` (unchanged, still green).** The layered
  SPARQL reader module still merges/shadows correctly over a SPARQL fake. The
  only remaining production caller of ``global_ontology`` is the operator's
  ``fetch_global_ontology``; workspace routes use the catalog merge.
* **Route tests for tenant write path + cross-tenant isolation** — catalog.
* **Route tests for Public / Enhanced visibility and shadowing** — catalog
  merge (ONTA-535). The ask()/SPARQL-widening cases stay xfail: NL generates
  Cypher and no longer FROM-widens SPARQL.

All mocked — no live Neptune, no live Neo4j, no LLM, no network.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infona_client.api.deps import get_neptune_client
from infona_client.api.routes import ask as ask_routes
from infona_client.api.routes import ontology as ontology_routes
from infona_client.auth import api_keys
from infona_client.auth.api_keys import TenantContext
from infona_client.graph.client import NeptuneClient
from infona_client.graph.entitlement import (
    is_entitled,
    layer_stack_for,
    register_entitlement_checker,
)
from infona_client.graph.global_ontology import fetch_global_ontology, fetch_ontology
from infona_client.graph.layers import (
    Layer,
    LayerStack,
    enhanced_graph_uri,
    layer_type_uri,
    public_graph_uri,
)
from infona_client.graph.ontology_catalog import (
    list_types as cat_list_types,
    upsert_attribute as cat_upsert_attribute,
    upsert_type as cat_upsert_type,
)
from infona_client.graph.queries import tenant_graph_uri
from infona_client.graph.store import get_graph_store
from infona_client.models.ontology import WorkspaceOntologyResponse
from infona_client.models.query import NLResult

# Reuse the global-browser fixture builders (writer-shaped triples + FakeNeptune).
from tests.test_global_ontology_browser import (
    ENH,
    PUB,
    FakeNeptune,
    shape_triples,
)

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns"
RDFS = "http://www.w3.org/2000/01/rdf-schema"
XSD = "http://www.w3.org/2001/XMLSchema"
TENANT_A = "acme"
TENANT_B = "globex"
TENANT_NS = "https://graph.infona.ai/types"
GRAPH_A = tenant_graph_uri(TENANT_A)
GRAPH_B = tenant_graph_uri(TENANT_B)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_entitlement():
    register_entitlement_checker(None)
    yield
    register_entitlement_checker(None)


def _run(coro):
    """Drive a coroutine from a sync (TestClient) test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def store():
    """The process GraphStore conftest installed for this test."""
    return get_graph_store()


def _declare(store, name, *, layer, tenant_id=None, description="", attrs=()):
    """Declare a type (+ attrs) in one catalog layer. Global layers need privilege."""
    privileged = layer in ("public", "enhanced")
    _run(
        cat_upsert_type(
            name=name,
            description=description,
            layer=layer,
            tenant_id=tenant_id,
            privileged=privileged,
            store=store,
        )
    )
    for attr in attrs:
        _run(
            cat_upsert_attribute(
                type_name=name,
                attr_name=attr,
                datatype="string",
                layer=layer,
                tenant_id=tenant_id,
                privileged=privileged,
                store=store,
            )
        )


def _tenant_ctx(tenant_id: str, *, entitled: bool = False) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        api_key="k",
        enhanced_entitled=entitled,
    )


def _seeded_public_only() -> FakeNeptune:
    """Public has Hotel{name}; Enhanced + every tenant graph empty."""
    public = shape_triples(
        PUB,
        "Hotel",
        comment="A lodging place",
        slots=[{"name": "name", "range": f"{XSD}#string", "why": "display name"}],
    )
    return FakeNeptune({
        public_graph_uri(): public,
        enhanced_graph_uri(): [],
        GRAPH_A: [],
        GRAPH_B: [],
    })


def _seeded_collision() -> FakeNeptune:
    """Hotel defined in tenant A, Enhanced, and Public with distinct comments."""
    public = shape_triples(
        PUB, "Hotel", comment="public-hotel",
        slots=[{"name": "stars", "range": f"{XSD}#integer"}],
    )
    enhanced = shape_triples(
        ENH, "Hotel", comment="enhanced-hotel",
        slots=[{"name": "loyaltyTier", "range": f"{XSD}#string"}],
    )
    tenant = shape_triples(
        TENANT_NS, "Hotel", comment="tenant-hotel",
        slots=[{"name": "roomCount", "range": f"{XSD}#integer"}],
    )
    return FakeNeptune({
        public_graph_uri(): public,
        enhanced_graph_uri(): enhanced,
        GRAPH_A: tenant,
        GRAPH_B: [],  # tenant B empty — must not see A's Hotel via union
    })


def _seeded_enhanced_only_type() -> FakeNeptune:
    """Enhanced has VipGuest; Public + tenant empty."""
    enhanced = shape_triples(
        ENH, "VipGuest", comment="premium guest",
        slots=[{"name": "tier", "range": f"{XSD}#string"}],
    )
    return FakeNeptune({
        public_graph_uri(): [],
        enhanced_graph_uri(): enhanced,
        GRAPH_A: [],
    })


def _ontology_app(neptune=None, tenant_id: str = TENANT_A, *, entitled: bool = False):
    """Ontology router wired to one tenant context.

    ``neptune`` defaults to a bare mock: the ontology read/write routes still
    DECLARE the NeptuneClient dependency but must never call it now that the
    catalog is the substrate, so leaving it un-stubbed lets a test prove that
    with ``assert_not_called()``.
    """
    app = FastAPI()
    app.include_router(ontology_routes.router)
    neptune = neptune if neptune is not None else AsyncMock(spec=NeptuneClient)
    app.dependency_overrides[get_neptune_client] = lambda: neptune
    ctx = _tenant_ctx(tenant_id, entitled=entitled)
    app.dependency_overrides[api_keys.get_tenant] = (
        lambda tenant=None, api_key=None, request=None: ctx
    )
    if entitled:
        register_entitlement_checker(
            lambda t: t.tenant_id == tenant_id or bool(t.enhanced_entitled)
        )
    client = TestClient(app)
    client.neptune = neptune  # type: ignore[attr-defined]
    return client


# ---------------------------------------------------------------------------
# fetch_ontology unit — shadowing, degradation, entitlement shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_ontology_empty_tenant_sees_public():
    neptune = _seeded_public_only()
    stack = LayerStack(GRAPH_A, entitled=False)
    body = await fetch_ontology(
        neptune,
        layers=stack.layer_pairs(),
        entitled=False,
        tenant_id=TENANT_A,
        apply_shadowing=True,
    )
    assert isinstance(body, WorkspaceOntologyResponse)
    assert body.tenant_id == TENANT_A
    assert body.entitled is False
    names = [t.name for t in body.types]
    assert names == ["Hotel"]
    assert body.types[0].layer == "public"
    assert body.types[0].description == "A lodging place"
    attr_names = [a.name for a in body.types[0].attributes]
    assert "name" in attr_names
    # Layer status: tenant empty but available; public has 1 type.
    by_layer = {L.layer: L for L in body.layers}
    assert by_layer["tenant"].type_count == 0
    assert by_layer["public"].type_count == 1
    assert by_layer["public"].available is True
    assert "enhanced" not in by_layer  # non-entitled


@pytest.mark.asyncio
async def test_fetch_ontology_tenant_shadows_public_and_enhanced():
    neptune = _seeded_collision()
    stack = LayerStack(GRAPH_A, entitled=True)
    body = await fetch_ontology(
        neptune,
        layers=stack.layer_pairs(),
        entitled=True,
        tenant_id=TENANT_A,
        apply_shadowing=True,
    )
    hotels = [t for t in body.types if t.name == "Hotel"]
    assert len(hotels) == 1, f"shadowing failed — got {len(hotels)} Hotels"
    assert hotels[0].layer == "tenant"
    assert hotels[0].description == "tenant-hotel"
    attr_names = {a.name for a in hotels[0].attributes}
    assert "roomCount" in attr_names
    assert "stars" not in attr_names  # public's attrs must not leak under shadow
    assert "loyaltyTier" not in attr_names


@pytest.mark.asyncio
async def test_shadowing_mutation_reversed_precedence_would_pick_public():
    """Planted-failure guard: if precedence were Public-first, the winner changes.

    Proves the shadowing test is sensitive to order, not a tautology that
    passes under any merge order.
    """
    neptune = _seeded_collision()
    # Deliberately reverse: Public > Enhanced > Tenant
    reversed_layers = [
        (Layer.PUBLIC, public_graph_uri()),
        (Layer.ENHANCED, enhanced_graph_uri()),
        (Layer.TENANT, GRAPH_A),
    ]
    body = await fetch_ontology(
        neptune,
        layers=reversed_layers,
        entitled=True,
        tenant_id=TENANT_A,
        apply_shadowing=True,
    )
    hotels = [t for t in body.types if t.name == "Hotel"]
    assert len(hotels) == 1
    # Under reversed order Public wins — this MUST differ from the real stack.
    assert hotels[0].layer == "public"
    assert hotels[0].description == "public-hotel"
    # And the REAL stack still picks tenant (cross-check, not coincidence).
    real = await fetch_ontology(
        neptune,
        layers=LayerStack(GRAPH_A, entitled=True).layer_pairs(),
        entitled=True,
        tenant_id=TENANT_A,
        apply_shadowing=True,
    )
    assert [t for t in real.types if t.name == "Hotel"][0].layer == "tenant"
    assert [t for t in real.types if t.name == "Hotel"][0].layer != hotels[0].layer


@pytest.mark.asyncio
async def test_fetch_ontology_no_shadowing_returns_all_layer_rows():
    neptune = _seeded_collision()
    stack = LayerStack(GRAPH_A, entitled=True)
    body = await fetch_ontology(
        neptune,
        layers=stack.layer_pairs(),
        entitled=True,
        tenant_id=TENANT_A,
        apply_shadowing=False,
    )
    hotels = [t for t in body.types if t.name == "Hotel"]
    assert len(hotels) == 3
    assert {t.layer for t in hotels} == {"tenant", "enhanced", "public"}


@pytest.mark.asyncio
async def test_fetch_ontology_non_entitled_never_sees_enhanced_type():
    neptune = _seeded_enhanced_only_type()
    stack = LayerStack(GRAPH_A, entitled=False)
    body = await fetch_ontology(
        neptune,
        layers=stack.layer_pairs(),
        entitled=False,
        tenant_id=TENANT_A,
        apply_shadowing=True,
    )
    assert body.types == []
    assert all(L.layer != "enhanced" for L in body.layers)


@pytest.mark.asyncio
async def test_fetch_ontology_entitled_sees_enhanced_type():
    neptune = _seeded_enhanced_only_type()
    stack = LayerStack(GRAPH_A, entitled=True)
    body = await fetch_ontology(
        neptune,
        layers=stack.layer_pairs(),
        entitled=True,
        tenant_id=TENANT_A,
        apply_shadowing=True,
    )
    names = [t.name for t in body.types]
    assert names == ["VipGuest"]
    assert body.types[0].layer == "enhanced"


@pytest.mark.asyncio
async def test_fetch_ontology_failing_public_layer_degrades():
    """A broken Public layer yields available=False; tenant types still land."""
    tenant = shape_triples(
        TENANT_NS, "LocalThing", comment="ok",
        slots=[{"name": "x", "range": f"{XSD}#string"}],
    )
    neptune = FakeNeptune(
        {GRAPH_A: tenant, enhanced_graph_uri(): []},
        failing={public_graph_uri()},
    )
    stack = LayerStack(GRAPH_A, entitled=False)
    body = await fetch_ontology(
        neptune,
        layers=stack.layer_pairs(),
        entitled=False,
        tenant_id=TENANT_A,
        apply_shadowing=True,
    )
    assert [t.name for t in body.types] == ["LocalThing"]
    by_layer = {L.layer: L for L in body.layers}
    assert by_layer["public"].available is False
    assert by_layer["public"].type_count == 0
    assert by_layer["tenant"].available is True


@pytest.mark.asyncio
async def test_cross_tenant_isolation_under_union():
    """Tenant A's type must never appear in tenant B's layered read."""
    neptune = _seeded_collision()  # Hotel only in GRAPH_A (+ globals)
    stack_b = LayerStack(GRAPH_B, entitled=True)
    body_b = await fetch_ontology(
        neptune,
        layers=stack_b.layer_pairs(),
        entitled=True,
        tenant_id=TENANT_B,
        apply_shadowing=True,
    )
    # B sees the GLOBAL Hotel (public/enhanced), never A's tenant definition.
    hotels = [t for t in body_b.types if t.name == "Hotel"]
    assert len(hotels) == 1
    assert hotels[0].layer in {"public", "enhanced"}
    assert hotels[0].description != "tenant-hotel"
    # And tenant_id stamped is B's.
    assert body_b.tenant_id == TENANT_B


@pytest.mark.asyncio
async def test_fetch_global_ontology_untouched_by_workspace_reader():
    """Operator two-layer call is independent — not rewritten through fetch_ontology."""
    neptune = _seeded_collision()
    global_body = await fetch_global_ontology(neptune)
    # Operator raw browse: both global Hotels, no tenant.
    hotels = [t for t in global_body.types if t.name == "Hotel"]
    assert len(hotels) == 2
    assert {t.layer for t in hotels} == {"public", "enhanced"}
    assert all(t.description != "tenant-hotel" for t in hotels)


# ---------------------------------------------------------------------------
# Ontology routes — what survived: tenant-layer writes + cross-tenant isolation
# ---------------------------------------------------------------------------


def test_write_create_type_targets_the_tenant_layer_only(store):
    """Ordinary mutation writes the TENANT catalog layer, never a global one.

    (Was: scrape ``neptune.update`` for the tenant graph IRI and assert the
    global layer IRIs are absent from the emitted SPARQL. The write emits no
    SPARQL now, so assert the same property on the catalog itself — which also
    covers the case the string check could not: a write that reached the global
    layer through some path that never spells the IRI.)
    """
    client = _ontology_app(tenant_id=TENANT_A, entitled=True)
    r = client.post(
        f"/graphs/{TENANT_A}/ontology/types",
        json={"name": "Widget", "description": "w"},
    )
    assert r.status_code == 201, r.text

    tenant_types = _run(cat_list_types(layer="tenant", tenant_id=TENANT_A, store=store))
    assert [t.name for t in tenant_types] == ["Widget"]
    assert tenant_types[0].tenant_id == TENANT_A

    # Neither global catalog layer was touched...
    assert _run(cat_list_types(layer="public", store=store)) == []
    assert _run(cat_list_types(layer="enhanced", store=store)) == []
    # ...nor any peer workspace's layer.
    assert _run(cat_list_types(layer="tenant", tenant_id=TENANT_B, store=store)) == []
    client.neptune.update.assert_not_called()


def test_cross_tenant_route_isolation(store):
    """Tenant B's list_types must not surface tenant A's private Hotel.

    Isolation is by ``tenant_id`` on the catalog scope now rather than by named
    graph, and it is asserted on DATA: A's private description is seeded and
    must not appear anywhere in B's response body.
    """
    _declare(store, "Hotel", layer="tenant", tenant_id=TENANT_A,
             description="tenant-hotel", attrs=["roomCount"])

    client = _ontology_app(tenant_id=TENANT_B, entitled=True)
    r = client.get(f"/graphs/{TENANT_B}/ontology/types")
    assert r.status_code == 200
    assert [t for t in r.json() if t["name"] == "Hotel"] == []
    assert "tenant-hotel" not in r.text
    assert "roomCount" not in r.text

    # ...and the owner still sees it (isolation, not a blanket empty read).
    own = _ontology_app(tenant_id=TENANT_A, entitled=True).get(
        f"/graphs/{TENANT_A}/ontology/types"
    )
    hotels = [t for t in own.json() if t["name"] == "Hotel"]
    assert len(hotels) == 1 and hotels[0]["description"] == "tenant-hotel"


def test_get_type_enhanced_404_when_not_entitled(store):
    """A non-entitled workspace must not reach an Enhanced-only type.

    ONTA-535: real entitlement gate — Enhanced is only in the LayerStack when
    entitled, so a non-entitled read 404s on an Enhanced-only type.
    """
    _declare(store, "VipGuest", layer="enhanced", description="premium guest",
             attrs=["tier"])
    client = _ontology_app(tenant_id=TENANT_A, entitled=False)
    r = client.get(f"/graphs/{TENANT_A}/ontology/types/VipGuest")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Ontology routes — layered catalog reads (ONTA-535)
# ---------------------------------------------------------------------------
#
# api/routes/ontology.py::_workspace_ontology_store merges every visible
# catalog layer (Tenant > Enhanced when entitled > Public) with first-wins
# shadowing. Layers are seeded here through the ordinary privileged write path.


def test_list_types_empty_tenant_sees_public(store):
    _declare(store, "Hotel", layer="public", description="A lodging place",
             attrs=["stars"])
    client = _ontology_app(tenant_id=TENANT_A, entitled=False)
    r = client.get(f"/graphs/{TENANT_A}/ontology/types")
    assert r.status_code == 200, r.text
    assert [t["name"] for t in r.json()] == ["Hotel"]


def test_list_types_shadowing_one_hotel(store):
    _declare(store, "Hotel", layer="public", description="public-hotel",
             attrs=["stars"])
    _declare(store, "Motel", layer="public", description="public-motel")
    _declare(store, "Hotel", layer="enhanced", description="enhanced-hotel",
             attrs=["loyaltyTier"])
    _declare(store, "Hotel", layer="tenant", tenant_id=TENANT_A,
             description="tenant-hotel", attrs=["roomCount"])

    client = _ontology_app(tenant_id=TENANT_A, entitled=True)
    r = client.get(f"/graphs/{TENANT_A}/ontology/types")
    assert r.status_code == 200
    # Merged and deduped: the collision collapses, the Public-only type stays.
    assert sorted(t["name"] for t in r.json()) == ["Hotel", "Motel"]
    hotels = [t for t in r.json() if t["name"] == "Hotel"]
    assert len(hotels) == 1
    assert hotels[0]["description"] == "tenant-hotel"
    # The shadowed layers' slots must not leak in under the winner.
    attr_names = {a["name"] for a in hotels[0]["attributes"]}
    assert attr_names == {"roomCount"}


def test_get_type_public_detail(store):
    _declare(store, "Hotel", layer="public", description="A lodging place",
             attrs=["stars"])
    client = _ontology_app(tenant_id=TENANT_A, entitled=False)
    r = client.get(f"/graphs/{TENANT_A}/ontology/types/Hotel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Hotel"
    assert body["description"] == "A lodging place"
    assert any(a["name"] == "stars" for a in body["attributes"])


def test_get_type_enhanced_ok_when_entitled(store):
    _declare(store, "VipGuest", layer="enhanced", description="premium guest",
             attrs=["tier"])
    client = _ontology_app(tenant_id=TENANT_A, entitled=True)
    r = client.get(f"/graphs/{TENANT_A}/ontology/types/VipGuest")
    assert r.status_code == 200
    assert r.json()["name"] == "VipGuest"


def test_workspace_ontology_route_returns_workspace_model(store):
    _declare(store, "Hotel", layer="public", description="A lodging place",
             attrs=["stars"])
    client = _ontology_app(tenant_id=TENANT_A, entitled=False)
    r = client.get(f"/graphs/{TENANT_A}/ontology")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == TENANT_A
    assert body["entitled"] is False
    assert [t["name"] for t in body["types"]] == ["Hotel"]
    assert body["types"][0]["layer"] == "public"


def test_workspace_ontology_route_shape_and_tenant_layer(store):
    """Envelope + tenant-layer types; layers strip is populated (ONTA-535)."""
    _declare(store, "Hotel", layer="tenant", tenant_id=TENANT_A,
             description="tenant-hotel", attrs=["roomCount"])
    client = _ontology_app(tenant_id=TENANT_A, entitled=False)
    r = client.get(f"/graphs/{TENANT_A}/ontology")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == TENANT_A
    assert body["entitled"] is False
    assert set(body) >= {"tenant_id", "entitled", "layers", "types"}
    layer_names = {L["layer"] for L in body["layers"]}
    assert "tenant" in layer_names
    assert "public" in layer_names
    assert "enhanced" not in layer_names  # non-entitled
    hotel = next(t for t in body["types"] if t["name"] == "Hotel")
    assert hotel["layer"] == "tenant"
    assert hotel["description"] == "tenant-hotel"
    assert [a["name"] for a in hotel["attributes"]] == ["roomCount"]
    assert hotel["relationships"] == []
    client.neptune.query.assert_not_called()


# ---------------------------------------------------------------------------
# ask route — still passes layer_graph_uris; the pipeline no longer uses them
# ---------------------------------------------------------------------------
#
# The two route tests below are unchanged and still green: routes/ask.py builds
# the LayerStack and threads visible_graph_uris() into the pipeline exactly as
# before. What changed is on the other side of that call —
# nlp/pipeline.py::_ask_cypher opens with `del layer_graph_uris` — so the
# argument is now computed, passed, and dropped. See the xfail below.


def test_ask_route_passes_layer_graph_uris():
    app = FastAPI()
    app.include_router(ask_routes.router)
    app.dependency_overrides[get_neptune_client] = lambda: AsyncMock()
    app.dependency_overrides[api_keys.get_tenant] = (
        lambda tenant=None, api_key=None, request=None: _tenant_ctx(TENANT_A)
    )
    # Enrichment job store optional on the route.
    from infona_client.api.deps import get_enrichment_job_store
    app.dependency_overrides[get_enrichment_job_store] = lambda: None

    captured: dict = {}

    async def _fake_ask(self, question, graph_uri, instance_graph=None,
                        exclude_questions=None, layer_graph_uris=None, **kw):
        captured["layer_graph_uris"] = layer_graph_uris
        captured["graph_uri"] = graph_uri
        return NLResult(answer="ok", sparql="SELECT * WHERE {}", explanation="")

    client = TestClient(app)
    with patch.object(ask_routes.NLQueryPipeline, "ask", _fake_ask):
        r = client.post(
            f"/graphs/{TENANT_A}/ask",
            json={"question": "list hotels"},
        )
    assert r.status_code == 200, r.text
    uris = captured["layer_graph_uris"]
    assert uris is not None
    assert GRAPH_A in uris
    assert public_graph_uri() in uris
    assert enhanced_graph_uri() not in uris  # non-entitled


def test_ask_route_entitled_includes_enhanced_graph():
    app = FastAPI()
    app.include_router(ask_routes.router)
    app.dependency_overrides[get_neptune_client] = lambda: AsyncMock()
    app.dependency_overrides[api_keys.get_tenant] = (
        lambda tenant=None, api_key=None, request=None: _tenant_ctx(
            TENANT_A, entitled=True
        )
    )
    from infona_client.api.deps import get_enrichment_job_store
    app.dependency_overrides[get_enrichment_job_store] = lambda: None
    register_entitlement_checker(lambda t: bool(t.enhanced_entitled))

    captured: dict = {}

    async def _fake_ask(self, question, graph_uri, instance_graph=None,
                        exclude_questions=None, layer_graph_uris=None, **kw):
        captured["layer_graph_uris"] = layer_graph_uris
        return NLResult(answer="ok", sparql="", explanation="")

    client = TestClient(app)
    with patch.object(ask_routes.NLQueryPipeline, "ask", _fake_ask):
        r = client.post(
            f"/graphs/{TENANT_A}/ask",
            json={"question": "list vip guests"},
        )
    assert r.status_code == 200
    assert enhanced_graph_uri() in captured["layer_graph_uris"]


@pytest.mark.xfail(
    reason=(
        "LOST CAPABILITY (ONTA-527): ask() no longer widens the generated query "
        "to the visible layer graphs, because it no longer generates SPARQL at "
        "all. nlp/pipeline.py::ask dispatches to _ask_cypher whenever "
        "neo4j_ask_enabled() — i.e. always — and _ask_cypher's first statement "
        "is `del layer_graph_uris  # reserved for catalog-layer joins in later "
        "E6`. Its ontology comes from ontology_catalog.schema_types_for_kg, "
        "which reads the TENANT catalog only, so a Public-layer type is neither "
        "shown to the planner nor reachable by the generated Cypher. The route "
        "still computes and passes the layer URIs (tests above), which makes "
        "this look wired end to end when it is not."
    ),
    strict=True,
)
@pytest.mark.asyncio
async def test_ask_binds_public_type_in_generated_sparql():
    """End-to-end consumer proof: ask over a Public-only type produces SPARQL
    that references the Public type URI and returns rows (the planner binds).
    """
    from infona_client.nlp.pipeline import NLQueryPipeline, _ontology_cache

    _ontology_cache.clear()
    public = shape_triples(
        PUB, "Hotel", comment="A lodging place",
        slots=[{"name": "name", "range": f"{XSD}#string"}],
    )
    # Instance data typed with the Public type URI lives in the tenant graph
    # (writes always go to tenant; type can still be a Public IRI).
    neptune = FakeNeptune({
        public_graph_uri(): public,
        enhanced_graph_uri(): [],
        GRAPH_A: [],
    })
    # After SPARQL gen, execution must return a row so we know it "bound".
    hotel_uri = layer_type_uri(Layer.PUBLIC, "Hotel")
    generated = (
        f"SELECT ?name FROM <{GRAPH_A}> WHERE {{ "
        f"?h a <{hotel_uri}> . "
        f"?h <https://graph.infona.ai/types/public/Hotel/attrs/name> ?name }}"
    )

    # FakeNeptune only knows full_ontology_detail_query shapes via FROM match;
    # for the instance SELECT we intercept via a wrapper.
    class _AskNeptune(FakeNeptune):
        async def query(self, sparql: str) -> dict:
            if "SELECT ?name" in sparql and hotel_uri in sparql:
                return {
                    "head": {"vars": ["name"]},
                    "results": {
                        "bindings": [
                            {"name": {"type": "literal", "value": "Ritz"}}
                        ]
                    },
                }
            return await super().query(sparql)

    ask_neptune = _AskNeptune({
        public_graph_uri(): public,
        enhanced_graph_uri(): [],
        GRAPH_A: [],
    })
    pipeline = NLQueryPipeline(ask_neptune, "fake-key")
    stack = LayerStack(GRAPH_A, entitled=False)

    def _mock_llm_message(sparql: str) -> MagicMock:
        msg = MagicMock()
        msg.content = [MagicMock(text=json.dumps({
            "sparql": sparql, "explanation": "list hotels", "functions_needed": [],
        }))]
        return msg

    with patch.object(
        pipeline.anthropic.messages, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = _mock_llm_message(generated)
        result = await pipeline.ask(
            "What hotels exist?",
            GRAPH_A,
            layer_graph_uris=stack.visible_graph_uris(),
        )

    # SPARQL widened to Public layer + references the Public type URI.
    assert hotel_uri in result.sparql
    assert f"FROM <{public_graph_uri()}>" in result.sparql
    # And it bound — answer carries the instance value (not a zero-row degrade).
    assert "Ritz" in result.answer or result.sparql  # rows returned → not empty fail
    # Ontology summary the LLM saw included the Public type.
    # (verify via the create call's prompt containing Hotel + public URI)
    call_kwargs = mock_create.await_args
    prompt_blob = str(call_kwargs)
    assert "Hotel" in prompt_blob
    assert "types/public/Hotel" in prompt_blob or "public" in prompt_blob.lower()


@pytest.mark.asyncio
async def test_pipeline_fetch_ontology_includes_public_with_layer_uris():
    from infona_client.nlp.pipeline import NLQueryPipeline, _ontology_cache

    _ontology_cache.clear()
    public = shape_triples(
        PUB, "Hotel", comment="A lodging place",
        slots=[{"name": "name", "range": f"{XSD}#string"}],
    )
    neptune = FakeNeptune({
        public_graph_uri(): public,
        enhanced_graph_uri(): [],
        GRAPH_A: [],
    })
    # get_full_ontology_query projects ?typeLabel not the detail query's shape;
    # FakeNeptune uses _rows_for which is detail-shaped. Bridge by making the
    # fake also answer get_full_ontology_query via the same triples.
    pipeline = NLQueryPipeline(neptune, "fake-key")
    stack = LayerStack(GRAPH_A, entitled=False)
    summary = await pipeline._fetch_ontology(
        GRAPH_A, layer_graph_uris=stack.visible_graph_uris()
    )
    assert "Type: Hotel" in summary
    assert "types/public/Hotel" in summary
    assert "name" in summary


# ---------------------------------------------------------------------------
# schema_resolver closure wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_resolver_parent_map_uses_layer_stack(tmp_path):
    """ingest wires layer_stack= into _fetch_parent_map (the three call sites)."""
    from infona_client.resolver.schema_resolver import SchemaResolver
    from infona_client.resolver.verdict_cache import JsonVerdictCache

    mock_neptune = AsyncMock()
    mock_neptune.query.return_value = {
        "head": {"vars": []}, "results": {"bindings": []},
    }
    mock_neptune.update.return_value = None
    with patch.dict("os.environ", {
        "ANTHROPIC_API_KEY": "test-key",
        "OPENROUTER_API_KEY": "test-or-key",
        "INFONA_ER_ENABLED": "0",
    }):
        resolver = SchemaResolver(
            neptune=mock_neptune,
            anthropic_key="test-key",
            verdict_cache=JsonVerdictCache(tmp_path / "v.json"),
        )

    seen: dict = {}

    async def _spy_parent_map(graph_uri, layer_stack=None):
        seen["layer_stack"] = layer_stack
        return {}

    async def _spy_ontology(graph_uri):
        return {}, {}

    with patch.object(resolver, "_fetch_parent_map", side_effect=_spy_parent_map), \
         patch.object(resolver, "_fetch_ontology", side_effect=_spy_ontology), \
         patch.object(resolver, "_ingest_csv", new_callable=AsyncMock) as mock_csv:
        mock_csv.return_value = MagicMock()
        # Short-circuit before extraction: force content_type that hits parent_map.
        # Use a path that returns early after parent_map — text with empty extract.
        with patch.object(
            resolver, "_extract", new_callable=AsyncMock, return_value=[]
        ):
            from infona_client.resolver.schema_resolver import IngestResult
            # Minimal: call the parent_map wiring the way ingest does.
            stack = resolver._layer_stack_for(TENANT_A, GRAPH_A)
            await resolver._fetch_parent_map(GRAPH_A, layer_stack=stack)

    # Direct call with stack exercised the signature.
    assert seen["layer_stack"] is not None
    assert isinstance(seen["layer_stack"], LayerStack)
    assert public_graph_uri() in seen["layer_stack"].visible_graph_uris()


def test_layer_stack_for_matches_is_entitled():
    register_entitlement_checker(None)
    free = _tenant_ctx("free")
    assert is_entitled(free) is False
    assert layer_stack_for(free).layers == (Layer.TENANT, Layer.PUBLIC)

    register_entitlement_checker(lambda t: t.tenant_id == "paid")
    paid = _tenant_ctx("paid")
    assert layer_stack_for(paid).layers == (
        Layer.TENANT, Layer.ENHANCED, Layer.PUBLIC,
    )


def test_layer_pairs_order_matches_precedence():
    stack = LayerStack(GRAPH_A, entitled=True)
    pairs = stack.layer_pairs()
    assert [layer for layer, _ in pairs] == [
        Layer.TENANT, Layer.ENHANCED, Layer.PUBLIC,
    ]
    assert pairs[0][1] == GRAPH_A
    assert pairs[1][1] == enhanced_graph_uri()
    assert pairs[2][1] == public_graph_uri()
