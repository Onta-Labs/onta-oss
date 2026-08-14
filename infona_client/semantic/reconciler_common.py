"""Call-time ``_host()`` lookup so monkeypatches on ``reconciler`` keep working."""

from __future__ import annotations


def _host():
    """Call-time lookup of the public ``reconciler`` module.

    Tests monkeypatch names on ``infona_client.semantic.reconciler``
    (``_MAX_SCAN_PAGES``, ``_MAX_CANDIDACY_ATTRS_PER_RUN``,
    ``_UPSERT_BATCH_CHUNKS``, ``_ASSERTION_HISTORY_HARD_CAP``,
    ``_now_monotonic``, ``random.shuffle``, ``reconcile_kg``,
    ``run_embed_fill_sweep``, mutable memo state). Siblings must look these
    up at call time.
    """
    from infona_client.semantic import reconciler as _mod

    return _mod
