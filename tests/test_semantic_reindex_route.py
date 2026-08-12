"""Route-level tests for ONTA-181's HTTP surface.

* ``POST /graphs/{tenant}/kgs/{kg}/search/reindex`` — the on-demand reconcile
  trigger (202 + schedule row; NOT an inline long-running request; 503 when the
  semantic index is disabled; same ``get_tenant`` auth as every KG route).
* ``DELETE /graphs/{tenant}/kgs/{kg}`` — clears ONLY that KG's semantic rows
  (the kg_name isolation contract) and drops its reconcile schedule row.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

import infona_client.graph.text_markers as tm
import infona_client.semantic.reconciler as rec
from infona_client.scheduling.store import get_schedule_store, reset_schedule_store
from infona_client.semantic.extract import content_hash
from infona_client.semantic.memory import InMemorySemanticIndex
from infona_client.semantic.protocol import SemanticChunk
from infona_client.semantic.registry import (
    register_semantic_index,
    reset_semantic_index,
)

TENANT = "test-tenant"  # conftest's static-key tenant


@pytest.fixture(autouse=True)
def _clean_state():
    reset_semantic_index()
    tm.reset_for_tests()
    rec.reset_for_tests()
    reset_schedule_store()
    yield
    reset_semantic_index()
    tm.reset_for_tests()
    rec.reset_for_tests()
    reset_schedule_store()


def _chunk(kg_name: str, n: int) -> SemanticChunk:
    text = f"chunk {n} of {kg_name}"
    return SemanticChunk(
        tenant_id=TENANT,
        kg_name=kg_name,
        entity_uri=f"https://graph.infona.ai/entities/Doc/{kg_name}-{n}",
        attr="description",
        chunk_ix=0,
        chunk_text=text,
        content_hash=content_hash(text),
    )


# --- reindex ------------------------------------------------------------------


def test_reindex_returns_202_and_seeds_due_now_schedule(
    monkeypatch, client, auth_headers
):
    monkeypatch.setenv("INFONA_SEMANTIC_INDEX_ENABLED", "true")
    register_semantic_index(InMemorySemanticIndex())

    resp = client.post(
        f"/graphs/{TENANT}/kgs/kg1/search/reindex", headers=auth_headers
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["kg_name"] == "kg1"
    assert body["schedule_id"] == rec.reconcile_schedule_id(TENANT, "kg1")
    # No runner in the test app (lifespan not entered) → in-process fallback.
    assert body["mode"] == "background-task"

    async def check():
        schedule = await get_schedule_store().get(body["schedule_id"])
        assert schedule is not None
        assert schedule.action == "semantic-reconcile"
        assert schedule.next_run <= datetime.now(timezone.utc)

    asyncio.run(check())


def test_reindex_reports_scheduled_mode_when_runner_present(
    monkeypatch, app, client, auth_headers
):
    monkeypatch.setenv("INFONA_SEMANTIC_INDEX_ENABLED", "true")
    app.state.schedule_runner = object()  # a live runner claims the row instead

    resp = client.post(
        f"/graphs/{TENANT}/kgs/kg1/search/reindex", headers=auth_headers
    )
    assert resp.status_code == 202
    assert resp.json()["mode"] == "scheduled"


def test_reindex_503_when_semantic_index_disabled(monkeypatch, client, auth_headers):
    monkeypatch.delenv("INFONA_SEMANTIC_INDEX_ENABLED", raising=False)
    resp = client.post(
        f"/graphs/{TENANT}/kgs/kg1/search/reindex", headers=auth_headers
    )
    assert resp.status_code == 503
    assert "INFONA_SEMANTIC_INDEX_ENABLED" in resp.json()["detail"]


def test_reindex_requires_auth(monkeypatch, client):
    monkeypatch.setenv("INFONA_SEMANTIC_INDEX_ENABLED", "true")
    resp = client.post(f"/graphs/{TENANT}/kgs/kg1/search/reindex")
    assert resp.status_code in (401, 403)


def test_reindex_static_key_foreign_tenant_is_403(monkeypatch, client, auth_headers):
    """A static key on a foreign path tenant is 403 — never schedules reconcile
    work under the key's tenant (or the path tenant) via silent reroute."""
    monkeypatch.setenv("INFONA_SEMANTIC_INDEX_ENABLED", "true")
    resp = client.post(
        "/graphs/other-tenant/kgs/kg1/search/reindex", headers=auth_headers
    )
    assert resp.status_code == 403
    assert "other-tenant" in resp.json()["detail"]

    async def check():
        store = get_schedule_store()
        assert (
            await store.get(rec.reconcile_schedule_id("other-tenant", "kg1")) is None
        )
        assert await store.get(rec.reconcile_schedule_id(TENANT, "kg1")) is None

    asyncio.run(check())


# --- KG delete isolation ---------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "LOST CAPABILITY (pre-dates ONTA-527, surfaced by it): deleting a KG "
        "no longer clears ANY of its derived state. "
        "api/routes/knowledge_graphs.py::delete_kg starts with `if "
        "neo4j_kg_registry_active():` — true whenever the backend is neo4j, "
        "i.e. always in production — which deletes the registry row, "
        "DETACH DELETEs the instance nodes, drops the kg_stats row, and "
        "RETURNS. Every cleanup below that early return is skipped: the "
        "semantic index clear (this test), the spatio-temporal index clear, "
        "the example-bank purge, the NL-planning cache eviction, the "
        "kg_status invalidation, and remove_reconcile_schedule. So a deleted "
        "KG keeps its semantic chunks (searchable, and re-served to any "
        "later query) and keeps a recurring semantic-reconcile schedule row "
        "pointed at a graph that no longer exists. The assertions below are "
        "left intact so this flips green as soon as the fan-out is restored."
    ),
    strict=True,
)
def test_kg_delete_clears_only_that_kgs_semantic_rows(client, auth_headers):
    """Deleting KG A clears A's chunks and reconcile schedule; KG B's rows and
    schedule are untouched — the whole reason the index carries kg_name."""
    index = InMemorySemanticIndex()
    register_semantic_index(index)

    async def seed():
        await index.upsert_chunks([_chunk("kga", 1), _chunk("kga", 2), _chunk("kgb", 1)])
        store = get_schedule_store()
        await rec.ensure_reconcile_schedule(store, TENANT, "kga")
        await rec.ensure_reconcile_schedule(store, TENANT, "kgb")

    asyncio.run(seed())

    resp = client.delete(f"/graphs/{TENANT}/kgs/kga", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "kga"}

    async def check():
        rows = await index.fetch_pending(limit=100)
        assert {r.kg_name for r in rows} == {"kgb"}
        store = get_schedule_store()
        assert await store.get(rec.reconcile_schedule_id(TENANT, "kga")) is None
        assert await store.get(rec.reconcile_schedule_id(TENANT, "kgb")) is not None

    asyncio.run(check())
