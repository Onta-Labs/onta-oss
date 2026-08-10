"""Resumable, bounded-concurrency row ingestion driver (COG-59).

Large row ingestion used to be a serial loop — one batch POSTed at a time with a
fixed ``asyncio.sleep(1)`` between them, no resume. A single ALB/idle timeout or
transient error failed the whole load, and re-running re-did everything.

This module drives ingestion as a **resumable job**:

* batches are posted with **bounded concurrency** (a semaphore caps in-flight
  POSTs and provides backpressure) instead of one-at-a-time;
* progress is **checkpointed** through a pluggable :class:`JobBackend` — the OSS
  default :class:`FileCheckpointStore` writes a local offset file, so a crash or
  restart resumes from the last committed batch instead of row 0;
* the checkpoint advances only over the **contiguous completed prefix**, so a
  resume never skips an un-posted batch even though batches finish out of order.

"Resume with no duplicates" relies on two things the rest of the pipeline already
guarantees: ``/ingest/csv/rows`` is **idempotent** (``apply_mapping`` mints
deterministic content-hash keys with ``(type, id)`` dedup, so re-posting a batch
is a no-op), and byte-batching is **deterministic** for a fixed file + budget, so
batch index *N* refers to the same rows across runs. Re-posting the partially
applied batch at the checkpoint boundary therefore cannot create duplicates.

The :class:`JobBackend` seam mirrors the ``register_governance_panel`` plugin
pattern: a future *proprietary* server-side backend (SQS queue + worker +
DynamoDB checkpoint) registers via :func:`register_job_backend`; OSS deployments
never do and fall back to the local file store. Pure stdlib + structlog.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Sized
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Protocol, runtime_checkable

import structlog

logger = structlog.stdlib.get_logger("cograph.resolver.ingest_runner")

#: A batch is a list of CSV rows; ``post_batch(batch, index)`` ships it.
Batch = list[dict[str, Any]]
PostBatch = Callable[[Batch, int], Awaitable[None]]
ProgressCb = Callable[[int, "int | None"], None]


@runtime_checkable
class JobBackend(Protocol):
    """Durable checkpoint store for a resumable ingest job.

    ``offset`` is the number of contiguously-completed batches: on resume the
    runner re-posts from this index, skipping everything before it. The OSS
    default is :class:`FileCheckpointStore`; a premium backend (SQS/DynamoDB)
    can implement the same three methods and register via
    :func:`register_job_backend`.
    """

    def load_offset(self, job_id: str) -> int: ...
    def save_offset(self, job_id: str, offset: int, *, total: int | None = None) -> None: ...
    def clear(self, job_id: str) -> None: ...


class FileCheckpointStore:
    """OSS default :class:`JobBackend` — one JSON file per job under a directory.

    Writes are atomic (temp file + ``os.replace``) so a crash mid-checkpoint
    can't corrupt the offset. Directory defaults to
    ``$OMNIX_INGEST_CHECKPOINT_DIR`` or ``~/.cograph/ingest-jobs``.
    """

    def __init__(self, directory: str | os.PathLike[str] | None = None):
        self._dir = Path(
            directory
            or os.environ.get("OMNIX_INGEST_CHECKPOINT_DIR")
            or (Path.home() / ".cograph" / "ingest-jobs")
        )

    def _path(self, job_id: str) -> Path:
        # job_id often embeds a tenant/kg (e.g. "demo-tenant:imdb") — sanitize
        # to a safe, collision-resistant filename.
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", job_id) or "job"
        return self._dir / f"{safe}.json"

    def load_offset(self, job_id: str) -> int:
        try:
            data = json.loads(self._path(job_id).read_text(encoding="utf-8"))
            return int(data.get("offset", 0))
        except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
            return 0

    def save_offset(self, job_id: str, offset: int, *, total: int | None = None) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(job_id)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({"job_id": job_id, "offset": offset, "total": total}),
            encoding="utf-8",
        )
        os.replace(tmp, path)  # atomic on POSIX + Windows

    def clear(self, job_id: str) -> None:
        self._path(job_id).unlink(missing_ok=True)


#: Module default backend (the place offsets land when no premium backend is
#: registered). Tests can construct their own pointed at a tmp dir.
_default_backend: JobBackend = FileCheckpointStore()
_registered_backend: JobBackend | None = None


def register_job_backend(backend: JobBackend | None) -> None:
    """Register (or clear, with ``None``) the ingest job checkpoint backend.

    Same plugin style as ``register_governance_panel`` / ``register_adapter``:
    a premium server-side job service calls this at startup; OSS deployments
    never do and fall back to the local :class:`FileCheckpointStore`.
    """
    global _registered_backend
    _registered_backend = backend
    logger.info(
        "ingest_job_backend_registered",
        backend=type(backend).__name__ if backend is not None else None,
    )


def job_backend() -> JobBackend:
    """The registered backend, or the OSS default file checkpoint store."""
    return _registered_backend if _registered_backend is not None else _default_backend


async def run_resumable_ingest(
    job_id: str,
    batches: Iterable[Batch],
    post_batch: PostBatch,
    *,
    concurrency: int = 4,
    on_progress: ProgressCb | None = None,
    backend: JobBackend | None = None,
    clear_on_complete: bool = False,
) -> int:
    """Post ``batches`` for ``job_id`` resumably, with bounded concurrency.

    Resumes from ``backend.load_offset(job_id)`` (skipping already-committed
    batches without re-posting), posts the remainder with at most
    ``concurrency`` in-flight POSTs, and advances the durable checkpoint over
    the contiguous completed prefix. ``on_progress(done, total)`` is called on
    each checkpoint advance (``total`` is the batch count when ``batches`` is
    sized, else ``None``).

    ``batches`` MUST be deterministic across runs for resume to be correct (the
    byte-batcher is — same file + budget → same batches). A ``post_batch`` that
    raises aborts the run with the checkpoint at the last contiguous success, so
    a re-run resumes from there. Returns the final committed offset.

    The committed offset persists after a successful full run, so re-running a
    completed job posts nothing (every batch index is below the checkpoint) —
    a true no-op. Set ``clear_on_complete=True`` to instead delete the
    checkpoint on completion (re-runs then start fresh; still dup-free, just
    redundant work).
    """
    backend = backend or job_backend()
    start = backend.load_offset(job_id)
    total = len(batches) if isinstance(batches, Sized) else None
    if concurrency < 1:
        concurrency = 1

    done_indices: set[int] = set()
    committed = start

    def commit() -> None:
        nonlocal committed
        advanced = False
        while committed in done_indices:
            done_indices.discard(committed)
            committed += 1
            advanced = True
        if advanced:
            backend.save_offset(job_id, committed, total=total)
            if on_progress:
                on_progress(committed, total)

    if on_progress:
        on_progress(committed, total)  # report the resume point up front

    async def post(index: int, batch: Batch) -> int:
        await post_batch(batch, index)
        return index

    pending: set[asyncio.Task[int]] = set()
    index_of: dict[asyncio.Task[int], int] = {}

    async def reap() -> None:
        # Wait for at least one in-flight task, fold completions into the
        # contiguous checkpoint. Re-raises the first failed POST.
        nonlocal pending
        finished, pending = await asyncio.wait(
            pending, return_when=asyncio.FIRST_COMPLETED
        )
        for task in finished:
            idx = index_of.pop(task)
            task.result()  # propagate POST failure → abort the run
            done_indices.add(idx)
        commit()

    try:
        for index, batch in enumerate(batches):
            if index < start:
                continue  # already committed on a previous run — don't re-post
            task = asyncio.create_task(post(index, batch))
            pending.add(task)
            index_of[task] = index
            if len(pending) >= concurrency:
                await reap()
        while pending:
            await reap()
    except BaseException:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        raise

    if clear_on_complete and (total is None or committed >= total):
        backend.clear(job_id)
    logger.info("csv_ingest_job_complete", job_id=job_id, committed=committed, total=total)
    return committed
