"""Process-local mutable Explorer state.

Same objects are re-exported from :mod:`infona_client.api.routes.explore`
so ``explore._summary_cache.clear()`` and inflight coalescing keep working
after the extract. Do not replace these names on a sibling module.
"""

from __future__ import annotations

_summary_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
_bg_tasks: set = set()
_recompute_inflight: set[tuple[str, str]] = set()
_recompute_pending: set[tuple[str, str]] = set()
