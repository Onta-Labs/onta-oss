"""Unit tests for the dlt extract wrapper (ONTA-553).

No real dlt required — a fake source is injected via ``source_factory``.
"""

from __future__ import annotations

import pytest

from infona_client.ingestion.dlt_source import (
    ExtractedResource,
    extract_records,
    lookup_resource_map,
)
from infona_client.ingestion.errors import DltNotInstalled, DltSecretMissing
from infona_client.ingestion.models import DltAuthSpec, DltResourceMap, DltSourceSpec
from infona_client.ingestion.secrets import (
    ResolvedSecrets,
    is_hosted_secret_ref,
    parse_secret_ref,
    resolve_ref,
    resolve_source_secrets,
)


class _FakeResource:
    def __init__(self, name: str, rows: list[dict]):
        self.name = name
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeSource:
    def __init__(self, resources: list[_FakeResource]):
        self.resources = {r.name: r for r in resources}


def _rest_spec(**kwargs) -> DltSourceSpec:
    base = dict(
        kind="rest_api",
        base_url="https://api.example.com",
        resources=["v1/contacts"],
        limit=100,
    )
    base.update(kwargs)
    return DltSourceSpec.model_validate(base)


def test_extract_records_yields_string_rows_and_never_needs_dlt():
    spec = _rest_spec()

    def factory(_spec, _secrets):
        return _FakeSource(
            [
                _FakeResource(
                    "v1/contacts",
                    [{"id": 1, "name": "Ada", "meta": {"ok": True}}, {"id": 2}],
                )
            ]
        )

    out = extract_records(spec, source_factory=factory)
    assert len(out) == 1
    assert isinstance(out[0], ExtractedResource)
    assert out[0].name == "v1/contacts"
    assert out[0].rows[0]["id"] == "1"
    assert out[0].rows[0]["name"] == "Ada"
    assert out[0].rows[0]["meta"] == '{"ok": true}'
    assert out[0].rows[0]["source_url"] == "https://api.example.com/v1/contacts"
    assert "extracted_at" in out[0].rows[0]
    assert len(out[0].rows) == 2


def test_extract_records_respects_limit():
    spec = _rest_spec(limit=2)

    def factory(_spec, _secrets):
        return _FakeSource(
            [_FakeResource("v1/contacts", [{"id": i} for i in range(10)])]
        )

    out = extract_records(spec, source_factory=factory)
    assert [r["id"] for r in out[0].rows] == ["0", "1"]


def test_extract_records_skips_unrequested_resources():
    spec = _rest_spec(resources=["v1/contacts"])

    def factory(_spec, _secrets):
        return _FakeSource(
            [
                _FakeResource("v1/contacts", [{"id": 1}]),
                _FakeResource("v1/deals", [{"id": 9}]),
            ]
        )

    names = [r.name for r in extract_records(spec, source_factory=factory)]
    assert names == ["v1/contacts"]


def test_lookup_resource_map_normalizes_slashes():
    mapping = {"v1/contacts": DltResourceMap(type="Contact", id_field="id")}
    assert lookup_resource_map("v1_contacts", mapping).type == "Contact"
    assert lookup_resource_map("v1/contacts", mapping).type == "Contact"
    assert lookup_resource_map("deals", mapping) is None


def test_require_dlt_errors_when_missing(monkeypatch):
    import infona_client.ingestion.dlt_source as mod

    def boom():
        raise ImportError("nope")

    monkeypatch.setattr(mod, "_import_dlt", lambda: (_ for _ in ()).throw(DltNotInstalled()))
    with pytest.raises(DltNotInstalled, match="infona-client\\[dlt\\]"):
        extract_records(_rest_spec())


def test_parse_and_resolve_env_ref():
    assert parse_secret_ref("env:EXAMPLE_TOKEN") == ("env", "EXAMPLE_TOKEN", "EXAMPLE_TOKEN")
    assert parse_secret_ref("store:dlt:hub/token") == ("store", "dlt:hub", "token")
    assert is_hosted_secret_ref("store:dlt:hub/token")
    assert not is_hosted_secret_ref("env:EXAMPLE_TOKEN")


@pytest.mark.asyncio
async def test_missing_env_secret_is_actionable():
    with pytest.raises(DltSecretMissing, match="set MISSING_TOKEN"):
        await resolve_ref("env:MISSING_TOKEN", env={})


@pytest.mark.asyncio
async def test_env_ref_does_not_read_process_environment(monkeypatch):
    monkeypatch.setenv("INFONA_ANTHROPIC_API_KEY", "platform-secret")
    with pytest.raises(DltSecretMissing, match="resolved on the client"):
        await resolve_ref("env:INFONA_ANTHROPIC_API_KEY")


@pytest.mark.asyncio
async def test_resolve_source_secrets_inline_token_not_via_env():
    spec = _rest_spec(auth=DltAuthSpec(type="bearer", token="pat-secret"))
    resolved = await resolve_source_secrets(spec, env={})
    assert resolved.token == "pat-secret"


@pytest.mark.asyncio
async def test_sql_dsn_env_ref():
    spec = DltSourceSpec(
        kind="sql",
        dsn="env:MY_DSN",
        resources=["contacts"],
    )
    resolved = await resolve_source_secrets(spec, env={"MY_DSN": "sqlite:///:memory:"})
    assert resolved.dsn == "sqlite:///:memory:"


def test_source_redacts_inline_token_and_dsn():
    spec = DltSourceSpec(
        kind="sql",
        dsn="postgresql://user:hunter2@db/app",
        resources=["t"],
        auth=DltAuthSpec(type="none", token="should-not-leak"),
    )
    dumped = spec.redacted()
    assert dumped["dsn"] == "***"
    assert dumped["auth"]["token"] is None


def test_redacted_masks_authorization_headers():
    spec = _rest_spec(headers={"Authorization": "Bearer pat-live", "Accept": "application/json"})
    dumped = spec.redacted()
    assert dumped["headers"]["Authorization"] == "***"
    assert dumped["headers"]["Accept"] == "application/json"


def test_ssrf_blocks_loopback_and_metadata():
    from infona_client.ingestion.dlt_source import validate_extract_target
    from infona_client.ingestion.errors import DltExtractError

    with pytest.raises(DltExtractError, match="fetchable"):
        validate_extract_target(
            DltSourceSpec.model_validate(
                {
                    "kind": "rest_api",
                    "base_url": "http://127.0.0.1/latest",
                    "resources": ["v1/contacts"],
                }
            )
        )
    with pytest.raises(DltExtractError, match="fetchable"):
        validate_extract_target(
            DltSourceSpec.model_validate(
                {
                    "kind": "rest_api",
                    "base_url": "http://169.254.169.254/latest/meta-data/",
                    "resources": ["v1/contacts"],
                }
            )
        )
    with pytest.raises(DltExtractError, match="relative"):
        validate_extract_target(
            DltSourceSpec.model_validate(
                {
                    "kind": "rest_api",
                    "base_url": "https://api.example.com",
                    "resources": ["http://169.254.169.254/latest/meta-data/"],
                }
            )
        )
    with pytest.raises(DltExtractError, match="not allowed|sqlite"):
        validate_extract_target(
            DltSourceSpec.model_validate(
                {
                    "kind": "sql",
                    "dsn": "postgresql://user:pw@127.0.0.1:5432/app",
                    "resources": ["t"],
                }
            ),
            dsn="postgresql://user:pw@127.0.0.1:5432/app",
        )


def test_ingest_request_accepts_kg_name_alias():
    from infona_client.ingestion.models import DltIngestRequest

    body = DltIngestRequest.model_validate(
        {
            "source": {
                "kind": "rest_api",
                "base_url": "https://api.example.com",
                "resources": ["v1/contacts"],
            },
            "map": {"v1/contacts": {"type": "Contact", "id_field": "id"}},
            "kg_name": "crm",
        }
    )
    assert body.kg == "crm"


def test_wrapper_module_does_not_configure_a_destination():
    from pathlib import Path

    text = Path(extract_records.__globals__["__file__"]).read_text()
    assert "dlt.destinations" not in text
    assert "destination=" not in text
    assert "dlt.pipeline" not in text
    assert '"paginator": "single_page"' in text
