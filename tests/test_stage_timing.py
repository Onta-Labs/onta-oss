"""Stage-timing observability (ONTA-198 follow-up).

`obs.timed` emits exactly ONE `stage_timing` log with the wrapped block's
`duration_ms` on BOTH the success and the exception path (and re-raises); and
`SchemaResolver.ingest` emits `stage_timing` for its two heavy halves —
`extract` (LLM) and `resolve_insert` (type-resolution + insert) — so a slow run
self-profiles instead of being reconstructed from CloudWatch request gaps.
"""

from __future__ import annotations

import json

import pytest
import structlog
from unittest.mock import AsyncMock, patch

from cograph_client.obs import timed
from cograph_client.resolver.schema_resolver import SchemaResolver
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
)
from cograph_client.resolver.verdict_cache import JsonVerdictCache

logger = structlog.stdlib.get_logger("test.stage_timing")


async def test_timed_logs_duration_on_success():
    with structlog.testing.capture_logs() as logs:
        async with timed(logger, "demo", n=3):
            pass
    ev = [e for e in logs if e.get("event") == "stage_timing"]
    assert len(ev) == 1
    assert ev[0]["stage"] == "demo"
    assert ev[0]["n"] == 3  # extra fields pass through
    assert isinstance(ev[0]["duration_ms"], (int, float))
    assert ev[0]["duration_ms"] >= 0


async def test_timed_logs_and_reraises_on_exception():
    """The `finally` still emits the span even when the block raises — a stage
    that dies mid-call must not vanish from the timing breakdown."""
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(ValueError):
            async with timed(logger, "boom"):
                raise ValueError("x")
    ev = [e for e in logs if e.get("event") == "stage_timing"]
    assert len(ev) == 1
    assert ev[0]["stage"] == "boom"
    assert "duration_ms" in ev[0]


@pytest.fixture
def mock_neptune():
    c = AsyncMock()
    c.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    c.update.return_value = None
    c.batch_exists.return_value = set()
    return c


async def test_ingest_emits_extract_and_resolve_spans(mock_neptune, tmp_path):
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

    with structlog.testing.capture_logs() as logs:
        with patch.object(resolver, "_extract", side_effect=fake_extract):
            with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
                await resolver.ingest(content, "test-tenant", content_type="json")

    timings = {
        e["stage"]: e for e in logs if e.get("event") == "stage_timing"
    }
    # Both heavy halves of the ingest are timed.
    assert "extract" in timings
    assert "resolve_insert" in timings
    for e in timings.values():
        assert isinstance(e["duration_ms"], (int, float))
        assert e["duration_ms"] >= 0
