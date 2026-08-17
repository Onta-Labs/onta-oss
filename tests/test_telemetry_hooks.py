"""ONTA-548 — job-entrypoint hooks emit the allowlisted payload only."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from infona_client.api.deps import get_neptune_client
from infona_client.api.routes import export as export_mod
from infona_client.api.routes.ask import _emit_query_executed
from infona_client.auth import api_keys
from infona_client.auth.api_keys import TenantContext
from infona_client.enrichment.job_store import InMemoryJobStore
from infona_client.models.query import NLResult
from infona_client.resolver.er.rebuild import rebuild_kg
from infona_client.resolver.file_ingest_job import (
    fail_file_ingest_job,
    finish_file_ingest_job,
    open_file_ingest_job,
)
from infona_client.resolver.models import IngestResult
from infona_client.telemetry import (
    ALLOWED_PAYLOAD_KEYS,
    record_job,
    reset_telemetry,
    set_test_sink,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("INFONA_TELEMETRY_STATE", str(tmp_path / "telemetry.json"))
    monkeypatch.setenv("INFONA_TELEMETRY", "1")
    for key in (
        "INFONA_TELEMETRY_URL",
        "INFONA_TELEMETRY_SINK",
        "INFONA_TELEMETRY_USE_CASE",
        "INFONA_TELEMETRY_SYNC",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_telemetry()
    yield
    reset_telemetry()


def _capture(monkeypatch) -> list[dict]:
    events: list[dict] = []
    set_test_sink(events.append)
    return events


def _assert_clean(payload: dict) -> None:
    assert set(payload) <= ALLOWED_PAYLOAD_KEYS
    blob = json.dumps(payload)
    for token in (
        "tenant-acme",
        "@",
        "MATCH ",
        "secret",
        "salary",
        ".csv",
        "Alice",
    ):
        assert token not in blob


@pytest.mark.asyncio
async def test_finish_ingest_records_bucket_and_csv(monkeypatch):
    events = _capture(monkeypatch)
    store = InMemoryJobStore()
    job = await open_file_ingest_job(
        store, tenant_id="demo-tenant", kg_name="books", content_type="csv"
    )
    result = IngestResult(rows_in=42, entities_resolved=40, triples_inserted=10)
    await finish_file_ingest_job(job, store, result=result)
    assert len(events) == 1
    payload = events[0]
    assert payload["job_type"] == "ingest"
    assert payload["row_count_bucket"] == "11-100"
    assert payload["source_type"] == "csv"
    assert "error_class" not in payload
    _assert_clean(payload)


@pytest.mark.asyncio
async def test_fail_ingest_records_exception_type_not_message(monkeypatch):
    events = _capture(monkeypatch)
    store = InMemoryJobStore()
    job = await open_file_ingest_job(
        store, tenant_id="demo-tenant", kg_name="hr", content_type="json"
    )
    try:
        raise ValueError("secret.csv leaked salary for Alice")
    except ValueError:
        await fail_file_ingest_job(job, store, "secret.csv leaked salary for Alice")
    assert len(events) == 1
    payload = events[0]
    assert payload["job_type"] == "ingest"
    assert payload["source_type"] == "json"
    assert payload["error_class"] == "ValueError"
    _assert_clean(payload)


def test_ask_hook_records_http_and_bucket(monkeypatch):
    events = _capture(monkeypatch)
    tenant = TenantContext(tenant_id="tenant-acme", api_key="k")
    result = NLResult(answer="Alice earns 120000", sparql="", explanation="")
    result.timing["rows"] = 3
    _emit_query_executed(tenant, "hr-prod", time.monotonic(), result, ok=True)
    assert events[-1]["job_type"] == "ask"
    assert events[-1]["source_type"] == "http"
    assert events[-1]["row_count_bucket"] == "1-10"
    _assert_clean(events[-1])


def test_ask_hook_error_is_class_only(monkeypatch):
    events = _capture(monkeypatch)
    tenant = TenantContext(tenant_id="tenant-acme", api_key="k")
    degraded = NLResult(answer="Could not answer", sparql="", explanation="")
    _emit_query_executed(tenant, "hr-prod", time.monotonic(), degraded, ok=False)
    assert events[-1]["error_class"] == "Exception"
    _assert_clean(events[-1])


@pytest.mark.asyncio
async def test_export_hook_records_format_and_bucket(monkeypatch):
    events = _capture(monkeypatch)
    fake_page = {
        "columns": ["name"],
        "rows": [{"id": "e1", "name": "A"}, {"id": "e2", "name": "B"}],
        "total": 2,
        "next_cursor": None,
    }

    def _tenant() -> TenantContext:
        return TenantContext(tenant_id="t1", api_key="k")

    app = FastAPI()
    app.include_router(export_mod.router)
    app.dependency_overrides[api_keys.get_tenant] = _tenant
    app.dependency_overrides[get_neptune_client] = lambda: MagicMock()
    with patch.object(
        export_mod, "get_type_records", new=AsyncMock(return_value=fake_page)
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get(
                "/graphs/t1/kgs/bookstore/export",
                params={"format": "json", "type": "Book"},
            )
    assert res.status_code == 200, res.text
    assert events[-1]["job_type"] == "export"
    assert events[-1]["source_type"] == "json"
    assert events[-1]["row_count_bucket"] == "1-10"
    _assert_clean(events[-1])


@pytest.mark.asyncio
async def test_export_invalid_name_records_http_family(monkeypatch):
    events = _capture(monkeypatch)
    app = FastAPI()
    app.include_router(export_mod.router)
    app.dependency_overrides[api_keys.get_tenant] = lambda: TenantContext(
        tenant_id="t1", api_key="k"
    )
    app.dependency_overrides[get_neptune_client] = lambda: MagicMock()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get("/graphs/t1/kgs/BAD NAME/export")
    assert res.status_code == 422
    assert events[-1]["job_type"] == "export"
    assert events[-1]["error_class"] == "http_4xx"
    _assert_clean(events[-1])


@pytest.mark.asyncio
async def test_er_rebuild_hook_records(monkeypatch):
    events = _capture(monkeypatch)

    async def _none(*_a, **_k):
        return []

    monkeypatch.setattr(
        "infona_client.resolver.er.rebuild._types_in_graph", _none
    )
    out = await rebuild_kg(object(), "https://graph.infona.ai/g/t/kg/demo")
    assert out["fragments_absorbed_total"] == 0
    assert events[-1]["job_type"] == "er rebuild"
    assert events[-1]["source_type"] == "http"
    assert events[-1]["row_count_bucket"] == "0"
    _assert_clean(events[-1])


@pytest.mark.asyncio
async def test_er_rebuild_error_class(monkeypatch):
    events = _capture(monkeypatch)

    async def _boom(*_a, **_k):
        raise RuntimeError("graph content of tenant-acme")

    monkeypatch.setattr(
        "infona_client.resolver.er.rebuild._types_in_graph", _boom
    )
    with pytest.raises(RuntimeError):
        await rebuild_kg(object(), "https://graph.infona.ai/g/t/kg/demo")
    assert events[-1]["job_type"] == "er rebuild"
    assert events[-1]["error_class"] == "RuntimeError"
    _assert_clean(events[-1])


def test_record_job_default_off_from_hooks(monkeypatch):
    monkeypatch.setenv("INFONA_TELEMETRY", "0")
    events = []
    set_test_sink(events.append)
    record_job("ingest", row_count=5, source_type="csv")
    assert events == []
