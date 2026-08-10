"""ONTA-409 — workspace-wide Active type counts via KgStats union.

Acceptance:
  * Union sums the same type across KGs
  * Types with zero / missing counts are omitted
  * Peer-tenant rows never leak
  * Empty store → 200 with types: []
  * Pure ``union_type_breakdowns`` is deterministic and skips non-positive
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infona_client.api.deps import get_neptune_client
from infona_client.api.routes import ontology as ontology_routes
from infona_client.auth import api_keys
from infona_client.auth.api_keys import TenantContext
from infona_client.graph.kg_stats_store import (
    KgStats,
    get_kg_stats_store,
    reset_kg_stats_store,
    union_type_breakdowns,
)

TENANT_A = "acme"
TENANT_B = "globex"


@pytest.fixture(autouse=True)
def _fresh_store():
    reset_kg_stats_store()
    yield
    reset_kg_stats_store()


def _tenant_ctx(tenant_id: str) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, api_key="k")


def _app(tenant_id: str) -> TestClient:
    app = FastAPI()
    app.include_router(ontology_routes.router)
    # Endpoint does not touch Neptune, but keep the override for parity with
    # the rest of the ontology test suite.
    app.dependency_overrides[get_neptune_client] = lambda: object()
    app.dependency_overrides[api_keys.get_tenant] = (
        lambda tenant=None, api_key=None, request=None: _tenant_ctx(tenant_id)
    )
    return TestClient(app)


# ── pure union helper ───────────────────────────────────────────────────────


def test_union_sums_across_kgs_and_omits_zeros():
    rows = [
        KgStats(
            tenant_id=TENANT_A,
            kg_name="hotels",
            type_breakdown={"Hotel": 10, "Room": 0, "Amenity": 3},
        ),
        KgStats(
            tenant_id=TENANT_A,
            kg_name="events",
            type_breakdown={"Hotel": 2, "Event": 5},
        ),
    ]
    out = union_type_breakdowns(rows)
    by_name = {name: (total, by_kg) for name, total, by_kg in out}
    assert by_name["Hotel"] == (12, {"hotels": 10, "events": 2})
    assert by_name["Amenity"] == (3, {"hotels": 3})
    assert by_name["Event"] == (5, {"events": 5})
    assert "Room" not in by_name  # zero omitted
    # Sorted by total desc, then name.
    assert [n for n, _, _ in out] == ["Hotel", "Event", "Amenity"]


def test_union_skips_empty_names_and_non_positive():
    # KgStats validates type_breakdown values as int, so non-numeric junk never
    # reaches the union helper through the store. Still pin empty-name + ≤0.
    rows = [
        KgStats(
            tenant_id=TENANT_A,
            kg_name="k",
            type_breakdown={"": 5, "Ok": 3, "Neg": -1, "Zero": 0},
        ),
    ]
    out = union_type_breakdowns(rows)
    assert out == [("Ok", 3, {"k": 3})]


def test_union_tolerates_raw_non_int_via_duck_type():
    """Defense in depth: a non-KgStats row with a bad value is skipped, not raised."""
    class _Row:
        kg_name = "k"
        type_breakdown = {"Ok": 2, "Bad": "x", "Also": None}

    out = union_type_breakdowns([_Row()])  # type: ignore[list-item]
    assert out == [("Ok", 2, {"k": 2})]


def test_union_empty_rows():
    assert union_type_breakdowns([]) == []


# ── HTTP route ──────────────────────────────────────────────────────────────


async def _seed(tenant_id: str, kg: str, breakdown: dict[str, int]) -> None:
    await get_kg_stats_store().upsert(
        KgStats(
            tenant_id=tenant_id,
            kg_name=kg,
            entity_count=sum(breakdown.values()),
            type_breakdown=breakdown,
        )
    )


@pytest.mark.asyncio
async def test_workspace_type_counts_multi_kg_union():
    await _seed(TENANT_A, "hotels", {"Hotel": 10, "Room": 4})
    await _seed(TENANT_A, "events", {"Hotel": 2, "Event": 7})

    res = _app(TENANT_A).get(f"/graphs/{TENANT_A}/ontology/type-counts")
    assert res.status_code == 200
    body = res.json()
    assert body["tenant_id"] == TENANT_A
    assert set(body["kg_names"]) == {"hotels", "events"}

    by_name = {t["name"]: t for t in body["types"]}
    assert by_name["Hotel"]["entity_count"] == 12
    assert by_name["Hotel"]["by_kg"] == {"hotels": 10, "events": 2}
    assert by_name["Room"]["entity_count"] == 4
    assert by_name["Event"]["entity_count"] == 7
    # Highest total first.
    assert body["types"][0]["name"] == "Hotel"


@pytest.mark.asyncio
async def test_workspace_type_counts_empty_store():
    res = _app(TENANT_A).get(f"/graphs/{TENANT_A}/ontology/type-counts")
    assert res.status_code == 200
    body = res.json()
    assert body["tenant_id"] == TENANT_A
    assert body["types"] == []
    assert body["kg_names"] == []


@pytest.mark.asyncio
async def test_workspace_type_counts_tenant_isolation():
    await _seed(TENANT_A, "a-kg", {"SecretType": 99, "Hotel": 1})
    await _seed(TENANT_B, "b-kg", {"PeerOnly": 5})

    a = _app(TENANT_A).get(f"/graphs/{TENANT_A}/ontology/type-counts").json()
    b = _app(TENANT_B).get(f"/graphs/{TENANT_B}/ontology/type-counts").json()

    a_names = {t["name"] for t in a["types"]}
    b_names = {t["name"] for t in b["types"]}
    assert a_names == {"SecretType", "Hotel"}
    assert b_names == {"PeerOnly"}
    assert "PeerOnly" not in a_names
    assert "SecretType" not in b_names
    assert a["kg_names"] == ["a-kg"]
    assert b["kg_names"] == ["b-kg"]
