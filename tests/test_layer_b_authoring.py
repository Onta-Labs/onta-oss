"""ONTA-399: Layer B (Enhanced) authoring — functions + durable skills.

Acceptance:

* A function can be attached to ``types/x/<Type>`` and appears in the Enhanced
  graph read path.
* A skill authored for layer B survives a process-restart simulation.
* Public refuses skills/functions.
* Non-entitled callers do not see Enhanced content when the read path uses
  entitlement (``is_entitled`` / ``LayerStack``).

Sources: fuzzy match remains the advisory fallback for A; no ``coversType``
redesign in this ticket (documented in the PR / deviations).
"""

from __future__ import annotations

import asyncio

import pytest

from infona_client.auth.api_keys import TenantContext
from infona_client.graph.entitlement import (
    is_entitled,
    register_entitlement_checker,
)
from infona_client.graph.layer_content import LayerContentError
from infona_client.graph.layers import (
    Layer,
    LayerStack,
    enhanced_graph_uri,
    layer_type_uri,
    public_graph_uri,
)
from infona_client.graph.queries import (
    list_functions_query,
    register_function_triple,
    resolve_function_attachment,
    tenant_graph_uri,
)
from infona_client.skills import (
    InMemoryGlobalTypeSkillStore,
    TypeSkill,
    global_skills_for_type,
    hydrate_global_skills_from_store,
    make_global_type_skill_store,
    merge_layers,
    register_skill_layer,
    reset_global_type_skill_store,
    reset_skill_layers,
    resolve_skills,
)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_skill_layers()
    reset_global_type_skill_store()
    register_entitlement_checker(None)
    yield
    reset_skill_layers()
    reset_global_type_skill_store()
    register_entitlement_checker(None)


def _enh_skill(**kw) -> TypeSkill:
    return TypeSkill(
        slug=kw.pop("slug", "entity-identity"),
        type_name=kw.pop("type_name", "Organization"),
        body=kw.pop(
            "body",
            "An Organization in Enhanced guidance is identified by legal name "
            "plus jurisdiction; never merge two orgs that only share a brand.",
        ),
        layer=Layer.ENHANCED,
        tenant_id=None,
        title=kw.pop("title", "Entity identity"),
        summary=kw.pop("summary", "How to identify an Organization"),
        **kw,
    )


# --------------------------------------------------------------------------- #
# Function attachment identity
# --------------------------------------------------------------------------- #
def test_resolve_function_attachment_bare_name_is_tenant():
    layer, uri = resolve_function_attachment("Organization")
    assert layer is Layer.TENANT
    assert uri == layer_type_uri(Layer.TENANT, "Organization")


def test_resolve_function_attachment_enhanced_path_and_uri():
    layer, uri = resolve_function_attachment("x/Organization")
    assert layer is Layer.ENHANCED
    assert uri == layer_type_uri(Layer.ENHANCED, "Organization")

    full = layer_type_uri(Layer.ENHANCED, "Organization")
    layer2, uri2 = resolve_function_attachment(full)
    assert layer2 is Layer.ENHANCED
    assert uri2 == full


def test_resolve_function_attachment_public_path_detected():
    layer, uri = resolve_function_attachment("public/Person")
    assert layer is Layer.PUBLIC
    assert uri == layer_type_uri(Layer.PUBLIC, "Person")


def test_register_function_triple_enhanced_round_trip_shape():
    """Write → SPARQL contains layer-qualified type + Enhanced graph."""
    sparql = register_function_triple(
        tenant_graph_uri("t1"),
        entity_type="Organization",
        function_name="lookup_lei",
        endpoint_url="https://api.example.com/lei",
        description="Resolve LEI for an Organization",
        layer=Layer.ENHANCED,
    )
    type_uri = layer_type_uri(Layer.ENHANCED, "Organization")
    assert f"<{type_uri}>" in sparql
    assert f"GRAPH <{enhanced_graph_uri()}>" in sparql
    assert "lookup_lei" in sparql
    assert "https://api.example.com/lei" in sparql
    # Must not attach to bare tenant namespace.
    assert f"<{layer_type_uri(Layer.TENANT, 'Organization')}>" not in sparql


def test_register_function_triple_refuses_public():
    with pytest.raises(LayerContentError, match="may not carry functions"):
        register_function_triple(
            public_graph_uri(),
            entity_type="public/Person",
            function_name="nope",
            endpoint_url="https://fn/x",
        )
    with pytest.raises(LayerContentError, match="may not carry functions"):
        register_function_triple(
            public_graph_uri(),
            entity_type="Person",
            function_name="nope",
            endpoint_url="https://fn/x",
            layer=Layer.PUBLIC,
        )


def test_enhanced_function_surfaces_in_layer_graph_read():
    """Simulate write into Enhanced graph + read via full_ontology folding."""
    from infona_client.graph.global_ontology import _TypeAccumulator

    # Build the INSERT — attachment identity must be layer-qualified.
    type_uri = layer_type_uri(Layer.ENHANCED, "Organization")
    sparql = register_function_triple(
        enhanced_graph_uri(),
        entity_type="Organization",
        function_name="lookup_lei",
        endpoint_url="https://api.example.com/lei",
        description="LEI lookup",
        layer=Layer.ENHANCED,
    )
    assert type_uri in sparql
    assert f"GRAPH <{enhanced_graph_uri()}>" in sparql

    # Fold the way the browser reader does for a function-bearing Enhanced type.
    acc = _TypeAccumulator("Organization", Layer.ENHANCED.value)
    acc.absorb(
        {
            "funcName": "lookup_lei",
            "funcDesc": "LEI lookup",
            "funcEndpoint": "https://api.example.com/lei",
        }
    )
    built = acc.build(subtypes=[])
    assert len(built.functions) == 1
    assert built.functions[0].name == "lookup_lei"
    assert built.functions[0].layer == Layer.ENHANCED.value
    assert built.functions[0].entity_type == "Organization"


def test_list_functions_query_filters_enhanced_type_uri():
    sparql = list_functions_query(
        enhanced_graph_uri(),
        entity_type="x/Organization",
    )
    assert layer_type_uri(Layer.ENHANCED, "Organization") in sparql


# --------------------------------------------------------------------------- #
# Durable Enhanced skills
# --------------------------------------------------------------------------- #
def test_global_store_round_trip_and_version_bump():
    store = InMemoryGlobalTypeSkillStore()

    async def go():
        first = await store.upsert(_enh_skill(body="v1"))
        assert first.version == 1
        assert first.layer is Layer.ENHANCED
        assert first.tenant_id is None
        second = await store.upsert(_enh_skill(body="v2"))
        assert second.version == 2
        assert second.body == "v2"
        got = await store.get(Layer.ENHANCED, "Organization", "entity-identity")
        assert got is not None and got.body == "v2"
        rows = await store.list_for_layer(Layer.ENHANCED, "organization")
        assert len(rows) == 1
        assert await store.delete(Layer.ENHANCED, "Organization", "entity-identity")
        assert await store.get(Layer.ENHANCED, "Organization", "entity-identity") is None

    asyncio.run(go())


def test_global_store_refuses_public_and_tenant():
    store = InMemoryGlobalTypeSkillStore()

    async def go():
        with pytest.raises(LayerContentError, match="may not carry skills"):
            await store.upsert(
                TypeSkill(
                    slug="nope",
                    type_name="Person",
                    body="should not land on Public",
                    layer=Layer.PUBLIC,
                    tenant_id=None,
                )
            )
        with pytest.raises(ValueError, match="only accepts Layer.ENHANCED"):
            await store.upsert(
                TypeSkill(
                    slug="nope",
                    type_name="Person",
                    body="tenant skill in global store",
                    layer=Layer.TENANT,
                    tenant_id="t1",
                )
            )

    asyncio.run(go())


def test_durable_skill_survives_restart_simulation():
    """Write → clear process registry/mirror → hydrate from store → still readable.

    This is the ONTA-399 acceptance: authored Enhanced skills survive restart
    without re-reading from the image.
    """
    store = InMemoryGlobalTypeSkillStore()

    async def go():
        await store.upsert(
            _enh_skill(
                slug="entity-identity",
                type_name="Organization",
                body="Durable Enhanced guidance about Organization identity.",
            )
        )
        # Visible via the sync operator read (write-through mirror).
        assert any(
            s.slug == "entity-identity"
            for s in global_skills_for_type("Organization", layer=Layer.ENHANCED)
        )

        # Simulate process restart: drop registry + mirror, keep store rows.
        reset_skill_layers()
        from infona_client.skills.global_store import reset_durable_skills_mirror

        reset_durable_skills_mirror()
        assert global_skills_for_type("Organization") == []

        n = await hydrate_global_skills_from_store(store)
        assert n == 1
        got = global_skills_for_type("Organization", layer=Layer.ENHANCED)
        assert len(got) == 1
        assert "Durable Enhanced guidance" in got[0].body

    asyncio.run(go())


def test_durable_skill_shadows_process_registry_on_same_slug():
    """Authored durable content wins over a stale file-seeded registration."""
    store = InMemoryGlobalTypeSkillStore()

    async def go():
        register_skill_layer(
            Layer.ENHANCED,
            [
                TypeSkill(
                    slug="entity-identity",
                    type_name="Organization",
                    body="FILE SEED",
                    layer=Layer.ENHANCED,
                    tenant_id=None,
                )
            ],
        )
        await store.upsert(_enh_skill(body="DURABLE AUTHORING"))
        got = global_skills_for_type("Organization", layer=Layer.ENHANCED)
        assert len(got) == 1
        assert got[0].body == "DURABLE AUTHORING"

    asyncio.run(go())


def test_store_selection_follows_dsn(monkeypatch):
    from infona_client.config import settings
    from infona_client.skills.global_store import PostgresGlobalTypeSkillStore

    reset_global_type_skill_store()
    monkeypatch.setattr(settings, "database_url", None, raising=False)
    assert isinstance(make_global_type_skill_store(), InMemoryGlobalTypeSkillStore)

    reset_global_type_skill_store()
    monkeypatch.setattr(
        settings, "database_url", "postgresql://u:p@h/db", raising=False
    )
    store = make_global_type_skill_store()
    assert isinstance(store, PostgresGlobalTypeSkillStore)
    assert store._pool is None


# --------------------------------------------------------------------------- #
# Entitlement: non-entitled cannot see Enhanced
# --------------------------------------------------------------------------- #
def test_non_entitled_cannot_see_enhanced_skills_via_resolve():
    store = InMemoryGlobalTypeSkillStore()

    async def go():
        await store.upsert(_enh_skill())
        # entitled=False: Enhanced excluded by LayerStack.
        free = await resolve_skills(
            "Organization",
            tenant_id="free-tenant",
            entitled=False,
            store=None,
        )
        assert free == []

        paid = await resolve_skills(
            "Organization",
            tenant_id="paid-tenant",
            entitled=True,
            store=None,
        )
        assert [s.slug for s in paid] == ["entity-identity"]

    asyncio.run(go())


def test_is_entitled_gates_layer_stack_for_enhanced_content():
    """Read path using is_entitled / LayerStack hides Enhanced for free tenants."""
    register_entitlement_checker(lambda t: t.tenant_id == "paid")
    free = TenantContext(tenant_id="free", api_key="k")
    paid = TenantContext(tenant_id="paid", api_key="k")
    assert is_entitled(free) is False
    assert is_entitled(paid) is True

    stack_free = LayerStack(
        tenant_graph_uri=tenant_graph_uri("free"),
        entitled=is_entitled(free),
    )
    stack_paid = LayerStack(
        tenant_graph_uri=tenant_graph_uri("paid"),
        entitled=is_entitled(paid),
    )
    assert Layer.ENHANCED not in stack_free.layers
    assert Layer.ENHANCED in stack_paid.layers

    by_layer = {
        Layer.ENHANCED: [_enh_skill()],
        Layer.TENANT: [],
    }
    assert merge_layers(by_layer, stack_free, type_name="Organization") == []
    assert [
        s.slug for s in merge_layers(by_layer, stack_paid, type_name="Organization")
    ] == ["entity-identity"]


def test_enhanced_function_graph_is_not_in_non_entitled_stack():
    stack = LayerStack(
        tenant_graph_uri=tenant_graph_uri("free"),
        entitled=False,
    )
    assert enhanced_graph_uri() not in stack.visible_graph_uris()
    entitled_stack = LayerStack(
        tenant_graph_uri=tenant_graph_uri("paid"),
        entitled=True,
    )
    assert enhanced_graph_uri() in entitled_stack.visible_graph_uris()


# --------------------------------------------------------------------------- #
# HTTP route: operator Enhanced vs tenant / public refuse
# --------------------------------------------------------------------------- #
def test_functions_route_tenant_register_and_list():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from infona_client.api.deps import get_neptune_client
    from infona_client.api.routes import functions as functions_routes
    from infona_client.auth import api_keys

    updates: list[str] = []
    queries: list[str] = []

    class FakeNeptune:
        async def update(self, sparql: str):
            updates.append(sparql)

        async def query(self, sparql: str):
            queries.append(sparql)
            return {
                "head": {"vars": ["name", "type", "endpoint", "desc"]},
                "results": {"bindings": []},
            }

    app = FastAPI()
    app.include_router(functions_routes.router)
    app.dependency_overrides[get_neptune_client] = lambda: FakeNeptune()
    app.dependency_overrides[api_keys.get_tenant] = lambda: TenantContext(
        tenant_id="t1", api_key="k", is_operator=False
    )
    client = TestClient(app)

    resp = client.post(
        "/graphs/t1/functions",
        json={
            "name": "calc",
            "entity_type": "Place",
            "endpoint_url": "https://fn/calc",
            "description": "d",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["layer"] == "tenant"
    assert "types/Place" in body["type_uri"]
    assert updates and "types/Place" in updates[0]

    # Non-operator cannot write Enhanced.
    resp2 = client.post(
        "/graphs/t1/functions",
        json={
            "name": "premium",
            "entity_type": "Organization",
            "endpoint_url": "https://fn/p",
            "layer": "enhanced",
        },
    )
    assert resp2.status_code == 403

    # Public refused.
    resp3 = client.post(
        "/graphs/t1/functions",
        json={
            "name": "nope",
            "entity_type": "public/Person",
            "endpoint_url": "https://fn/x",
        },
    )
    assert resp3.status_code == 422


def test_functions_route_operator_can_register_enhanced():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from infona_client.api.deps import get_neptune_client
    from infona_client.api.routes import functions as functions_routes
    from infona_client.auth import api_keys

    updates: list[str] = []

    class FakeNeptune:
        async def update(self, sparql: str):
            updates.append(sparql)

        async def query(self, sparql: str):  # pragma: no cover
            return {"head": {"vars": []}, "results": {"bindings": []}}

    app = FastAPI()
    app.include_router(functions_routes.router)
    app.dependency_overrides[get_neptune_client] = lambda: FakeNeptune()
    app.dependency_overrides[api_keys.get_tenant] = lambda: TenantContext(
        tenant_id="t1", api_key="k", is_operator=True
    )
    client = TestClient(app)

    resp = client.post(
        "/graphs/t1/functions",
        json={
            "name": "lookup_lei",
            "entity_type": "Organization",
            "endpoint_url": "https://fn/lei",
            "layer": "enhanced",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["layer"] == "enhanced"
    assert body["type_uri"] == layer_type_uri(Layer.ENHANCED, "Organization")
    assert body["graph_uri"] == enhanced_graph_uri()
    assert updates and enhanced_graph_uri() in updates[0]
    assert layer_type_uri(Layer.ENHANCED, "Organization") in updates[0]


# --------------------------------------------------------------------------- #
# Seed fixture content is loadable
# --------------------------------------------------------------------------- #
def test_minimal_enhanced_skill_seed_fixture():
    """The demonstrable layer-B skill body used by premium seed / tests."""
    skill = _enh_skill()
    assert skill.layer is Layer.ENHANCED
    assert skill.type_uri == layer_type_uri(Layer.ENHANCED, "Organization")
    assert "legal name" in skill.body or "jurisdiction" in skill.body or "Organization" in skill.body
