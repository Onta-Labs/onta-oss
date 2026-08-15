"""Write-time conflict resolution receipts for P6 (ONTA-276).

Writes go through ``insert_facts`` / ``refresh_after_write``. Look up those
primitives via :func:`_host` so tests that monkeypatch
``infona_client.pipeline.mutations.<name>`` keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

import structlog

from infona_client.api_registry.spec import AuthorityLevel
from infona_client.graph.kg_writer import GraphDelta, build_graph_delta
from infona_client.graph.provenance import (
    build_conflict_loss_triples,
    build_provenance_triples,
    fetch_provenance,
)
from infona_client.graph.validity import (
    STATUS_DEPRECATED,
    build_closed_interval_triples,
    build_open_interval_triples,
    fetch_current_object_terms,
    statement_id,
)
from infona_client.pipeline.conflict import (
    DEFAULT_CONFLICT_POLICY,
    ConflictPolicy,
    FactClaim,
)
from infona_client.pipeline.mutations_policy import (
    DEFAULT_RECENCY_POLICY,
    RecencyPolicy,
    Triple,
    _host,
    _predicate_leaf,
    _provenance_enabled,
    _scope,
)

logger = structlog.stdlib.get_logger("infona.pipeline.mutations")


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

    1. Build the incoming :class:`~infona_client.pipeline.conflict.FactClaim` from
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
        delta = await _host().insert_facts(
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
            await _host().refresh_after_write(
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

    delta = await _host().insert_facts(
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
        await _host().refresh_after_write(
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
