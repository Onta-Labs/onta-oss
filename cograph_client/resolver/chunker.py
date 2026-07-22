"""Content chunking utilities for large text, JSON, and CSV data."""

from __future__ import annotations

import json
import math
import os
import re


# --- token-budget batching (ONTA-196) --------------------------------------- #
#
# The reification/lift extraction prompt makes each record expand to MANY output
# tokens (a Model row → the Model + a reified Score + an Organization + the
# relationships linking them, ~600-700 tokens). A flat records-per-batch count
# (the old ``batch_size=25``) ignores that expansion, so a dense chunk's JSON
# blows past the extraction ``max_tokens`` cap, gets truncated, fails to parse,
# and drops the whole batch into the slow split-and-retry recovery. Instead we
# SIZE each batch so its PREDICTED output stays comfortably under a fraction of
# the cap, and let the common dense-record case succeed on the first extract.
#
# Both knobs are env-overridable so ops can retune without a deploy (a domain
# whose records reify heavily can lower the ratio; a flat domain can raise it).

#: Assumed OUTPUT tokens one record expands to under the reification/lift prompt.
#: Conservative on purpose — over-estimating shrinks batches (more, cheaper
#: extractions) but keeps them UNDER the cap; under-estimating is what causes the
#: truncation this fix removes. Env: ``OMNIX_EXTRACT_TOKENS_PER_RECORD``.
EXTRACT_TOKENS_PER_RECORD = int(os.environ.get("OMNIX_EXTRACT_TOKENS_PER_RECORD", "700"))

#: Target fraction of the extraction ``max_tokens`` an initial batch's predicted
#: output should fill. Well under 1.0 so a batch that runs a little denser than
#: assumed still fits — the recovery path stays a rare safety net, not the norm.
#: Env: ``OMNIX_EXTRACT_BATCH_TARGET_FRAC``.
EXTRACT_BATCH_TARGET_FRAC = float(os.environ.get("OMNIX_EXTRACT_BATCH_TARGET_FRAC", "0.55"))

#: Floor on the CALIBRATED tokens-per-record (ONTA-197 item 2). After the first
#: budgeted batch extracts, we measure the REAL output-tokens-per-record and
#: re-size the remaining batches with it. An anomalously light first batch (near
#: empty, or one whose records happen to reify very little) could otherwise
#: produce an absurdly small ratio → an oversized batch that overflows the cap on
#: a denser remainder. Clamp the observed ratio to at least this floor so a fluke
#: first batch can only ever SHRINK the win, never blow the budget.
#: Env: ``OMNIX_EXTRACT_MIN_TOKENS_PER_RECORD``.
EXTRACT_MIN_TOKENS_PER_RECORD = int(
    os.environ.get("OMNIX_EXTRACT_MIN_TOKENS_PER_RECORD", "80")
)

#: Chars-per-token divisor for the cheap output-size → tokens estimate used by
#: calibration. ~4 chars/token is the standard rough heuristic for English/JSON;
#: it need only be in the right ballpark because the target fraction (0.55) leaves
#: ample headroom and the split-and-retry recovery remains the hard safety net.
_CHARS_PER_TOKEN = 4.0

#: How many OUTPUT tokens one INPUT token expands to under the reification/lift
#: prompt (ONTA-381). Dense multi-attribute source pages reify into many more
#: entities + relationships than the flat :data:`EXTRACT_TOKENS_PER_RECORD`
#: default assumed — sizing against INPUT density × this expansion catches those
#: pages BEFORE the first call so they don't hit ``finish_reason=length``. Env:
#: ``OMNIX_EXTRACT_OUTPUT_EXPANSION``.
EXTRACT_OUTPUT_EXPANSION = float(os.environ.get("OMNIX_EXTRACT_OUTPUT_EXPANSION", "4.0"))

#: Headroom multiplier on the adaptive completion budget (ONTA-381). The call's
#: ``max_tokens`` is ``ceil(n_records * tokens_per_record * headroom)``, clamped
#: between the base and hard caps — a little slack so a batch that runs slightly
#: denser than assumed still finishes cleanly. Env:
#: ``OMNIX_EXTRACT_COMPLETION_HEADROOM``.
EXTRACT_COMPLETION_HEADROOM = float(
    os.environ.get("OMNIX_EXTRACT_COMPLETION_HEADROOM", "1.25")
)

#: Default extraction completion ceiling used when a caller doesn't pass an
#: explicit ``max_tokens`` (mirrors :attr:`SchemaResolver.EXTRACT_MAX_TOKENS`).
#: Raised 8192 → 16384 in ONTA-381: a dense 5-record page routinely expands past
#: the old 8192 mid-JSON. Env: ``OMNIX_EXTRACT_MAX_TOKENS``.
_DEFAULT_EXTRACT_MAX_TOKENS = int(os.environ.get("OMNIX_EXTRACT_MAX_TOKENS", "16384"))

#: Absolute hard ceiling on one extraction call's completion budget (ONTA-381).
#: Adaptive sizing may stretch up to this when a multi-record chunk's predicted
#: output exceeds the base cap; beyond it we rely on proactive split instead of
#: unbounded cost. Env: ``OMNIX_EXTRACT_MAX_TOKENS_HARD``.
_DEFAULT_EXTRACT_MAX_TOKENS_HARD = int(
    os.environ.get("OMNIX_EXTRACT_MAX_TOKENS_HARD", "32768")
)


def estimate_output_tokens(text: str) -> int:
    """Cheap output-token estimate from a serialized model reply's length.

    Calibration (ONTA-197 item 2) needs the REAL output size of the first batch
    to derive an observed tokens-per-record, but the extraction call site does
    not surface provider ``usage`` counts. The serialized reply length / ~4 is a
    good-enough proxy: we only use it to pick a batch SIZE, and the 0.55 target
    fraction plus the recovery path absorb the estimate's slack. Returns 0 for
    empty text.
    """
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def calibrated_tokens_per_record(
    observed_output_tokens: int,
    records_in_batch: int,
    *,
    floor: int | None = None,
) -> int:
    """Observed output-tokens-per-record from the first batch, clamped to a floor.

    ``ceil(observed_output_tokens / records_in_batch)`` — ROUND UP so we never
    under-budget the remaining batches — then clamped up to ``floor`` (default
    :data:`EXTRACT_MIN_TOKENS_PER_RECORD`) so a fluke-light first batch cannot
    yield a tiny ratio that would oversize a denser remainder. Returns the floor
    when the batch is empty or the count is non-positive (nothing to learn from).
    """
    fl = floor if floor is not None else EXTRACT_MIN_TOKENS_PER_RECORD
    if records_in_batch <= 0 or observed_output_tokens <= 0:
        return max(1, fl)
    per_record = math.ceil(observed_output_tokens / records_in_batch)
    return max(fl, per_record)


def estimate_tokens_per_record_from_input(
    content: str,
    *,
    default: int | None = None,
    expansion: float | None = None,
    sample_size: int = 8,
) -> int:
    """Estimate OUTPUT tokens/record from INPUT JSON density (ONTA-381).

    Dense multi-attribute source pages (long records) reify into far more output
    tokens than the flat :data:`EXTRACT_TOKENS_PER_RECORD` default. Sample up to
    ``sample_size`` records, take mean input tokens × ``expansion``, and return
    ``max(default, that)`` so:

    * light / short records stay at the conservative default (no needless
      over-splitting);
    * dense pages get a higher tokens-per-record → smaller proactive batches
      BEFORE the first LLM call, so a 5-record dense page that would have hit
      ``finish_reason=length`` at the old 8192 cap is split (or fitted under a
      raised adaptive budget) instead of falling into reactive recovery.

    Returns ``default`` for non-array / empty / unparseable content.
    """
    dflt = default if default is not None else EXTRACT_TOKENS_PER_RECORD
    exp = expansion if expansion is not None else EXTRACT_OUTPUT_EXPANSION
    if not content or not math.isfinite(exp) or exp <= 0:
        return max(1, dflt)
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return max(1, dflt)
    if not isinstance(data, list) or not data:
        return max(1, dflt)
    sample = data[: max(1, sample_size)]
    total_chars = 0
    for rec in sample:
        try:
            total_chars += len(json.dumps(rec, default=str))
        except (TypeError, ValueError):
            continue
    if total_chars <= 0:
        return max(1, dflt)
    avg_input_tokens = (total_chars / len(sample)) / _CHARS_PER_TOKEN
    estimated = int(math.ceil(avg_input_tokens * exp))
    return max(1, dflt, estimated)


def adaptive_completion_tokens(
    n_records: int,
    *,
    base_cap: int | None = None,
    hard_cap: int | None = None,
    tokens_per_record: int | None = None,
    headroom: float | None = None,
) -> int:
    """Completion-token budget for one extraction call of ``n_records`` (ONTA-381).

    Scales with predicted output (``n_records * tokens_per_record * headroom``)
    so a denser multi-record chunk gets enough room to finish clean JSON instead
    of hitting ``finish_reason=length`` mid-stream. Clamped:

    * **floor** ``base_cap`` (the comfortable default ceiling — small calls still
      get the historical room; never starved by a tiny predicted size);
    * **ceiling** ``hard_cap`` (absolute cost bound — beyond this, proactive
      batch sizing / reactive split must shrink the chunk).

    Non-positive / unknown record counts return ``base_cap`` unchanged. Pathological
    non-finite knobs fall back to ``base_cap``.
    """
    base = base_cap if base_cap is not None else _DEFAULT_EXTRACT_MAX_TOKENS
    hard = hard_cap if hard_cap is not None else _DEFAULT_EXTRACT_MAX_TOKENS_HARD
    tpr = tokens_per_record if tokens_per_record is not None else EXTRACT_TOKENS_PER_RECORD
    hr = headroom if headroom is not None else EXTRACT_COMPLETION_HEADROOM
    if not (
        math.isfinite(base)
        and math.isfinite(hard)
        and math.isfinite(tpr)
        and math.isfinite(hr)
    ):
        return max(1, int(base) if math.isfinite(base) else _DEFAULT_EXTRACT_MAX_TOKENS)
    base_i = max(1, int(base))
    hard_i = max(base_i, int(hard))
    if n_records is None or n_records <= 0 or tpr <= 0 or hr <= 0:
        return base_i
    predicted = int(math.ceil(n_records * tpr * hr))
    return max(base_i, min(hard_i, predicted))


def token_budget_batch_size(
    max_tokens: int,
    *,
    tokens_per_record: int | None = None,
    target_frac: float | None = None,
) -> int:
    """Records-per-batch whose PREDICTED extraction output fits the token budget.

    ``floor(max_tokens * target_frac / tokens_per_record)``, clamped to ≥ 1 so a
    pathological config never yields a zero-size (infinite-loop) batch. Defaults
    read the module constants (env-overridable); callers may override per-call.

    Example: with the defaults (max_tokens=8192, frac=0.55, 700 tok/record) a
    batch is ``floor(8192 * 0.55 / 700) = 6`` records — a ~4200-token predicted
    output, comfortably under the 8192 cap, where the old flat 25 predicted
    ~17500 and truncated.
    """
    tpr = tokens_per_record if tokens_per_record is not None else EXTRACT_TOKENS_PER_RECORD
    frac = target_frac if target_frac is not None else EXTRACT_BATCH_TARGET_FRAC
    # A non-finite tpr/frac (an ``inf``/``nan`` env value for
    # OMNIX_EXTRACT_BATCH_TARGET_FRAC or OMNIX_EXTRACT_TOKENS_PER_RECORD) slips
    # past the ``<= 0`` guards — ``nan <= 0`` and ``inf <= 0`` are both False —
    # and would make ``int(max_tokens * frac / tpr)`` raise on ``inf``/``nan``.
    # Treat any non-finite knob as a pathological config and clamp to 1, the same
    # safe floor the ``<= 0`` cases take.
    if not (math.isfinite(tpr) and math.isfinite(frac) and math.isfinite(max_tokens)):
        return 1
    if tpr <= 0 or frac <= 0 or max_tokens <= 0:
        return 1
    return max(1, int((max_tokens * frac) / tpr))


def chunk_text(content: str, max_chars: int = 3000, overlap: int = 200) -> list[str]:
    """Split text into chunks on sentence boundaries with overlap.

    Args:
        content: Raw text to split.
        max_chars: Maximum characters per chunk.
        overlap: Characters of overlap between chunks for context continuity.

    Returns:
        List of text chunks. Returns [content] if it fits in one chunk.
    """
    if len(content) <= max_chars:
        return [content]

    # Split into sentences
    sentences = re.split(r'(?<=[.!?])\s+', content)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for sentence in sentences:
        sentence_len = len(sentence)

        if current_len + sentence_len > max_chars and current:
            # Emit current chunk
            chunks.append(" ".join(current))
            # Start new chunk with overlap from the end of the previous
            overlap_text = " ".join(current)
            if len(overlap_text) > overlap:
                overlap_text = overlap_text[-overlap:]
            current = [overlap_text]
            current_len = len(overlap_text)

        current.append(sentence)
        current_len += sentence_len + 1  # +1 for space

    if current:
        chunks.append(" ".join(current))

    return chunks if chunks else [content]


def chunk_json_array(
    content: str,
    batch_size: int | None = None,
    *,
    max_tokens: int | None = None,
    tokens_per_record: int | None = None,
) -> list[str]:
    """Split a JSON array into batches of objects.

    If the root is not an array, returns the content as a single chunk.

    Args:
        content: JSON string.
        batch_size: Records per chunk. When ``None`` (the default) the size is
            derived from the extraction TOKEN BUDGET via
            :func:`token_budget_batch_size` — sized so each batch's predicted
            reified output stays under a fraction of ``max_tokens``, so the
            common dense-record case extracts first-try instead of overflowing
            the cap and falling into the slow split-and-retry recovery
            (ONTA-196). An explicit int overrides the budget (used by tests and
            any caller that wants a fixed size).
        max_tokens: The extraction ``max_tokens`` cap the derived batch size is
            budgeted against. Only consulted when ``batch_size`` is None; falls
            back to the assumed cap the module constants encode when omitted.
        tokens_per_record: Override the assumed OUTPUT tokens/record used to
            derive the budgeted batch size. Only consulted when ``batch_size`` is
            None. The resolver passes a CALIBRATED value here after measuring the
            first batch (ONTA-197 item 2) so the remaining batches track the
            data's REAL density instead of the conservative default.

    Returns:
        List of JSON string chunks.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return [content]

    if not isinstance(data, list):
        return [content]

    if batch_size is None:
        # Budget against the caller's real extraction cap; when unknown, size
        # against the same default ceiling the resolver uses (16384 post-
        # ONTA-381) so a bare call is still token-safe rather than reverting
        # to a flat count. When the caller doesn't pass an explicit
        # tokens_per_record, estimate from INPUT density so dense multi-
        # attribute pages proactively shrink below the truncation zone
        # (ONTA-381) instead of relying on reactive split-and-retry.
        cap = max_tokens if max_tokens is not None else _DEFAULT_EXTRACT_MAX_TOKENS
        tpr = tokens_per_record
        if tpr is None:
            tpr = estimate_tokens_per_record_from_input(content)
        batch_size = token_budget_batch_size(cap, tokens_per_record=tpr)

    if len(data) <= batch_size:
        return [content]

    chunks: list[str] = []
    for i in range(0, len(data), batch_size):
        batch = data[i : i + batch_size]
        chunks.append(json.dumps(batch, default=str))

    return chunks


def split_json_array_chunk(chunk: str) -> list[str]:
    """Split one JSON-array chunk string into two halves (recovery helper).

    Used by the ingest extraction loop to RECOVER a chunk whose extraction
    yielded nothing (e.g. the LLM output was truncated at max_tokens, so the
    JSON failed to parse and the whole batch would otherwise be silently lost).
    Splitting and retrying each half shrinks the per-call output until it fits.

    Returns the two half-chunks (as JSON strings). Returns ``[]`` if the chunk
    isn't a JSON array or holds fewer than 2 records — i.e. it can't be split
    further, signalling the caller to stop recursing.
    """
    try:
        data = json.loads(chunk)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list) or len(data) < 2:
        return []
    mid = len(data) // 2
    return [
        json.dumps(data[:mid], default=str),
        json.dumps(data[mid:], default=str),
    ]


def json_array_len(chunk: str) -> int:
    """Number of records in a JSON-array chunk string; 0 if not an array.

    Lets the ingest loop tell "this chunk genuinely had records but extraction
    returned zero" (a loss to recover) from "this chunk was legitimately empty".
    """
    try:
        data = json.loads(chunk)
    except json.JSONDecodeError:
        return 0
    return len(data) if isinstance(data, list) else 0
