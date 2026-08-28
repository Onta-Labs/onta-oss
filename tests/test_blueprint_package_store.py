"""Blueprint package catalog lives on the tenant-confined GraphStore."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from infona_client.blueprint.catalog import (
    CatalogedPackage,
    GraphStoreBlueprintPackageStore,
    make_blueprint_package_store,
    reset_blueprint_package_store,
)
from infona_client.blueprint.models import parse_blueprint
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.schema_bootstrap import SCHEMA_STATEMENTS
from infona_client.graph.store import configure_graph_store, get_graph_store

MIN = (
    Path(__file__).resolve().parents[1]
    / "infona_client/blueprint/data/clinical_trials_min.json"
)


@pytest.fixture(autouse=True)
def _reset_package_wrapper():
    reset_blueprint_package_store()
    yield
    reset_blueprint_package_store()


def _row(
    tenant_id: str = "t1", origin: str = "fork", blueprint_id: str | None = None
) -> CatalogedPackage:
    data = json.loads(MIN.read_text(encoding="utf-8"))
    if blueprint_id is not None:
        data["id"] = blueprint_id
        data["namespace"] = blueprint_id.split("/", 1)[0]
    return CatalogedPackage(
        tenant_id=tenant_id,
        manifest=parse_blueprint(data),
        origin=origin,  # type: ignore[arg-type]
        stored_at="2026-08-28T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_graph_store_package_survives_wrapper_reload():
    store = get_graph_store()
    assert isinstance(store, MemoryGraphStore)
    wrapper = make_blueprint_package_store()
    await wrapper.put(_row(origin="fork", blueprint_id="acme/clinical-trials"))
    reset_blueprint_package_store()
    reloaded = make_blueprint_package_store()
    assert reloaded is not wrapper
    got = await reloaded.get("t1", "acme/clinical-trials")
    assert got is not None
    assert got.origin == "fork"
    assert got.manifest.id == "acme/clinical-trials"
    assert got.manifest.attribution
    raw = await store.blueprint_package_get("t1", "acme/clinical-trials")
    assert raw is not None
    assert raw["origin"] == "fork"
    assert await reloaded.get("peer", "acme/clinical-trials") is None
    listed = await reloaded.list_for_tenant("t1")
    assert [row.manifest.id for row in listed] == ["acme/clinical-trials"]
    assert await reloaded.list_for_tenant("peer") == []
    assert await reloaded.delete("t1", "acme/clinical-trials") is True
    assert await reloaded.get("t1", "acme/clinical-trials") is None


@pytest.mark.asyncio
async def test_neo4j_run_path_roundtrip_and_reload():
    """Neo4j has no native methods — store-level _run, same as the lock."""

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
    first = GraphStoreBlueprintPackageStore()
    await first.put(_row(origin="fork", blueprint_id="acme/clinical-trials"))
    assert (
        json.loads(fake.rows[("t1", "acme/clinical-trials")])["origin"] == "fork"
    )

    second = GraphStoreBlueprintPackageStore()
    got = await second.get("t1", "acme/clinical-trials")
    assert got is not None
    assert got.manifest.concepts[0].name == "ClinicalTrial"
    listed = await second.list_for_tenant("t1")
    assert [row.manifest.id for row in listed] == ["acme/clinical-trials"]
    assert await second.list_for_tenant("peer") == []
    assert await second.delete("t1", "acme/clinical-trials") is True
    assert await second.get("t1", "acme/clinical-trials") is None


def test_bootstrap_uniqueness_covers_the_package_catalog():
    names = {n for n, _ in SCHEMA_STATEMENTS}
    assert "blueprint_package_tenant_id_unique" in names
    body = "\n".join(
        c for n, c in SCHEMA_STATEMENTS if n == "blueprint_package_tenant_id_unique"
    )
    assert ":BlueprintPackage" in body
    assert "tenant_id" in body and "blueprint_id" in body
    assert "IF NOT EXISTS" in body
