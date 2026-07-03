"""FIX 1 — extraction truncation must not silently drop a whole chunk.

A 50-record chunk's reified JSON output can exceed the LLM ``max_tokens``, get
truncated, fail to parse, and return an EMPTY :class:`ExtractionResult` — so the
WHOLE batch vanishes, logged only as a warning, with no row-conservation
accounting. The fix makes a failed/empty JSON chunk RECOVER: split its array in
half and retry each half (down to a floor), accounting for any record still lost.

These tests drive ``SchemaResolver.ingest`` with a mocked ``_extract`` that fails
for a dense chunk but succeeds once the chunk is small enough, and assert:
  * NO records are lost (every record lands as an entity),
  * splitting actually occurred (the dense chunk was retried in halves),
  * a chunk that can NEVER be extracted is accounted for in IngestResult
    (rows_in / rows_dropped), not silently presented as complete.

Harness mirrors tests/test_multityping_retail.py: a bare AsyncMock Neptune with
``_extract`` / ``_fetch_ontology`` patched, no network.
"""

from __future__ import annotations

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
def pin_batch_25(monkeypatch):
    """Pin the token-budget batch size to 25 records for the recovery tests.

    These tests exercise the split-and-retry RECOVERY (a dense chunk fails, its
    halves succeed), which is orthogonal to the ONTA-196 initial batch sizing.
    Sizing the token budget so ``floor(max_tokens * frac / tpr) == 25`` (with the
    resolver's 8192 cap: 8192 * 0.55 / 180 ≈ 25) keeps the initial batches at the
    historical 25 so the recovery assertions read the same, without hard-coding a
    number the production default no longer uses."""
    monkeypatch.setattr(chunker, "EXTRACT_TOKENS_PER_RECORD", 180)
    monkeypatch.setattr(chunker, "EXTRACT_BATCH_TARGET_FRAC", 0.55)
    assert chunker.token_budget_batch_size(8192) == 25


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


def _entity_for(record: dict) -> ExtractedEntity:
    """One Model entity per record, keyed by the record's id."""
    return ExtractedEntity(
        type_name="Model",
        id=str(record["id"]),
        attributes=[ExtractedAttribute(name="name", value=record["name"], datatype="string")],
    )


def _make_records(n: int) -> list[dict]:
    return [{"id": i, "name": f"model_{i}"} for i in range(n)]


def _fake_extract_factory(success_max: int, calls: list[int]):
    """Build a fake ``_extract`` that records each chunk's record count and only
    SUCCEEDS when the chunk holds ``<= success_max`` records — emulating
    truncation: a denser chunk overflows the token cap and returns EMPTY."""

    async def fake_extract(content, content_type, existing_types=None):
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = []
        n = len(data) if isinstance(data, list) else 0
        calls.append(n)
        if 0 < n <= success_max:
            return ExtractionResult(entities=[_entity_for(r) for r in data], relationships=[])
        # Too dense → truncated → empty extraction (the silent-loss path).
        return ExtractionResult(entities=[], relationships=[])

    return fake_extract


@pytest.mark.asyncio
async def test_dense_chunk_recovers_by_splitting_no_records_lost(
    mock_neptune, mock_cache, pin_batch_25
):
    """50 records → 2 chunks of 25 (batch pinned). Each 25-chunk fails, but a
    half (~12-13) succeeds: every record must still land, and a split must have
    occurred (a 25-record extraction attempt followed by smaller ones)."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    records = _make_records(50)
    content = json.dumps(records)

    calls: list[int] = []
    # Succeeds at <=13 records: the 25-record chunk fails, each ~12/13 half wins.
    fake_extract = _fake_extract_factory(success_max=13, calls=calls)

    with patch.object(resolver, "_extract", side_effect=fake_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            result = await resolver.ingest(content, "test-tenant", content_type="json")

    # No record lost: all 50 records produced an entity.
    assert result.rows_in == 50
    assert result.rows_dropped == 0
    assert result.entities_extracted == 50
    assert result.entities_resolved == 50

    # Splitting actually occurred: at least one chunk was attempted at full size
    # (25) and then re-attempted at a smaller size (the halves).
    assert 25 in calls, calls
    assert any(0 < c < 25 for c in calls), calls


@pytest.mark.asyncio
async def test_single_chunk_json_recovers_by_splitting(
    mock_neptune, mock_cache, pin_batch_25
):
    """REGRESSION (live): 11 records fit in ONE 25-record chunk, so ingest takes
    the ``len(chunks) <= 1`` branch — which previously used bare _extract with NO
    recovery, so a truncated reified output silently returned ZERO entities and
    the whole pull vanished. The single-chunk JSON path must now route through
    recovery too: the 11-record chunk fails, splits, and every record lands."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    records = _make_records(11)
    content = json.dumps(records)

    calls: list[int] = []
    # Succeeds at <=6: the single 11-record chunk fails (truncates), each half wins.
    fake_extract = _fake_extract_factory(success_max=6, calls=calls)

    with patch.object(resolver, "_extract", side_effect=fake_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            result = await resolver.ingest(content, "test-tenant", content_type="json")

    assert result.rows_in == 11
    assert result.rows_dropped == 0
    assert result.entities_extracted == 11
    assert result.entities_resolved == 11
    # The full single chunk was attempted (11) then split smaller — recovery ran
    # on the single-chunk path (the bug was that it didn't).
    assert 11 in calls, calls
    assert any(0 < c < 11 for c in calls), calls


@pytest.mark.asyncio
async def test_unrecoverable_chunk_is_accounted_not_silently_dropped(mock_neptune, mock_cache):
    """If a chunk can NEVER be extracted (fails even at the minimum size), its
    records must surface in rows_dropped — the run is not presented as complete
    with the loss hidden."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    records = _make_records(50)
    content = json.dumps(records)

    calls: list[int] = []
    # success_max=0 → every extraction returns empty, even single-record floors.
    fake_extract = _fake_extract_factory(success_max=0, calls=calls)

    with patch.object(resolver, "_extract", side_effect=fake_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            result = await resolver.ingest(content, "test-tenant", content_type="json")

    # Every record is accounted for as a drop — nothing landed, nothing hidden.
    assert result.rows_in == 50
    assert result.rows_dropped == 50
    assert result.entities_extracted == 0
    # Recursion bottomed out at the floor (chunks of <= 3 were attempted).
    assert any(0 < c <= SchemaResolver._RECOVERY_MIN_RECORDS for c in calls), calls


@pytest.mark.asyncio
async def test_healthy_chunks_do_not_split(mock_neptune, mock_cache, pin_batch_25):
    """When extraction succeeds at full chunk size, no splitting happens and the
    behavior is the plain per-chunk path (no spurious extra _extract calls)."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    records = _make_records(50)
    content = json.dumps(records)

    calls: list[int] = []
    fake_extract = _fake_extract_factory(success_max=25, calls=calls)

    with patch.object(resolver, "_extract", side_effect=fake_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            result = await resolver.ingest(content, "test-tenant", content_type="json")

    assert result.rows_in == 50
    assert result.rows_dropped == 0
    assert result.entities_resolved == 50
    # Exactly two chunks attempted, both at full size — no recovery splits.
    assert calls == [25, 25], calls


def _token_scaling_extract_factory(
    max_tokens: int, tokens_per_record: int, calls: list[int]
):
    """A fake ``_extract`` emulating a REAL truncating model: each record expands
    to ``tokens_per_record`` OUTPUT tokens, and the reply is truncated (returns
    EMPTY) whenever a chunk's predicted output ``n * tokens_per_record`` exceeds
    ``max_tokens``. This is the exact failure ONTA-196 batching prevents — a
    chunk sized past the cap silently drops."""

    async def fake_extract(content, content_type, existing_types=None):
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = []
        n = len(data) if isinstance(data, list) else 0
        calls.append(n)
        if 0 < n and n * tokens_per_record <= max_tokens:
            return ExtractionResult(entities=[_entity_for(r) for r in data], relationships=[])
        return ExtractionResult(entities=[], relationships=[])  # overflow → truncated

    return fake_extract


@pytest.mark.asyncio
async def test_token_budget_batching_avoids_truncation_first_try(
    mock_neptune, mock_cache, monkeypatch
):
    """ONTA-196 core: a DENSE input that would truncate a flat 25-record batch is
    now token-BUDGETED into batches that fit, so every chunk extracts FIRST TRY —
    the recovery split path never fires.

    The mocked extractor scales output with record count (each record →
    tokens_per_record output tokens) and truncates once a chunk's predicted output
    exceeds max_tokens — the real failure mode. With the resolver's real cap the
    budgeted batch size keeps every chunk under it, so:
      * no record is lost (rows_dropped == 0),
      * NO chunk is ever attempted above the budgeted size (no split-retry).
    A flat 25 would have overflowed (25 * 700 = 17500 > 8192) and truncated."""
    # Defaults: 700 tok/record, 0.55 frac, 8192 cap → batch size 6.
    monkeypatch.setattr(chunker, "EXTRACT_TOKENS_PER_RECORD", 700)
    monkeypatch.setattr(chunker, "EXTRACT_BATCH_TARGET_FRAC", 0.55)
    budget = chunker.token_budget_batch_size(SchemaResolver.EXTRACT_MAX_TOKENS)
    # Sanity: a flat 25 WOULD overflow the cap at this density; the budget must not.
    assert 25 * 700 > SchemaResolver.EXTRACT_MAX_TOKENS
    assert budget * 700 <= SchemaResolver.EXTRACT_MAX_TOKENS

    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    records = _make_records(50)
    content = json.dumps(records)

    calls: list[int] = []
    fake_extract = _token_scaling_extract_factory(
        max_tokens=SchemaResolver.EXTRACT_MAX_TOKENS,
        tokens_per_record=700,
        calls=calls,
    )

    with patch.object(resolver, "_extract", side_effect=fake_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            result = await resolver.ingest(content, "test-tenant", content_type="json")

    # Every record landed, none dropped — the dense case succeeded on first extract.
    assert result.rows_in == 50
    assert result.rows_dropped == 0
    assert result.entities_resolved == 50
    # Recovery never fired: no chunk was ever attempted ABOVE the budgeted size,
    # and no chunk truncated (every attempt fit the cap).
    assert calls, "extraction ran"
    assert max(calls) <= budget, calls
    assert all(c * 700 <= SchemaResolver.EXTRACT_MAX_TOKENS for c in calls), calls


def _capture_parse_error_truncated(monkeypatch) -> dict:
    """Spy on the resolver logger so a test can read the ``truncated`` kwarg the
    ``extraction_parse_error`` warning carries — robust to structlog's rendering
    (caplog doesn't reliably capture structlog kwargs)."""
    from cograph_client.resolver import schema_resolver as sr

    captured: dict = {}
    orig = sr.logger.warning

    def spy(event, *args, **kwargs):
        if event == "extraction_parse_error":
            captured.update(kwargs)
        return orig(event, *args, **kwargs)

    monkeypatch.setattr(sr.logger, "warning", spy)
    return captured


@pytest.mark.asyncio
async def test_extract_marks_truncated_on_openrouter_length_finish(
    mock_neptune, mock_cache, monkeypatch
):
    """ONTA-196 item 3: on the OpenRouter path, a ``finish_reason == "length"``
    marks the reply TRUNCATED (mirroring the Anthropic ``stop_reason`` check), so
    a parse failure is logged as a truncation to recover — not a malformed reply.
    We force the OpenRouter path and return a length-truncated, unparseable body,
    then assert the parse-error log carries ``truncated=True``."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    # Force the OpenRouter branch of _extract regardless of environment.
    monkeypatch.setattr(resolver, "EXTRACT_PROVIDER", "openrouter")
    monkeypatch.setattr(resolver, "_openrouter_key", "or-key")

    async def fake_or(user_content):
        # Truncated (length) + unparseable JSON → the truncation-signal path.
        return '{"entities": [{"type_name": "Model", "id": "m0"', "length"

    monkeypatch.setattr(resolver, "_extract_via_openrouter", fake_or)
    captured = _capture_parse_error_truncated(monkeypatch)

    result = await resolver._extract("[{}]", "json", {})

    assert result.entities == []  # truncated reply → empty, recovery handles upstream
    assert captured.get("truncated") is True


@pytest.mark.asyncio
async def test_extract_not_truncated_on_openrouter_clean_finish(
    mock_neptune, mock_cache, monkeypatch
):
    """A clean OpenRouter finish (``finish_reason == "stop"``) that still fails to
    parse is NOT flagged truncated — it's a genuinely malformed reply, and the
    truncation flag must not cry wolf."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    monkeypatch.setattr(resolver, "EXTRACT_PROVIDER", "openrouter")
    monkeypatch.setattr(resolver, "_openrouter_key", "or-key")

    async def fake_or(user_content):
        return "not json at all", "stop"

    monkeypatch.setattr(resolver, "_extract_via_openrouter", fake_or)
    captured = _capture_parse_error_truncated(monkeypatch)

    result = await resolver._extract("[{}]", "json", {})

    assert result.entities == []
    assert captured.get("truncated") is False
