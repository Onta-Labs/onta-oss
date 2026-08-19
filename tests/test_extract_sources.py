"""Persist family for extract sources (ONTA-554). Not ApiSourceSpec."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from infona_client.api_registry.secret_store import reset_tenant_secret_store
from infona_client.ingestion.extract_source_store import reset_extract_source_store
from infona_client.ingestion.models import DltIngestRequest
from infona_client.resolver.models import IngestResult


CREATE = {
    "slug": "crm",
    "title": "Example CRM",
    "source": {
        "kind": "rest_api",
        "base_url": "https://api.example.com",
        "auth": {"type": "bearer", "secret_ref": "token"},
        "resources": ["v1/contacts"],
    },
    "map": {"v1/contacts": {"type": "Contact", "id_field": "id"}},
    "kg": "people",
    "secrets": {"token": "pat-never-echo"},
}


def setup_function():
    reset_extract_source_store()
    reset_tenant_secret_store()


def test_crud_and_secret_never_echoed(client, auth_headers, monkeypatch):
    # In-memory cipher so store_secret works in unit tests.
    monkeypatch.setenv("INFONA_SECRETS_KEY", "0" * 64)
    from infona_client.api_registry.crypto import reset_secret_cipher

    reset_secret_cipher()

    created = client.post(
        "/graphs/test-tenant/extract-sources", json=CREATE, headers=auth_headers
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["slug"] == "crm"
    assert body["has_secret"] is True
    assert "pat-never-echo" not in created.text

    listed = client.get("/graphs/test-tenant/extract-sources", headers=auth_headers)
    assert listed.status_code == 200
    assert listed.json()[0]["slug"] == "crm"
    assert "pat-never-echo" not in listed.text

    got = client.get("/graphs/test-tenant/extract-sources/crm", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["source"]["auth"]["token"] is None
    assert "pat-never-echo" not in got.text

    leaked = dict(CREATE)
    leaked["slug"] = "crm-hdr"
    leaked["source"] = {
        **CREATE["source"],
        "headers": {"Authorization": "Bearer pat-in-header", "Accept": "application/json"},
        "auth": {"type": "none"},
    }
    leaked["secrets"] = {}
    hdr = client.post(
        "/graphs/test-tenant/extract-sources", json=leaked, headers=auth_headers
    )
    assert hdr.status_code == 200, hdr.text
    got_hdr = client.get(
        "/graphs/test-tenant/extract-sources/crm-hdr", headers=auth_headers
    )
    assert "pat-in-header" not in got_hdr.text
    assert "Authorization" not in (got_hdr.json().get("source", {}).get("headers") or {})

    deleted = client.delete("/graphs/test-tenant/extract-sources/crm", headers=auth_headers)
    assert deleted.status_code == 200
    missing = client.get("/graphs/test-tenant/extract-sources/crm", headers=auth_headers)
    assert missing.status_code == 404


@patch("infona_client.ingestion.run.refresh_after_write", new_callable=AsyncMock)
@patch("infona_client.ingestion.run.extract_records")
@patch("infona_client.ingestion.run.SchemaResolver")
def test_run_uses_same_handoff_as_ingest_dlt(
    mock_resolver_cls, mock_extract, mock_refresh, client, auth_headers, monkeypatch
):
    monkeypatch.setenv("INFONA_SECRETS_KEY", "0" * 64)
    from infona_client.api_registry.crypto import reset_secret_cipher

    reset_secret_cipher()

    assert client.post(
        "/graphs/test-tenant/extract-sources", json=CREATE, headers=auth_headers
    ).status_code == 200

    from infona_client.ingestion.dlt_source import ExtractedResource

    mock_extract.return_value = [
        ExtractedResource(name="v1/contacts", rows=[{"id": "1", "name": "Ada"}])
    ]
    inst = AsyncMock()
    inst.ingest_structured_rows.return_value = IngestResult(
        rows_in=1, entities_resolved=1, triples_inserted=2, types_created=["Contact"]
    )
    mock_resolver_cls.return_value = inst

    resp = client.post(
        "/graphs/test-tenant/extract-sources/crm/run",
        json={},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert mock_refresh.await_count == 1
    called_spec = mock_extract.call_args.args[0]
    # Saved logical name is rewritten to store: for the 553 runner.
    assert called_spec.auth.secret_ref.startswith("store:dlt:crm/")


@patch("infona_client.ingestion.hosted.is_entitled", return_value=False)
@patch("infona_client.ingestion.hosted.get_entitlement_checker", return_value=lambda t: False)
def test_persist_403_when_not_entitled(_chk, _ent, client, auth_headers):
    resp = client.post(
        "/graphs/test-tenant/extract-sources", json=CREATE, headers=auth_headers
    )
    assert resp.status_code == 403


def test_run_request_shape_is_dlt_ingest_request():
    from infona_client.api.routes.extract_sources import _run_request
    from infona_client.ingestion.models import DltExtractSource, DltSourceSpec, DltResourceMap

    stored = DltExtractSource(
        slug="crm",
        title="CRM",
        kind="rest_api",
        source=DltSourceSpec(
            kind="rest_api",
            base_url="https://api.example.com",
            auth={"type": "bearer", "secret_ref": "token"},
            resources=["v1/contacts"],
        ),
        map={"v1/contacts": DltResourceMap(type="Contact", id_field="id")},
        kg="people",
    )
    req = _run_request(stored, None)
    assert isinstance(req, DltIngestRequest)
    assert req.source.auth.secret_ref == "store:dlt:crm/token"
    assert req.kg == "people"
