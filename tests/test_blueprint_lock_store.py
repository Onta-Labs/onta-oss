"""Blueprint install lock lives on the tenant-confined GraphStore."""

from __future__ import annotations

import json

import pytest

from infona_client.blueprint.lock import (
    BlueprintLock,
    GraphStoreBlueprintLockStore,
    make_blueprint_lock_store,
    reset_blueprint_lock_store,
)
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.schema_bootstrap import SCHEMA_STATEMENTS
from infona_client.graph.store import configure_graph_store, get_graph_store


@pytest.fixture(autouse=True)
def _reset_lock_wrapper():
    reset_blueprint_lock_store()
    yield
    reset_blueprint_lock_store()


def _lock(tenant_id: str = "t1", blueprint_id: str = "infona/clinical-trials") -> BlueprintLock:
    return BlueprintLock(
        tenant_id=tenant_id,
        blueprint_id=blueprint_id,
        name="Clinical Trials",
        version="0.1.0",
        acquisition_revision=1,
        content_hash="abc" * 16 + "ab",
        kg="clinical-trials",
        installed_at="2026-08-28T00:00:00+00:00",
        sample_included=True,
        sample_captured_at="2026-06-01",
        sample_subjects=["https://graph.infona.ai/entities/ClinicalTrial/nct1"],
        created_types=["ClinicalTrial"],
        owned_types=["ClinicalTrial"],
        owned_attributes=[("ClinicalTrial", "nct_id")],
        owned_skills=[("ClinicalTrial", "cite-nct")],
    )


@pytest.mark.asyncio
async def test_graph_store_lock_survives_wrapper_reload():
    store = get_graph_store()
    assert isinstance(store, MemoryGraphStore)
    wrapper = make_blueprint_lock_store()
    await wrapper.put(_lock())
    reset_blueprint_lock_store()
    reloaded = make_blueprint_lock_store()
    assert reloaded is not wrapper
    got = await reloaded.get("t1", "infona/clinical-trials")
    assert got is not None
    assert got.version == "0.1.0"
    assert got.owned_skills == [("ClinicalTrial", "cite-nct")]
    raw = await store.blueprint_lock_get("t1", "infona/clinical-trials")
    assert raw is not None
    assert raw["kg"] == "clinical-trials"
    assert await reloaded.get("peer", "infona/clinical-trials") is None
    assert await reloaded.delete("t1", "infona/clinical-trials") is True
    assert await reloaded.get("t1", "infona/clinical-trials") is None


@pytest.mark.asyncio
async def test_neo4j_run_path_roundtrip_and_reload():
    """Neo4j has no native methods — store-level _run, same as kg_registry."""

    class _RunOnlyStore:
        def __init__(self) -> None:
            self.rows: dict[tuple[str, str], str] = {}

        async def _run(self, cypher, params, *, writing, database):
            tid = params["tenant_id"]
            bid = params.get("blueprint_id")
            if "DETACH DELETE" in cypher:
                existed = (tid, bid) in self.rows
                self.rows.pop((tid, bid), None)
                return [{"n": 1 if existed else 0}]
            if "MERGE" in cypher:
                self.rows[(tid, bid)] = params["payload"]
                return [{"payload": params["payload"]}]
            if bid:
                payload = self.rows.get((tid, bid))
                return [{"payload": payload}] if payload is not None else []
            return [
                {"payload": payload}
                for (t, _), payload in self.rows.items()
                if t == tid
            ]

    fake = _RunOnlyStore()
    configure_graph_store(fake)
    first = GraphStoreBlueprintLockStore()
    await first.put(_lock())
    assert json.loads(fake.rows[("t1", "infona/clinical-trials")])["version"] == "0.1.0"

    second = GraphStoreBlueprintLockStore()
    got = await second.get("t1", "infona/clinical-trials")
    assert got is not None
    assert got.sample_subjects == _lock().sample_subjects
    listed = await second.list_for_tenant("t1")
    assert [row.blueprint_id for row in listed] == ["infona/clinical-trials"]
    assert await second.list_for_tenant("peer") == []
    assert await second.delete("t1", "infona/clinical-trials") is True
    assert await second.get("t1", "infona/clinical-trials") is None


def test_bootstrap_uniqueness_covers_the_install_lock():
    names = {n for n, _ in SCHEMA_STATEMENTS}
    assert "blueprint_lock_tenant_id_unique" in names
    body = "\n".join(c for n, c in SCHEMA_STATEMENTS if n == "blueprint_lock_tenant_id_unique")
    assert ":BlueprintInstallLock" in body
    assert "tenant_id" in body and "blueprint_id" in body
    assert "IF NOT EXISTS" in body
