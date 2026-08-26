"""DELETE /graphs/{tenant}/functions/{function_name} — tenant-layer detach.

Hermetic. Synthetic names only. Store is the product path (Neo4j has no
SPARQL update); FakeNeptune is a no-op residual writer.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infona_client.auth.api_keys import TenantContext
from infona_client.functions.store import (
    StoredFunction,
    make_function_store,
    reset_function_store,
)

TENANT = "fn-del-t"
SYNTH_TYPE = "SynthWidget"
SYNTH_TYPE_B = "SynthGadget"
SYNTH_NAME = "synth_score"
ENDPOINT = "https://example.test/synth-score"

_BODY = {
    "name": SYNTH_NAME,
    "entity_type": SYNTH_TYPE,
    "endpoint_url": ENDPOINT,
    "description": "synthetic score",
}


@pytest.fixture(autouse=True)
def _reset_store():
    reset_function_store()
    yield
    reset_function_store()


def _client(*, is_operator: bool = False) -> TestClient:
    from infona_client.api.deps import get_neptune_client
    from infona_client.api.routes import functions as functions_routes
    from infona_client.auth import api_keys

    class FakeNeptune:
        async def update(self, sparql: str):
            return None

        async def query(self, sparql: str):
            return {"head": {"vars": []}, "results": {"bindings": []}}

    app = FastAPI()
    app.include_router(functions_routes.router)
    app.dependency_overrides[get_neptune_client] = lambda: FakeNeptune()
    app.dependency_overrides[api_keys.get_tenant] = lambda: TenantContext(
        tenant_id=TENANT, api_key="k", is_operator=is_operator
    )
    return TestClient(app)


def _names(rows: list[dict]) -> set[tuple[str, str]]:
    return {(r["entity_type"], r["name"]) for r in rows}


def test_delete_function_roundtrip():
    client = _client()
    created = client.post(f"/graphs/{TENANT}/functions", json=_BODY)
    assert created.status_code == 201, created.text

    listed = client.get(f"/graphs/{TENANT}/functions")
    assert listed.status_code == 200, listed.text
    assert (SYNTH_TYPE, SYNTH_NAME) in _names(listed.json())

    deleted = client.delete(
        f"/graphs/{TENANT}/functions/{SYNTH_NAME}",
        params={"entity_type": SYNTH_TYPE},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": SYNTH_NAME, "entity_type": SYNTH_TYPE}

    after = client.get(f"/graphs/{TENANT}/functions")
    assert after.status_code == 200, after.text
    assert (SYNTH_TYPE, SYNTH_NAME) not in _names(after.json())


def test_delete_function_missing_is_404():
    client = _client()
    resp = client.delete(
        f"/graphs/{TENANT}/functions/{SYNTH_NAME}",
        params={"entity_type": SYNTH_TYPE},
    )
    assert resp.status_code == 404, resp.text
    assert SYNTH_NAME in resp.json()["detail"]


def test_delete_function_requires_entity_type():
    client = _client()
    resp = client.delete(f"/graphs/{TENANT}/functions/{SYNTH_NAME}")
    assert resp.status_code == 422, resp.text


def test_delete_function_same_name_on_other_type_is_untouched():
    client = _client()
    assert client.post(f"/graphs/{TENANT}/functions", json=_BODY).status_code == 201
    other = {**_BODY, "entity_type": SYNTH_TYPE_B}
    assert client.post(f"/graphs/{TENANT}/functions", json=other).status_code == 201

    deleted = client.delete(
        f"/graphs/{TENANT}/functions/{SYNTH_NAME}",
        params={"entity_type": SYNTH_TYPE},
    )
    assert deleted.status_code == 200, deleted.text

    listed = client.get(f"/graphs/{TENANT}/functions").json()
    keys = _names(listed)
    assert (SYNTH_TYPE, SYNTH_NAME) not in keys
    assert (SYNTH_TYPE_B, SYNTH_NAME) in keys


def test_delete_function_refuses_enhanced_attachment():
    client = _client(is_operator=True)
    created = client.post(
        f"/graphs/{TENANT}/functions",
        json={**_BODY, "layer": "enhanced"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["layer"] == "enhanced"

    resp = client.delete(
        f"/graphs/{TENANT}/functions/{SYNTH_NAME}",
        params={"entity_type": SYNTH_TYPE},
    )
    assert resp.status_code == 403, resp.text
    assert "enhanced" in resp.json()["detail"].lower()

    # Still in the store (tenant list may surface it; detach must not).
    store_rows = asyncio.run(
        make_function_store().list_for_tenant(TENANT, entity_type=SYNTH_TYPE)
    )
    assert any(r.name == SYNTH_NAME and r.layer == "enhanced" for r in store_rows)


def test_delete_function_refuses_enhanced_entity_type():
    client = _client()
    resp = client.delete(
        f"/graphs/{TENANT}/functions/{SYNTH_NAME}",
        params={"entity_type": f"x/{SYNTH_TYPE}"},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_store_delete_is_keyed_by_tenant_type_name():
    store = make_function_store()
    rec = StoredFunction(
        tenant_id=TENANT,
        name=SYNTH_NAME,
        entity_type=SYNTH_TYPE,
        endpoint_url=ENDPOINT,
    )
    await store.upsert(rec)
    assert await store.delete(TENANT, SYNTH_TYPE, SYNTH_NAME) is True
    assert await store.list_for_tenant(TENANT) == []
    assert await store.delete(TENANT, SYNTH_TYPE, SYNTH_NAME) is False
