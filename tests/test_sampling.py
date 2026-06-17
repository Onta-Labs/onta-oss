"""COG-59 — representative reservoir sampling for schema inference (AC1).

Schema-inference quality must be independent of where representative rows sit in
the file: a distinguishing value that first appears AFTER the legacy 5000-row
head cutoff has to reach the sample, which head-truncation never achieves.
"""

from cograph_client.resolver.profiler import profile_table
from cograph_client.resolver.sampling import reservoir_sample, sample_for_inference


def _rows(n: int, *, populated_after: int) -> list[dict]:
    """n rows where column ``late`` is empty until index ``populated_after``,
    then carries a distinct value — the pattern that hides in a file's tail."""
    return [
        {"id": str(i), "late": ("" if i < populated_after else f"v{i}")}
        for i in range(n)
    ]


def test_reservoir_is_deterministic_for_seed():
    rows = _rows(10_000, populated_after=5000)
    a = reservoir_sample(rows, 500, seed=42)
    assert a == reservoir_sample(rows, 500, seed=42)
    # A different seed selects a different subset (overwhelmingly likely).
    assert a != reservoir_sample(rows, 500, seed=7)


def test_reservoir_returns_whole_stream_when_k_exceeds_n():
    rows = _rows(100, populated_after=0)
    assert reservoir_sample(rows, 500, seed=0) == rows  # order preserved


def test_sample_surfaces_tail_values_invisible_to_head():
    n, cutoff = 100_000, 5000
    rows = _rows(n, populated_after=cutoff)

    # The legacy head sample (first 5000 rows) sees zero populated values.
    head_profile = profile_table(["id", "late"], rows[:cutoff], n)
    assert head_profile.column("late").completeness == 0.0

    # A whole-file reservoir sample surfaces the tail-only values.
    sample, total = sample_for_inference(rows, k=3000, seed=0)
    assert total == n
    assert len(sample) == 3000
    late = profile_table(["id", "late"], sample, total).column("late")
    assert late.completeness > 0.8  # ~95% of the file is populated
    assert late.examples           # concrete tail values are now visible


def test_sample_reports_true_total_and_bounded_size():
    rows = _rows(1234, populated_after=0)
    sample, total = sample_for_inference(rows, k=100, seed=0)
    assert total == 1234
    assert len(sample) == 100


def test_streams_a_generator_once_without_materializing():
    n = 10_000
    gen = ({"id": str(i)} for i in range(n))  # single-use iterator
    sample, total = sample_for_inference(gen, k=200, seed=1)
    assert total == n
    assert len(sample) == 200
