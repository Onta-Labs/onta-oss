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

**Reading the snapshot (ONTA-534).** The snapshot used to be read with ONE
SPARQL ``SELECT``. Under the shipped Neo4j GraphStore the SPARQL HTTP client is
retired and raises ``SparqlClientRetired`` unconditionally, and this module's
``except`` turned that raise into ``{}`` — which :func:`diff_snapshots` reads as
"couldn't read this fire" and reports as NO changes. The result was the worst
failure a standing alert can have: a user sets a watch, gets a 200, and it can
never fire, indefinitely, with nothing surfaced. :func:`snapshot_watch` now reads
the structured ``cells`` form from the GraphStore first (via
:mod:`infona_client.graph.explore_store`) and keeps the SPARQL ``SELECT`` as the
residual arm.

The conservative direction is DELIBERATE and is preserved exactly: an unreadable
snapshot must never fire a false alarm, so every path that cannot answer returns
``{}`` rather than guessing. What changed is only that the probe can now ANSWER
— the fix is a working read, not a looser alarm. Concretely, the store arm
declines (falls through to SPARQL) rather than returning a PARTIAL map: a partial
snapshot would be persisted as the next baseline with keys silently missing, and
those keys would then reappear as ``added`` on a later fire — a false alarm one
tick later. The raw-``sparql`` authoring mode has no store equivalent at all and
still degrades to ``{}``; it is logged so the degradation is greppable instead of
silent.

Boundary: OSS. Imports only stdlib / ``infona_client.*``. No ``from infona.*``.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog

logger = structlog.stdlib.get_logger("infona.scheduling.watch")

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


def _join(multi: dict[str, list[str]]) -> dict[str, str]:
    """Deterministic join so a multi-valued cell compares by content, not row order."""
    return {k: "|".join(sorted(vs)) for k, vs in multi.items()}


def _cell_leaf(predicate: str) -> Optional[str]:
    """Attribute leaf a watch cell's predicate IRI names, or ``None``.

    Uses the SAME classifier the write path uses (``graph/facts.classify_triple``)
    so a watch cell resolves to exactly the key ``insert_facts`` stored the value
    under — ``types/<T>/attrs/<leaf>`` → ``leaf``, ``onto/<leaf>`` → ``leaf``,
    ``rdfs:label`` → ``name``. The placeholder object only exists to satisfy the
    classifier's signature; only ``Fact.key`` is used, and the caller looks the
    leaf up in BOTH literal properties and relationships, so the placeholder's
    literal-vs-rel classification is not load-bearing.
    """
    from infona_client.graph.facts import classify_triple

    if not isinstance(predicate, str) or not predicate.strip():
        return None
    try:
        fact = classify_triple("urn:infona:watch-probe", predicate.strip(), "x")
    except Exception:  # noqa: BLE001 — a malformed predicate is just unwatchable
        return None
    return fact.key if fact is not None else None


def _detail_values(detail: Any, leaf: str) -> list[str]:
    """Every current value of ``leaf`` on one entity, as comparable strings.

    Literal properties and relationship targets are both collected: a watch cell
    names a (subject, attribute) pair without saying which kind it is, and the
    SPARQL arm it replaces was equally kind-agnostic (``?value`` bound whatever
    the object was). ``name`` / ``source`` come off the Entity record itself
    because :func:`~infona_client.graph.explore_store._public_properties` strips
    them from the property map as structural fields.
    """
    out: list[str] = []
    if leaf == "name":
        if detail.name:
            out.append(str(detail.name))
    elif leaf == "source":
        if detail.source:
            out.append(str(detail.source))
    raw = (detail.properties or {}).get(leaf)
    if isinstance(raw, (list, tuple, set)):
        out.extend("" if v is None else str(v) for v in raw)
    elif raw is not None:
        out.append(str(raw))
    for rel in detail.outgoing or ():
        if rel.attr == leaf and rel.other_id:
            out.append(str(rel.other_id))
    return out


async def _store_snapshot(
    watch: dict, instance_graph: Optional[str], *, store: Any = None
) -> Optional[dict[str, str]]:
    """The ``cells`` watch read against the GraphStore, or ``None`` to decline.

    ``None`` means "the store had nothing to say" — the raw-``sparql`` authoring
    mode (which has no store equivalent), a graph URI that is not a per-KG
    instance graph, an unconfigured store, a store error, or a read that produced
    no value at all. The caller then keeps its residual SPARQL arm rather than
    inventing an answer, mirroring
    :meth:`infona_client.nlp.pipeline_active_types.PipelineActiveTypesMixin._store_instance_types`.

    A read error on ANY watched subject declines the WHOLE snapshot rather than
    returning what was read so far. A partial map is the dangerous shape here: it
    would be persisted as the next baseline minus the keys that failed, and those
    keys would come back as ``added`` on the following fire — a false alarm, which
    is precisely the direction this module refuses to fail in.
    """
    if watch.get("sparql"):
        # ``_watch_sparql`` prefers a raw SELECT over ``cells``; matching that
        # here keeps the two arms answering the same authored question.
        return None
    cells = watch.get("cells")
    if not isinstance(cells, list) or not cells:
        return None

    from infona_client.graph.queries import parse_kg_graph_uri

    parsed = parse_kg_graph_uri(instance_graph or "")
    if not parsed:
        return None
    tenant_id, kg = parsed

    # subject → [(snapshot key, attribute leaf)], so an entity is read ONCE no
    # matter how many of its cells the watch names.
    wanted: dict[str, list[tuple[str, str]]] = {}
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        key = cell.get("key")
        subject = cell.get("subject")
        leaf = _cell_leaf(cell.get("predicate") or "")
        if not (key and subject and leaf):
            continue
        wanted.setdefault(str(subject), []).append((str(key), leaf))
    if not wanted:
        return None

    try:
        from infona_client.graph.explore_store import (
            get_entity_detail_pg,
            resolve_explore_session,
        )

        session = resolve_explore_session(store=store, tenant_id=tenant_id, kg=kg)
    except Exception as exc:  # noqa: BLE001 — no store → keep the SPARQL arm
        logger.debug(
            "watch_snapshot_store_unavailable",
            instance_graph=instance_graph,
            error=str(exc),
        )
        return None

    multi: dict[str, list[str]] = {}
    for subject, keyed_leaves in wanted.items():
        try:
            detail = await get_entity_detail_pg(session, subject)
        except Exception:  # noqa: BLE001 — decline whole, never partial
            logger.warning(
                "watch_snapshot_store_read_failed",
                instance_graph=instance_graph,
                subject=subject,
                exc_info=True,
            )
            return None
        if detail is None:
            continue
        for key, leaf in keyed_leaves:
            for value in _detail_values(detail, leaf):
                multi.setdefault(key, []).append(value)
    # An empty map is indistinguishable from "this store knows nothing about the
    # watch", so decline rather than hand back a confidently empty snapshot.
    return _join(multi) or None


async def snapshot_watch(
    neptune: Any,
    watch: dict,
    instance_graph: Optional[str],
    *,
    store: Any = None,
) -> dict[str, str]:
    """Read the current watched value(s) into a comparable ``{key: value}`` map.

    Reads the structured ``cells`` form from the GraphStore first
    (:func:`_store_snapshot`); when the store declines, falls back to the residual
    SPARQL ``SELECT`` and reduces its ``?key``/``?value`` bindings the same way.
    Multiple values for one key are joined deterministically (sorted, ``|``) so
    the snapshot is stable across query-order nondeterminism — a set of routed
    models or affiliations compares by content, not row order.

    Never raises: any store/Neptune/parse error yields ``{}``, which
    :func:`diff_snapshots` treats as "couldn't read" → no spurious change. That
    conservatism is the point (ONTA-235) and survives the ONTA-534 port intact —
    the port only makes the probe able to ANSWER on the shipped backend, where
    the SPARQL arm raises unconditionally.
    """
    if not isinstance(watch, dict):
        return {}
    from_store = await _store_snapshot(watch, instance_graph, store=store)
    if from_store is not None:
        return from_store
    sparql = _watch_sparql(watch, instance_graph)
    if not sparql:
        return {}
    try:
        data = await neptune.query(sparql)
    except Exception:  # noqa: BLE001 — a read hiccup must not fire a false alarm
        logger.warning(
            "watch_snapshot_query_failed",
            instance_graph=instance_graph,
            # Raw-SPARQL watches have no GraphStore port, so on the shipped
            # backend this arm is the only one and it always raises. Logged so a
            # permanently-silent alert is greppable rather than invisible.
            raw_sparql_watch=bool(watch.get("sparql")),
            exc_info=True,
        )
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
    return _join(multi)


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
