"""ONTA-410 — group_changelog_entries pure grouping.

Hundreds of rapid mid-ingest commits collapse into few groups; isolated
commits stay separate; empty input is empty (never an error).
"""

from __future__ import annotations

from cograph_client.graph.ontology_changelog import (
    ChangelogEntry,
    group_changelog_entries,
)
from cograph_client.models.ontology import ChangeKind, ChangeRecord


def _entry(
    *,
    n: int,
    ts: str,
    actor: str | None = "ingest",
    message: str | None = "schema discovery",
    action: str = "commit_ontology",
    kinds: list[ChangeKind] | None = None,
) -> ChangelogEntry:
    changes = [
        ChangeRecord(kind=k, type_name=f"T{n}")
        for k in (kinds or [ChangeKind.ADD_TYPE])
    ]
    return ChangelogEntry(
        entry_uri=f"https://graph.onta.sh/gov/log/{n:04d}",
        action=action,
        subject="https://graph.onta.sh/graphs/acme",
        timestamp=ts,
        tenant_id="acme",
        actor=actor,
        message=message,
        revision=n,
        changes=changes,
    )


def test_empty_changelog_yields_empty_groups():
    assert group_changelog_entries([]) == []


def test_single_entry_is_one_group():
    e = _entry(n=1, ts="2026-07-28T12:00:00Z")
    groups = group_changelog_entries([e])
    assert len(groups) == 1
    g = groups[0]
    assert g.count == 1
    assert g.start == g.end == "2026-07-28T12:00:00Z"
    assert g.actor == "ingest"
    assert g.message == "schema discovery"
    assert g.sample_actions == ("commit_ontology",)
    assert g.change_summary_counts == {"add_type": 1}
    assert g.entries == (e,)


def test_three_hundred_rapid_commits_collapse():
    """Exit criterion: 300 automatic mid-ingest revisions → few groups."""
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
    # Newest first: 1s apart, same job identity.
    entries = [
        _entry(
            n=300 - i,
            ts=(start + timedelta(seconds=299 - i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            actor="ingest",
            message="file-ingest-job-42",
        )
        for i in range(300)
    ]

    groups = group_changelog_entries(entries)
    assert len(groups) < 10, f"expected << 300 groups, got {len(groups)}"
    assert sum(g.count for g in groups) == 300
    # Same job identity → ideally a single group (all consecutive + same identity).
    assert len(groups) == 1
    assert groups[0].count == 300
    assert groups[0].actor == "ingest"
    assert groups[0].message == "file-ingest-job-42"
    assert groups[0].change_summary_counts["add_type"] == 300


def test_isolated_commits_stay_separate():
    a = _entry(
        n=3,
        ts="2026-07-28T15:00:00Z",
        actor="alice",
        message="rename phone",
    )
    b = _entry(
        n=2,
        ts="2026-07-28T10:00:00Z",
        actor="bob",
        message="add Person",
    )
    c = _entry(
        n=1,
        ts="2026-07-27T08:00:00Z",
        actor="carol",
        message="seed types",
    )
    groups = group_changelog_entries([a, b, c])
    assert len(groups) == 3
    assert [g.count for g in groups] == [1, 1, 1]
    assert [g.actor for g in groups] == ["alice", "bob", "carol"]


def test_time_window_groups_without_shared_message():
    """Rapid consecutive commits with no shared job identity still collapse."""
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
    entries = [
        _entry(
            n=3 - i,
            ts=(t0 - timedelta(seconds=i * 10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            actor=None,
            message=None,
        )
        for i in range(3)
    ]
    groups = group_changelog_entries(entries, window_s=60)
    assert len(groups) == 1
    assert groups[0].count == 3


def test_gap_beyond_window_splits_blank_identity():
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
    newer = _entry(
        n=2,
        ts=t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        actor=None,
        message=None,
    )
    older = _entry(
        n=1,
        ts=(t0 - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        actor=None,
        message=None,
    )
    groups = group_changelog_entries([newer, older], window_s=60)
    assert len(groups) == 2


def test_change_summary_counts_aggregate_kinds():
    e = _entry(
        n=1,
        ts="2026-07-28T12:00:00Z",
        kinds=[ChangeKind.ADD_TYPE, ChangeKind.ADD_ATTRIBUTE, ChangeKind.ADD_TYPE],
    )
    g = group_changelog_entries([e])[0]
    assert g.change_summary_counts == {"add_attribute": 1, "add_type": 2}
