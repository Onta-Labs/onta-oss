"""ONTA-548 — opt-in anonymous job telemetry (default off)."""

from __future__ import annotations

import json
from urllib.error import URLError

import pytest

from infona_client.telemetry import (
    ALLOWED_PAYLOAD_KEYS,
    is_enabled,
    record_job,
    reset_telemetry,
    set_test_sink,
)
from infona_client.telemetry.consent import (
    consent_file_opt_in,
    env_override,
    save_state,
)
from infona_client.telemetry.sanitize import (
    FORBIDDEN_FIELD_NAMES,
    build_payload,
    error_class,
    normalize_source_type,
    row_count_bucket,
)
from infona_client.telemetry.send import TIMEOUT_SEC, configured_url

FORBIDDEN_VALUES = (
    "user@example.com",
    "MATCH (n) RETURN n",
    "SELECT * FROM kg",
    "/tmp/secret-customers.csv",
    "tenant-acme-prod",
    "What is Alice's salary?",
    "Alice earns 120000",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("INFONA_TELEMETRY_STATE", str(tmp_path / "telemetry.json"))
    monkeypatch.setenv("INFONA_TELEMETRY_FILE", str(tmp_path / "events.jsonl"))
    for key in (
        "INFONA_TELEMETRY",
        "INFONA_TELEMETRY_URL",
        "INFONA_TELEMETRY_SINK",
        "INFONA_TELEMETRY_USE_CASE",
        "INFONA_TELEMETRY_SYNC",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_telemetry()
    yield
    reset_telemetry()


def _seen(monkeypatch) -> list[dict]:
    events: list[dict] = []
    monkeypatch.setenv("INFONA_TELEMETRY", "1")
    set_test_sink(events.append)
    return events


def test_default_off_no_network(monkeypatch):
    """Unset env + no consent → record_job is a no-op and never opens a URL."""
    opened: list[object] = []

    def _boom(*_a, **_k):
        opened.append(1)
        raise AssertionError("urlopen must not run when telemetry is off")

    monkeypatch.setattr("infona_client.telemetry.send.urlopen", _boom)
    monkeypatch.setenv("INFONA_TELEMETRY_URL", "https://example.test/capture")
    events: list[dict] = []
    set_test_sink(events.append)
    record_job("ingest", row_count=9, source_type="csv")
    assert events == []
    assert opened == []
    assert is_enabled() is False
    assert env_override() is None


def test_opt_in_sends_allowed_fields_only(monkeypatch):
    events = _seen(monkeypatch)
    monkeypatch.setenv("INFONA_TELEMETRY_USE_CASE", "research")
    record_job("ingest", row_count=42, source_type="csv")
    assert len(events) == 1
    payload = events[0]
    assert set(payload) <= ALLOWED_PAYLOAD_KEYS
    assert payload["event"] == "job"
    assert payload["job_type"] == "ingest"
    assert payload["row_count_bucket"] == "11-100"
    assert payload["source_type"] == "csv"
    assert payload["use_case"] == "research"
    assert "install_id" in payload
    assert "error_class" not in payload
    assert "rows" not in payload
    assert 42 not in payload.values()


def test_zero_env_wins_over_consent_file(monkeypatch, tmp_path):
    save_state({"opt_in": True, "asked": True, "install_id": "abc"})
    reset_telemetry()
    assert consent_file_opt_in() is True
    monkeypatch.setenv("INFONA_TELEMETRY", "0")
    events: list[dict] = []
    set_test_sink(events.append)
    monkeypatch.setenv("INFONA_TELEMETRY_URL", "https://example.test/capture")
    record_job("ask", row_count=3, source_type="http")
    assert events == []
    assert is_enabled() is False
    assert env_override() is False


def test_zero_wins_over_explicit_on(monkeypatch):
    """Last-write env is a single var; 0 is the off token even after a yes file."""
    save_state({"opt_in": True, "asked": True, "install_id": "abc"})
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("INFONA_TELEMETRY", "0")
    reset_telemetry()
    assert is_enabled() is False


def test_consent_file_enables_outside_pytest(monkeypatch):
    save_state({"opt_in": True, "asked": True, "install_id": "abc"})
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    reset_telemetry()
    assert is_enabled() is True
    monkeypatch.setenv("INFONA_TELEMETRY", "0")
    assert is_enabled() is False


def test_forbidden_fields_never_in_payload():
    payload = build_payload(
        install_id="i1",
        job_type="ask",
        row_count=7,
        source_type="/tmp/secret-customers.csv",
        extra={
            "tenant": "tenant-acme-prod",
            "tenant_id": "tenant-acme-prod",
            "email": "user@example.com",
            "prompt": "What is Alice's salary?",
            "question": "What is Alice's salary?",
            "answer": "Alice earns 120000",
            "cypher": "MATCH (n) RETURN n",
            "sparql": "SELECT * FROM kg",
            "filename": "/tmp/secret-customers.csv",
            "kg_name": "hr-prod",
            "message": "ValueError: leaked row",
            "row_count": 7,
            "rows": 7,
        },
    )
    assert payload is not None
    assert set(payload) <= ALLOWED_PAYLOAD_KEYS
    assert not (set(payload) & FORBIDDEN_FIELD_NAMES)
    blob = json.dumps(payload)
    for leaked in FORBIDDEN_VALUES:
        assert leaked not in blob
    assert payload["source_type"] == "csv"
    assert payload["row_count_bucket"] == "1-10"


@pytest.mark.parametrize(
    ("n", "bucket"),
    [
        (0, "0"),
        (1, "1-10"),
        (10, "1-10"),
        (11, "11-100"),
        (100, "11-100"),
        (101, "101-1000"),
        (1000, "101-1000"),
        (1001, "1001-10000"),
        (10000, "1001-10000"),
        (10001, "10000+"),
    ],
)
def test_row_count_buckets(n, bucket):
    assert row_count_bucket(n) == bucket


def test_source_type_strips_filenames_and_unknown_tokens():
    assert normalize_source_type("csv") == "csv"
    assert normalize_source_type("file:json") == "json"
    assert normalize_source_type("/data/exports/q3.csv") == "csv"
    assert normalize_source_type("customers.jsonl") == "jsonl"
    assert normalize_source_type("not-a-connector") == "unknown"


def test_error_class_never_includes_message():
    assert error_class(ValueError("secret.csv leaked")) == "ValueError"
    assert error_class(404) == "http_4xx"
    assert error_class(503) == "http_5xx"
    assert error_class("ValueError: /tmp/secret.csv") == "ValueError"
    assert error_class("boom with emails") == "Exception"
    assert "secret" not in (error_class("ValueError: secret") or "")


def test_unknown_job_type_is_dropped(monkeypatch):
    events = _seen(monkeypatch)
    record_job("enrich", row_count=1, source_type="csv")
    assert events == []


def test_fail_open_on_sink_error(monkeypatch):
    monkeypatch.setenv("INFONA_TELEMETRY", "1")

    def _raise(_payload):
        raise RuntimeError("sink down")

    set_test_sink(_raise)
    record_job("export", row_count=1, source_type="json")  # must not raise


def test_http_post_uses_short_timeout(monkeypatch):
    monkeypatch.setenv("INFONA_TELEMETRY", "1")
    monkeypatch.setenv("INFONA_TELEMETRY_URL", "https://example.test/capture")
    monkeypatch.setenv("INFONA_TELEMETRY_SYNC", "1")
    seen: list[tuple] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def _urlopen(req, timeout=None):
        seen.append((req.full_url, timeout, req.data, dict(req.header_items())))
        return _Resp()

    monkeypatch.setattr("infona_client.telemetry.send.urlopen", _urlopen)
    record_job("ask", row_count=2, source_type="http")
    assert len(seen) == 1
    url, timeout, data, _headers = seen[0]
    assert url == "https://example.test/capture"
    assert timeout == TIMEOUT_SEC
    assert timeout <= 2.0
    body = json.loads(data.decode())
    assert set(body) <= ALLOWED_PAYLOAD_KEYS
    assert body["job_type"] == "ask"


def test_http_error_is_fail_open(monkeypatch):
    monkeypatch.setenv("INFONA_TELEMETRY", "1")
    monkeypatch.setenv("INFONA_TELEMETRY_URL", "https://example.test/capture")
    monkeypatch.setenv("INFONA_TELEMETRY_SYNC", "1")

    def _urlopen(*_a, **_k):
        raise URLError("down")

    monkeypatch.setattr("infona_client.telemetry.send.urlopen", _urlopen)
    record_job("ingest", row_count=1, source_type="text")


def test_file_sink_writes_jsonl(monkeypatch, tmp_path):
    path = tmp_path / "out.jsonl"
    monkeypatch.setenv("INFONA_TELEMETRY", "1")
    monkeypatch.setenv("INFONA_TELEMETRY_SINK", "file")
    monkeypatch.setenv("INFONA_TELEMETRY_FILE", str(path))
    record_job("export", row_count=0, source_type="json")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload) <= ALLOWED_PAYLOAD_KEYS
    assert payload["job_type"] == "export"
    assert payload["row_count_bucket"] == "0"


def test_default_has_no_shipped_url():
    assert configured_url() == ""


def test_use_case_must_be_coarse_enum(monkeypatch):
    events = _seen(monkeypatch)
    monkeypatch.setenv("INFONA_TELEMETRY_USE_CASE", "acme-corp-hr")
    record_job("ingest", row_count=1, source_type="csv")
    assert "use_case" not in events[0]
