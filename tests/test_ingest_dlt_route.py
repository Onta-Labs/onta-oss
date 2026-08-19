"""HTTP contract for POST /ingest/dlt (ONTA-553)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from infona_client.ingestion.dlt_source import ExtractedResource
from infona_client.ingestion.errors import DltNotInstalled, DltSecretMissing
from infona_client.ingestion.models import DltIngestRequest
from infona_client.resolver.models import IngestResult


BODY = {
    "source": {
        "kind": "rest_api",
        "base_url": "https://api.example.com",
        "auth": {"type": "bearer", "secret_ref": "env:EXAMPLE_TOKEN"},
        "resources": ["v1/contacts"],
    },
    "map": {"v1/contacts": {"type": "Contact", "id_field": "id"}},
    "kg": "crm",
}


def _extracted():
    return [
        ExtractedResource(
            name="v1/contacts",
            rows=[{"id": "1", "name": "Ada", "source_url": "https://api.example.com/v1/contacts"}],
        )
    ]


@patch("infona_client.ingestion.run.refresh_after_write", new_callable=AsyncMock)
@patch("infona_client.ingestion.run.extract_records")
@patch("infona_client.ingestion.run.SchemaResolver")
@patch("infona_client.ingestion.secrets.resolve_ref", new_callable=AsyncMock)
def test_ingest_dlt_handoff_and_one_refresh(
    mock_resolve, mock_resolver_cls, mock_extract, mock_refresh, client, auth_headers
):
    mock_resolve.return_value = "tok"
    mock_extract.return_value = _extracted()
    inst = AsyncMock()
    inst.ingest_structured_rows.return_value = IngestResult(
        rows_in=1,
        entities_resolved=1,
        triples_inserted=3,
        types_created=["Contact"],
        attributes_added=["Contact.name"],
    )
    mock_resolver_cls.return_value = inst

    resp = client.post("/graphs/test-tenant/ingest/dlt", json=BODY, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["rows_in"] == 1
    assert data["types_created"] == ["Contact"]
    assert mock_refresh.await_count == 1
    assert mock_refresh.await_args.kwargs["kg_name"] == "crm"
    assert mock_refresh.await_args.kwargs["affected_types"] == {"Contact"}
    inst.ingest_structured_rows.assert_awaited()
    kwargs = inst.ingest_structured_rows.await_args.kwargs
    assert kwargs["type_name"] == "Contact"
    assert kwargs["key_attribute"] == "id"


@patch("infona_client.ingestion.run.extract_records", side_effect=DltNotInstalled())
@patch("infona_client.ingestion.secrets.resolve_ref", new_callable=AsyncMock)
def test_missing_extra_is_503(mock_resolve, _extract, client, auth_headers):
    mock_resolve.return_value = "tok"
    resp = client.post("/graphs/test-tenant/ingest/dlt", json=BODY, headers=auth_headers)
    assert resp.status_code == 503
    assert "infona-client[dlt]" in resp.json()["detail"]


def test_missing_env_secret_is_422_not_500(client, auth_headers, monkeypatch):
    monkeypatch.delenv("EXAMPLE_TOKEN", raising=False)
    resp = client.post("/graphs/test-tenant/ingest/dlt", json=BODY, headers=auth_headers)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "env:" in detail
    # Server must not read process env (credential oracle).
    monkeypatch.setenv("EXAMPLE_TOKEN", "leaked-platform-secret")
    resp2 = client.post("/graphs/test-tenant/ingest/dlt", json=BODY, headers=auth_headers)
    assert resp2.status_code == 422
    assert "leaked-platform-secret" not in resp2.text


def test_missing_map_is_422(client, auth_headers):
    body = {
        "source": BODY["source"],
        "map": {},
        "kg": "crm",
    }
    resp = client.post("/graphs/test-tenant/ingest/dlt", json=body, headers=auth_headers)
    assert resp.status_code == 422


def test_frozen_shape_parses():
    req = DltIngestRequest.model_validate(BODY)
    assert req.source.kind == "rest_api"
    assert req.map["v1/contacts"].type == "Contact"
    assert req.kg == "crm"


@patch("infona_client.ingestion.hosted.is_entitled", return_value=False)
@patch("infona_client.ingestion.hosted.get_entitlement_checker", return_value=lambda t: False)
def test_store_secret_gated_when_checker_registered(
    _chk, _ent, client, auth_headers
):
    body = {
        "source": {
            "kind": "rest_api",
            "base_url": "https://api.example.com",
            "auth": {"type": "bearer", "secret_ref": "store:dlt:hub/token"},
            "resources": ["v1/contacts"],
        },
        "map": BODY["map"],
        "kg": "crm",
    }
    resp = client.post("/graphs/test-tenant/ingest/dlt", json=body, headers=auth_headers)
    assert resp.status_code == 403
    assert "entitlement" in resp.json()["detail"].lower()


@patch("infona_client.ingestion.hosted.get_entitlement_checker", return_value=lambda t: False)
def test_env_byok_ungated_when_checker_registered(_chk, client, auth_headers, monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "tok")
    # Will fail later (no dlt / no extract) — must NOT 403.
    resp = client.post("/graphs/test-tenant/ingest/dlt", json=BODY, headers=auth_headers)
    assert resp.status_code != 403
