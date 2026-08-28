"""Blueprint overlay lives on the tenant-confined GraphStore."""

from __future__ import annotations

import json

import pytest

from infona_client.blueprint.overlay import (
    GraphStoreBlueprintOverlayStore,
    OverlayConflict,
    OverlayDocument,
    StoredOverlay,
    make_blueprint_overlay_store,
    reset_blueprint_overlay_store,
)
from infona_client.graph.memory_store import MemoryGraphStore
from infona_client.graph.schema_bootstrap import SCHEMA_STATEMENTS
from infona_client.graph.store import configure_graph_store, get_graph_store


@pytest.fixture(autouse=True)
def _reset_overlay_wrapper():
    reset_blueprint_overlay_store()
    yield
    reset_blueprint_overlay_store()


def _row(
    tenant_id: str = "t1", blueprint_id: str = "infona/clinical-trials"
) -> StoredOverlay:
    return StoredOverlay(
        tenant_id=tenant_id,
        blueprint_id=blueprint_id,
        document=OverlayDocument.model_validate(
            {
                "concepts": [
                    {
                        "name": "ClinicalTrial",
                        "attributes": [
                            {
                                "name": "internal_priority",
                                "kind": "literal",
                                "datatype": "string",
                                "optional": True,
                            }
                        ],
                    }
                ],
                "sources": [{"id": "ctgov", "declared_cadence": "daily"}],
            }
        ),
        conflicts=[
            OverlayConflict(
                kind="removed_extended_type",
                path="concepts.Organization",
                message="upstream removed Organization",
            )
        ],
        updated_at="2026-08-28T00:00:00+00:00",
        base_version="0.1.0",
        base_content_hash="abc" * 16 + "ab",
    )


@pytest.mark.asyncio
async def test_graph_store_overlay_survives_wrapper_reload():
    store = get_graph_store()
    assert isinstance(store, MemoryGraphStore)
    wrapper = make_blueprint_overlay_store()
    await wrapper.put(_row())
    reset_blueprint_overlay_store()
    reloaded = make_blueprint_overlay_store()
    assert reloaded is not wrapper
    got = await reloaded.get("t1", "infona/clinical-trials")
    assert got is not None
    assert got.base_version == "0.1.0"
    assert got.document.sources[0].declared_cadence == "daily"
    assert got.conflicts[0].kind == "removed_extended_type"
    raw = await store.blueprint_overlay_get("t1", "infona/clinical-trials")
    assert raw is not None
    assert raw["base_version"] == "0.1.0"
    assert await reloaded.get("peer", "infona/clinical-trials") is None
    assert await reloaded.delete("t1", "infona/clinical-trials") is True
    assert await reloaded.get("t1", "infona/clinical-trials") is None


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
            return []

    fake = _RunOnlyStore()
    configure_graph_store(fake)
    first = GraphStoreBlueprintOverlayStore()
    await first.put(_row())
    assert (
        json.loads(fake.rows[("t1", "infona/clinical-trials")])["base_version"]
        == "0.1.0"
    )

    second = GraphStoreBlueprintOverlayStore()
    got = await second.get("t1", "infona/clinical-trials")
    assert got is not None
    assert got.document.concepts[0].name == "ClinicalTrial"
    assert await second.get("peer", "infona/clinical-trials") is None
    assert await second.delete("t1", "infona/clinical-trials") is True
    assert await second.get("t1", "infona/clinical-trials") is None


def test_bootstrap_uniqueness_covers_the_overlay():
    names = {n for n, _ in SCHEMA_STATEMENTS}
    assert "blueprint_overlay_tenant_id_unique" in names
    body = "\n".join(
        c for n, c in SCHEMA_STATEMENTS if n == "blueprint_overlay_tenant_id_unique"
    )
    assert ":BlueprintOverlay" in body
    assert "tenant_id" in body and "blueprint_id" in body
    assert "IF NOT EXISTS" in body
