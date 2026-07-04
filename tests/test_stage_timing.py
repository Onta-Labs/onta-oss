"""Stage-timing observability (ONTA-198 follow-up).

`obs.timed` emits exactly ONE `stage_timing` log with the wrapped block's
`duration_ms` on BOTH the success and the exception path (and re-raises); and
`SchemaResolver.ingest` emits `stage_timing` for its two heavy halves —
`extract` (LLM) and `resolve_insert` (type-resolution + insert) — so a slow run
self-profiles instead of being reconstructed from CloudWatch request gaps.

The assertions record against a mock logger rather than
``structlog.testing.capture_logs()`` on purpose: under the full suite the
``cograph.resolver`` module logger is cached by earlier tests, so ``capture_logs``
would silently intercept nothing. A mock swapped in for the module logger is
order-independent.
"""

from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cograph_client.obs import timed
from cograph_client.resolver.schema_resolver import SchemaResolver
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
)
from cograph_client.resolver.verdict_cache import JsonVerdictCache


def _stage_calls(mock_logger):
    """The (stage → kwargs) of every `stage_timing` info() call on a mock logger."""
    out = {}
    for c in mock_logger.info.call_args_list:
        if c.args and c.args[0] == "stage_timing":
            out[c.kwargs.get("stage")] = c.kwargs
    return out


async def test_timed_logs_duration_on_success():
    log = MagicMock()
    async with timed(log, "demo", n=3):
        pass
    calls = _stage_calls(log)
    assert set(calls) == {"demo"}
    assert calls["demo"]["n"] == 3  # extra fields pass through
    assert isinstance(calls["demo"]["duration_ms"], (int, float))
    assert calls["demo"]["duration_ms"] >= 0


async def test_timed_logs_and_reraises_on_exception():
    """The `finally` still emits the span even when the block raises — a stage
    that dies mid-call must not vanish from the timing breakdown."""
    log = MagicMock()
    with pytest.raises(ValueError):
        async with timed(log, "boom"):
            raise ValueError("x")
    calls = _stage_calls(log)
    assert set(calls) == {"boom"}
    assert "duration_ms" in calls["boom"]


@pytest.fixture
def mock_neptune():
    c = AsyncMock()
    c.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    c.update.return_value = None
    c.batch_exists.return_value = set()
    return c


async def test_ingest_emits_extract_and_resolve_spans(mock_neptune, tmp_path, monkeypatch):
    import cograph_client.resolver.schema_resolver as sr

    rec = MagicMock()
    monkeypatch.setattr(sr, "logger", rec)

    resolver = SchemaResolver(
        mock_neptune, "fake-key", JsonVerdictCache(tmp_path / "c.json")
    )
    records = [{"id": i, "name": f"m{i}"} for i in range(3)]
    content = json.dumps(records)

    async def fake_extract(content, content_type, existing_types=None):
        data = json.loads(content)
        return ExtractionResult(
            entities=[
                ExtractedEntity(
                    type_name="Model",
                    id=str(r["id"]),
                    attributes=[
                        ExtractedAttribute(
                            name="name", value=r["name"], datatype="string"
                        )
                    ],
                )
                for r in data
            ],
            relationships=[],
        )

    with patch.object(resolver, "_extract", side_effect=fake_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            await resolver.ingest(content, "test-tenant", content_type="json")

    calls = _stage_calls(rec)
    # Both heavy halves of the ingest are timed.
    assert "extract" in calls
    assert "resolve_insert" in calls
    for kw in calls.values():
        assert isinstance(kw["duration_ms"], (int, float))
        assert kw["duration_ms"] >= 0
