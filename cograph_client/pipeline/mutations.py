"""P6 mutation ops — supersession & retraction (ONTA-277).

P6 is the sole KG writer. Until now it could only ADD facts: a refresh that
brought a new CEO left the old ``hasCEO`` edge in place, and the answer layer (P7)
would then cite the stale one. Freshness that ADDS without SUPERSEDING makes the
graph worse over time. This module gives P6 the ability to *retire* facts:

- **Supersession** (:func:`supersede_fact`) — a NEWER fact for the same
  subject+functional-attribute CLOSES the old fact's validity interval. The old
  fact is no longer *current* (a "current facts" query stops citing it) but STAYS
  in the graph, queryable as history, marked with a closed interval +
  ``superseded_by`` pointer.
- **Retraction** (:func:`retract_fact`) — an explicit assertion that a fact is
  no-longer-true, distinct from supersession (which is driven by a replacement).
  The default path closes the fact's currency (history stays queryable); an opt-in
  ``hard_delete`` genuinely removes the instance triple via ``delete_facts``.
- **Conflict resolution** (:func:`write_with_conflict_resolution`, ONTA-276) — when
  an incoming fact CONTRADICTS the existing current value on a FUNCTIONAL attribute
  (revenue $10M vs $12M), a deterministic policy (``pipeline/conflict.py``) picks
  the WINNER by authority + confidence + recency and CLOSES the loser's interval
  with ``STATUS_DEPRECATED`` — the loser stays present-but-not-current, queryable
  WITH its provenance and the reason it lost. Same closure mechanism as
  supersession (never a delete), driven by a stronger CONTEMPORANEOUS source rather
  than a newer replacement.

**The banned ghost-edge mechanism (do NOT reintroduce).** Supersession closes a
validity interval — it does NOT delete or re-point the superseded edge as a
cleanup hack. The superseded triple remains in the instance graph exactly as
written; only a companion validity node (``graph/validity.py``) records that its
interval closed. This keeps history and lineage intact.

**Orchestration, not hand-rolled writes.** These ops compose the kg_writer
primitives — ``insert_facts`` (the new fact + companion validity/provenance
triples, returning the A6 :class:`~cograph_client.graph.kg_writer.GraphDelta`
receipt), ``delete_facts`` (only on the opt-in hard-delete path), and one
``refresh_after_write`` per op. They construct NO raw SPARQL and touch NO graph
directly; the valid-time interval triples and governance events are built by the
``graph/validity.py`` and ``graph/provenance.py`` builders and routed to their
companion graphs by ``insert_facts``. This is why a mutation stays on the shared
write path the convergence guard enforces.

Boundary: OSS. Imports only stdlib / ``cograph_client.*``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

import structlog

from cograph_client.api_registry.spec import AuthorityLevel
from cograph_client.graph.kg_writer import (
    GraphDelta,
    build_graph_delta,
    delete_facts,
    insert_facts,
    refresh_after_write,
    rewrite_subject,
)
from cograph_client.graph.ontology_queries import OMNIX_ONTO
from cograph_client.graph.provenance import (
    build_conflict_loss_triples,
    build_provenance_triples,
    build_merge_lineage_triples,
    build_retraction_triples,
    build_split_triples,
    build_supersession_triples,
    fetch_provenance,
    fetch_merge_lineage,
)
from cograph_client.graph.queries import _escape_value, parse_kg_graph_uri
from cograph_client.graph.suppression import build_suppression_triples
from cograph_client.graph.validity import (
    STATUS_DEPRECATED,
    STATUS_RETRACTED,
    STATUS_SUPERSEDED,
    build_closed_interval_triples,
    build_open_interval_triples,
    fetch_current_object_terms,
    statement_id,
)
from cograph_client.pipeline.conflict import (
    DEFAULT_CONFLICT_POLICY,
    ConflictPolicy,
    FactClaim,
)

logger = structlog.stdlib.get_logger("cograph.pipeline.mutations")

Triple = tuple[str, str, str]


def _provenance_enabled() -> bool:
    """Whether the op writes a companion-graph governance event (supersede /
    retract), gated by the SAME ``COGRAPH_PROVENANCE_ENABLED`` env var the rest of
    the write path uses for tombstone/rewrite provenance (default OFF). The
    valid-time interval (``graph/validity.py``) is ALWAYS written regardless — it
    is load-bearing for the "current facts" read, not optional governance."""
    return os.environ.get("COGRAPH_PROVENANCE_ENABLED", "0") == "1"


def _predicate_leaf(predicate: str) -> str:
    """The leaf name of a predicate URI (``…/onto/hasCEO`` → ``hasCEO``).

    Used to key the recency policy per (type, attribute). Falls back to the whole
    string for a predicate with no path separator.
    """
    return predicate.rstrip("/").rsplit("/", 1)[-1] if predicate else predicate


# --------------------------------------------------------------------------- #
# Per-entity-class recency policy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RecencyPolicy:
    """Decides, per entity type + attribute, whether a newer fact SUPERSEDES the
    old (recency wins — functional / single-valued) or COEXISTS with it
    (multi-valued — append).

    The default is **single-valued (supersede)**: most attributes are functional
    (an entity has one current CEO, one current headquarters), and the whole point
    of ONTA-277 is that a fresh fact should retire the stale one. A caller marks
    the genuinely multi-valued attributes (a company has many employees, a paper
    many authors) as ``multivalued`` so those append instead.

    Fully injectable/overridable — every op takes a ``policy=`` argument. Overrides
    are keyed by ``(type_name, attribute_leaf)``; ``single_valued`` overrides win
    over ``multivalued`` when both name the same key (an explicit "this one is
    functional" beats a broad default), and both win over ``default_multivalued``.
    """

    default_multivalued: bool = False
    multivalued: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    single_valued: frozenset[tuple[str, str]] = field(default_factory=frozenset)

    def supersedes(self, type_name: str, attribute: str) -> bool:
        """True → a newer fact should CLOSE the old (recency wins).
        False → the values COEXIST (multi-valued append)."""
        key = (type_name or "", attribute or "")
        if key in self.single_valued:
            return True
        if key in self.multivalued:
            return False
        return not self.default_multivalued


# The sensible default: single-valued everywhere → recency wins. Callers override
# for their multi-valued attributes.
DEFAULT_RECENCY_POLICY = RecencyPolicy()


@dataclass(frozen=True)
class MutationReceipt:
    """The result of a P6 mutation op — its A6 receipt plus what it retired.

    ``graph_delta`` is the deterministic A6 :class:`GraphDelta` (the same replay-
    stable receipt every KG write produces): for a supersede it reflects the NEW
    instance fact written; for a coexist-append it reflects the appended fact; for
    a pure interval-close retraction it is an empty-facts delta (a retraction adds
    no facts — the closure is recorded in the validity/provenance companions).
    ``superseded`` / ``retracted`` list the ``(s, p, o)`` facts whose interval was
    CLOSED (present-but-not-current afterward); ``inserted`` the new instance
    facts. ``removed`` counts triples hard-deleted (only on the opt-in path).
    """

    op: str  # "supersede" | "retract"
    graph_delta: GraphDelta
    inserted: tuple[Triple, ...] = ()
    superseded: tuple[Triple, ...] = ()
    retracted: tuple[Triple, ...] = ()
    coexisted: bool = False
    removed: int = 0


def _scope(instance_graph: str, tenant_id: Optional[str], kg_name: Optional[str]):
    """Resolve (tenant_id, kg_name) for the post-write refresh, preferring explicit
    args and falling back to parsing the instance-graph URI. Returns ``None`` when
    neither is available (a non-KG test stub graph), so the caller skips refresh —
    the mutation itself already landed in the store (mirrors ``er/rebuild``)."""
    if tenant_id and kg_name:
        return tenant_id, kg_name
    scope = parse_kg_graph_uri(instance_graph)
    if scope is None:
        return None
    return (tenant_id or scope[0], kg_name or scope[1])


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

    delta = await insert_facts(
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
        await refresh_after_write(
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
            removed = await delete_facts(
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
            await insert_facts(
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
        delta = await insert_facts(
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
        await refresh_after_write(
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


# --------------------------------------------------------------------------- #
# Write-time conflict resolution on functional attributes (ONTA-276)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ConflictReceipt:
    """The result of a P6 conflict-resolving write — its A6 receipt plus the
    arbitration outcome.

    ``graph_delta`` is the deterministic A6 :class:`GraphDelta` of the instance
    facts written (the winner + any deprecated loser triple — both stay in the
    graph). ``winner`` / ``loser`` are the ``(s, p, o)`` facts that won / lost;
    ``deprecated`` lists every loser triple whose validity interval was CLOSED with
    ``STATUS_DEPRECATED`` (present-but-not-current, never deleted). ``reason`` is
    the deciding axis (``authority`` / ``confidence`` / ``recency`` / ``value``, or
    ``no_conflict``); ``conflict`` is True only when a real contradiction was
    arbitrated; ``coexisted`` True on a multi-valued attribute (no arbitration).
    """

    op: str  # "conflict"
    graph_delta: GraphDelta
    winner: Triple
    loser: Optional[Triple] = None
    deprecated: tuple[Triple, ...] = ()
    reason: str = ""
    conflict: bool = False
    coexisted: bool = False


def _parse_ts(value: str) -> Optional[datetime]:
    """Best-effort ISO-8601 → datetime for a provenance timestamp read back from the
    store (used for the recency axis). Returns ``None`` on anything unparseable so a
    missing/odd stamp never breaks arbitration."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


async def _read_existing_claims(
    neptune, instance_graph: str, subject: str, predicate: str
) -> list[FactClaim]:
    """Read the CURRENT value(s) of ``(subject, predicate)`` from the store, each
    enriched with the trust signals recorded in the companion provenance graph
    (source / confidence / authority / timestamp), as :class:`FactClaim`s the policy
    can rank against an incoming fact.

    Uses the shared readers (``fetch_current_object_terms`` +
    ``provenance.fetch_provenance``) — no bespoke query. A current value with no
    provenance record still becomes a claim (authority unknown → ranked weakest,
    neutral confidence), so an unannotated legacy fact is never invisible to
    arbitration. Best-effort: a provenance read failure degrades to values-only.
    """
    values = await fetch_current_object_terms(neptune, instance_graph, subject, predicate)
    if not values:
        return []
    # Strongest (highest-confidence) provenance record per object value.
    prov_by_value: dict[str, "object"] = {}
    try:
        records = await fetch_provenance(neptune, instance_graph, subject, predicate)
    except Exception:  # noqa: BLE001 — provenance read is best-effort
        records = []
    for r in records:
        cur = prov_by_value.get(r.obj)
        if cur is None or r.confidence > cur.confidence:
            prov_by_value[r.obj] = r
    claims: list[FactClaim] = []
    for v in values:
        r = prov_by_value.get(v)
        authority: Optional[AuthorityLevel] = None
        confidence: Optional[float] = None
        source = ""
        observed_at: Optional[datetime] = None
        if r is not None:
            source = r.source
            confidence = r.confidence
            observed_at = _parse_ts(r.timestamp)
            if r.authority:
                try:
                    authority = AuthorityLevel(r.authority)
                except ValueError:
                    authority = None
        claims.append(
            FactClaim(
                value=v,
                authority=authority,
                confidence=confidence,
                observed_at=observed_at,
                source=source,
            )
        )
    return claims


async def write_with_conflict_resolution(
    neptune,
    instance_graph: str,
    *,
    subject: str,
    predicate: str,
    type_name: str,
    value: str,
    authority: Optional[AuthorityLevel] = None,
    confidence: Optional[float] = None,
    source: str = "",
    observed_at: Optional[datetime] = None,
    run_id: Optional[str] = None,
    reason: str = "",
    existing_claims: Optional[Sequence[FactClaim]] = None,
    tenant_id: Optional[str] = None,
    kg_name: Optional[str] = None,
    conflict_policy: ConflictPolicy = DEFAULT_CONFLICT_POLICY,
    recency_policy: RecencyPolicy = DEFAULT_RECENCY_POLICY,
    manifest=None,
    refresh: bool = True,
) -> ConflictReceipt:
    """Write ``(subject, predicate, value)`` onto a FUNCTIONAL attribute, resolving
    any collision with the existing current value deterministically (ONTA-276).

    Flow (functional attribute — the ``recency_policy`` default):

    1. Build the incoming :class:`~cograph_client.pipeline.conflict.FactClaim` from
       the caller's ``value`` + trust signals (``authority`` / ``confidence`` /
       ``source`` — the signals carried on the fact THROUGH A4).
    2. Discover the existing current value(s) of ``(subject, predicate)`` and their
       provenance-recorded trust signals (:func:`_read_existing_claims`), unless the
       caller supplies ``existing_claims`` explicitly.
    3. Run :meth:`ConflictPolicy.resolve` — a deterministic, total ordering
       (authority > confidence > recency > value) picks the WINNER.
    4. In ONE :func:`insert_facts`: write the winner (open a fresh validity interval
       when the winner is the newly-arriving fact; an existing winner is already
       current), write each LOSER's instance triple if not already present and CLOSE
       its validity interval with ``STATUS_DEPRECATED`` + a ``superseded_by`` pointer
       at the winner (present-but-not-current — never deleted), and record the
       incoming fact's provenance (source / confidence / authority) so a deprecated
       incoming loser stays queryable WITH its provenance. A governance conflict-loss
       event is also recorded per loser when provenance is enabled.
    5. One :func:`refresh_after_write` for the touched type.

    The result: a "current facts" query returns ONLY the winner, while a
    history/full query still returns the loser WITH its closed/deprecated interval,
    its provenance, and the reason it lost. Returns a :class:`ConflictReceipt`
    carrying the A6 :class:`GraphDelta`.

    On a MULTI-VALUED attribute (``recency_policy`` marks it multivalued) there is no
    single-value collision: the value COEXISTS (append-only), written current with
    provenance, ``coexisted=True`` and ``conflict=False``.

    ``refresh`` (default ``True``) controls only step 5, the post-write
    :func:`refresh_after_write` housekeeping pass. Pass ``refresh=False`` when the
    CALLER batches this op across many rows and issues ITS OWN single final
    ``refresh_after_write`` for the touched type(s) — the enrichment refresh path
    (:meth:`enrichment.executor.EnrichmentExecutor._apply_refresh_writes`) does
    exactly this, so a per-row internal refresh here would make a bulk refresh do
    ~N+1 housekeeping passes (Neptune query + re-embed + stats recompute) instead
    of the one the caller already runs. The insert/arbitration is unaffected; only
    the derived-index refresh is deferred to the caller.
    """
    at = observed_at or datetime.now(timezone.utc)
    leaf = _predicate_leaf(predicate)
    functional = recency_policy.supersedes(type_name, leaf)

    incoming = FactClaim(
        value=value,
        authority=authority,
        confidence=confidence,
        observed_at=at,
        source=source,
    )

    # Provenance for the incoming fact — ALWAYS recorded (unconditional, not gated),
    # because a deprecated INCOMING loser must retain its (source, confidence,
    # authority) so "why did it lose / what was the other claim" stays queryable.
    def _incoming_provenance() -> list[Triple]:
        if not (source or authority is not None or confidence is not None):
            return []
        return list(
            build_provenance_triples(
                subject,
                predicate,
                incoming.value,
                source or "",
                confidence=incoming.effective_confidence,
                timestamp=at,
                graph_uri=instance_graph,
                authority=incoming.authority_str,
            )
        )

    # Multi-valued attribute → coexist (append), no arbitration.
    if not functional:
        validity_triples = list(
            build_open_interval_triples(
                subject, predicate, incoming.value, valid_from=at, graph_uri=instance_graph
            )
        )
        delta = await insert_facts(
            neptune,
            instance_graph,
            [(subject, predicate, incoming.value)],
            provenance_triples=_incoming_provenance() or None,
            validity_triples=validity_triples or None,
            # Opening an interval for the coexisting value → clear any prior closure
            # so a value re-added after being deprecated becomes current (ONTA-277).
            reopen_facts=[(subject, predicate, incoming.value)],
            run_id=run_id,
        )
        scope = _scope(instance_graph, tenant_id, kg_name)
        if refresh and scope is not None:
            await refresh_after_write(
                neptune,
                tenant_id=scope[0],
                kg_name=scope[1],
                affected_types=[type_name] if type_name else (),
            )
        if manifest is not None:
            manifest.record_completed(ref=subject)
        return ConflictReceipt(
            op="conflict",
            graph_delta=delta
            if delta is not None
            else build_graph_delta(instance_graph, [(subject, predicate, incoming.value)], run_id=run_id),
            winner=(subject, predicate, incoming.value),
            loser=None,
            reason="no_conflict",
            conflict=False,
            coexisted=True,
        )

    # 2. Existing current claims (value + provenance-recorded trust signals).
    if existing_claims is None:
        existing_claims = await _read_existing_claims(neptune, instance_graph, subject, predicate)

    # 3. Deterministic arbitration.
    decision = conflict_policy.resolve(existing_claims, incoming)
    winner = decision.winner
    losers = list(decision.losers)

    # 4. Build the write.
    #    Instance triples: the winner + every loser (idempotent for whichever is
    #    already present) — both stay in the graph.
    instance_triples: list[Triple] = []
    seen: set[str] = set()
    for v in [winner.value] + [l.value for l in losers]:
        if v not in seen:
            instance_triples.append((subject, predicate, v))
            seen.add(v)

    validity_triples: list[Triple] = []
    reopen: list[Triple] = []
    # Open an interval for the winner ONLY when it is the newly-arriving fact; an
    # existing winner is already current (leave its interval untouched).
    if decision.winner_is_incoming:
        validity_triples.extend(
            build_open_interval_triples(
                subject, predicate, winner.value, valid_from=at, graph_uri=instance_graph
            )
        )
        # Opening the winner's interval → clear any prior closure off its node, so a
        # previously-deprecated value that wins again becomes genuinely current
        # (ONTA-277 resurrection — e.g. a 10M→12M→10M conflict oscillation).
        reopen.append((subject, predicate, winner.value))
    # Close each loser's interval as DEPRECATED, pointing at the winner.
    winner_stmt = statement_id(subject, predicate, winner.value)
    deprecated: list[Triple] = []
    for l in losers:
        deprecated.append((subject, predicate, l.value))
        validity_triples.extend(
            build_closed_interval_triples(
                subject,
                predicate,
                l.value,
                valid_to=at,
                superseded_by=winner_stmt,
                status=STATUS_DEPRECATED,
                graph_uri=instance_graph,
            )
        )

    prov_triples: list[Triple] = _incoming_provenance()
    # Governance conflict-loss event per loser (gated, like supersede/retract).
    if _provenance_enabled() and decision.conflict:
        for l in losers:
            prov_triples.extend(
                build_conflict_loss_triples(
                    subject,
                    predicate,
                    l.value,
                    winner.value,
                    graph_uri=instance_graph,
                    reason=decision.reason,
                    loser_source=l.source,
                    loser_confidence=l.effective_confidence,
                    loser_authority=l.authority_str,
                    timestamp=at,
                    touched_types=[type_name] if type_name else (),
                )
            )

    delta = await insert_facts(
        neptune,
        instance_graph,
        instance_triples,
        provenance_triples=prov_triples or None,
        validity_triples=validity_triples or None,
        reopen_facts=reopen or None,
        run_id=run_id,
    )

    # 5. One post-write refresh (best-effort; skipped for a non-KG stub graph, and
    #    deferred to the caller when refresh=False — see the docstring's note on the
    #    batched enrichment-refresh path, which runs one final refresh_after_write).
    scope = _scope(instance_graph, tenant_id, kg_name)
    if refresh and scope is not None:
        await refresh_after_write(
            neptune,
            tenant_id=scope[0],
            kg_name=scope[1],
            affected_types=[type_name] if type_name else (),
        )

    if manifest is not None:
        manifest.record_completed(ref=subject)

    logger.info(
        "write_with_conflict_resolution",
        subject=subject,
        predicate=predicate,
        conflict=decision.conflict,
        reason=decision.reason,
        winner_is_incoming=decision.winner_is_incoming,
        deprecated=len(deprecated),
    )
    return ConflictReceipt(
        op="conflict",
        graph_delta=delta
        if delta is not None
        else build_graph_delta(instance_graph, instance_triples, run_id=run_id),
        winner=(subject, predicate, winner.value),
        loser=(subject, predicate, decision.loser.value) if decision.loser else None,
        deprecated=tuple(deprecated),
        reason=decision.reason,
        conflict=decision.conflict,
        coexisted=False,
    )


# --------------------------------------------------------------------------- #
# First-class merge / split ops with receipts (ONTA-274)
# --------------------------------------------------------------------------- #
#
# Identity drift is domain reality (Facebook→Meta; mergers/spinoffs). A correct
# mint in March is provably wrong in September when new evidence reveals two nodes
# are the same entity: "Twitter Inc." + "X Corp" coexisting → half the facts on
# each → the answer layer answers with half the picture at full confidence. These
# ops make merge/split a DESIGNED, lineage-preserving P6 operation — NOT the banned
# post-hoc merge-as-sloppy-ER-bug-fix. They are always EVIDENCE-DRIVEN (a ``reason``
# is threaded into provenance) and REVERSIBLE (a merge records the snapshot a split
# reads back), and — like supersede/retract above — they ORCHESTRATE the kg_writer
# primitives (``rewrite_subject`` / ``insert_facts`` / ``delete_facts`` /
# ``refresh_after_write``); they construct no raw SPARQL and fork no write path.

SAME_AS = f"{OMNIX_ONTO}/sameAs"  # the alias/redirect INSTANCE edge (node-valued →
#                                   on onto/, never attrs/, so NL queries can see it)


@dataclass(frozen=True)
class MergeReceipt:
    """A6 receipt for a first-class :func:`merge_entities` op (sibling of
    :class:`MutationReceipt`, sharing its ``op`` + ``graph_delta`` shape).

    ``graph_delta`` is the deterministic A6 delta of the UNIFIED node's facts,
    carrying ``fan_in = {merged_fact_id: canonical_fact_id}`` — the replay-stable
    record that ``merged`` collapsed onto ``canonical``. ``same_as`` is the
    alias/``sameAs`` lineage edge written so history knows the two URIs were unified
    (and by what evidence, via provenance). ``unified_facts`` is what now resolves
    under the canonical node."""

    op: str  # "merge"
    graph_delta: GraphDelta
    canonical: str
    merged: str
    same_as: Triple
    unified_facts: tuple[Triple, ...] = ()
    merged_fact_count: int = 0


@dataclass(frozen=True)
class SplitReceipt:
    """A6 receipt for a first-class :func:`split_entity` op (sibling of
    :class:`MutationReceipt`).

    ``restored`` is the previously-merged-away URI now re-established as an
    independent node; ``restored_facts`` the facts re-attributed to it;
    ``graph_delta`` the A6 delta of those re-materialized facts; ``removed`` the
    count of canonical triples withdrawn (the merged-exclusive facts + the
    ``sameAs`` edge)."""

    op: str  # "split"
    graph_delta: GraphDelta
    canonical: str
    restored: str
    restored_facts: tuple[Triple, ...] = ()
    removed: int = 0


def _to_canonical_form(triple: Triple, merged: str, canonical: str) -> Triple:
    """A merged node's ORIGINAL triple as it appears on the canonical AFTER the
    re-key: ``merged`` in the subject or object slot becomes ``canonical`` (exactly
    what ``rewrite_subject`` does to both directions)."""
    s, p, o = triple
    return (canonical if s == merged else s, p, canonical if o == merged else o)


async def _fetch_node_triples(neptune, instance_graph: str, uri: str) -> list[Triple]:
    """Snapshot every INSTANCE triple referencing ``uri`` — as SUBJECT and as OBJECT
    — in write-convention terms, so a merge can record what belonged to the node and
    a split can restore it byte-for-byte.

    Reads RAW SPARQL JSON (not ``parse_sparql_results``, which drops datatype) so a
    typed literal round-trips exactly (ONTA-247). Only the instance graph is read, so
    companion (provenance/validity) triples are never captured. Best-effort: a read
    hiccup yields ``[]`` (the caller then has nothing to re-key/restore for that node).
    """
    from cograph_client.graph.provenance import _term_from_binding

    esc = _escape_value(uri)
    out: list[Triple] = []
    try:
        raw = await neptune.query(
            f"SELECT ?p ?o WHERE {{ GRAPH <{instance_graph}> {{ {esc} ?p ?o }} }}"
        )
        for row in raw.get("results", {}).get("bindings", []):
            p = _term_from_binding(row.get("p"))
            o = _term_from_binding(row.get("o"))
            if p:
                out.append((uri, p, o))
    except Exception:  # noqa: BLE001 — snapshot read is best-effort
        logger.warning("merge_snapshot_subject_read_failed", uri=uri, exc_info=True)
    try:
        raw = await neptune.query(
            f"SELECT ?s ?p WHERE {{ GRAPH <{instance_graph}> {{ ?s ?p {esc} }} }}"
        )
        for row in raw.get("results", {}).get("bindings", []):
            s = _term_from_binding(row.get("s"))
            p = _term_from_binding(row.get("p"))
            if s and p:
                out.append((s, p, uri))
    except Exception:  # noqa: BLE001 — snapshot read is best-effort
        logger.warning("merge_snapshot_object_read_failed", uri=uri, exc_info=True)
    # Dedup, order-stable, so the snapshot + A6 delta are deterministic.
    return list(dict.fromkeys(out))


async def merge_entities(
    neptune,
    instance_graph: str,
    *,
    a: str,
    b: str,
    type_name: str,
    canonical: Optional[str] = None,
    reason: str = "",
    observed_at: Optional[datetime] = None,
    run_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    kg_name: Optional[str] = None,
    manifest=None,
) -> MergeReceipt:
    """Unify two entities that new evidence shows are the same real-world thing —
    a first-class, lineage-preserving, EVIDENCE-DRIVEN P6 op (ONTA-274).

    ``a`` and ``b`` are the two entity URIs; ``canonical`` (defaults to ``b``) is the
    survivor and the other is merged away. ``reason`` is the driving evidence — merge
    is NEVER a silent cleanup, so it is threaded into the always-on lineage record.

    Flow (orchestration over the kg_writer primitives — no raw writes):

    1. SNAPSHOT both nodes' facts BEFORE re-keying (:func:`_fetch_node_triples`), so
       the merge is reversible.
    2. RE-POINT via :func:`kg_writer.rewrite_subject` — every triple referencing the
       merged-away URI (as subject AND object) moves onto the canonical in ONE re-key
       event (recorded as a ``rewrite`` provenance event), NOT delete+insert, so no
       fact is lost and lineage stays intact.
    3. WRITE the ``sameAs`` alias/redirect INSTANCE edge (``canonical sameAs merged``,
       on ``onto/`` so NL queries can traverse it) + the ALWAYS-ON reversible lineage
       snapshot (provenance graph), both through the shared :func:`insert_facts`.
    4. ONE :func:`refresh_after_write` with ``rewritten_subjects={merged: canonical}``
       so derived secondary indexes RE-KEY (not accrue ghost rows) — exactly the
       param that exists for this.

    Returns a :class:`MergeReceipt` whose A6 ``graph_delta`` carries
    ``fan_in={merged: canonical}``.
    """
    at = observed_at or datetime.now(timezone.utc)
    if not a or not b:
        raise ValueError("merge_entities requires two entity URIs")
    if canonical is None:
        canonical = b
    if canonical not in (a, b):
        raise ValueError(f"canonical must be one of the two merge operands, got {canonical!r}")
    merged = a if canonical == b else b
    if merged == canonical:
        raise ValueError(f"cannot merge an entity into itself ({merged!r})")

    touched = [type_name] if type_name else []

    # 1. Snapshot both sides BEFORE the re-key (reversibility depends on this).
    merged_facts = await _fetch_node_triples(neptune, instance_graph, merged)
    canonical_facts = await _fetch_node_triples(neptune, instance_graph, canonical)

    # 2. Re-point every triple referencing `merged` onto `canonical` — ONE re-key
    #    event (rewrite_subject), never delete+insert. Lineage-preserving by design.
    await rewrite_subject(
        neptune, instance_graph, merged, canonical,
        touched_types=touched, reason=reason or "merge_entities",
    )

    # 3. The alias/sameAs lineage edge (instance) + the always-on reversible snapshot
    #    (provenance), both via the shared insert primitive.
    same_as: Triple = (canonical, SAME_AS, merged)
    lineage_triples = build_merge_lineage_triples(
        canonical, merged,
        merged_facts=merged_facts, canonical_facts=canonical_facts,
        graph_uri=instance_graph, reason=reason, timestamp=at, touched_types=touched,
    )
    await insert_facts(
        neptune, instance_graph, [same_as], provenance_triples=lineage_triples or None,
    )

    # 4. One post-write refresh, re-keying the merged subject in derived indexes.
    scope = _scope(instance_graph, tenant_id, kg_name)
    if scope is not None:
        await refresh_after_write(
            neptune, tenant_id=scope[0], kg_name=scope[1],
            affected_types=touched, rewritten_subjects={merged: canonical},
        )

    if manifest is not None:
        manifest.record_completed(ref=canonical)

    # 5. A6 receipt — the unified node's facts + fan_in recording merged → canonical.
    unified = list(
        dict.fromkeys(
            [_to_canonical_form(f, merged, canonical) for f in merged_facts + canonical_facts]
            + [same_as]
        )
    )
    graph_delta = build_graph_delta(
        instance_graph, unified, run_id=run_id, fan_in={merged: canonical}
    )
    logger.info(
        "merge_entities",
        canonical=canonical, merged=merged,
        merged_facts=len(merged_facts), unified_facts=len(unified),
    )
    return MergeReceipt(
        op="merge",
        graph_delta=graph_delta,
        canonical=canonical,
        merged=merged,
        same_as=same_as,
        unified_facts=tuple(unified),
        merged_fact_count=len(merged_facts),
    )


async def split_entity(
    neptune,
    instance_graph: str,
    *,
    canonical: str,
    merged: str,
    type_name: str,
    reason: str = "",
    partition: Optional[tuple[list[Triple], list[Triple]]] = None,
    observed_at: Optional[datetime] = None,
    run_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    kg_name: Optional[str] = None,
    manifest=None,
) -> SplitReceipt:
    """Separate a previously-merged (or over-merged) node back into two distinct
    nodes with their facts correctly re-attributed — the clean reverse of
    :func:`merge_entities` (ONTA-274).

    Mechanism: read back the reversible lineage the merge recorded
    (:func:`fetch_merge_lineage`) to learn which facts belonged to ``merged`` vs
    ``canonical`` — OR accept an explicit ``partition = (merged_facts,
    canonical_facts)`` when the caller knows the split better than the recorded
    lineage (the over-merge case). Then, over the kg_writer primitives:

    1. RESTORE ``merged``'s own facts (idempotent :func:`insert_facts`) — its
       identity + facts return.
    2. WITHDRAW from ``canonical`` the merged-EXCLUSIVE facts (those ``merged`` had
       that ``canonical`` did not) + the ``sameAs`` edge, via :func:`delete_facts`.
       Facts the two genuinely SHARED (e.g. ``rdf:type``) stay on ``canonical``.
    3. ONE :func:`refresh_after_write`.

    The merge lineage snapshot is deliberately LEFT in the provenance graph, so
    history preserves the full merge→split story. A merge followed by a split
    therefore returns to two nodes with their facts correctly re-attributed.
    """
    at = observed_at or datetime.now(timezone.utc)
    if not canonical or not merged:
        raise ValueError("split_entity requires the canonical and merged URIs")
    touched = [type_name] if type_name else []

    # 1. Determine each side's facts — explicit partition wins, else recorded lineage.
    if partition is not None:
        merged_facts, canonical_facts = list(partition[0]), list(partition[1])
    else:
        lineage = await fetch_merge_lineage(neptune, instance_graph, canonical, merged)
        merged_facts, canonical_facts = lineage.merged_facts, lineage.canonical_facts
    if not merged_facts:
        raise ValueError(
            f"no merge lineage found to split {merged!r} out of {canonical!r}; "
            "pass an explicit partition=(merged_facts, canonical_facts)"
        )

    # 2. Restore the merged node's own facts (idempotent insert re-establishes it).
    restore = list(dict.fromkeys(merged_facts))
    await insert_facts(neptune, instance_graph, restore)

    # 3. Withdraw from the canonical the merged-EXCLUSIVE facts + the sameAs edge.
    #    A fact the two genuinely shared (its canonical form is in canonical_facts)
    #    is KEPT on the canonical — only what was uniquely merged's is removed.
    canon_set = set(canonical_facts)
    to_remove: list[Triple] = [
        cf for cf in (_to_canonical_form(f, merged, canonical) for f in restore)
        if cf not in canon_set
    ]
    to_remove.append((canonical, SAME_AS, merged))
    to_remove = list(dict.fromkeys(to_remove))
    removed = await delete_facts(
        neptune, instance_graph, triples=to_remove,
        touched_types=touched, reason=reason or "split_entity",
    )

    # 4. Governance split event (gated like the other events; merge lineage kept).
    if _provenance_enabled():
        split_prov = build_split_triples(
            canonical, merged, graph_uri=instance_graph,
            reason=reason, timestamp=at, touched_types=touched,
        )
        if split_prov:
            await insert_facts(neptune, instance_graph, [], provenance_triples=split_prov)

    # 5. One post-write refresh for the touched type.
    scope = _scope(instance_graph, tenant_id, kg_name)
    if scope is not None:
        await refresh_after_write(
            neptune, tenant_id=scope[0], kg_name=scope[1], affected_types=touched,
        )

    if manifest is not None:
        manifest.record_completed(ref=merged)

    graph_delta = build_graph_delta(instance_graph, restore, run_id=run_id)
    logger.info(
        "split_entity",
        canonical=canonical, restored=merged,
        restored_facts=len(restore), removed=removed,
    )
    return SplitReceipt(
        op="split",
        graph_delta=graph_delta,
        canonical=canonical,
        restored=merged,
        restored_facts=tuple(restore),
        removed=removed,
    )


__all__ = [
    "RecencyPolicy",
    "DEFAULT_RECENCY_POLICY",
    "MutationReceipt",
    "supersede_fact",
    "retract_fact",
    "ConflictReceipt",
    "write_with_conflict_resolution",
    "MergeReceipt",
    "SplitReceipt",
    "SAME_AS",
    "merge_entities",
    "split_entity",
]
