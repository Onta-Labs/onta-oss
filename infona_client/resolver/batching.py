"""Payload-byte batching for row ingestion (COG-59).

Row batches were sized by a fixed row count (``BATCH_SIZE = 500``). On wide
rows, 500 JSON objects can exceed AWS ALB's ~1MB default request-body limit and
the POST fails with a 413 — so the safe row count is dataset-dependent and was
tuned by trial and error.

``iter_byte_batches`` sizes each batch by the **serialized byte size** of the
rows it will carry, against a configured body budget, so a batch never exceeds
the limit regardless of how wide the rows are. It streams its input and yields
batches lazily, so memory stays bounded on large files.

Pure stdlib + structlog; no domain knowledge, no I/O.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Iterable, Iterator

import structlog

logger = structlog.stdlib.get_logger("cograph.resolver.batching")

#: Body budget for one ``/ingest/csv/rows`` POST. Default stays under the ALB
#: 1MB request-body limit with headroom; override per-deployment if the ALB's
#: limit is confirmed/raised (the ticket's payload note).
DEFAULT_MAX_BODY_BYTES = int(os.environ.get("OMNIX_INGEST_MAX_BODY_BYTES", "900000"))

#: JSON structural overhead of the rows array itself: the enclosing ``[`` ``]``.
_ARRAY_BRACKETS = 2
#: Bytes between two serialized elements. ``json.dumps`` default separator is
#: ``", "`` (2 bytes); compact/other serializers are <= this, so 2 is exact for
#: the default and conservative (never under-counts) for the rest.
_DEFAULT_SEP_BYTES = 2


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def body_overhead_bytes(
    envelope_without_rows: dict[str, Any],
    *,
    serialize: Callable[[Any], str] = lambda o: json.dumps(o, default=str),
) -> int:
    """Serialized byte size of everything in the POST body *except* the rows.

    Pass the request envelope with ``rows`` omitted (or empty) — e.g.
    ``{"mapping": mapping, "source": ..., "kg_name": ...}`` — to reserve that
    many bytes so the rows budget reflects the real on-the-wire body.
    """
    return _utf8_len(serialize(envelope_without_rows))


def iter_byte_batches(
    rows: Iterable[dict[str, Any]],
    *,
    max_bytes: int = DEFAULT_MAX_BODY_BYTES,
    overhead_bytes: int = 0,
    serialize: Callable[[Any], str] = lambda o: json.dumps(o, default=str),
    sep_bytes: int = _DEFAULT_SEP_BYTES,
) -> Iterator[list[dict[str, Any]]]:
    """Yield row batches whose serialized rows-array stays within the budget.

    The budget for the rows array is ``max_bytes - overhead_bytes`` (the
    overhead reserves space for the surrounding ``mapping``/``source``/``kg_name``
    envelope, see :func:`body_overhead_bytes`). Each batch is sized so that
    ``len(serialize(batch))`` does not exceed that budget: the running size is
    the array brackets, the serialized size of each element, and ``sep_bytes``
    between consecutive elements (2 = ``json.dumps`` default ``", "``; pass 1
    for a newline-joined serializer).

    Conservation guarantees (mirrors the row-conservation contract elsewhere in
    the resolver): every input row is emitted in exactly one batch, in input
    order, and no batch is empty. A single row whose own serialized size exceeds
    the budget is emitted **alone** (it would otherwise be undeliverable) and
    logged loudly — splitting a row is not possible without corrupting it.
    """
    budget = max_bytes - overhead_bytes
    if budget <= _ARRAY_BRACKETS:
        logger.warning(
            "csv_batch_budget_too_small",
            max_bytes=max_bytes,
            overhead_bytes=overhead_bytes,
            fallback=max_bytes,
        )
        budget = max_bytes

    batch: list[dict[str, Any]] = []
    batch_bytes = _ARRAY_BRACKETS  # "[" + "]"

    for row in rows:
        elem_bytes = _utf8_len(serialize(row))
        # The separator is only charged between elements — not before the first.
        add = elem_bytes if not batch else sep_bytes + elem_bytes

        # Flush the in-progress batch if this row would push it over budget.
        if batch and batch_bytes + add > budget:
            yield batch
            batch, batch_bytes = [], _ARRAY_BRACKETS
            add = elem_bytes  # this row now leads a fresh batch

        # An oversized row can't share a batch — and can't be split. Emit it
        # alone so the rest of the file still ingests; the POST may 413, which
        # the caller surfaces, but we never silently drop it.
        if not batch and (_ARRAY_BRACKETS + elem_bytes) > budget:
            logger.warning(
                "csv_oversized_row",
                row_bytes=elem_bytes,
                budget=budget,
                detail="row exceeds body budget; emitted alone (may 413)",
            )
            yield [row]
            continue

        batch.append(row)
        batch_bytes += add

    if batch:
        yield batch
