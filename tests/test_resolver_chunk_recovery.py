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
    halves succeed), which is orthogonal to the ONTA-196/381 initial batch sizing.
    Sizing the token budget so ``floor(max_tokens * frac / tpr) == 25`` (with an
    8192 cap: 8192 * 0.55 / 180 ≈ 25) keeps the initial batches at the historical
    25 so the recovery assertions read the same. Pins EXTRACT_MAX_TOKENS to 8192
    because the production default rose to 16384 in ONTA-381 (which would make
    the same tpr yield batches of 50)."""
    monkeypatch.setattr(chunker, "EXTRACT_TOKENS_PER_RECORD", 180)
    monkeypatch.setattr(chunker, "EXTRACT_BATCH_TARGET_FRAC", 0.55)
    monkeypatch.setattr(SchemaResolver, "EXTRACT_MAX_TOKENS", 8192)
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


def _padded_entity_for(record: dict, tokens_per_record: int) -> ExtractedEntity:
    """A Model entity whose SERIALIZED size ≈ ``tokens_per_record`` output tokens.

    ONTA-197 calibration measures the first batch's REAL output size to re-size
    the rest, so a truncation mock must EMIT output consistent with the density it
    claims — a stub entity that truncates at 700 tok/record but serializes to ~50
    chars would let calibration (correctly) conclude the data is light and grow
    the batches. We pad a ``blob`` attribute so ``model_dump`` serializes to about
    ``tokens_per_record * 4`` chars (the ~4 chars/token the estimator assumes),
    making the mock's emitted size track its declared density."""
    approx_chars = max(1, tokens_per_record * 4)
    return ExtractedEntity(
        type_name="Model",
        id=str(record["id"]),
        attributes=[
            ExtractedAttribute(name="name", value=record["name"], datatype="string"),
            ExtractedAttribute(name="blob", value="x" * approx_chars, datatype="string"),
        ],
    )


def _token_scaling_extract_factory(
    max_tokens: int, tokens_per_record: int, calls: list[int]
):
    """A fake ``_extract`` emulating a REAL truncating model: each record expands
    to ``tokens_per_record`` OUTPUT tokens, and the reply is truncated (returns
    EMPTY) whenever a chunk's predicted output ``n * tokens_per_record`` exceeds
    ``max_tokens``. This is the exact failure ONTA-196 batching prevents — a
    chunk sized past the cap silently drops. Successful replies EMIT output sized
    to ``tokens_per_record`` (via :func:`_padded_entity_for`) so ONTA-197
    calibration observes the true density rather than a stub."""

    async def fake_extract(content, content_type, existing_types=None):
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = []
        n = len(data) if isinstance(data, list) else 0
        calls.append(n)
        if 0 < n and n * tokens_per_record <= max_tokens:
            return ExtractionResult(
                entities=[_padded_entity_for(r, tokens_per_record) for r in data],
                relationships=[],
            )
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
    A flat 25 would have overflowed the base cap and truncated."""
    # Defaults: 700 tok/record, 0.55 frac → batch size depends on EXTRACT_MAX_TOKENS
    # (16384 post-ONTA-381 → 12; was 6 under the old 8192 cap).
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


def _dense_page_records(n: int) -> list[dict]:
    """5-record dense multi-attribute page fixture (ONTA-381 acceptance).

    Emulates the live BC-universities-style records whose reified extraction
    output expanded past the historical 8192 completion cap mid-JSON
    (``completion_tokens: 8192``, ``finish_reason: length`` → parse error →
    reactive ``extraction_chunk_split_retry``).
    """
    return [
        {
            "id": i,
            "name": f"University of Dense Example {i}",
            "website": f"https://uni{i}.example.edu",
            "description": "x" * 3000,
            "city": "Vancouver",
            "province": "British Columbia",
            "fields": {f"attr_{j}": ("payload_" + "z" * 100) for j in range(30)},
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_dense_five_record_page_no_length_truncation_first_try(
    mock_neptune, mock_cache, monkeypatch
):
    """ONTA-381 acceptance: a 5-record dense-page batch extracts without
    ``finish_reason=length`` (or is proactively split before the first call so
    each attempt stays under the adaptive budget). No parse error, no reactive
    split-and-retry, equal-or-better yield (all 5 records land).

    The mock extractor truncates (returns empty + would be length-capped) only
    when predicted output exceeds the *adaptive* completion budget for that
    chunk. Under the pre-ONTA-381 flat 8192 cap a 5-record dense page
    (≈1600+ tok/record) would always overflow; with raised base + adaptive
    budget + proactive input-aware sizing it must succeed first-try.
    """
    # Pin density knobs so the fixture is deterministic across env overrides.
    monkeypatch.setattr(chunker, "EXTRACT_TOKENS_PER_RECORD", 700)
    monkeypatch.setattr(chunker, "EXTRACT_BATCH_TARGET_FRAC", 0.55)
    monkeypatch.setattr(chunker, "EXTRACT_OUTPUT_EXPANSION", 4.0)
    monkeypatch.setattr(chunker, "EXTRACT_COMPLETION_HEADROOM", 1.25)

    records = _dense_page_records(5)
    content = json.dumps(records)
    tpr = chunker.estimate_tokens_per_record_from_input(content)
    # Sanity: this fixture is dense enough to have overflowed the old 8192 cap.
    assert 5 * tpr > 8192, (tpr, 5 * tpr)

    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)

    calls: list[dict] = []
    split_retries: list = []

    # Spy on the recovery split log so we can assert it did NOT fire.
    from cograph_client.resolver import schema_resolver as sr_mod

    orig_warning = sr_mod.logger.warning

    def spy_warning(event, *args, **kwargs):
        if event == "extraction_chunk_split_retry":
            split_retries.append(kwargs)
        return orig_warning(event, *args, **kwargs)

    monkeypatch.setattr(sr_mod.logger, "warning", spy_warning)

    async def adaptive_extract(content, content_type, existing_types=None, **kwargs):
        """Truncate only when predicted output exceeds the adaptive budget."""
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = []
        n = len(data) if isinstance(data, list) else 0
        budget = resolver._completion_budget_for(n, tokens_per_record=tpr)
        predicted = n * tpr
        calls.append({"n": n, "budget": budget, "predicted": predicted})
        if 0 < n and predicted <= budget:
            return ExtractionResult(
                entities=[_padded_entity_for(r, min(tpr, 200)) for r in data],
                relationships=[],
            )
        # Would have been finish_reason=length under a too-small flat cap.
        return ExtractionResult(entities=[], relationships=[])

    with patch.object(resolver, "_extract", side_effect=adaptive_extract):
        with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
            result = await resolver.ingest(content, "test-tenant", content_type="json")

    # All 5 records land — equal-or-better yield vs the truncated baseline.
    assert result.rows_in == 5
    assert result.rows_dropped == 0
    assert result.entities_resolved == 5
    # First-try success: every attempt fit its adaptive budget (no empty
    # truncation that would trigger reactive recovery).
    assert calls, "extraction ran"
    assert all(c["predicted"] <= c["budget"] for c in calls), calls
    # Reactive split-and-retry must not fire — proactive sizing + adaptive
    # budget keep the recovery path as a safety net only.
    assert split_retries == [], split_retries


@pytest.mark.asyncio
async def test_extract_requests_adaptive_max_tokens_on_openrouter(
    mock_neptune, mock_cache, monkeypatch
):
    """ONTA-381: ``_extract`` threads an adaptive completion budget into
    ``_extract_via_openrouter`` (and logs it), never the stale flat 8192."""
    resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
    monkeypatch.setattr(resolver, "EXTRACT_PROVIDER", "openrouter")
    monkeypatch.setattr(resolver, "_openrouter_key", "or-key")
    # Pin the raised defaults so the assertion is stable.
    monkeypatch.setattr(resolver, "EXTRACT_MAX_TOKENS", 16384)
    monkeypatch.setattr(resolver, "EXTRACT_MAX_TOKENS_HARD", 32768)

    captured: dict = {}

    async def fake_or(user_content, system_prompt=None, **kwargs):
        captured.update(kwargs)
        return (
            json.dumps({"entities": [], "relationships": []}),
            "stop",
            {"prompt_tokens": 10, "completion_tokens": 20},
        )

    monkeypatch.setattr(resolver, "_extract_via_openrouter", fake_or)

    records = _dense_page_records(5)
    await resolver._extract(json.dumps(records), "json", {})

    assert "max_tokens" in captured
    # Adaptive budget is at least the raised base ceiling (never the old 8192).
    assert captured["max_tokens"] >= 16384
    assert captured["max_tokens"] <= 32768


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

    async def fake_or(user_content, system_prompt=None, **kwargs):
        # Truncated (length) + unparseable JSON → the truncation-signal path.
        # (content, finish_reason, usage) — the ONTA-200 3-tuple contract.
        # ``**kwargs`` absorbs ONTA-381's adaptive ``max_tokens`` kwarg.
        return '{"entities": [{"type_name": "Model", "id": "m0"', "length", None

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

    async def fake_or(user_content, system_prompt=None, **kwargs):
        # (content, finish_reason, usage) — the ONTA-200 3-tuple contract.
        # ``**kwargs`` absorbs ONTA-381's adaptive ``max_tokens`` kwarg.
        return "not json at all", "stop", None

    monkeypatch.setattr(resolver, "_extract_via_openrouter", fake_or)
    captured = _capture_parse_error_truncated(monkeypatch)

    result = await resolver._extract("[{}]", "json", {})

    assert result.entities == []
    assert captured.get("truncated") is False
