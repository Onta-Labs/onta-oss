"""Extra-gated dlt tests. Skip when ``infona-client[dlt]`` is not installed.

Wave-1 integration: a local HTTP fixture (2 pages) and a SQLite tempfile
exercise ``rest_api_source`` / ``sql_database`` → :func:`extract_records` →
:meth:`SchemaResolver.ingest_structured_rows` → one
:func:`refresh_after_write`. Graph writes are mocked (no live Neo4j).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from infona_client.ingestion.dlt_source import dlt_available, extract_records, require_dlt
from infona_client.ingestion.models import DltIngestRequest, DltResourceMap, DltSourceSpec
from infona_client.ingestion.run import run_dlt_ingest
from infona_client.ingestion.secrets import ResolvedSecrets
from infona_client.resolver.models import IngestResult

pytestmark = pytest.mark.skipif(
    not dlt_available(), reason="infona-client[dlt] not installed"
)

PAGE1 = [{"id": "1", "name": "Ada"}, {"id": "2", "name": "Bob"}]
PAGE2 = [{"id": "3", "name": "Cara"}]
ALL_IDS = {"1", "2", "3"}


def test_require_dlt_succeeds_when_extra_present():
    require_dlt()
    import dlt  # noqa: F401 — allowed here only because this file is tests/

    assert dlt is not None


@contextmanager
def _contacts_http_fixture() -> Iterator[tuple[str, list[int]]]:
    """Serve ``/v1/contacts`` as a JSON array with RFC5988 Link pagination."""
    pages_hit: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # quiet
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.rstrip("/") != "/v1/contacts":
                self.send_error(404)
                return
            qs = parse_qs(parsed.query)
            page = int((qs.get("page") or ["1"])[0])
            pages_hit.append(page)
            host = self.headers.get("Host") or "127.0.0.1"
            if page <= 1:
                items, nxt = PAGE1, f"http://{host}/v1/contacts?page=2"
            else:
                items, nxt = PAGE2, None
            body = json.dumps(items).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if nxt:
                self.send_header("Link", f'<{nxt}>; rel="next"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", pages_hit
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _rest_source(base_url: str):
    """Build a real dlt ``rest_api_source`` that follows Link headers."""
    from dlt.sources.rest_api import rest_api_source

    return rest_api_source(
        {
            "client": {"base_url": base_url},
            "resources": [
                {
                    "name": "v1/contacts",
                    "endpoint": {
                        "path": "v1/contacts",
                        "paginator": {"type": "header_link", "links_next_key": "next"},
                    },
                }
            ],
        }
    )


@contextmanager
def _sqlite_contacts() -> Iterator[str]:
    """Tempfile SQLite with a ``contacts`` table (3 rows)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "crm.db"
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE contacts (id TEXT PRIMARY KEY, name TEXT)")
            conn.executemany(
                "INSERT INTO contacts (id, name) VALUES (?, ?)",
                [("1", "Ada"), ("2", "Bob"), ("3", "Cara")],
            )
            conn.commit()
        finally:
            conn.close()
        # Absolute sqlite URL needs four slashes.
        yield f"sqlite:///{path}"


def _capture_ingest():
    """Mock ``ingest_structured_rows``; record rows; return counts."""
    captured: list[dict] = []

    async def fake(self, rows, tenant_id, **kwargs):
        captured.extend(list(rows))
        type_name = kwargs.get("type_name") or "Contact"
        n = len(rows)
        return IngestResult(
            rows_in=n,
            entities_resolved=n,
            triples_inserted=n * 3,
            types_created=[type_name],
        )

    return captured, fake


async def _handoff(
    body: DltIngestRequest,
    *,
    secrets: ResolvedSecrets | None = None,
    source_factory=None,
) -> tuple[IngestResult, list[dict], AsyncMock]:
    captured, fake = _capture_ingest()
    mock_refresh = AsyncMock()
    with (
        patch(
            "infona_client.ingestion.run.SchemaResolver.ingest_structured_rows",
            fake,
        ),
        patch("infona_client.ingestion.run.refresh_after_write", mock_refresh),
    ):
        result = await run_dlt_ingest(
            tenant_id="test-tenant",
            body=body,
            neptune=MagicMock(),
            anthropic_key="",
            secrets=secrets or ResolvedSecrets(),
            source_factory=source_factory,
        )
    return result, captured, mock_refresh


@pytest.mark.asyncio
async def test_rest_api_two_pages_extract_then_one_refresh():
    """HTTP fixture, 2 pages: rest_api_source → extract → ingest → 1 refresh."""
    with _contacts_http_fixture() as (base_url, pages_hit):
        spec = DltSourceSpec.model_validate(
            {
                "kind": "rest_api",
                "base_url": base_url,
                "resources": ["v1/contacts"],
                "limit": 1000,
            }
        )
        source = _rest_source(base_url)
        extracted = extract_records(spec, source_factory=lambda _s, _sec: source)
        assert len(extracted) == 1
        ids = {row["id"] for row in extracted[0].rows}
        assert ids == ALL_IDS, extracted[0].rows
        assert len(extracted[0].rows) == 3
        assert set(pages_hit) >= {1, 2}

        body = DltIngestRequest.model_validate(
            {
                "source": spec.model_dump(),
                "map": {"v1/contacts": {"type": "Contact", "id_field": "id"}},
                "kg": "crm",
            }
        )
        result, captured, mock_refresh = await _handoff(
            body, source_factory=lambda _s, _sec: _rest_source(base_url)
        )
        assert result.rows_in == 3
        assert {row["id"] for row in captured} == ALL_IDS
        assert mock_refresh.await_count == 1
        assert mock_refresh.await_args.kwargs["kg_name"] == "crm"
        assert mock_refresh.await_args.kwargs["affected_types"] == {"Contact"}


@pytest.mark.asyncio
async def test_sql_sqlite_extract_then_one_refresh():
    """SQLite tempfile, ``kind=sql``: same extract → ingest → 1 refresh handoff."""
    with _sqlite_contacts() as dsn:
        spec = DltSourceSpec.model_validate(
            {
                "kind": "sql",
                "dsn": dsn,
                "resources": ["contacts"],
                "limit": 1000,
            }
        )
        extracted = extract_records(spec, secrets=ResolvedSecrets(dsn=dsn))
        assert len(extracted) == 1
        ids = {row["id"] for row in extracted[0].rows}
        assert ids == ALL_IDS, extracted[0].rows
        assert len(extracted[0].rows) == 3

        body = DltIngestRequest(
            source=spec,
            map={"contacts": DltResourceMap(type="Contact", id_field="id")},
            kg="crm",
        )
        result, captured, mock_refresh = await _handoff(
            body, secrets=ResolvedSecrets(dsn=dsn)
        )
        assert result.rows_in == 3
        assert {row["id"] for row in captured} == ALL_IDS
        assert mock_refresh.await_count == 1
        assert mock_refresh.await_args.kwargs["kg_name"] == "crm"
        assert mock_refresh.await_args.kwargs["affected_types"] == {"Contact"}
