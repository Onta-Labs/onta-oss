"""ONTA-197 items 2 + 3 — first-batch calibration and bounded concurrency.

These drive ``SchemaResolver.ingest`` with a mocked ``_extract`` (no network) and
assert the two composed behaviors:

  * **Calibration (item 2)** — after the first budgeted batch extracts, the REAL
    output-tokens-per-record is measured and the remaining batches are re-sized.
    For SPARSE records (light real output) this shrinks the total extraction call
    count versus the conservative ~700-tok/record default; for DENSE records it
    never produces an overflowing batch (no truncation reintroduced).
  * **Concurrency (item 3)** — the remaining chunks extract under an
    ``asyncio.Semaphore``: the max simultaneously-in-flight extractions is
    ``> 1`` (they actually overlap) yet never exceeds the configured size, while
    every record is conserved and result order is preserved.

Harness mirrors tests/test_resolver_chunk_recovery.py: a bare AsyncMock Neptune
with ``_extract`` / ``_fetch_ontology`` patched.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from cograph_client.resolver import chunker
from cograph_client.resolver.schema_resolver import SchemaResolver
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
)
from cograph_client.resolver.verdict_cache import JsonVerdictCache


@pytest.fixture
def mock_neptune():
    client = AsyncMock()
    client.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    client.update.return_value = None
    client.batch_exists.return_value = set()
    return client


@pytest.fixture
def mock_cache(tmp_path):
    return JsonVerdictCache(tmp_path / "cache.json")


def _make_records(n: int) -> list[dict]:
    return [{"id": i, "name": f"model_{i}"} for i in range(n)]


def _entity_sized(record: dict, real_tokens_per_record: int) -> ExtractedEntity:
    """A Model entity whose serialized size ≈ ``real_tokens_per_record`` tokens.

    Calibration measures the FIRST batch's real emitted size, so the mock must
    emit output proportional to the density it wants to model (~4 chars/token)."""
    approx_chars = max(1, real_tokens_per_record * 4)
    return ExtractedEntity(
        type_name="Model",
        id=str(record["id"]),
        attributes=[
            ExtractedAttribute(name="name", value=record["name"], datatype="string"),
            ExtractedAttribute(name="blob", value="x" * approx_chars, datatype="string"),
        ],
    )


def _density_extract_factory(real_tokens_per_record: int, calls: list[int]):
    """A fake ``_extract`` whose output size tracks ``real_tokens_per_record`` and
    which TRUNCATES (returns empty) when a chunk's real output exceeds the cap."""

    async def fake_extract(content, content_type, existing_types=None):
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = []
        n = len(data) if isinstance(data, list) else 0
        calls.append(n)
        if 0 < n and n * real_tokens_per_record <= SchemaResolver.EXTRACT_MAX_TOKENS:
            return ExtractionResult(
                entities=[_entity_sized(r, real_tokens_per_record) for r in data],
                relationships=[],
            )
        return ExtractionResult(entities=[], relationships=[])  # truncated

    return fake_extract


# --- Item 2: calibration ------------------------------------------------------


@pytest.mark.asyncio
async def test_calibration_shrinks_call_count_for_sparse_records(
    mock_neptune, mock_cache, monkeypatch
):
    """Sparse records (light real output) → calibration re-sizes the remainder to
    BIGGER batches, so the total number of extraction calls is FEWER than the
    conservative ~700-tok/record default would have produced. No record lost."""
    # Conservative default: 700 tok/record, 0.55 frac, 8192 cap → first batch 6.
    monkeypatch.setattr(chunker, "EXTRACT_TOKENS_PER_RECORD", 700)
    monkeypatch.setattr(chunker, "EXTRACT_BATCH_TARGET_FRAC", 0.55)
    # Keep concurrency high so it never limits how big the remainder chunks are.
    monkeypatch.setattr(SchemaResolver, "EXTRACT_CONCURRENCY", 8)

    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    records = _make_records(60)
    content = json.dumps(records)

    # Records are SPARSE: each expands to only ~50 real output tokens, far under
    # the conservative 700 the first batch is sized against.
    calls: list[int] = []
    fake_extract = _density_extract_factory(real_tokens_per_record=50, calls=calls)

    with patch.object(resolver, "_extract", side_effect=fake_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            result = await resolver.ingest(content, "test-tenant", content_type="json")

    assert result.rows_in == 60
    assert result.rows_dropped == 0
    assert result.entities_resolved == 60

    # The conservative default would split 60 records into
    # ceil(60 / token_budget_batch_size(cap)) chunks. Calibration measures the
    # light real density and grows the remainder batches, so the total call
    # count is materially fewer than that conservative split.
    conservative_chunks = -(-60 // chunker.token_budget_batch_size(
        SchemaResolver.EXTRACT_MAX_TOKENS
    ))  # ceil
    assert conservative_chunks >= 2  # enough room for calibration to shrink calls
    assert len(calls) < conservative_chunks, calls
    # And nothing truncated: every attempted chunk fit the real cap.
    assert all(
        c * 50 <= SchemaResolver.EXTRACT_MAX_TOKENS for c in calls
    ), calls


@pytest.mark.asyncio
async def test_calibration_never_overflows_for_dense_records(
    mock_neptune, mock_cache, monkeypatch
):
    """Dense records (heavy real output) → calibration must NOT grow batches into
    the truncation zone. Every attempted chunk stays under the cap (no split-retry
    reintroduced), and no record is lost."""
    monkeypatch.setattr(chunker, "EXTRACT_TOKENS_PER_RECORD", 700)
    monkeypatch.setattr(chunker, "EXTRACT_BATCH_TARGET_FRAC", 0.55)
    monkeypatch.setattr(SchemaResolver, "EXTRACT_CONCURRENCY", 8)

    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    records = _make_records(60)
    content = json.dumps(records)

    # DENSE: each record really expands to ~700 output tokens (matches the
    # conservative assumption). Calibration must keep the batches small.
    calls: list[int] = []
    fake_extract = _density_extract_factory(real_tokens_per_record=700, calls=calls)

    with patch.object(resolver, "_extract", side_effect=fake_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            result = await resolver.ingest(content, "test-tenant", content_type="json")

    assert result.rows_in == 60
    assert result.rows_dropped == 0
    assert result.entities_resolved == 60
    # No attempted chunk ever exceeded the real cap → no truncation was triggered.
    assert calls, "extraction ran"
    assert all(
        c * 700 <= SchemaResolver.EXTRACT_MAX_TOKENS for c in calls
    ), calls


# --- Item 3: bounded concurrency ----------------------------------------------


def _overlap_tracking_extract_factory(
    real_tokens_per_record: int, in_flight: dict
):
    """A fake ``_extract`` that records max concurrent in-flight calls. Each call
    sleeps briefly so overlapping calls actually coexist under the semaphore."""

    async def fake_extract(content, content_type, existing_types=None):
        in_flight["cur"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["cur"])
        try:
            await asyncio.sleep(0.02)  # hold the slot so peers pile up
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                data = []
            n = len(data) if isinstance(data, list) else 0
            if 0 < n and n * real_tokens_per_record <= SchemaResolver.EXTRACT_MAX_TOKENS:
                return ExtractionResult(
                    entities=[_entity_sized(r, real_tokens_per_record) for r in data],
                    relationships=[],
                )
            return ExtractionResult(entities=[], relationships=[])
        finally:
            in_flight["cur"] -= 1

    return fake_extract


@pytest.mark.asyncio
async def test_concurrency_overlaps_bounded_and_conserves_records(
    mock_neptune, mock_cache, monkeypatch
):
    """The remainder chunks extract CONCURRENTLY: max in-flight is > 1 (real
    overlap) but never exceeds EXTRACT_CONCURRENCY, and every record is conserved.
    """
    # Small conservative batches (many remainder chunks) with a modest semaphore.
    monkeypatch.setattr(chunker, "EXTRACT_TOKENS_PER_RECORD", 700)
    monkeypatch.setattr(chunker, "EXTRACT_BATCH_TARGET_FRAC", 0.55)
    monkeypatch.setattr(SchemaResolver, "EXTRACT_CONCURRENCY", 3)

    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    records = _make_records(60)
    content = json.dumps(records)

    in_flight = {"cur": 0, "max": 0}
    # Dense enough that calibration keeps batches small → many remainder chunks
    # to overlap.
    fake_extract = _overlap_tracking_extract_factory(
        real_tokens_per_record=700, in_flight=in_flight
    )

    with patch.object(resolver, "_extract", side_effect=fake_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            result = await resolver.ingest(content, "test-tenant", content_type="json")

    # Every record conserved.
    assert result.rows_in == 60
    assert result.rows_dropped == 0
    assert result.entities_resolved == 60
    # Concurrency actually overlapped, and stayed within the bound.
    assert in_flight["max"] > 1, "remainder chunks did not overlap"
    assert in_flight["max"] <= 3, in_flight["max"]


@pytest.mark.asyncio
async def test_concurrency_preserves_result_order(
    mock_neptune, mock_cache, monkeypatch
):
    """Even though chunks complete out of order (later chunks finish first), the
    merged entity order follows the INPUT chunk order — asyncio.gather preserves
    input order regardless of completion order."""
    monkeypatch.setattr(chunker, "EXTRACT_TOKENS_PER_RECORD", 700)
    monkeypatch.setattr(chunker, "EXTRACT_BATCH_TARGET_FRAC", 0.55)
    monkeypatch.setattr(SchemaResolver, "EXTRACT_CONCURRENCY", 8)

    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    records = _make_records(60)
    content = json.dumps(records)

    async def fake_extract(content_, content_type, existing_types=None):
        data = json.loads(content_)
        n = len(data)
        # Make LATER chunks (higher ids) finish FIRST, so completion order is the
        # reverse of input order — a strong test that gather restores input order.
        first_id = int(data[0]["id"]) if data else 0
        await asyncio.sleep(max(0.0, (100 - first_id) * 0.0005))
        return ExtractionResult(
            entities=[_entity_sized(r, 50) for r in data],
            relationships=[],
        )

    captured_ids: list[int] = []
    orig_merge_target = None

    with patch.object(resolver, "_extract", side_effect=fake_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            # Spy the final extraction result by wrapping _resolve_and_insert to
            # capture the merged entity id order it receives.
            orig = resolver._resolve_and_insert

            async def spy(extraction, *a, **k):
                captured_ids.extend(int(e.id) for e in extraction.entities)
                return await orig(extraction, *a, **k)

            with patch.object(resolver, "_resolve_and_insert", side_effect=spy):
                result = await resolver.ingest(content, "test-tenant", content_type="json")

    assert result.rows_dropped == 0
    assert result.entities_resolved == 60
    # The merged order is strictly ascending by id == input order, despite the
    # reversed completion order enforced by the sleeps.
    assert captured_ids == sorted(captured_ids), captured_ids
    assert captured_ids == list(range(60)), captured_ids
