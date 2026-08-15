"""Supersede / retract apply path for P6 mutation ops (ONTA-277).

Writes go through ``insert_facts`` / ``delete_facts`` / ``refresh_after_write``.
Look up those primitives via :func:`_host` so tests that monkeypatch
``infona_client.pipeline.mutations.<name>`` keep working.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog

from infona_client.graph.kg_writer import build_graph_delta
from infona_client.graph.provenance import (
    build_retraction_triples,
    build_supersession_triples,
)
from infona_client.graph.suppression import build_suppression_triples
from infona_client.graph.validity import (
    STATUS_RETRACTED,
    STATUS_SUPERSEDED,
    build_closed_interval_triples,
    build_open_interval_triples,
    fetch_current_object_terms,
    statement_id,
)
from infona_client.pipeline.mutations_policy import (
    DEFAULT_RECENCY_POLICY,
    MutationReceipt,
    RecencyPolicy,
    Triple,
    _host,
    _predicate_leaf,
    _provenance_enabled,
    _scope,
)

logger = structlog.stdlib.get_logger("infona.pipeline.mutations")


async def supersede_fact(
    neptune,
    instance_graph: str,
    *,
    subject: str,
    predicate: str,
    new_value: str,
    type_name: str,
    old_value: Optional[str] = None,
    observed_at: Optional[datetime] = None,
    run_id: Optional[str] = None,
    reason: str = "",
    tenant_id: Optional[str] = None,
    kg_name: Optional[str] = None,
    policy: RecencyPolicy = DEFAULT_RECENCY_POLICY,
    provenance_triples: Optional[list[Triple]] = None,
    manifest=None,
) -> MutationReceipt:
    """Assert ``(subject, predicate, new_value)`` as the current fact, closing the
    prior value's validity interval when the attribute is functional.

    Flow (functional / single-valued attribute — the policy default):

    1. Discover the current value(s) of ``(subject, predicate)`` — either the
       caller-supplied ``old_value`` or, if omitted, the currently-valid objects
       read from the instance graph (:func:`fetch_current_object_terms`), each
       term reconstructed exactly so a typed literal closes correctly.
    2. In ONE :func:`insert_facts` call: write the new instance fact, OPEN a
       validity interval for it (``valid_from = observed_at``), and CLOSE each
       superseded value's interval (``valid_to = observed_at`` + ``superseded_by``
       pointer) — the superseded instance triple is UNTOUCHED. Optionally record a
       governance ``supersede`` event in the provenance graph (gated). The call
       returns the A6 :class:`GraphDelta` receipt of the new fact.
    3. One :func:`refresh_after_write` for the touched type.

    When the policy marks the attribute MULTI-VALUED, no interval is closed: the
    new value COEXISTS (append-only) — the same insert + open-interval, with
    ``superseded = ()`` and ``coexisted = True``.

    ``observed_at`` is the valid-time the new fact takes effect (defaults to now);
    ``run_id`` threads the A6 receipt identity. Never deletes or re-points the old
    edge — that banned ghost-edge mechanism is exactly what this op avoids.
    """
    at = observed_at or datetime.now(timezone.utc)
    leaf = _predicate_leaf(predicate)
    coexist = not policy.supersedes(type_name, leaf)

    # 1. Which current values does this new fact retire? (single-valued only)
    to_close: list[str] = []
    if not coexist:
        if old_value is not None:
            to_close = [old_value]
        else:
            to_close = await fetch_current_object_terms(neptune, instance_graph, subject, predicate)
        # Never close the value we are (re-)asserting as current.
        to_close = [o for o in to_close if o != new_value]

    # 2. Build the write: new instance fact + companion validity (+ provenance).
    instance_triples: list[Triple] = [(subject, predicate, new_value)]
    validity_triples: list[Triple] = list(
        build_open_interval_triples(
            subject, predicate, new_value, valid_from=at, graph_uri=instance_graph
        )
    )
    prov_triples: list[Triple] = list(provenance_triples or [])
    superseded: list[Triple] = []
    for old in to_close:
        superseded.append((subject, predicate, old))
        validity_triples.extend(
            build_closed_interval_triples(
                subject,
                predicate,
                old,
                valid_to=at,
                # Point at the replacement fact's statement id, so history can
                # follow old → new (matches the provenance supersede event).
                superseded_by=statement_id(subject, predicate, new_value),
                status=STATUS_SUPERSEDED,
                graph_uri=instance_graph,
            )
        )
        if _provenance_enabled():
            prov_triples.extend(
                build_supersession_triples(
                    subject,
                    predicate,
                    old,
                    new_value,
                    graph_uri=instance_graph,
                    reason=reason,
                    timestamp=at,
                    touched_types=[type_name] if type_name else (),
                )
            )

    delta = await _host().insert_facts(
        neptune,
        instance_graph,
        instance_triples,
        provenance_triples=prov_triples or None,
        validity_triples=validity_triples or None,
        # We OPEN an interval for new_value (both the supersede and coexist paths),
        # so clear any prior closure off its node — a value re-asserted after being
        # superseded/retracted must become current again (ONTA-277 resurrection).
        reopen_facts=[(subject, predicate, new_value)],
        run_id=run_id,
    )

    # 3. One post-write refresh for the touched type (best-effort; skipped for a
    #    non-KG stub graph — the write itself already landed).
    scope = _scope(instance_graph, tenant_id, kg_name)
    if scope is not None:
        await _host().refresh_after_write(
            neptune,
            tenant_id=scope[0],
            kg_name=scope[1],
            affected_types=[type_name] if type_name else (),
        )

    if manifest is not None:
        manifest.record_completed(ref=subject)

    logger.info(
        "supersede_fact",
        subject=subject,
        predicate=predicate,
        superseded=len(superseded),
        coexisted=coexist,
    )
    return MutationReceipt(
        op="supersede",
        graph_delta=delta if delta is not None else build_graph_delta(instance_graph, instance_triples, run_id=run_id),
        inserted=tuple(instance_triples),
        superseded=tuple(superseded),
        coexisted=coexist,
    )


async def retract_fact(
    neptune,
    instance_graph: str,
    *,
    subject: str,
    predicate: str,
    type_name: str,
    value: Optional[str] = None,
    observed_at: Optional[datetime] = None,
    run_id: Optional[str] = None,
    reason: str = "",
    tenant_id: Optional[str] = None,
    kg_name: Optional[str] = None,
    hard_delete: bool = False,
    manifest=None,
) -> MutationReceipt:
    """Explicitly retire a fact — assert ``(subject, predicate, value)`` is
    no-longer-true, distinct from supersession (no replacement fact drives it).

    Default (``hard_delete=False``, PREFERRED): CLOSE the fact's validity interval
    (``valid_to = observed_at``, ``status = retracted``) so a "current facts" query
    stops citing it while a history query still returns it — the instance triple is
    UNTOUCHED. A governance ``retract`` event is recorded in the provenance graph
    (gated). When ``value`` is omitted, every currently-valid object of
    ``(subject, predicate)`` is retracted.

    Opt-in (``hard_delete=True``): genuinely remove the instance triple(s) via
    ``kg_writer.delete_facts`` (which writes a provenance tombstone) — the only
    sanctioned removal path. Use only when the design truly needs the triple gone;
    prefer interval-close so history stays queryable.

    Either way, a retraction ALSO writes a STICKY SUPPRESSION marker (ONTA-279) for
    each ``(subject, predicate, value)`` retracted, via
    ``insert_facts(suppression_triples=…)`` into the companion suppression graph.
    Unlike the validity closure (which a later re-assertion's ``reopen_facts``
    clears), the suppression marker is reopen-PROOF: it keeps a refresh/re-scrape
    from silently re-acquiring a value the user retracted, until an explicit
    un-suppress. The instance triple itself is untouched (soft) or deleted (hard) —
    suppression is an orthogonal governance signal, not a removal.

    Returns a :class:`MutationReceipt`; a retraction adds no facts, so its
    ``graph_delta`` is an empty-facts A6 receipt (the closure/removal is recorded
    in the validity/provenance companions and, for hard-delete, in ``removed``).
    """
    at = observed_at or datetime.now(timezone.utc)

    # Which values are being retracted? (explicit value, or all current values.)
    if value is not None:
        targets = [value]
    else:
        targets = await fetch_current_object_terms(neptune, instance_graph, subject, predicate)

    removed = 0
    retracted: list[Triple] = []

    # Sticky, reopen-PROOF suppression marker for every retracted value (ONTA-279),
    # written on BOTH the soft (interval-close) and hard (delete) paths so a later
    # refresh can never silently re-acquire a retracted value.
    sup_triples: list[Triple] = []
    for v in targets:
        sup_triples.extend(
            build_suppression_triples(
                subject,
                predicate,
                v,
                suppressed_at=at,
                reason=reason or "retract",
                graph_uri=instance_graph,
            )
        )

    if hard_delete:
        # Genuine removal — the ONE sanctioned removal primitive (writes a tombstone).
        del_triples = [(subject, predicate, v) for v in targets]
        if del_triples:
            removed = await _host().delete_facts(
                neptune,
                instance_graph,
                triples=del_triples,
                touched_types=[type_name] if type_name else (),
                reason=reason or "retract (hard delete)",
            )
            retracted = list(del_triples)
        # Write the suppression markers through the shared insert primitive (no
        # instance facts) so a hard-deleted value also stays off the refresh rail.
        if sup_triples:
            await _host().insert_facts(
                neptune, instance_graph, [], suppression_triples=sup_triples
            )
        # No instance facts added → an empty A6 receipt with the run identity.
        delta = build_graph_delta(instance_graph, [], run_id=run_id)
    else:
        # Preferred path: close the interval, keep the triple queryable as history.
        validity_triples: list[Triple] = []
        prov_triples: list[Triple] = []
        for v in targets:
            retracted.append((subject, predicate, v))
            validity_triples.extend(
                build_closed_interval_triples(
                    subject,
                    predicate,
                    v,
                    valid_to=at,
                    status=STATUS_RETRACTED,
                    graph_uri=instance_graph,
                )
            )
            if _provenance_enabled():
                prov_triples.extend(
                    build_retraction_triples(
                        subject,
                        predicate,
                        v,
                        graph_uri=instance_graph,
                        reason=reason,
                        timestamp=at,
                        touched_types=[type_name] if type_name else (),
                    )
                )
        # Route the companion writes through the shared insert primitive (no
        # instance facts) → empty-facts A6 receipt with the run identity. The
        # suppression markers ride the same write so the retraction is atomic.
        delta = await _host().insert_facts(
            neptune,
            instance_graph,
            [],
            provenance_triples=prov_triples or None,
            validity_triples=validity_triples or None,
            suppression_triples=sup_triples or None,
            run_id=run_id,
        )
        if delta is None:
            delta = build_graph_delta(instance_graph, [], run_id=run_id)

    scope = _scope(instance_graph, tenant_id, kg_name)
    if scope is not None:
        await _host().refresh_after_write(
            neptune,
            tenant_id=scope[0],
            kg_name=scope[1],
            affected_types=[type_name] if type_name else (),
        )

    if manifest is not None:
        manifest.record_completed(ref=subject)

    logger.info(
        "retract_fact",
        subject=subject,
        predicate=predicate,
        retracted=len(retracted),
        hard_delete=hard_delete,
    )
    return MutationReceipt(
        op="retract",
        graph_delta=delta,
        retracted=tuple(retracted),
        removed=removed,
    )
