"""The single insertion primitive + derived-index write hooks.

Every instance insert goes through :func:`insert_facts`. Do not add a second
write path. Look up sibling / facade names via :func:`_host` so tests that
monkeypatch ``infona_client.graph.kg_writer.<name>`` keep working.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional, Sequence

import structlog

from infona_client.graph.facts import Fact, triples_to_facts
from infona_client.graph.queries import parse_kg_graph_uri

if TYPE_CHECKING:
    from infona_client.graph.kg_writer_delta import GraphDelta
    from infona_client.graph.store import GraphSession, GraphStore

logger = structlog.stdlib.get_logger("infona.graph.kg_writer")

Triple = tuple[str, str, str]


def _host():
    from infona_client.graph import kg_writer as _mod

    return _mod

async def insert_facts(
    neptune,
    instance_graph: str,
    instance_triples: Optional[list[Triple]] = None,
    *,
    facts: Optional[Sequence[Fact]] = None,
    provenance_triples: Optional[list[Triple]] = None,
    validity_triples: Optional[list[Triple]] = None,
    suppression_triples: Optional[list[Triple]] = None,
    reopen_facts: Optional[list[Triple]] = None,
    run_id: Optional[str] = None,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
) -> Optional[GraphDelta]:
    """Write instance facts to the KG through the property-graph store.

    The ONE insertion primitive for both ingest and enrichment. Facts are
    written via :mod:`infona_client.graph.pg_ops` against a scoped
    :class:`GraphSession` resolved from ``session`` / ``store`` / the process
    store. Prefer structured :class:`Fact` objects; legacy ``instance_triples``
    are mapped via :func:`triples_to_facts`.

    **``neptune`` is vestigial** (ONTA-527): the SPARQL insert path is gone, so
    the argument is ignored here. It stays in the signature because every
    converged writer passes it positionally and
    :func:`refresh_after_write` still takes one; dropping it is a separate
    mechanical sweep.

    **RDF companion payloads (ONTA-536):**

    * ``provenance_triples`` — ported. Parsed into statement-metadata records
      and folded onto Assertion ``source_url`` / ``confidence`` / ``verified_at``
      plus ``:ProvEvent`` assert companions (confidence, statement id, asserted-at
      timestamp). Callers (ingest / enrichment) still build the same ADR 0002
      payload via ``build_provenance_triples``; the named-graph SPARQL write is
      gone, the governance fields are not.
    * ``suppression_triples`` — ported (ONTA-279 / E7). Parsed into
      :class:`~infona_client.graph.suppression_store.SuppressionMark` records and
      persisted as ``:Suppression`` nodes scoped to this KG. Callers (retraction)
      still build the same companion-graph payload via ``build_suppression_triples``
      / ``build_entity_suppression_triples``; only the storage changed. Until this
      port, the payload was dropped on the floor and a retracted value silently
      came back on the next enrichment refresh.
    * ``validity_triples`` / ``reopen_facts`` — still unported (E7). Callers get a
      warning rather than silence.

    ``run_id`` (ONTA-271): when given, returns a deterministic A6
    :class:`GraphDelta` over the *triple* form of the write (Fact-only writes
    without triples yield an empty domain set unless triples are also supplied).
    """
    _host()._warn_unported_companions(
        instance_graph,
        validity_triples=validity_triples,
        reopen_facts=reopen_facts,
    )
    instance_triples = list(instance_triples or [])
    gs = _host()._resolve_graph_session(
        store=store, session=session, instance_graph=instance_graph
    )
    return await _host()._insert_facts_store(
        gs,
        instance_graph,
        instance_triples=instance_triples,
        facts=facts,
        provenance_triples=list(provenance_triples or []),
        suppression_triples=list(suppression_triples or []),
        run_id=run_id,
    )


async def _insert_facts_store(
    session: "GraphSession",
    instance_graph: str,
    *,
    instance_triples: list[Triple],
    facts: Optional[Sequence[Fact]],
    provenance_triples: Optional[list[Triple]] = None,
    suppression_triples: Optional[list[Triple]] = None,
    run_id: Optional[str],
) -> Optional[GraphDelta]:
    """Property-graph insert path (Memory / Neo4j). Template/native ops only."""
    from infona_client.graph import pg_ops
    from infona_client.graph.provenance import parse_provenance_records

    fact_list: list[Fact] = list(facts) if facts else []
    if instance_triples:
        fact_list.extend(triples_to_facts(instance_triples))
    # ADR 0013: fold attr_meta enrichment companions onto Assertion provenance
    # (source_url / verified_at / provenance) BEFORE apply_facts. Neptune keeps
    # the RDF companions as instance triples; the store path does not reify them
    # as domain Facts (classify_triple skips attr_meta).
    citations: list = []
    if instance_triples:
        try:
            citations = pg_ops.parse_attr_meta_citations(instance_triples)
            if citations:
                fact_list = pg_ops.fold_attr_citations_onto_facts(fact_list, citations)
        except Exception:  # noqa: BLE001 — fold is best-effort
            logger.warning(
                "insert_facts_store_attr_citation_fold_failed",
                instance_graph=instance_graph,
                exc_info=True,
            )
            citations = []
    # ONTA-536: fold ADR 0002 companion-provenance records onto domain Facts
    # (source / confidence / asserted-at) before Assertion SoT write.
    prov_records = []
    if provenance_triples:
        try:
            prov_records = parse_provenance_records(list(provenance_triples))
            if prov_records and fact_list:
                fact_list = pg_ops.fold_provenance_records_onto_facts(
                    fact_list, prov_records
                )
        except Exception:  # noqa: BLE001 — fold is best-effort
            logger.warning(
                "insert_facts_store_provenance_fold_failed",
                instance_graph=instance_graph,
                exc_info=True,
            )
            prov_records = []
    prov_on = _host()._provenance_enabled(store_path=True)
    if fact_list:
        await pg_ops.apply_facts(
            session,
            fact_list,
            provenance_enabled=prov_on,
        )
    elif prov_on and prov_records:
        # Legacy path: insert_facts([], provenance_triples=…) — no domain Facts
        # in this batch; still land recoverable ProvEvents (ingest per-entity).
        try:
            await pg_ops.apply_provenance_records(
                session, prov_records, provenance_enabled=True
            )
        except Exception:  # noqa: BLE001 — companions never fail the write
            logger.warning(
                "insert_facts_store_provenance_only_failed",
                instance_graph=instance_graph,
                exc_info=True,
            )
    # Residual :AttrCitation nodes for citation-only attrs (no domain Fact in
    # this batch) or multi-value slots — secondary to Assertion provenance.
    if citations:
        try:
            await pg_ops.apply_attr_citations(session, citations)
        except Exception:  # noqa: BLE001 — companions never fail the write
            logger.warning(
                "insert_facts_store_attr_citation_failed",
                instance_graph=instance_graph,
                exc_info=True,
            )
    # ONTA-279 / E7: sticky suppression markers. Persisted here — NOT as domain
    # facts — so the marker survives a hard-delete of the very triple it
    # suppresses and no reopen can clear it.
    #
    # Failures are logged at ERROR, never raised, matching the companion
    # convention (the instance write has already committed by now, so raising
    # would report failure for a write that partly succeeded). Note the read's
    # fail-closed path does NOT cover this: an ABSENT marker reads as an empty
    # set, not as a read failure, so a dropped mark reproduces the exact
    # re-acquisition bug this port fixes. That is why these three log lines are
    # ERROR and name the marks — they are the only signal that a retraction did
    # not actually stick.
    if suppression_triples:
        try:
            from infona_client.graph.suppression_store import (
                apply_suppression_marks,
                parse_suppression_marks,
            )

            marks = parse_suppression_marks(suppression_triples)
            if not marks:
                logger.error(
                    "insert_facts_suppression_payload_unparsed",
                    instance_graph=instance_graph,
                    triples=len(suppression_triples),
                    detail="no suppression mark parsed from the payload; nothing suppressed",
                )
            else:
                written = await apply_suppression_marks(session, marks)
                if not written:
                    logger.error(
                        "insert_facts_suppression_store_unsupported",
                        instance_graph=instance_graph,
                        marks=len(marks),
                        detail="session implements no write_suppression; marks dropped",
                    )
        except Exception:  # noqa: BLE001 — companions never fail the write
            logger.error(
                "insert_facts_suppression_write_failed",
                instance_graph=instance_graph,
                marks=len(suppression_triples),
                exc_info=True,
            )
    # Secondary indexes still key off graph URI + triple-shaped payloads when
    # available (best-effort). Spatio-temporal is datatype-driven (needs only
    # the write's triples); semantic rebuilds each touched entity from a
    # GraphStore re-read (ONTA-533 — was SPARQL and therefore dead since the
    # Neo4j cutover).
    if instance_triples:
        await _host()._index_spatiotemporal(instance_graph, instance_triples)
        await _host()._index_semantic(None, instance_graph, instance_triples)
    if run_id is not None:
        return _host().build_graph_delta(instance_graph, instance_triples, run_id=run_id)
    return None


async def _index_spatiotemporal(
    instance_graph: str, instance_triples: list[Triple]
) -> None:
    """Populate the spatio-temporal secondary index from the just-written triples.

    A companion store derived from the same facts (like the provenance graph) —
    kept HERE in the single insertion primitive so EVERY converged writer (ingest,
    enrichment, normalization, dedupe, …) auto-indexes its geometry-bearing
    entities with no per-caller wiring. Datatype-driven: ``extract_spatiotemporal_facts``
    only emits a fact for an entity carrying a ``geo:wktLiteral``, so a write with
    no coordinates does ~no work and pays only a list scan.

    Best-effort and fully isolated: a derived-index hiccup must NEVER fail the
    primary KG write (Neptune is the source of truth; this index is eventually
    consistent). Skips non-KG graphs (the URI doesn't parse to a tenant/KG).
    """
    scope = parse_kg_graph_uri(instance_graph)
    if scope is None:
        return  # not a per-KG instance graph → nothing to scope an index row to
    tenant_id, kg_name = scope
    try:
        from infona_client.spatiotemporal.extract import extract_spatiotemporal_facts
        from infona_client.spatiotemporal.registry import get_spatiotemporal_index

        facts = extract_spatiotemporal_facts(
            instance_triples, tenant_id=tenant_id, kg_name=kg_name
        )
        if facts:
            # Time-bounded so a hung backend can't block the write (see the
            # _host()._INDEX_UPSERT_TIMEOUT_S note); TimeoutError is caught below.
            await asyncio.wait_for(
                get_spatiotemporal_index().upsert_many(facts),
                timeout=_host()._INDEX_UPSERT_TIMEOUT_S,
            )
    except Exception:  # noqa: BLE001 — never fail a KG write on a derived-index hiccup
        logger.warning(
            "spatiotemporal_index_update_failed",
            instance_graph=instance_graph,
            exc_info=True,
        )


async def _index_semantic(
    neptune, instance_graph: str, instance_triples: list[Triple]
) -> None:
    """Populate the semantic instance index from the just-written triples (ONTA-181).

    The FRESHNESS half of the ONTA-173 consistency model (the claim-based
    reconciler in ``semantic/reconciler.py`` is the correctness half): chunks
    of marked free-text attributes land in the same request that wrote the KG,
    with ``embedding=NULL`` — the store-side generated tsvector makes them
    lexically searchable instantly; vector recall follows within one embed-fill
    sweep. Kept HERE in the single insertion primitive (like the
    spatio-temporal hook above) so EVERY converged writer auto-indexes with no
    per-caller wiring.

    Env-gated OFF by default (``INFONA_SEMANTIC_INDEX_ENABLED`` — cost/rollout
    control: indexing implies embedding spend and index growth). Marker-driven:
    only predicates the tenant's textKind map (``graph/text_markers.py``) marks
    ``free_text`` are extracted — free text has no distinguishing datatype, so
    unlike the spatio-temporal hook this one consults the (TTL-cached) marker
    map. ``neptune`` is vestigial (ONTA-527 / ONTA-533): markers and the
    completeness re-read both go through GraphStore.

    ONE exception, marker-INDEPENDENT (ONTA-421): every named entity also gets
    an identity doc, so a write that carries only ``rdfs:label`` / ``name`` /
    ``title`` now touches the index where it previously touched nothing. That is
    the cost side of the fix: a KG with no marked attribute at all used to pay
    zero re-reads per write and now pays one bounded GraphStore assertion
    re-read (still capped by ``INFONA_SEMANTIC_HOOK_MAX_ENTITIES``, still under
    the one timeout, still best-effort). ``INFONA_SEMANTIC_IDENTITY_INDEX=0``
    restores the old write-path behavior — but note it is not free to flip: the
    next reconcile of each KG then sees every identity doc as a ghost and
    batch-deletes it (symmetric and self-healing on flip-back, but a mass delete
    to expect rather than discover).

    Completeness contract (the ONTA-173 partial-doc fix): the write's triples
    only tell the hook WHICH (entity, marked attr) docs were touched — the docs
    themselves are rebuilt from a re-read of the touched entities' FULL current
    Assertion SoT in GraphStore (the write has already been committed by the
    time the hook runs, so the re-read includes it). Upsert is replace-per-doc
    and the reconciler builds docs from the full KG, so indexing only the
    write's triples would (a) wipe the previously indexed tail of a multi-valued
    attr that just got one value appended, and (b) feed the intra-entity
    cross-attr dedup a different input than the reconciler sees, making mirrored
    attrs flip-flop between hook and reconcile runs.

    Empty-doc contract: a marked attr on a touched entity whose RE-READ
    canonicalized doc came out empty (or was deduped away because it mirrors
    another attr's doc) gets ``delete(entity, tenant, kg_name=…, attr=…)`` —
    per the ONTA-175 upsert contract an empty doc has no chunk rows to carry
    its key, so the hook must issue the delete explicitly.

    Best-effort and time-bounded exactly like ``_host()._index_spatiotemporal``: the
    whole body (marker read + entity re-read + upsert + deletes + schedule
    ensure) runs under one ``asyncio.wait_for`` so a hung index backend can't
    block the KG write, and ANY failure is logged, never raised — the KG write
    must NEVER fail on an index hiccup (the primary store is already the source
    of truth at this point).
    """
    scope = parse_kg_graph_uri(instance_graph)
    if scope is None:
        return  # not a per-KG instance graph → nothing to scope an index row to
    tenant_id, kg_name = scope
    try:
        from infona_client.semantic.reconciler import semantic_index_enabled

        if not semantic_index_enabled():
            return
        await asyncio.wait_for(
            _host()._index_semantic_inner(
                neptune, instance_graph, tenant_id, kg_name, instance_triples
            ),
            timeout=_host()._semantic_upsert_timeout_s(),
        )
    except Exception:  # noqa: BLE001 — never fail a KG write on a derived-index hiccup
        logger.warning(
            "semantic_index_update_failed",
            instance_graph=instance_graph,
            exc_info=True,
        )


async def _index_semantic_inner(
    neptune,
    instance_graph: str,
    tenant_id: str,
    kg_name: str,
    instance_triples: list[Triple],
) -> None:
    """The unguarded body of :func:`_host()._index_semantic` (wrapped in one timeout).

    Imports are lazy so ``graph/`` stays importable without pulling in the
    semantic subsystem (mirrors the spatio-temporal hook's lazy imports).
    """
    from infona_client.graph.text_markers import get_free_text_map
    from infona_client.semantic.extract import extract_semantic_chunks
    from infona_client.semantic.reconciler import (
        ensure_reconcile_schedule_from_hook,
        indexable_doc_keys,
    )
    from infona_client.semantic.registry import get_semantic_index

    marker_map = await get_free_text_map(neptune, tenant_id)
    marked = {uri for uri, is_free_text in marker_map.items() if is_free_text}
    # Which docs did THIS write touch? Marked free-text docs AND identity docs
    # (ONTA-421 — an entity's own name is indexed with no marker involved, so a
    # name-only write must be picked up here too; before, such a write indexed
    # nothing and the entity stayed permanently unfindable by name). Only the
    # touched entities are re-read, so a write carrying neither a marked value
    # nor a name still costs zero extra store reads.
    touched = indexable_doc_keys(instance_triples, marked)
    if touched:
        entity_uris = sorted({entity_uri for entity_uri, _ in touched})
        cap = _host()._semantic_hook_max_entities()
        if len(entity_uris) > cap:
            # Bounded: index the first `cap` entities (deterministic — sorted),
            # loudly leave the rest to the reconciler's next full scan.
            logger.warning(
                "semantic_index_hook_entity_cap",
                tenant_id=tenant_id,
                kg_name=kg_name,
                touched_entities=len(entity_uris),
                cap=cap,
            )
            entity_uris = entity_uris[:cap]

        # Completeness re-read (see _host()._index_semantic's docstring): rebuild the
        # touched entities' docs from their FULL current Assertion SoT —
        # NEVER from the write's own (possibly partial) triples. On failure we
        # SKIP indexing for this write (the reconciler repairs on its cadence);
        # degrading to write-local partial docs would reintroduce the exact
        # partial-doc-wipe bug this re-read exists to fix.
        try:
            fetched = await _host()._fetch_touched_entity_triples(
                neptune, instance_graph, entity_uris
            )
        except Exception:  # noqa: BLE001 — skip the index, never the KG write
            logger.warning(
                "semantic_index_hook_fetch_failed",
                tenant_id=tenant_id,
                kg_name=kg_name,
                touched_entities=len(entity_uris),
                exc_info=True,
            )
            await ensure_reconcile_schedule_from_hook(tenant_id, kg_name)
            return

        index = get_semantic_index()
        chunks = extract_semantic_chunks(
            fetched,
            tenant_id=tenant_id,
            kg_name=kg_name,
            marked_predicates=marked,
        )
        if chunks:
            await index.upsert_chunks(chunks)
        # Empty-doc deletes, from the RE-READ values: a marked (entity, attr)
        # doc present in the fetched triples that produced no chunks of its own
        # (canonical text emptied, or deduped away as a mirror of another
        # attr's doc — see the docstring above). Only the touched entities are
        # considered — full ghost repair (deleted entities, marker flips) is
        # the reconciler's job.
        emitted = {(c.entity_uri, c.attr) for c in chunks}
        emptied = indexable_doc_keys(fetched, marked) - emitted
        for entity_uri, attr in sorted(emptied):
            await index.delete(entity_uri, tenant_id, kg_name=kg_name, attr=attr)
        logger.info(
            "semantic_index_hook",
            tenant_id=tenant_id,
            kg_name=kg_name,
            entities_fetched=len(entity_uris),
            chunks_written=len(chunks),
            docs_deleted=len(emptied),
        )
    # Ensure the KG's recurring reconcile schedule exists (memoized — one
    # store round-trip per (tenant, kg) per process), even when nothing is
    # marked yet: the reconciler's default candidacy heuristic may mark
    # attributes this hook can't (client-mapped CSV rows, enrichment-minted).
    await ensure_reconcile_schedule_from_hook(tenant_id, kg_name)


async def _fetch_touched_entity_triples(
    neptune, instance_graph: str, entity_uris: list[str]
) -> list[Triple]:
    """Fetch the full current ``(s, p, o)`` triples of the given entities.

    **GraphStore path (ONTA-533):** re-read Assertion SoT via
    ``session.read_assertions_for_subject`` for each touched entity. This is
    the production path after the Neo4j cutover — SPARQL is no longer available.

    ALL predicates are fetched — marked attrs plus ``rdf:type`` / label
    predicates for the extractor's denormalized display fields; the extractor
    itself filters to the marked set, exactly as it does for the reconciler's
    scan, so the two can never disagree on matching.

    Ordered like the reconciler's scan (``ORDER BY ?e ?p ?o``, re-sorted
    client-side to be safe) so the extractor's first-attr-wins intra-entity
    dedup picks the SAME winner the reconciler's full scan picks — otherwise
    mirrored attrs flip-flop between hook writes and reconcile runs.
    """
    from infona_client.semantic.reconciler import _assertion_row_to_semantic_triples

    triples: list[Triple] = []
    try:
        session = _host()._resolve_graph_session(instance_graph=instance_graph)
        reader = getattr(session, "read_assertions_for_subject", None)
        if callable(reader):
            for uri in entity_uris:
                rows = await reader(uri)
                for row in rows:
                    if not isinstance(row, dict):
                        to_dict = getattr(row, "to_dict", None)
                        if callable(to_dict):
                            row = to_dict()
                        elif hasattr(row, "keys"):
                            row = dict(row)
                        else:
                            continue
                    if not isinstance(row, dict):
                        continue
                    triples.extend(_assertion_row_to_semantic_triples(row))
            triples.sort()
            return triples
    except Exception:
        # Fall through to SPARQL only when a live client was supplied (hermetic
        # FakeNeptune tests that never installed a read_assertions path).
        if neptune is None or not hasattr(neptune, "query"):
            raise

    from infona_client.graph.parser import parse_sparql_results

    values = " ".join(f"<{u}>" for u in entity_uris)
    sparql = (
        f"SELECT ?e ?p ?o FROM <{instance_graph}> WHERE {{\n"
        f"  VALUES ?e {{ {values} }}\n"
        f"  ?e ?p ?o .\n"
        f"}} ORDER BY ?e ?p ?o"
    )
    _, rows = parse_sparql_results(await neptune.query(sparql))
    for row in rows:
        e, p = row.get("e", ""), row.get("p", "")
        if e and p:
            triples.append((e, p, row.get("o", "")))
    triples.sort()
    return triples
