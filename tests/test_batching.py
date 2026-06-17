"""COG-59 — payload-byte batching keeps each POST body under the limit (AC3).

Batch size is computed from serialized payload bytes, not a fixed row count, so
wide rows never exceed the ALB ~1MB request-body limit and 413.
"""

import json

from cograph_client.resolver.batching import body_overhead_bytes, iter_byte_batches


def _body_bytes(batch: list[dict]) -> int:
    return len(json.dumps(batch, default=str).encode("utf-8"))


def test_every_batch_within_budget_and_conserves_rows():
    rows = [{"i": i, "blob": "x" * 200} for i in range(500)]
    max_bytes = 4000
    batches = list(iter_byte_batches(rows, max_bytes=max_bytes))

    # Conservation: every input row appears exactly once, in order.
    assert [r for b in batches for r in b] == rows
    assert all(b for b in batches)  # no empty batch
    for b in batches:
        assert _body_bytes(b) <= max_bytes


def test_overhead_is_reserved_from_the_budget():
    rows = [{"i": i, "blob": "y" * 100} for i in range(200)]
    envelope = {"mapping": {"entity_type": "T", "columns": []}, "kg_name": "k", "source": "s"}
    overhead = body_overhead_bytes(envelope)
    max_bytes = 3000
    budget = max_bytes - overhead

    for b in iter_byte_batches(rows, max_bytes=max_bytes, overhead_bytes=overhead):
        assert _body_bytes(b) <= budget  # rows-array fits within the reserved budget


def test_oversized_row_is_emitted_alone():
    small = {"i": 1, "blob": "a" * 10}
    huge = {"i": 2, "blob": "z" * 5000}
    rows = [small, huge, small]

    batches = list(iter_byte_batches(rows, max_bytes=1000))
    assert [huge] in batches                       # the huge row sits by itself
    assert [r for b in batches for r in b] == rows  # still no row dropped/reordered


def test_each_row_alone_when_all_exceed_budget():
    rows = [{"i": i, "blob": "q" * 2000} for i in range(3)]
    batches = list(iter_byte_batches(rows, max_bytes=500))
    assert len(batches) == 3
    assert all(len(b) == 1 for b in batches)


def test_empty_input_yields_no_batches():
    assert list(iter_byte_batches([], max_bytes=1000)) == []


def test_multibyte_values_counted_as_utf8():
    # A char that is 3 bytes in UTF-8 must count as 3, not 1, against the budget.
    rows = [{"v": "あ" * 100} for _ in range(50)]
    max_bytes = 2000
    for b in iter_byte_batches(rows, max_bytes=max_bytes):
        assert _body_bytes(b) <= max_bytes
