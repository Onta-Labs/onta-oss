"""Read-side sample flag for Blueprint sample facts (INF-591 / INF-587).

Install already writes sample rows through ``insert_facts`` with
``Fact.provenance="blueprint-sample"`` and ``source`` ending in ``#sample``.
The workspace lock lists those subjects. This module is the ONE classifier
those existing marks feed — not a second sample store.

Every surface that shows a record or an answer must call this. Sample is
never current. Tenant-confined: locks are keyed ``(tenant_id, blueprint_id)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from infona_client.blueprint.plan import SAMPLE_SOURCE_MARK
from infona_client.graph.iri import ENTITY_URI_PREFIX

SAMPLE_FLAG = "sample"
SAMPLE_VERDICT = "sample"


def is_sample_mark(
    source: str | None = None,
    provenance: str | None = None,
) -> bool:
    """True when Assertion ``source`` / ``provenance`` is the install sample mark."""
    if provenance == SAMPLE_SOURCE_MARK:
        return True
    src = (source or "").strip()
    if not src:
        return False
    if src == SAMPLE_SOURCE_MARK:
        return True
    if src.endswith("#sample"):
        return True
    return src.startswith("blueprint:") and "#sample" in src


@dataclass(frozen=True, slots=True)
class SampleIndex:
    """Tenant+KG sample subjects from the existing Blueprint lock.

    ``captured_at`` is the earliest capture date among matching pins so a
    mixed-pin KG still has one honest timestamp. Empty when this workspace
    has no sample in ``kg``.
    """

    subjects: frozenset[str] = field(default_factory=frozenset)
    captured_at: str | None = None
    captured_by_subject: Mapping[str, str | None] = field(default_factory=dict)

    def is_sample(self, subject: str | None) -> bool:
        if not subject:
            return False
        return subject in self.subjects

    def flags_for(self, subject: str | None) -> list[str]:
        return [SAMPLE_FLAG] if self.is_sample(subject) else []

    def captured_for(self, subject: str | None) -> str | None:
        if not subject:
            return self.captured_at
        return self.captured_by_subject.get(subject, self.captured_at)

    def count_for_type(self, type_name: str) -> int:
        if not type_name or not self.subjects:
            return 0
        prefix = f"{ENTITY_URI_PREFIX}{type_name}/"
        return sum(1 for s in self.subjects if s.startswith(prefix))


_EMPTY = SampleIndex()


async def sample_index_for_kg(tenant_id: str, kg: str) -> SampleIndex:
    """Load sample subjects for ``tenant_id`` / ``kg`` from the install lock.

    Fail-soft: a lock-store hiccup must not take down explore / ask. The
    fact-level ``is_sample_mark`` classifier still catches stamped Assertions.
    """
    if not tenant_id or not kg:
        return _EMPTY
    try:
        from infona_client.blueprint.lock import make_blueprint_lock_store

        locks = await make_blueprint_lock_store().list_for_tenant(tenant_id)
    except Exception:  # noqa: BLE001 — read-side mark; degrade to empty
        return _EMPTY
    subjects: set[str] = set()
    captured_by: dict[str, str | None] = {}
    captured_dates: list[str] = []
    for lock in locks:
        if lock.kg != kg or not lock.sample_included:
            continue
        captured = lock.sample_captured_at
        if captured:
            captured_dates.append(captured)
        for subject in lock.sample_subjects:
            subjects.add(subject)
            captured_by[subject] = captured
    if not subjects:
        return _EMPTY
    captured_at = min(captured_dates) if captured_dates else None
    return SampleIndex(
        subjects=frozenset(subjects),
        captured_at=captured_at,
        captured_by_subject=captured_by,
    )


def mark_record(
    row: dict[str, Any],
    index: SampleIndex,
    *,
    subject: str | None = None,
) -> dict[str, Any]:
    """Stamp Explorer row flags. ``sample_is_current`` is always false."""
    sid = subject if subject is not None else str(row.get("id") or "")
    if not index.is_sample(sid):
        return row
    flags = [f for f in (row.get("flags") or []) if f != SAMPLE_FLAG]
    flags.append(SAMPLE_FLAG)
    row["flags"] = flags
    row["sample_is_current"] = False
    captured = index.captured_for(sid)
    if captured:
        row["sample_captured_at"] = captured
    return row


def mark_records(
    rows: Iterable[Mapping[str, Any]],
    index: SampleIndex,
) -> list[dict[str, Any]]:
    return [mark_record(dict(row), index) for row in rows]


def sample_status_label(
    *,
    included: bool,
    captured_at: str | None,
    sample_is_current: bool | None = None,
) -> str:
    """Human line. ``sample_is_current`` is ignored so a lying payload cannot win."""
    _ = sample_is_current  # a lying payload cannot win
    if not included:
        return "No sample"
    if captured_at:
        return f"Sample · not current · captured {captured_at}"
    return "Sample · not current"


def sample_answer_note(captured_at: str | None) -> str:
    captured = captured_at or "unknown"
    return f"Sample, captured {captured}, not current."


__all__ = [
    "SAMPLE_FLAG",
    "SAMPLE_SOURCE_MARK",
    "SAMPLE_VERDICT",
    "SampleIndex",
    "is_sample_mark",
    "mark_record",
    "mark_records",
    "sample_answer_note",
    "sample_index_for_kg",
    "sample_status_label",
]
