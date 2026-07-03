"""Tests for content chunking utilities."""

import json

import pytest

from cograph_client.resolver import chunker
from cograph_client.resolver.chunker import (
    chunk_text,
    chunk_json_array,
    split_json_array_chunk,
    json_array_len,
    token_budget_batch_size,
)


class TestChunkText:
    def test_small_text_single_chunk(self):
        text = "Hello world. This is short."
        chunks = chunk_text(text, max_chars=3000)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_large_text_splits(self):
        sentences = [f"Sentence number {i} is here." for i in range(100)]
        text = " ".join(sentences)
        chunks = chunk_text(text, max_chars=200)
        assert len(chunks) > 1
        # All content should be represented
        combined = " ".join(chunks)
        for s in sentences:
            assert s in combined

    def test_splits_into_multiple_chunks(self):
        sentences = [f"This is sentence {i}." for i in range(50)]
        text = " ".join(sentences)
        chunks = chunk_text(text, max_chars=100, overlap=0)
        assert len(chunks) > 1

    def test_empty_text(self):
        chunks = chunk_text("")
        assert chunks == [""]

    def test_overlap_provides_context(self):
        text = "First sentence. Second sentence. Third sentence. Fourth sentence."
        chunks = chunk_text(text, max_chars=40, overlap=20)
        if len(chunks) > 1:
            # Second chunk should contain overlap from first
            assert len(chunks[1]) > 0


class TestChunkJsonArray:
    def test_small_array_single_chunk(self):
        data = [{"id": i} for i in range(10)]
        content = json.dumps(data)
        chunks = chunk_json_array(content, batch_size=50)
        assert len(chunks) == 1

    def test_large_array_splits(self):
        data = [{"id": i, "name": f"item_{i}"} for i in range(120)]
        content = json.dumps(data)
        chunks = chunk_json_array(content, batch_size=50)
        assert len(chunks) == 3  # 50 + 50 + 20

        # Verify all items present
        all_items = []
        for chunk in chunks:
            all_items.extend(json.loads(chunk))
        assert len(all_items) == 120

    def test_non_array_single_chunk(self):
        content = json.dumps({"key": "value"})
        chunks = chunk_json_array(content)
        assert len(chunks) == 1
        assert chunks[0] == content

    def test_invalid_json(self):
        chunks = chunk_json_array("not json at all")
        assert len(chunks) == 1
        assert chunks[0] == "not json at all"

    def test_empty_array(self):
        chunks = chunk_json_array("[]")
        assert len(chunks) == 1

    def test_default_batch_size_is_token_budgeted(self):
        """ONTA-196: with no explicit batch_size the array is split by the TOKEN
        BUDGET (records-per-batch derived from the extraction cap), NOT a flat
        count. With the module defaults each batch is small enough that its
        predicted reified output stays under the cap — many batches, no overflow.
        Every record is conserved regardless of the exact size."""
        data = [{"id": i} for i in range(60)]
        content = json.dumps(data)
        chunks = chunk_json_array(content)  # token-budgeted default
        expected = token_budget_batch_size(
            int(chunker.os.environ.get("OMNIX_EXTRACT_MAX_TOKENS", "8192"))
        )
        # Batches never exceed the budgeted size (the last may be smaller).
        for c in chunks[:-1]:
            assert json_array_len(c) == expected
        all_items = []
        for c in chunks:
            all_items.extend(json.loads(c))
        assert len(all_items) == 60

    def test_explicit_batch_size_overrides_budget(self):
        """An explicit batch_size still wins — used by callers/tests that want a
        fixed size independent of the token budget."""
        data = [{"id": i} for i in range(60)]
        chunks = chunk_json_array(json.dumps(data), batch_size=50)
        assert len(chunks) == 2  # 50 + 10

    def test_max_tokens_widens_batches(self):
        """A larger extraction cap budgets LARGER batches (fewer, denser chunks)
        — the sizing tracks the real cap the caller passes."""
        data = [{"id": i} for i in range(200)]
        content = json.dumps(data)
        small = chunk_json_array(content, max_tokens=4096)
        large = chunk_json_array(content, max_tokens=32768)
        # A bigger cap → bigger batches → fewer chunks.
        assert len(large) < len(small)


class TestTokenBudgetBatchSize:
    """ONTA-196: records-per-batch derived from the extraction token budget."""

    def test_default_keeps_predicted_output_under_target_fraction(self):
        """With the module defaults, a batch's PREDICTED output (size ×
        tokens_per_record) stays under target_frac × max_tokens — the whole point
        of the fix (the old flat 25 predicted ~17500 tokens against an 8192 cap
        and truncated)."""
        size = token_budget_batch_size(8192)
        assert size >= 1
        predicted = size * chunker.EXTRACT_TOKENS_PER_RECORD
        assert predicted <= 8192 * chunker.EXTRACT_BATCH_TARGET_FRAC
        # And it's the LARGEST size that still fits (adding one more record would
        # exceed the budget) — not needlessly tiny.
        assert (size + 1) * chunker.EXTRACT_TOKENS_PER_RECORD > 8192 * chunker.EXTRACT_BATCH_TARGET_FRAC

    def test_scales_with_cap(self):
        """Doubling the cap (at fixed ratio/frac) roughly doubles the batch."""
        small = token_budget_batch_size(8192)
        large = token_budget_batch_size(16384)
        assert large > small

    def test_per_call_overrides(self):
        """Explicit tokens_per_record / target_frac override the module defaults."""
        # 1000 * 0.5 / 100 = 5
        assert token_budget_batch_size(1000, tokens_per_record=100, target_frac=0.5) == 5

    @pytest.mark.parametrize("bad", [0, -1])
    def test_never_returns_zero(self, bad):
        """A pathological cap / ratio / fraction clamps to 1, never 0 (which would
        be an infinite-loop batch)."""
        assert token_budget_batch_size(bad) == 1
        assert token_budget_batch_size(8192, tokens_per_record=bad) == 1
        assert token_budget_batch_size(8192, target_frac=bad) == 1

    @pytest.mark.parametrize("bad", [float("inf"), float("nan"), float("-inf")])
    def test_non_finite_frac_clamps_not_crashes(self, bad):
        """ONTA-197 item 4: a non-finite ``target_frac`` (an inf/nan env value for
        OMNIX_EXTRACT_BATCH_TARGET_FRAC) slips past the ``<= 0`` guards —
        ``nan <= 0`` and ``inf <= 0`` are both False — and ``int(inf/nan)`` would
        raise. The math.isfinite guard must make it clamp to the ≥1 floor
        instead of crashing."""
        # Must not raise, and must return the safe floor.
        assert token_budget_batch_size(8192, target_frac=bad) == 1

    @pytest.mark.parametrize("bad", [float("inf"), float("nan"), float("-inf")])
    def test_non_finite_tokens_per_record_clamps_not_crashes(self, bad):
        """A non-finite ``tokens_per_record`` is likewise clamped, not crashed."""
        assert token_budget_batch_size(8192, tokens_per_record=bad) == 1

    def test_env_overrides_are_read(self, monkeypatch):
        """The module constants are env-overridable so ops can retune the sizing
        without a deploy (patched here to emulate the env being set at import)."""
        monkeypatch.setattr(chunker, "EXTRACT_TOKENS_PER_RECORD", 180)
        monkeypatch.setattr(chunker, "EXTRACT_BATCH_TARGET_FRAC", 0.55)
        assert token_budget_batch_size(8192) == 25  # 8192 * 0.55 / 180 ≈ 25


class TestCalibrationHelpers:
    """ONTA-197 item 2: first-batch calibration primitives."""

    def test_estimate_output_tokens_from_length(self):
        from cograph_client.resolver.chunker import estimate_output_tokens

        assert estimate_output_tokens("") == 0
        # ~4 chars/token → 400 chars ≈ 100 tokens.
        assert estimate_output_tokens("x" * 400) == 100
        # Never zero for non-empty text.
        assert estimate_output_tokens("x") >= 1

    def test_calibrated_ratio_rounds_up_and_floors(self):
        from cograph_client.resolver.chunker import (
            calibrated_tokens_per_record,
            EXTRACT_MIN_TOKENS_PER_RECORD,
        )

        # 1000 output tokens over 6 records → ceil(166.7) = 167, above the floor.
        assert calibrated_tokens_per_record(1000, 6) == 167
        # A light batch (below the floor) clamps UP to the floor so a fluke-light
        # first batch cannot oversize a denser remainder.
        assert calibrated_tokens_per_record(60, 6) == EXTRACT_MIN_TOKENS_PER_RECORD
        # An explicit floor overrides.
        assert calibrated_tokens_per_record(60, 6, floor=200) == 200

    def test_calibrated_ratio_empty_batch_returns_floor(self):
        from cograph_client.resolver.chunker import (
            calibrated_tokens_per_record,
            EXTRACT_MIN_TOKENS_PER_RECORD,
        )

        assert calibrated_tokens_per_record(0, 0) == EXTRACT_MIN_TOKENS_PER_RECORD
        assert calibrated_tokens_per_record(1000, 0) == EXTRACT_MIN_TOKENS_PER_RECORD
        assert calibrated_tokens_per_record(0, 10) == EXTRACT_MIN_TOKENS_PER_RECORD

    def test_chunk_json_array_honors_tokens_per_record_override(self):
        """A CALIBRATED tokens_per_record widens/narrows the derived batch size."""
        data = [{"id": i} for i in range(200)]
        content = json.dumps(data)
        # Light density (small tpr) → bigger batches → fewer chunks than the
        # conservative default.
        light = chunk_json_array(content, max_tokens=8192, tokens_per_record=100)
        heavy = chunk_json_array(content, max_tokens=8192, tokens_per_record=700)
        assert len(light) < len(heavy)
        # Records conserved either way.
        for chunks in (light, heavy):
            total = sum(len(json.loads(c)) for c in chunks)
            assert total == 200


class TestSplitJsonArrayChunk:
    """FIX 1: the recovery helper that halves a chunk whose extraction failed."""

    def test_splits_in_half_conserving_records(self):
        data = [{"id": i} for i in range(10)]
        halves = split_json_array_chunk(json.dumps(data))
        assert len(halves) == 2
        left, right = json.loads(halves[0]), json.loads(halves[1])
        assert len(left) == 5 and len(right) == 5
        # No record lost, order preserved.
        assert left + right == data

    def test_odd_length_splits_lower_then_upper(self):
        data = [{"id": i} for i in range(7)]
        halves = split_json_array_chunk(json.dumps(data))
        left, right = json.loads(halves[0]), json.loads(halves[1])
        assert len(left) == 3 and len(right) == 4
        assert left + right == data

    def test_single_record_cannot_split(self):
        assert split_json_array_chunk(json.dumps([{"id": 1}])) == []

    def test_empty_array_cannot_split(self):
        assert split_json_array_chunk("[]") == []

    def test_non_array_cannot_split(self):
        assert split_json_array_chunk(json.dumps({"k": "v"})) == []

    def test_invalid_json_cannot_split(self):
        assert split_json_array_chunk("not json") == []


class TestJsonArrayLen:
    def test_counts_array_records(self):
        assert json_array_len(json.dumps([{"a": 1}, {"b": 2}, {"c": 3}])) == 3

    def test_empty_array_is_zero(self):
        assert json_array_len("[]") == 0

    def test_non_array_is_zero(self):
        assert json_array_len(json.dumps({"k": "v"})) == 0

    def test_invalid_json_is_zero(self):
        assert json_array_len("garbage") == 0
