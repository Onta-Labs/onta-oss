"""Change/delta detection for a ``notify`` schedule (ONTA-235).

A ``notify`` schedule WATCHES value(s) in a KG and delivers a notification ONLY
when they change since the previous fire. This module owns the watch semantics:

1. **Snapshot** — on each fire, read the current watched value(s) into a stable,
   comparable form (:func:`snapshot_watch`). The watch descriptor is
   domain-agnostic: it names one or more (subject, attribute) cells, or carries a
   raw ``SELECT ?key ?value`` SPARQL that yields a value map. Works for ANY type /
   ANY attribute — no persona-specific fields are hardcoded.
2. **Diff** — compare the fresh snapshot against the one persisted on the schedule
   row from last fire (:func:`diff_snapshots`), yielding a list of per-key
   ``old → new`` changes (added / removed / changed).
3. **Persist** — the caller writes the fresh snapshot back onto the row so the
   NEXT fire diffs against it.

The snapshot lives in ``schedule.params['last_snapshot']`` (a plain JSON dict of
``{key: value}``), so it rides the existing ``Schedule`` model + store with no
schema change — the row is the durable, per-tenant watch state.

Boundary: OSS. Imports only stdlib / ``cograph_client.*``. No ``from cograph.*``.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog

logger = structlog.stdlib.get_logger("cograph.scheduling.watch")

#: Where the last-fire snapshot is stashed on a schedule's params (a flat
#: ``{key: value}`` JSON map). Read/written by dispatch on every ``notify`` fire.
SNAPSHOT_KEY = "last_snapshot"


def _first_binding_value(binding: dict, var: str) -> Optional[str]:
    """Extract the ``.value`` of a SPARQL result binding var, or ``None``."""
    cell = binding.get(var)
    if isinstance(cell, dict):
        v = cell.get("value")
        return None if v is None else str(v)
    return None


def _watch_sparql(watch: dict, instance_graph: Optional[str]) -> Optional[str]:
    """Build (or pass through) the SELECT that reads the watched value(s).

    Two authoring modes, both domain-agnostic:

    - ``watch['sparql']`` — a raw ``SELECT ?key ?value ...`` the caller supplies.
      Used verbatim (the watch is fully user-authored). This is the general escape
      hatch that makes the mechanism work for ANY query.
    - ``watch['cells']`` — a list of ``{key, subject, predicate}`` descriptors; we
      assemble a UNION SELECT that reads each cell's current object literal. This
      is the structured convenience form for "watch these specific attributes".

    Returns ``None`` when neither is present (nothing to watch → no delta, no
    delivery), so a malformed watch degrades quietly.
    """
    raw = watch.get("sparql")
    if isinstance(raw, str) and raw.strip():
        return raw

    cells = watch.get("cells")
    if not isinstance(cells, list) or not cells:
        return None

    graph_clause = f"FROM <{instance_graph}>\n" if instance_graph else ""
    blocks: list[str] = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        key = cell.get("key")
        subject = cell.get("subject")
        predicate = cell.get("predicate")
        if not (key and subject and predicate):
            continue
        # A single cell → one BIND(key) + the object literal. Escape the key for
        # the SPARQL string literal (quotes/backslashes) so an odd key can't break
        # the query; subject/predicate are IRIs the caller controls.
        safe_key = str(key).replace("\\", "\\\\").replace('"', '\\"')
        blocks.append(
            "  {\n"
            f'    BIND("{safe_key}" AS ?key)\n'
            f"    <{subject}> <{predicate}> ?value .\n"
            "  }"
        )
    if not blocks:
        return None
    return (
        "SELECT ?key ?value\n"
        f"{graph_clause}"
        "WHERE {\n" + "\n  UNION\n".join(blocks) + "\n}"
    )


async def snapshot_watch(
    neptune: Any, watch: dict, instance_graph: Optional[str]
) -> dict[str, str]:
    """Read the current watched value(s) into a comparable ``{key: value}`` map.

    Runs the watch SELECT and reduces its ``?key``/``?value`` bindings to a plain
    dict. Multiple values for one key are joined deterministically (sorted, ``|``)
    so the snapshot is stable across query-order nondeterminism — a set of routed
    models or affiliations compares by content, not row order. Never raises: any
    Neptune/parse error yields ``{}`` (treated as "couldn't read" → no spurious
    change, see :func:`diff_snapshots`).
    """
    if not isinstance(watch, dict):
        return {}
    sparql = _watch_sparql(watch, instance_graph)
    if not sparql:
        return {}
    try:
        data = await neptune.query(sparql)
    except Exception:  # noqa: BLE001 — a read hiccup must not fire a false alarm
        logger.warning("watch_snapshot_query_failed", exc_info=True)
        return {}
    bindings = (data or {}).get("results", {}).get("bindings", []) or []
    multi: dict[str, list[str]] = {}
    for b in bindings:
        if not isinstance(b, dict):
            continue
        key = _first_binding_value(b, "key")
        value = _first_binding_value(b, "value")
        if key is None:
            continue
        multi.setdefault(key, []).append("" if value is None else value)
    # Deterministic join so a multi-valued cell compares by content, not row order.
    return {k: "|".join(sorted(vs)) for k, vs in multi.items()}


def diff_snapshots(
    previous: Optional[dict], current: dict
) -> list[dict[str, Any]]:
    """Return the per-key changes between two snapshots as ``old → new`` records.

    Each change is ``{"key", "old", "new", "change"}`` where ``change`` is one of
    ``added`` / ``removed`` / ``changed``. Semantics:

    - ``previous is None`` (a schedule that has never fired) → NO changes. The
      first fire only ESTABLISHES the baseline; it must not deliver a spurious
      "everything is new" alert. (The caller still persists the baseline.)
    - An EMPTY ``current`` while ``previous`` had keys is treated as "couldn't read
      this fire" (snapshot_watch returns ``{}`` on error/absence) and yields NO
      changes — we never report a mass "removed" on a transient read failure.
    - Otherwise: keys only in ``current`` are ``added``; keys whose value differs
      are ``changed``. Removals are intentionally NOT reported for a non-empty
      current (a watched cell disappearing is rare and ambiguous vs a read gap);
      the mechanism reports appearances + value changes, which is what the two
      target flows need (a price changed, a new deprecation date, a physician
      changed practice). This keeps false positives near zero.
    """
    if previous is None:
        return []
    if not current:
        # Couldn't read the watched values this fire — do not fabricate removals.
        return []
    changes: list[dict[str, Any]] = []
    for key, new in current.items():
        old = previous.get(key)
        if old is None:
            changes.append(
                {"key": key, "old": None, "new": new, "change": "added"}
            )
        elif old != new:
            changes.append(
                {"key": key, "old": old, "new": new, "change": "changed"}
            )
    return changes


__all__ = [
    "SNAPSHOT_KEY",
    "diff_snapshots",
    "snapshot_watch",
]
