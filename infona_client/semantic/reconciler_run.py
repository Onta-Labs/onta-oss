"""Per-KG reconcile duty — extract, upsert, ghost-delete.

Patchable ``_UPSERT_BATCH_CHUNKS`` is read via ``_host()``. Schema writes
(candidacy) stay on ``commit_ontology``; this module never writes instance
triples.
"""

from __future__ import annotations

from typing import Any, Optional

from infona_client.semantic.extract import (
    _local_name,
    extract_semantic_chunks,
    identity_index_enabled,
    is_identity_predicate,
)
from infona_client.semantic.protocol import SemanticChunk, SemanticIndex
from infona_client.semantic.reconciler_candidacy import (
    _apply_default_candidacy,
    _distinct_literal_predicates,
    _fetch_marker_map,
)
from infona_client.semantic.reconciler_common import _host
from infona_client.semantic.reconciler_const import (
    Triple,
    _LABEL_LOCALS,
    _RDF_TYPE,
    _RDFS_LABEL,
)
from infona_client.semantic.reconciler_env import semantic_index_enabled
from infona_client.semantic.reconciler_scan import _scan_triples
from infona_client.semantic.registry import get_semantic_index


async def _upsert_in_doc_batches(idx: SemanticIndex, chunks: list[SemanticChunk]) -> None:
    """Upsert in batches packed on DOC boundaries (complete-document contract:
    all chunks of one (entity, attr) doc must travel in one call). Batched so a
    crash mid-reconcile persists partial progress — the rerun skips unchanged
    hashes and converges (the partial-resume property)."""
    batch: list[SemanticChunk] = []
    current_doc: Optional[tuple] = None
    cap = _host()._UPSERT_BATCH_CHUNKS
    for chunk in chunks:
        if (
            batch
            and len(batch) >= cap
            and chunk.doc_key() != current_doc
        ):
            await idx.upsert_chunks(batch)
            batch = []
        batch.append(chunk)
        current_doc = chunk.doc_key()
    if batch:
        await idx.upsert_chunks(batch)


async def reconcile_kg(
    neptune: Any,
    tenant_id: str,
    kg_name: str,
    *,
    index: Optional[SemanticIndex] = None,
) -> dict[str, int]:
    """Reconcile ONE KG's semantic index against the instance graph (source of truth).

    Steps (idempotent end-to-end — an interrupted run leaves the index strictly
    closer to converged, and the rerun finishes the job):

    1. fetch the marker map (uncached, raising — see :func:`_fetch_marker_map`);
    2. apply the default candidacy heuristic to undecided attributes and fold
       any new ``free_text`` verdicts into this run's marker set;
    3. snapshot the index's doc listing (``list_docs``) — taken **FIRST,
       before every other instance-graph read of the run** (marker fetch,
       candidacy sampling, the scan). The ordering is load-bearing: a doc the
       write hook indexes anywhere inside the run's read window would otherwise
       be in the listing but absent from the expected set and get
       ghost-deleted;
    4. scan the instance graph for marked predicates (plus ``rdf:type`` /
       label predicates for display parity with hook-written rows);
    5. re-extract chunks and upsert by ``content_hash``;
    6. DELETE ghosts: snapshot docs absent from the expected set. SKIPPED
       when the backend predates ``list_docs`` or the scan was truncated.

    Raises on store failures — the schedule runner logs and retries on the
    next cadence; acting on partial reads would be worse than waiting.
    """
    from infona_client.graph.queries import kg_graph_uri

    h = _host()
    counters = {
        "chunks_written": 0,
        "skipped_unchanged_hash": 0,
        "attrs_repaired": 0,
        "ghosts_deleted": 0,
        "attrs_marked_free_text": 0,
        "attrs_marked_not_text": 0,
    }
    if not semantic_index_enabled():
        h.logger.info(
            "semantic_reconcile_skipped_disabled",
            tenant_id=tenant_id,
            kg_name=kg_name,
        )
        return counters

    idx = index if index is not None else get_semantic_index()
    kg_graph = kg_graph_uri(tenant_id, kg_name)

    # 0. Snapshot the index FIRST — before ANY of this run's instance reads
    # (marker fetch, candidacy sampling, the scan). getattr, not a direct
    # call: list_docs is a Protocol method now, but a third-party backend
    # compiled against the pre-list_docs Protocol must degrade gracefully.
    lister = getattr(idx, "list_docs", None)
    doc_listing_supported = callable(lister)
    current: dict[tuple[str, str], str] = {}
    current_attrs: dict[tuple[str, str], Optional[dict[str, Any]]] = {}
    if doc_listing_supported:
        for row in await lister(tenant_id, kg_name=kg_name):
            key = (row[0], row[1])
            current[key] = row[2]
            current_attrs[key] = (
                dict(row[3]) if len(row) > 3 and row[3] is not None else None
            )

    # 1+2. Markers, then the default heuristic for undecided attributes.
    marker_map = await _fetch_marker_map(neptune, tenant_id)
    literal_preds = await _distinct_literal_predicates(neptune, kg_graph)
    candidacy = await _apply_default_candidacy(
        neptune, tenant_id, kg_graph, literal_preds, marker_map
    )
    counters.update(candidacy)
    if candidacy["attrs_marked_free_text"] or candidacy["attrs_marked_not_text"]:
        marker_map = await _fetch_marker_map(neptune, tenant_id)

    marked = {uri for uri, is_ft in marker_map.items() if is_ft}
    marked_locals = {_local_name(u) for u in marked}

    # 3. Scan: marked predicates by exact URI OR local name (the extractor's
    # conflation — a marked local name covers same-named attrs on every type),
    # plus rdf:type / label predicates for the denormalized display attrs.
    scan_preds: set[str] = set(marked)
    for pred in literal_preds:
        local = _local_name(pred)
        if local in marked_locals or local in _LABEL_LOCALS:
            scan_preds.add(pred)
    scan_preds.add(_RDF_TYPE)
    scan_preds.add(_RDFS_LABEL)

    # 4. Scan (keyset-paginated, whole-entity groups).
    label_preds = {p for p in literal_preds if is_identity_predicate(p)}
    triples: list[Triple] = []
    scan_truncated = False
    if marked or (identity_index_enabled() and label_preds):
        triples, scan_truncated = await _scan_triples(
            neptune, kg_graph, sorted(scan_preds)
        )

    # Client-side re-sort for strict parity with the write hook, which sorts
    # its re-read triples in Python (kg_writer._fetch_touched_entity_triples).
    triples.sort()

    chunks = extract_semantic_chunks(
        triples, tenant_id=tenant_id, kg_name=kg_name, marked_predicates=marked
    )

    # 5. Diff against the snapshot, upsert changes.
    expected: dict[tuple[str, str], str] = {}
    for c in chunks:
        expected[(c.entity_uri, c.attr)] = c.content_hash

    if doc_listing_supported:
        to_write = []
        repaired: set[tuple[str, str]] = set()
        for c in chunks:
            key = (c.entity_uri, c.attr)
            if current.get(key) != c.content_hash:
                to_write.append(c)
                continue
            stored_attrs = current_attrs.get(key)
            if stored_attrs is not None and stored_attrs != c.attrs:
                to_write.append(c)
                repaired.add(key)
        counters["skipped_unchanged_hash"] = len(chunks) - len(to_write)
        counters["attrs_repaired"] = len(repaired)
    else:
        to_write = chunks

    if to_write:
        await _upsert_in_doc_batches(idx, to_write)
        counters["chunks_written"] = len(to_write)

    # 6. Ghosts: snapshot docs not in the expected set.
    if not doc_listing_supported:
        h.logger.warning(
            "semantic_reconcile_ghost_scan_skipped",
            tenant_id=tenant_id,
            kg_name=kg_name,
            reason="backend predates the Protocol's list_docs method",
        )
    elif scan_truncated:
        h.logger.warning(
            "semantic_reconcile_ghosts_skipped_scan_truncated",
            tenant_id=tenant_id,
            kg_name=kg_name,
            reason=(
                "the instance-graph scan hit the page cap; the expected set is "
                "partial, so ghost deletion is skipped this run (raise "
                "INFONA_SEMANTIC_SCAN_PAGE_SIZE for KGs this large)"
            ),
        )
    else:
        ghosts = sorted(set(current) - set(expected))
        if ghosts:
            deleter = getattr(idx, "delete_docs", None)
            if callable(deleter):
                await deleter(ghosts, tenant_id, kg_name=kg_name)
            else:
                for entity_uri, attr in ghosts:
                    await idx.delete(
                        entity_uri, tenant_id, kg_name=kg_name, attr=attr
                    )
        counters["ghosts_deleted"] = len(ghosts)

    h.logger.info(
        "semantic_reconcile",
        tenant_id=tenant_id,
        kg_name=kg_name,
        marked_attrs=len(marked),
        doc_listing_supported=doc_listing_supported,
        **counters,
    )
    return counters
