"""Delete / rewrite primitives for the shared write path (ADR 0007).

:func:`delete_facts` and :func:`rewrite_subject` are the only sanctioned
removal / rename entry points. Look up sibling / facade names via
:func:`_host` so tests that monkeypatch ``infona_client.graph.kg_writer.<name>``
keep working.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Optional

import structlog

from infona_client.graph.history import lexical_value
from infona_client.graph.scope import GraphScopeError

if TYPE_CHECKING:
    from infona_client.graph.store import GraphSession, GraphStore

logger = structlog.stdlib.get_logger("infona.graph.kg_writer")

Triple = tuple[str, str, str]


def _host():
    from infona_client.graph import kg_writer as _mod

    return _mod

async def delete_facts(
    neptune,
    instance_graph: str,
    *,
    subjects: Optional[list[str]] = None,
    triples: Optional[list[Triple]] = None,
    new_values: Optional[dict[tuple[str, str], str]] = None,
    touched_types: Iterable[str] = (),
    reason: str = "",
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
) -> int:
    """Remove instance facts from the KG — the single removal primitive (ADR 0007).

    The mirror of :func:`insert_facts`. Two removal shapes:

    * ``subjects`` — whole-entity removal (all props + incident rels in scope).
    * ``triples`` — specific ``(s, p, o)``; object ``None`` = predicate-scoped clear.

    **Dual-backend:** with ``store`` / ``session`` / ``INFONA_GRAPH_BACKEND=neo4j``,
    uses :mod:`pg_ops` (property-graph). Otherwise Neptune SPARQL (unchanged).

    Does NOT itself touch derived secondary indexes: call
    :func:`refresh_after_write` with ``deleted_subjects`` once per operation.
    """
    subjects = [s for s in (subjects or []) if s]
    all_triples = list(triples or [])
    concrete = [(s, p, o) for (s, p, o) in all_triples if o is not None and s and p]
    sp_pairs = [(s, p) for (s, p, o) in all_triples if o is None and s and p]

    gs = _host()._resolve_graph_session(
        store=store, session=session, instance_graph=instance_graph
    )
    return await _host()._delete_facts_store(
        gs,
        instance_graph,
        subjects=subjects,
        concrete=concrete,
        sp_pairs=sp_pairs,
        all_triples=all_triples,
        reason=reason,
        new_values=dict(new_values or {}),
    )


async def _delete_facts_store(
    session: "GraphSession",
    instance_graph: str,
    *,
    subjects: list[str],
    concrete: list[Triple],
    sp_pairs: list[tuple[str, str]],
    all_triples: list,
    reason: str,
    new_values: Optional[dict[tuple[str, str], str]] = None,
) -> int:
    """Property-graph delete path."""
    from infona_client.graph import pg_ops
    from infona_client.graph.facts import classify_triple
    from infona_client.graph.iri import ONTO_PRED_PREFIX

    removed = 0
    prov_on = _host()._provenance_enabled(store_path=True)
    new_values = new_values or {}

    # ONTA-236 / ONTA-536: record old→new ValueHistory BEFORE clearing, for
    # every predicate-scoped pair the caller declared a replacement value for.
    if _host()._value_history_enabled() and sp_pairs and new_values:
        try:
            await _host()._record_value_history_store(session, sp_pairs, new_values)
        except Exception:  # noqa: BLE001 — history never fails the delete
            logger.warning(
                "delete_facts_store_value_history_failed",
                instance_graph=instance_graph,
                exc_info=True,
            )

    # Concrete (s,p,o) — map to prop/rel deletes.
    for s, p, o in concrete:
        fact = classify_triple(s, p, o)
        if fact is None:
            continue
        if fact.kind == "rel":
            removed += await pg_ops.delete_rels(
                session, start_id=s, end_id_exact=str(o), attr_leaf=fact.key
            )
        elif fact.kind == "literal":
            removed += await pg_ops.delete_literals(session, s, [fact.key])
        elif fact.kind == "type":
            # Domain labels: Wave-1 best-effort — full entity delete covers multi-type cleanup.
            pass
        if prov_on and fact.kind in ("rel", "literal"):
            try:
                obj_repr = str(o) if o is not None else None
                await pg_ops.create_prov_event(
                    session,
                    event_type="tombstone",
                    subject_id=s,
                    attr=fact.key,
                    object_repr=obj_repr,
                    reason=reason,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "delete_facts_store_tombstone_failed",
                    instance_graph=instance_graph,
                    exc_info=True,
                )

    # Predicate-scoped clears.
    for s, p in sp_pairs:
        leaf = None
        if p.startswith(ONTO_PRED_PREFIX):
            leaf = p[len(ONTO_PRED_PREFIX) :]
            # onto/* could be rel or literal — clear both shapes.
            removed += await pg_ops.delete_rels(session, start_id=s, attr_leaf=leaf)
            try:
                removed += await pg_ops.delete_literals(session, s, [leaf])
            except GraphScopeError:
                pass
        elif "/attrs/" in p:
            leaf = p.rsplit("/attrs/", 1)[-1]
            if leaf and "/" not in leaf:
                removed += await pg_ops.delete_literals(session, s, [leaf])
        elif p.endswith("label") or p.endswith("#label"):
            leaf = "name"
            removed += await pg_ops.delete_literals(session, s, ["name"])
        if prov_on and leaf:
            try:
                await pg_ops.create_prov_event(
                    session,
                    event_type="tombstone",
                    subject_id=s,
                    attr=leaf,
                    reason=reason,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "delete_facts_store_tombstone_failed",
                    instance_graph=instance_graph,
                    exc_info=True,
                )

    for sid in subjects:
        # Record tombstone first (subject_id is the durable address; ABOUT links
        # while the Entity still exists when the store implements it).
        if prov_on:
            try:
                await pg_ops.create_prov_event(
                    session,
                    event_type="tombstone",
                    subject_id=sid,
                    reason=reason,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "delete_facts_store_prov_failed",
                    instance_graph=instance_graph,
                    exc_info=True,
                )
        removed += await pg_ops.delete_entity(session, sid)
    return removed


async def _record_value_history_store(
    session: "GraphSession",
    sp_pairs: list[tuple[str, str]],
    new_values: dict[tuple[str, str], str],
) -> None:
    """Property-graph ValueHistory writer (ONTA-236 / ONTA-536).

    For each predicate-scoped clear with a declared ``new_value``, read the
    current Assertion/Entity literal and, when it genuinely differs, append a
    ``:ValueHistory`` row via ``session.write_value_history``.
    """
    tracked = [(s, p) for (s, p) in sp_pairs if (s, p) in new_values]
    if not tracked:
        return
    write_vh = getattr(session, "write_value_history", None)
    if not callable(write_vh):
        return
    now = datetime.now(timezone.utc).isoformat()

    for s, p in tracked:
        new = new_values.get((s, p))
        if new is None:
            continue
        new_lex = lexical_value(new)
        # Resolve current value from Assertion SoT / entity props.
        old_lex: str | None = None
        leaf: str | None = None
        if "/attrs/" in p:
            cand = p.rsplit("/attrs/", 1)[-1]
            leaf = cand if cand and "/" not in cand else None
        elif "/onto/" in p:
            cand = p.rsplit("/onto/", 1)[-1]
            leaf = cand if cand and "/" not in cand else None
        elif p:
            leaf = p.rstrip("/").rsplit("/", 1)[-1] or None
        if leaf:
            # Prefer Assertion scan for the subject.
            read_a = getattr(session, "read_assertions_for_subject", None)
            rows: list = []
            if callable(read_a):
                try:
                    rows = list(await read_a(s))
                except Exception:  # noqa: BLE001
                    rows = []
            for row in rows:
                d = row.to_dict() if hasattr(row, "to_dict") else dict(row)
                prop = str(d.get("property_id") or "")
                prop_leaf = prop.rstrip("/").rsplit("/", 1)[-1]
                if prop_leaf != leaf and prop != p:
                    continue
                val = d.get("literal_value")
                if val is None:
                    val = d.get("object_id")
                if val is not None:
                    old_lex = lexical_value(str(val))
                    break
            if old_lex is None:
                # Fallback: entity property cache.
                get_e = getattr(session, "write_get_entity", None)
                if callable(get_e):
                    try:
                        ent = await get_e(s)
                    except Exception:  # noqa: BLE001
                        ent = None
                    if ent:
                        props = ent.get("properties") or ent.get("props") or ent
                        if isinstance(props, dict) and leaf in props:
                            old_lex = lexical_value(str(props[leaf]))
        if old_lex is None or old_lex == new_lex:
            continue
        await write_vh(
            subject_id=s,
            predicate=p,
            old_value=old_lex,
            new_value=new_lex,
            changed_at=now,
        )


async def rewrite_subject(
    neptune,
    instance_graph: str,
    old_uri: str,
    new_uri: str,
    *,
    touched_types: Iterable[str] = (),
    reason: str = "",
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
) -> None:
    """Rename a subject in place — the single URI-rewrite primitive (ADR 0007).

    Moves every fact referencing ``old_uri`` (as subject AND as object/endpoint)
    onto ``new_uri`` as ONE semantic event — **not** delete+insert — so derived
    indexes re-key cheaply. Dual-backend: GraphStore path re-keys Entity ``id`` +
    rel endpoints via :func:`pg_ops.rewrite_entity_id`; Neptune path uses
    ``rewrite_subject_update``. Provenance rewrite event gated by
    ``INFONA_PROVENANCE_ENABLED``. Also retargets ``:ValidityInterval`` rows
    whose ``subject`` is ``old_uri`` (re-keying ``interval_id = sha1(s|p|o)``);
    a failure there is logged and does not fail the URI rewrite.

    Does NOT itself touch derived secondary indexes: call
    :func:`refresh_after_write` with ``rewritten_subjects={old: new}`` once per
    rebuild batch so a single housekeeping pass re-keys them.
    """
    if not old_uri or not new_uri or old_uri == new_uri:
        return

    gs = _host()._resolve_graph_session(
        store=store, session=session, instance_graph=instance_graph
    )
    from infona_client.graph import pg_ops

    await pg_ops.rewrite_entity_id(gs, old_uri, new_uri)
    try:
        native = getattr(gs, "rewrite_validity_subject", None)
        if callable(native):
            await native(old_uri, new_uri)
    except Exception:  # noqa: BLE001 — closures must not fail the URI rewrite
        logger.warning(
            "rewrite_subject_validity_rekey_failed",
            instance_graph=instance_graph,
            old_uri=old_uri,
            new_uri=new_uri,
            exc_info=True,
        )
    if _host()._provenance_enabled(store_path=True):
        try:
            await pg_ops.create_prov_event(
                gs,
                event_type="rewrite",
                subject_id=new_uri,
                old_id=old_uri,
                new_id=new_uri,
                reason=reason,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "rewrite_subject_store_prov_failed",
                instance_graph=instance_graph,
                exc_info=True,
            )
