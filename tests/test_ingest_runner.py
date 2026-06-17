"""COG-59 — resumable, bounded-concurrency row ingestion (AC2).

A large ingest runs as a checkpointed job: it survives a crash, resumes from the
last committed batch without re-doing committed work, never exceeds its
concurrency bound, and (given the idempotent server) re-running a completed job
is a no-op.
"""

import pytest

from cograph_client.resolver.ingest_runner import (
    FileCheckpointStore,
    job_backend,
    register_job_backend,
    run_resumable_ingest,
)


class MemBackend:
    """In-memory JobBackend for tests."""

    def __init__(self):
        self.offsets: dict[str, int] = {}
        self.saves: list[int] = []

    def load_offset(self, job_id: str) -> int:
        return self.offsets.get(job_id, 0)

    def save_offset(self, job_id: str, offset: int, *, total=None) -> None:
        self.offsets[job_id] = offset
        self.saves.append(offset)

    def clear(self, job_id: str) -> None:
        self.offsets.pop(job_id, None)


def _batches(n: int) -> list[list[dict]]:
    return [[{"r": i}] for i in range(n)]


async def test_posts_all_batches_and_checkpoints_to_total():
    posted = []

    async def post(batch, idx):
        posted.append(idx)

    backend = MemBackend()
    committed = await run_resumable_ingest("j", _batches(5), post, concurrency=2, backend=backend)

    assert committed == 5
    assert sorted(posted) == [0, 1, 2, 3, 4]
    assert backend.load_offset("j") == 5


async def test_resumes_after_crash_without_redoing_committed():
    backend = MemBackend()
    batches = _batches(10)
    run1 = []

    class Boom(Exception):
        pass

    async def post_fail(batch, idx):
        if idx >= 4:
            raise Boom()
        run1.append(idx)

    with pytest.raises(Boom):
        await run_resumable_ingest("j", batches, post_fail, concurrency=1, backend=backend)

    # Checkpoint advanced over the contiguous committed prefix only.
    start = backend.load_offset("j")
    assert start == 4
    assert run1 == [0, 1, 2, 3]

    # Resume: only indices >= checkpoint are posted; the run completes.
    run2 = []

    async def post_ok(batch, idx):
        run2.append(idx)

    committed = await run_resumable_ingest("j", batches, post_ok, concurrency=1, backend=backend)
    assert committed == 10
    assert all(i >= start for i in run2)            # no committed batch re-posted
    assert set(run1) | set(run2) == set(range(10))  # every batch posted exactly once overall


async def test_rerunning_completed_job_is_a_noop():
    backend = MemBackend()
    batches = _batches(6)

    async def post(batch, idx):
        pass

    await run_resumable_ingest("j", batches, post, concurrency=3, backend=backend)
    assert backend.load_offset("j") == 6

    reposted = []

    async def post2(batch, idx):
        reposted.append(idx)

    committed = await run_resumable_ingest("j", batches, post2, concurrency=3, backend=backend)
    assert committed == 6
    assert reposted == []  # nothing re-posted


async def test_concurrency_bound_is_respected():
    inflight = 0
    peak = 0

    import asyncio

    async def post(batch, idx):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.005)
        inflight -= 1

    backend = MemBackend()
    await run_resumable_ingest("j", _batches(20), post, concurrency=4, backend=backend)
    assert peak <= 4


async def test_failed_batch_leaves_contiguous_checkpoint_under_concurrency():
    # Out-of-order completion + a mid-stream failure must still checkpoint only
    # the contiguous completed prefix (never an index past a gap).
    backend = MemBackend()

    async def post(batch, idx):
        if idx == 2:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await run_resumable_ingest("j", _batches(6), post, concurrency=3, backend=backend)

    # 0 and 1 may commit; 2 failed, so the checkpoint cannot pass 2.
    assert backend.load_offset("j") <= 2


def test_file_checkpoint_store_roundtrip(tmp_path):
    store = FileCheckpointStore(tmp_path)
    assert store.load_offset("a:b") == 0
    store.save_offset("a:b", 3, total=10)
    assert store.load_offset("a:b") == 3
    store.clear("a:b")
    assert store.load_offset("a:b") == 0


async def test_file_checkpoint_store_drives_real_resume(tmp_path):
    store = FileCheckpointStore(tmp_path)
    batches = _batches(8)

    async def post_fail(batch, idx):
        if idx >= 5:
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await run_resumable_ingest("t:kg", batches, post_fail, concurrency=1, backend=store)
    assert store.load_offset("t:kg") == 5

    seen = []

    async def post_ok(batch, idx):
        seen.append(idx)

    committed = await run_resumable_ingest("t:kg", batches, post_ok, concurrency=1, backend=store)
    assert committed == 8
    assert seen == [5, 6, 7]  # resumed exactly from the checkpoint


def test_register_job_backend_swaps_and_clears():
    custom = MemBackend()
    register_job_backend(custom)
    try:
        assert job_backend() is custom
    finally:
        register_job_backend(None)
    assert job_backend() is not custom  # back to the OSS default file store
