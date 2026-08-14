"""Post-write housekeeping shared by every converged writer (ADR 0007).

:func:`refresh_after_write` is the only sanctioned refresh. There is no
``refresh_after_delete``. Look up sibling / facade names via :func:`_host`
so tests that monkeypatch ``infona_client.graph.kg_writer.<name>`` keep working.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Iterable, Optional

import structlog

from infona_client.graph.queries import tenant_graph_uri

if TYPE_CHECKING:
    from infona_client.graph.store import GraphSession, GraphStore

logger = structlog.stdlib.get_logger("infona.graph.kg_writer")


def _host():
    from infona_client.graph import kg_writer as _mod

    return _mod

async def refresh_after_write(
    neptune,
    *,
    tenant_id: str,
    kg_name: Optional[str],
    affected_types: Iterable[str] = (),
    recompute_stats: bool = True,
    deleted_subjects: Iterable[str] = (),
    rewritten_subjects: Optional[dict[str, str]] = None,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
) -> None:
    """Post-write housekeeping shared by ingest + enrichment + removals (ADR 0007).

    Runs the refreshes a write can invalidate, in order:

    1. **Invalidate the NL-planning ontology cache** for the tenant graph, so a
       newly-declared type/attribute is visible to query planning immediately
       instead of after a TTL.
    2. **Re-embed the affected types** (``affected_types`` — types whose schema
       changed: new types, or types that gained an attribute) so semantic
       retrieval never serves a stale schema embedding. No-op when the embedding
       service is unconfigured.
    3. **Register the KG** in the tenant metadata graph (idempotently) when
       ``kg_name`` is given, so a non-UI writer (web-discovery, CLI, MCP) that
       ingested into a brand-new KG still shows up in the Explorer dropdown —
       not just KGs created via the "New KG" button (ONTA-153).
    4. **Invalidate the stored triple count** for the KG when ``kg_name`` is
       given, so the next ``list_kgs`` / ``kg list`` live-counts instead of
       serving a stale ``kg_triple_count`` (commonly a sticky ``0`` written when
       the empty KG was first listed). Kept HERE rather than only on the
       Explorer recompute path so every converged writer benefits immediately —
       recompute is background and may lag or fail without clearing the count.
    5. **Schedule the Explorer type-stats recompute** for the KG (coverage %,
       counts) when ``kg_name`` is given and ``recompute_stats`` is set.
    6. **Evict / re-key derived secondary indexes** for removals and renames:
       ``deleted_subjects`` are dropped from the spatiotemporal index (and the
       upcoming semantic index); ``rewritten_subjects`` (old → new, from an ER
       merge) are re-keyed rather than evicted. Both default empty so every
       existing call site is untouched. This is the removal-side mirror of
       ``insert_facts``'s ``_index_spatiotemporal`` — a sibling
       ``refresh_after_delete`` is deliberately NOT added (a forked refresh is the
       exact drift this convergence bans; an attribute *update* is a delete +
       insert and must run one refresh, not two).

    Best-effort: embedding and stats failures are logged and swallowed (a write
    must not fail because a downstream refresh hiccuped), matching the ingest
    routes' existing non-blocking behavior. Imports are lazy to keep ``graph/``
    free of ``nlp`` / API-route import cycles.
    """
    onto_graph = tenant_graph_uri(tenant_id)

    # 1. NL-planning ontology cache.
    try:
        from infona_client.nlp.pipeline import NLQueryPipeline

        NLQueryPipeline.invalidate_cache(onto_graph)
    except Exception:  # noqa: BLE001 — never fail a write on a cache hiccup
        logger.warning("ontology_cache_invalidate_failed", exc_info=True)

    # 1b. Low-cardinality dim registry (NL filter binding). Best-effort
    #     invalidate so the next /ask rebuilds from fresh inventory; never
    #     block a write on dim-cache housekeeping.
    if kg_name:
        try:
            from infona_client.nlp.dim_registry import invalidate_dim_registry

            invalidate_dim_registry(tenant_id, kg_name)
        except Exception:  # noqa: BLE001
            logger.debug("dim_registry_invalidate_failed", exc_info=True)

    # NOTE (ONTA-177/ONTA-173): the free-text marker cache
    # (graph/text_markers.py) is deliberately NOT invalidated here. Most writes
    # touch no textKind markers, and refresh_after_write runs after EVERY
    # converged write — an unconditional invalidation here defeated the cache's
    # 60s TTL on the hot path (every write→refresh cycle forced the semantic
    # hook's next marker read back to Neptune). Marker WRITE sites own their
    # own invalidation instead: the schema pass's candidacy seams
    # (SchemaResolver._mark_free_text_attributes / _apply_mapping_text_markers)
    # and the reconciler's default-candidacy heuristic each self-invalidate
    # right after upserting markers; the TTL remains the cross-process backstop.

    # 2. Re-embed affected types (dedup, order-preserving).
    # Prefer GraphStore catalog (Neo4j-only product path). Neptune SPARQL
    # residual remains inside embed_types when catalog is empty.
    types = list(dict.fromkeys(t for t in affected_types if t))
    if types:
        try:
            from infona_client.graph.store import get_optional_graph_store
            from infona_client.nlp.pipeline import get_embedding_service

            svc = get_embedding_service()
            if svc is not None:
                gs = store
                if gs is None:
                    try:
                        gs = get_optional_graph_store()
                    except Exception:
                        gs = None
                await svc.embed_types(
                    onto_graph,
                    types,
                    neptune,
                    store=gs,
                    tenant_id=tenant_id,
                )
        except Exception:  # noqa: BLE001 — non-blocking, mirrors the ingest routes
            logger.warning("embed_types_failed", types=types, exc_info=True)

    # 3. Register the KG so non-UI writers don't leave it invisible to list_kgs
    #    (ONTA-153). Property-graph :KnowledgeGraph nodes; the SPARQL tenant-meta
    #    branch went out with the Neptune path (ONTA-527).
    if kg_name:
        try:
            from infona_client.graph.kg_registry import ensure_kg_registered_store

            await ensure_kg_registered_store(tenant_id, kg_name)
        except Exception:  # noqa: BLE001
            logger.warning(
                "ensure_kg_registered_store_failed", kg_name=kg_name, exc_info=True
            )

    # 4. Drop the stored triple count so list_kgs recomputes on next read.
    #    Must run on every successful instance write (not only Explorer recompute):
    #    a stored 0 from listing an empty KG otherwise sticks after ingest.
    #    Lazy import avoids an import cycle with api.routes.knowledge_graphs.
    if kg_name and neptune is not None:
        try:
            from infona_client.api.routes.knowledge_graphs import (
                invalidate_triple_count,
            )

            await invalidate_triple_count(neptune, tenant_id, kg_name)
        except Exception:  # noqa: BLE001 — never fail a write on a count hiccup
            logger.warning(
                "invalidate_triple_count_failed",
                kg_name=kg_name,
                exc_info=True,
            )

    # 5. Explorer type-stats recompute (background, best-effort).
    if recompute_stats and kg_name and neptune is not None:
        try:
            from infona_client.api.routes.explore import schedule_recompute

            schedule_recompute(neptune, tenant_id, kg_name)
        except Exception:  # noqa: BLE001
            logger.warning("schedule_recompute_failed", exc_info=True)

    # 6. Derived secondary-index maintenance for removals / renames.
    await _host()._deindex_secondary(
        tenant_id, kg_name, list(deleted_subjects), rewritten_subjects or {}
    )


async def _deindex_secondary(
    tenant_id: str,
    kg_name: Optional[str],
    deleted_subjects: list[str],
    rewritten_subjects: dict[str, str],
) -> None:
    """Evict deleted subjects and re-key renamed ones from derived secondary indexes.

    The removal-side mirror of :func:`_index_spatiotemporal`: when a fact LEAVES
    the graph (delete) or a subject is RENAMED (ER merge), every derived index
    keyed by subject URI must drop the ghost row (delete) or move it to the new
    key (re-key), exactly as an insert upserts. Leaving this out is what let the
    spatiotemporal index accumulate ghost rows for merged-away / deleted subjects
    (ADR 0007). Best-effort + time-bounded, same isolation as the insert side —
    Neptune is the source of truth and this index is eventually consistent.

    SEMANTIC-INDEX SEAM (ONTA-173): the upcoming embeddings-keyed-by-node-URI
    index subscribes to the SAME three event kinds — insert (via
    ``_index_spatiotemporal``), delete (evict below), rewrite (re-key below). Add
    its ``delete`` / ``rekey`` calls right alongside the spatiotemporal ones here
    so it never inherits the ghost-row problem this function exists to prevent.
    """
    if not deleted_subjects and not rewritten_subjects:
        return
    try:
        from infona_client.spatiotemporal.registry import get_spatiotemporal_index

        index = get_spatiotemporal_index()

        async def _work() -> None:
            for uri in deleted_subjects:
                await index.delete(uri, tenant_id, kg_name=kg_name)
            for old, new in rewritten_subjects.items():
                rekey = getattr(index, "rekey", None)
                if rekey is not None:
                    await rekey(old, new, tenant_id, kg_name=kg_name)
                else:
                    # A backend that predates re-key (an out-of-tree override):
                    # evict the stale key so a ghost row can't survive. Correctness
                    # (no ghost) over the re-key cost saving.
                    await index.delete(old, tenant_id, kg_name=kg_name)

        # Time-bounded so a hung backend can't block the write (see the
        # _host()._INDEX_UPSERT_TIMEOUT_S note); TimeoutError is caught below.
        await asyncio.wait_for(_work(), timeout=_host()._INDEX_UPSERT_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — never fail a write on a derived-index hiccup
        logger.warning(
            "spatiotemporal_index_deindex_failed",
            tenant_id=tenant_id,
            kg_name=kg_name,
            exc_info=True,
        )

    # Semantic index (ONTA-173) — the seam this function's docstring reserves:
    # same three event kinds, so deletes/rewrites evict here just like inserts
    # index in _index_semantic. Gated like the write hook (rows can only exist
    # when the gate has been on; touching the backend on the delete path while
    # the feature is off would create pools/DDL for nothing). Own try/except +
    # timeout so a semantic hiccup neither fails the write nor masquerades as
    # a spatiotemporal failure in the logs.
    try:
        from infona_client.semantic.reconciler import semantic_index_enabled

        if not semantic_index_enabled():
            return
        from infona_client.semantic.registry import get_semantic_index

        sem = get_semantic_index()

        async def _semantic_evict() -> None:
            for uri in deleted_subjects:
                await sem.delete(uri, tenant_id, kg_name=kg_name)
            for old in rewritten_subjects:
                # No rekey on the SemanticIndex protocol: chunks/hashes derive
                # from Neptune values, so evict the stale key and let the write
                # hook / reconciler re-index the NEW URI from the re-keyed
                # triples (correctness over the re-embed cost, mirroring the
                # spatiotemporal no-rekey fallback above).
                await sem.delete(old, tenant_id, kg_name=kg_name)

        await asyncio.wait_for(_semantic_evict(), timeout=_host()._semantic_upsert_timeout_s())
    except Exception:  # noqa: BLE001 — never fail a write on a derived-index hiccup
        logger.warning(
            "semantic_index_deindex_failed",
            tenant_id=tenant_id,
            kg_name=kg_name,
            exc_info=True,
        )
