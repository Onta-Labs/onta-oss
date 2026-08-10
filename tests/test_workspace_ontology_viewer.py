"""ONTA-408 — workspace ontology viewer: isolation + overlays + operator untouched.

Acceptance:
  * Cross-tenant isolation on ``GET /graphs/{tenant}/ontology`` is adversarial
  * Tenant-custom sources appear for the owner and never for another tenant
  * Tenant skills appear for the owner and never for another tenant
  * Operator ``GET /operator/ontology/global`` payload stays independent
    (no tenant_id, no tenant_custom sources, no tenant skills)
  * ``require_operator`` remains router-wide on the operator router

All mocked — no live Neptune, no LLM, no network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infona_client.api.deps import get_neptune_client
from infona_client.api.routes import ontology as ontology_routes
from infona_client.api.routes import operator as operator_routes
from infona_client.auth import api_keys
from infona_client.auth.api_keys import TenantContext
from infona_client.graph.entitlement import register_entitlement_checker
from infona_client.graph.global_ontology import fetch_global_ontology, fetch_ontology
from infona_client.graph.layers import (
    Layer,
    LayerStack,
    enhanced_graph_uri,
    public_graph_uri,
)
from infona_client.graph.queries import tenant_graph_uri
from infona_client.skills.models import TypeSkill
from infona_client.skills.store import InMemoryTypeSkillStore, reset_type_skill_store

from infona_client.api_registry.catalog import (
    LAYER_TENANT_CUSTOM,
    reset_api_source_catalog,
    set_tenant_custom_specs,
)
from infona_client.api_registry.spec import ApiSourceSpec

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


@pytest.fixture(autouse=True)
def _reset_side_stores():
    register_entitlement_checker(None)
    reset_api_source_catalog()
    reset_type_skill_store()
    yield
    register_entitlement_checker(None)
    reset_api_source_catalog()
    reset_type_skill_store()


def _tenant_ctx(
    tenant_id: str, *, entitled: bool = False, is_operator: bool = False
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        api_key="k",
        enhanced_entitled=entitled,
        is_operator=is_operator,
    )


def _seeded_private_hotels() -> FakeNeptune:
    """Tenant A and Tenant B each have a private Hotel; Public has BaseHotel."""
    public = shape_triples(
        PUB,
        "BaseHotel",
        comment="shared-public",
        slots=[{"name": "name", "range": f"{XSD}#string"}],
    )
    a = shape_triples(
        TENANT_NS,
        "Hotel",
        comment="acme-secret-hotel",
        slots=[{"name": "acmeRoomCount", "range": f"{XSD}#integer"}],
    )
    b = shape_triples(
        TENANT_NS,
        "Hotel",
        comment="globex-secret-hotel",
        slots=[{"name": "globexSuiteCode", "range": f"{XSD}#string"}],
    )
    return FakeNeptune({
        public_graph_uri(): public,
        enhanced_graph_uri(): [],
        GRAPH_A: a,
        GRAPH_B: b,
    })


def _ontology_app(neptune, tenant_id: str, *, entitled: bool = False):
    app = FastAPI()
    app.include_router(ontology_routes.router)
    app.dependency_overrides[get_neptune_client] = lambda: neptune
    ctx = _tenant_ctx(tenant_id, entitled=entitled)
    app.dependency_overrides[api_keys.get_tenant] = (
        lambda tenant=None, api_key=None, request=None: ctx
    )
    return TestClient(app)


def _operator_app(neptune):
    app = FastAPI()
    app.include_router(operator_routes.router)
    app.dependency_overrides[get_neptune_client] = lambda: neptune
    ctx = _tenant_ctx(TENANT_A, is_operator=True)
    app.dependency_overrides[api_keys.get_tenant] = (
        lambda tenant=None, api_key=None, request=None: ctx
    )
    return TestClient(app)


def _spec(slug: str, kinds: list[str]) -> ApiSourceSpec:
    return ApiSourceSpec.from_dict({
        "slug": slug,
        "title": f"Title {slug}",
        "publisher": "TestCo",
        "description": "test source",
        "coverage": {"entity_kinds": kinds, "attributes": ["name"]},
        "authority_level": "supplementary",
        "endpoints": [
            {
                "name": "lookup",
                "method": "GET",
                "path": "/v1/x",
                "field_mappings": {"name": "name"},
            }
        ],
    })


# ---------------------------------------------------------------------------
# Cross-tenant isolation (adversarial)
# ---------------------------------------------------------------------------


def test_workspace_route_does_not_leak_peer_tenant_types():
    """Tenant B must never see Tenant A's private Hotel description/slots."""
    neptune = _seeded_private_hotels()
    a = _ontology_app(neptune, TENANT_A).get(f"/graphs/{TENANT_A}/ontology")
    b = _ontology_app(neptune, TENANT_B).get(f"/graphs/{TENANT_B}/ontology")
    assert a.status_code == 200 and b.status_code == 200

    a_body, b_body = a.json(), b.json()
    assert a_body["tenant_id"] == TENANT_A
    assert b_body["tenant_id"] == TENANT_B

    a_hotels = [t for t in a_body["types"] if t["name"] == "Hotel"]
    b_hotels = [t for t in b_body["types"] if t["name"] == "Hotel"]
    assert len(a_hotels) == 1 and a_hotels[0]["description"] == "acme-secret-hotel"
    assert len(b_hotels) == 1 and b_hotels[0]["description"] == "globex-secret-hotel"

    # Secret strings must not cross the tenant boundary in either direction.
    a_dump = str(a_body)
    b_dump = str(b_body)
    assert "globex-secret-hotel" not in a_dump
    assert "globexSuiteCode" not in a_dump
    assert "acme-secret-hotel" not in b_dump
    assert "acmeRoomCount" not in b_dump

    # Public BaseHotel is visible to both (layered C+A).
    assert any(t["name"] == "BaseHotel" for t in a_body["types"])
    assert any(t["name"] == "BaseHotel" for t in b_body["types"])


def test_workspace_route_rejects_wrong_path_tenant_via_context():
    """The path tenant is the auth context: tenant A context on tenant B path
    still returns A's data only because get_tenant supplies the context.
    (Isolation is by named-graph LayerStack, not by trusting the path alone.)
    """
    neptune = _seeded_private_hotels()
    # Wire the app with tenant A context regardless of path.
    app = FastAPI()
    app.include_router(ontology_routes.router)
    app.dependency_overrides[get_neptune_client] = lambda: neptune
    app.dependency_overrides[api_keys.get_tenant] = (
        lambda tenant=None, api_key=None, request=None: _tenant_ctx(TENANT_A)
    )
    client = TestClient(app)
    # Even if a caller crafts the B path, the overridden context is A — so the
    # payload is A's. Production get_tenant enforces path==claims; this pin
    # is that the reader uses the *context* tenant_id, never peeks at B's graph.
    r = client.get(f"/graphs/{TENANT_B}/ontology")
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == TENANT_A
    assert "globex-secret-hotel" not in str(body)
    assert any(
        t["name"] == "Hotel" and t["description"] == "acme-secret-hotel"
        for t in body["types"]
    )


@pytest.mark.asyncio
async def test_fetch_ontology_isolation_under_graph_union():
    """Even with both tenants' triples in one FakeNeptune, LayerStack only
    queries the caller's tenant graph URI — B never absorbs A's Hotel."""
    neptune = _seeded_private_hotels()
    body_b = await fetch_ontology(
        neptune,
        layers=LayerStack(GRAPH_B, entitled=False).layer_pairs(),
        entitled=False,
        tenant_id=TENANT_B,
        apply_shadowing=True,
    )
    hotels = [t for t in body_b.types if t.name == "Hotel"]
    assert len(hotels) == 1
    assert hotels[0].description == "globex-secret-hotel"
    assert hotels[0].layer == "tenant"
    assert not any(a.name == "acmeRoomCount" for a in hotels[0].attributes)


# ---------------------------------------------------------------------------
# Tenant source overlay isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_custom_sources_isolated_and_visible_to_owner():
    neptune = _seeded_private_hotels()
    # Seed per-tenant custom sources that cover "Hotel" by entity_kinds.
    set_tenant_custom_specs(TENANT_A, [_spec("acme_pms", ["hotel", "lodging"])])
    set_tenant_custom_specs(TENANT_B, [_spec("globex_crs", ["hotel", "property"])])

    from infona_client.api_registry.catalog import get_api_source_catalog

    body_a = await fetch_ontology(
        neptune,
        layers=LayerStack(GRAPH_A, entitled=False).layer_pairs(),
        catalog=get_api_source_catalog(TENANT_A),
        entitled=False,
        tenant_id=TENANT_A,
        apply_shadowing=True,
    )
    body_b = await fetch_ontology(
        neptune,
        layers=LayerStack(GRAPH_B, entitled=False).layer_pairs(),
        catalog=get_api_source_catalog(TENANT_B),
        entitled=False,
        tenant_id=TENANT_B,
        apply_shadowing=True,
    )

    a_hotel = next(t for t in body_a.types if t.name == "Hotel")
    b_hotel = next(t for t in body_b.types if t.name == "Hotel")
    a_slugs = {s.slug for s in a_hotel.sources}
    b_slugs = {s.slug for s in b_hotel.sources}
    assert "acme_pms" in a_slugs
    assert "globex_crs" not in a_slugs
    assert "globex_crs" in b_slugs
    assert "acme_pms" not in b_slugs
    # registry_layer is the catalog axis — tenant_custom for private entries.
    a_src = next(s for s in a_hotel.sources if s.slug == "acme_pms")
    assert a_src.registry_layer == LAYER_TENANT_CUSTOM


# ---------------------------------------------------------------------------
# Tenant skill overlay isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_skills_isolated_and_visible_to_owner():
    neptune = _seeded_private_hotels()
    store = InMemoryTypeSkillStore()
    # Pin the process-wide store so fetch_ontology's make_type_skill_store hits it.
    import infona_client.skills.store as skill_store_mod

    skill_store_mod._store = store  # type: ignore[attr-defined]
    await store.upsert(
        TypeSkill(
            slug="check-in-policy",
            type_name="Hotel",
            body="At Acme, a Hotel is always a franchise location.",
            title="Acme check-in",
            summary="Franchise only.",
            layer=Layer.TENANT,
            tenant_id=TENANT_A,
        )
    )
    await store.upsert(
        TypeSkill(
            slug="check-in-policy",
            type_name="Hotel",
            body="At Globex, a Hotel is a corporate-owned property.",
            title="Globex check-in",
            summary="Corporate only.",
            layer=Layer.TENANT,
            tenant_id=TENANT_B,
        )
    )

    body_a = await fetch_ontology(
        neptune,
        layers=LayerStack(GRAPH_A, entitled=False).layer_pairs(),
        entitled=False,
        tenant_id=TENANT_A,
        apply_shadowing=True,
    )
    body_b = await fetch_ontology(
        neptune,
        layers=LayerStack(GRAPH_B, entitled=False).layer_pairs(),
        entitled=False,
        tenant_id=TENANT_B,
        apply_shadowing=True,
    )

    a_hotel = next(t for t in body_a.types if t.name == "Hotel")
    b_hotel = next(t for t in body_b.types if t.name == "Hotel")
    a_skills = {s.slug: s for s in a_hotel.skills}
    b_skills = {s.slug: s for s in b_hotel.skills}
    assert "check-in-policy" in a_skills
    assert a_skills["check-in-policy"].layer == "tenant"
    assert a_skills["check-in-policy"].title == "Acme check-in"
    assert "Franchise" in (a_skills["check-in-policy"].summary or "")
    assert "Corporate" not in str(a_hotel.skills)

    assert "check-in-policy" in b_skills
    assert b_skills["check-in-policy"].title == "Globex check-in"
    assert "Franchise" not in str(b_hotel.skills)


# ---------------------------------------------------------------------------
# Operator route stays independent + require_operator router-wide
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_fetch_global_has_no_tenant_fields_or_custom():
    """Operator payload must stay free of workspace private overlays."""
    neptune = _seeded_private_hotels()
    set_tenant_custom_specs(TENANT_A, [_spec("acme_pms", ["hotel"])])
    import infona_client.skills.store as skill_store_mod
    from infona_client.skills.store import InMemoryTypeSkillStore

    store = InMemoryTypeSkillStore()
    skill_store_mod._store = store  # type: ignore[attr-defined]
    await store.upsert(
        TypeSkill(
            slug="secret",
            type_name="BaseHotel",
            body="tenant secret skill body",
            title="Secret",
            layer=Layer.TENANT,
            tenant_id=TENANT_A,
        )
    )

    body = await fetch_global_ontology(neptune)
    dump = body.model_dump()
    assert "tenant_id" not in dump
    assert "entitled" not in dump
    # No tenant-layer types (Hotel is tenant-only; BaseHotel is public).
    names = {t.name for t in body.types}
    assert "Hotel" not in names
    assert "BaseHotel" in names
    # No tenant_custom source, no tenant skill.
    for t in body.types:
        assert all(s.registry_layer != LAYER_TENANT_CUSTOM for s in t.sources)
        assert all(s.layer != "tenant" for s in t.skills)
        assert "acme_pms" not in {s.slug for s in t.sources}
        assert "secret" not in {s.slug for s in t.skills}


def test_operator_route_require_operator_router_wide():
    """Every /operator route must carry the router-wide require_operator gate."""
    # The gate is declared as a router-level dependency — assert it is present
    # so a future edit that demotes it to per-route cannot land silently.
    dep_calls = {getattr(d.dependency, "__name__", str(d.dependency)) for d in operator_routes.router.dependencies}
    assert "require_operator" in dep_calls

    # And every route path is under /operator (no tenant ontology path sneaks in).
    for route in operator_routes.router.routes:
        path = getattr(route, "path", "")
        assert path.startswith("/operator"), path
        assert "/graphs/" not in path, path


def test_operator_http_route_unchanged_shape():
    """GET /operator/ontology/global still returns GlobalOntologyResponse shape."""
    neptune = _seeded_private_hotels()
    r = _operator_app(neptune).get("/operator/ontology/global")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"layers", "types"}
    assert "tenant_id" not in body
    assert "entitled" not in body
    # Layers are the two GLOBAL ones only.
    layer_names = {L["layer"] for L in body["layers"]}
    assert layer_names == {"public", "enhanced"}
    assert "tenant" not in layer_names


def test_workspace_route_returns_workspace_model_shape():
    neptune = _seeded_private_hotels()
    r = _ontology_app(neptune, TENANT_A).get(f"/graphs/{TENANT_A}/ontology")
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == TENANT_A
    assert "entitled" in body
    assert "layers" in body and "types" in body
    # Workspace layers include tenant.
    assert any(L["layer"] == "tenant" for L in body["layers"])
    # Winning layer for private Hotel is tenant.
    hotel = next(t for t in body["types"] if t["name"] == "Hotel")
    assert hotel["layer"] == "tenant"
