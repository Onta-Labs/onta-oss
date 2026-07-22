"""Tests for content chunking utilities."""

import json

import pytest

from cograph_client.resolver import chunker
from cograph_client.resolver.chunker import (
    adaptive_completion_tokens,
    chunk_text,
    chunk_json_array,
    estimate_tokens_per_record_from_input,
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
            int(chunker.os.environ.get("OMNIX_EXTRACT_MAX_TOKENS", "16384"))
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


class TestInputAwareDensityAndAdaptiveBudget:
    """ONTA-381: proactive input-density sizing + adaptive completion budget."""

    def test_dense_input_raises_tokens_per_record_above_default(self):
        """A multi-attribute dense-page record estimates ABOVE the flat default
        so proactive batching shrinks before the first LLM call."""
        dense = [
            {
                "id": i,
                "name": f"University of Dense Example {i}",
                "website": f"https://uni{i}.example.edu",
                "description": "x" * 2500,
                "fields": {f"f{j}": ("value_" + "y" * 80) for j in range(25)},
            }
            for i in range(5)
        ]
        tpr = estimate_tokens_per_record_from_input(json.dumps(dense))
        assert tpr > chunker.EXTRACT_TOKENS_PER_RECORD

    def test_light_input_stays_at_default(self):
        """Tiny records must NOT inflate the ratio — the default floor wins."""
        light = [{"id": i, "name": f"m{i}"} for i in range(10)]
        tpr = estimate_tokens_per_record_from_input(json.dumps(light))
        assert tpr == chunker.EXTRACT_TOKENS_PER_RECORD

    def test_non_array_returns_default(self):
        assert estimate_tokens_per_record_from_input("{}") == chunker.EXTRACT_TOKENS_PER_RECORD
        assert estimate_tokens_per_record_from_input("not json") == chunker.EXTRACT_TOKENS_PER_RECORD
        assert estimate_tokens_per_record_from_input("") == chunker.EXTRACT_TOKENS_PER_RECORD

    def test_dense_five_record_page_proactively_fits_or_splits(self):
        """Acceptance: a 5-record dense page is either kept as one batch that
        fits the raised/adaptive budget, or proactively split BEFORE any LLM
        call — never sized as a flat overflow past the old 8192 cap.

        Emulates the live symptom (5 dense records → completion_tokens: 8192,
        finish_reason=length) by forcing the OLD 8192 base cap + a high tpr
        derived from dense input; the proactive sizer must then either:
          * produce batches whose predicted output stays under the target
            fraction of the cap, OR
          * split the 5 into smaller first-try batches.
        """
        dense = [
            {
                "id": i,
                "name": f"University of British Columbia Campus {i}",
                "website": f"https://uni{i}.bc.ca",
                "description": "x" * 3000,
                "fields": {f"attr_{j}": ("payload_" + "z" * 100) for j in range(30)},
            }
            for i in range(5)
        ]
        content = json.dumps(dense)
        tpr = estimate_tokens_per_record_from_input(content)
        # Dense enough that 5 * tpr would blow past the historical 8192 cap.
        assert 5 * tpr > 8192

        chunks = chunk_json_array(content, max_tokens=8192)
        # Either proactively split, or (if expansion is mild enough to fit under
        # the target fraction) keep as one — but every resulting batch's
        # PREDICTED output must stay under the target fraction of the cap.
        for c in chunks:
            n = json_array_len(c)
            assert n >= 1
            predicted = n * tpr
            assert predicted <= 8192 * chunker.EXTRACT_BATCH_TARGET_FRAC + tpr
            # Never larger than what token_budget_batch_size allows at this density.
            assert n <= token_budget_batch_size(8192, tokens_per_record=tpr)

        # Records conserved.
        total = sum(json_array_len(c) for c in chunks)
        assert total == 5

    def test_adaptive_completion_tokens_floors_at_base_cap(self):
        """Small/unknown record counts still get the base ceiling (never starved)."""
        assert adaptive_completion_tokens(0, base_cap=16384, hard_cap=32768) == 16384
        assert adaptive_completion_tokens(1, base_cap=16384, hard_cap=32768,
                                          tokens_per_record=700) == 16384

    def test_adaptive_completion_tokens_stretches_toward_hard_cap(self):
        """A multi-record chunk whose predicted output exceeds base_cap stretches
        up toward the hard cap — the ONTA-381 fix for the flat-8192 truncation."""
        # 12 records * 1600 tpr * 1.25 headroom = 24000 → clamped to hard 32768,
        # above base 16384.
        budget = adaptive_completion_tokens(
            12, base_cap=16384, hard_cap=32768, tokens_per_record=1600, headroom=1.25,
        )
        assert budget > 16384
        assert budget <= 32768
        assert budget == min(32768, int(12 * 1600 * 1.25))

    def test_adaptive_completion_tokens_never_exceeds_hard_cap(self):
        budget = adaptive_completion_tokens(
            100, base_cap=16384, hard_cap=32768, tokens_per_record=2000, headroom=1.5,
        )
        assert budget == 32768

    @pytest.mark.parametrize("bad", [float("inf"), float("nan"), float("-inf")])
    def test_adaptive_non_finite_knobs_fall_back(self, bad):
        """Pathological non-finite knobs must not raise — fall back to base."""
        assert adaptive_completion_tokens(5, base_cap=8192, hard_cap=bad) == 8192
        assert adaptive_completion_tokens(
            5, base_cap=8192, hard_cap=16384, tokens_per_record=bad
        ) == 8192


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
