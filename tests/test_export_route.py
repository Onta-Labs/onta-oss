"""F10 export route — shape and wiring smoke (no live Neo4j required)."""

from __future__ import annotations

import csv
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from infona_client.api.deps import get_neptune_client
from infona_client.api.routes import export as export_mod
from infona_client.auth import api_keys
from infona_client.auth.api_keys import TenantContext


def _tenant() -> TenantContext:
    return TenantContext(tenant_id="t1", api_key="k")


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(export_mod.router)
    app.dependency_overrides[api_keys.get_tenant] = _tenant
    app.dependency_overrides[get_neptune_client] = lambda: MagicMock()
    return app


@pytest.mark.asyncio
async def test_export_json_single_type():
    fake_page = {
        "columns": ["name", "price"],
        "rows": [
            {"id": "e1", "name": "Book A", "price": "10"},
            {"id": "e2", "name": "Book B", "price": "12"},
        ],
        "total": 2,
        "next_cursor": None,
    }

    with patch.object(
        export_mod,
        "get_type_records",
        new=AsyncMock(return_value=fake_page),
    ):
        app = _app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get(
                "/graphs/t1/kgs/bookstore/export",
                params={"format": "json", "type": "Book"},
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["kg"] == "bookstore"
        assert body["row_count"] == 2
        assert body["types"][0]["type"] == "Book"
        assert len(body["types"][0]["rows"]) == 2


@pytest.mark.asyncio
async def test_export_csv_all_types_empty_kg():
    with patch.object(
        export_mod,
        "list_type_counts",
        new=AsyncMock(return_value=[]),
    ):
        app = _app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get(
                "/graphs/t1/kgs/empty/export",
                params={"format": "csv"},
            )
        assert res.status_code == 200, res.text
        assert "text/csv" in res.headers.get("content-type", "")
        reader = list(csv.reader(io.StringIO(res.text)))
        assert reader[0][0] == "type"


def test_rows_to_csv_flattens_types():
    blocks = [
        export_mod.ExportTypeBlock(
            type="Book",
            columns=["name", "price"],
            rows=[{"id": "1", "name": "A", "price": "9"}],
            total=1,
        ),
        export_mod.ExportTypeBlock(
            type="Author",
            columns=["name"],
            rows=[{"id": "2", "name": "Tolkien"}],
            total=1,
        ),
    ]
    text = export_mod._rows_to_csv(blocks)
    assert "Book" in text and "Author" in text
    assert "Tolkien" in text
