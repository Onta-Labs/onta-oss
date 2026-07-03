"""Content chunking utilities for large text, JSON, and CSV data."""

from __future__ import annotations

import json
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

    Returns:
        List of JSON string chunks.
    """
    if batch_size is None:
        # Budget against the caller's real extraction cap; when unknown, size
        # against the same default ceiling the resolver uses (8192) so a bare
        # call is still token-safe rather than reverting to a flat count.
        cap = max_tokens if max_tokens is not None else int(
            os.environ.get("OMNIX_EXTRACT_MAX_TOKENS", "8192")
        )
        batch_size = token_budget_batch_size(cap)

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return [content]

    if not isinstance(data, list):
        return [content]

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
