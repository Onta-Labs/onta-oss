"""The single insertion + post-write housekeeping path for the knowledge graph.

**Convergence rule (do not bypass).** Every process that writes instance data
into a KG — CSV/JSON ingestion AND enrichment (search results) — MUST go through
these two functions. They are the one place that decides *how* facts are written
and *what* must be refreshed afterwards, so the two paths can never drift. The
moment a writer hand-rolls its own ``insert_triples`` + housekeeping, the paths
diverge silently (the exact bug this module exists to prevent: enrichment used to
write un-batched and never re-embedded or invalidated the NL-planning cache, so a
freshly-enriched attribute served stale embeddings and stale query plans while an
ingested one did not).

Split into two composable steps because the two writers differ in *what facts
they produce* (ingest mints entities; enrichment fills attributes on existing
ones) but are identical in *how those facts get written and refreshed*:

- :func:`insert_facts` — batched instance-triple write, plus the optional
  canonical companion-provenance graph (ADR 0002 §4) AND the spatio-temporal
  secondary index (another companion store derived from the same facts: every
  geometry-bearing entity is auto-indexed for geo/time queries, best-effort).
  Always batched so a large write can never blow past Neptune's per-statement
  size limit.
- :func:`refresh_after_write` — invalidate the NL-planning ontology cache,
  re-embed the affected types (so semantic retrieval never serves a stale schema
  embedding after a new attribute lands), invalidate the stored ``kg_triple_count``
  so ``list_kgs`` recomputes after data changes, and schedule the Explorer
  type-stats recompute. Every successful write calls this with the types it touched.

Removals join the same path (ADR 0007). A fact *leaving* the graph or a subject
being *renamed* carries the identical fan-out obligation as an insert, so:

- :func:`delete_facts` — the one removal primitive (batched whole-subject or
  triple deletes) + a provenance *tombstone*.
- :func:`rewrite_subject` — the one URI-rewrite primitive (ER merge) + a
  provenance *rewrite* event; expressed as a single re-key event, NOT
  delete-then-insert, so derived indexes re-key cheaply instead of recomputing.
- :func:`refresh_after_write` grows ``deleted_subjects`` / ``rewritten_subjects``
  kwargs (no sibling ``refresh_after_delete`` — that fork is the banned drift):
  the same housekeeping pass evicts deleted subjects and re-keys renamed ones
  from every derived secondary index.

Implementation lives in sibling ``kg_writer_*.py`` modules. Every previously
importable name is re-exported here. Residual SPARQL companions
(:func:`ensure_kg_registered`, :func:`_record_value_history`,
:func:`rewrite_predicates`) stay in this facade so the write-path allowlist
does not fork.

Layering note: this module sits in ``graph/`` and must stay importable without
pulling in ``nlp`` or the API routes, so the embedding-service / ontology-cache /
stats-recompute dependencies are imported lazily inside
:func:`refresh_after_write` (they live in higher layers). Housekeeping is
best-effort — embedding/stats failures are logged, never raised — matching the
non-blocking behavior the ingest routes already had.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from infona_client.graph.history import (
    build_value_change_triples,
    history_graph_uri,
    lexical_value,
)
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import (
    KG_NAME_PRED,
    _escape_literal,
    batched_insert_triples,
    is_valid_kg_name,
    kg_meta_uri,
    rewrite_predicate_update,
    select_subject_predicate_objects_query,
    tenant_graph_uri,
)
from infona_client.graph.kg_writer_delta import (  # noqa: F401 — public re-exports
    DELTA_NONCE_PREDICATES,
    GraphDelta,
    build_graph_delta,
)
from infona_client.graph.kg_writer_insert import (  # noqa: F401 — public re-exports
    _fetch_touched_entity_triples,
    _index_semantic,
    _index_semantic_inner,
    _index_spatiotemporal,
    _insert_facts_store,
    insert_facts,
)
from infona_client.graph.kg_writer_mutate import (  # noqa: F401 — public re-exports
    _delete_facts_store,
    _record_value_history_store,
    delete_facts,
    rewrite_subject,
)
from infona_client.graph.kg_writer_refresh import (  # noqa: F401 — public re-exports
    _deindex_secondary,
    refresh_after_write,
)
from infona_client.graph.kg_writer_session import (  # noqa: F401 — public re-exports
    _INDEX_UPSERT_TIMEOUT_S,
    _chunk,
    _count_matching,
    _provenance_enabled,
    _resolve_graph_session,
    _semantic_hook_max_entities,
    _semantic_upsert_timeout_s,
    _value_history_enabled,
    _warn_unported_companions,
)

# NOTE: `graph_backend` used to be defined here as well as in `graph/store.py`.
# Two copies of a backend switch is exactly the drift ONTA-527 removes — the one
# switch now lives in `graph.store` and this module does not re-export it.

logger = structlog.stdlib.get_logger("infona.graph.kg_writer")

Triple = tuple[str, str, str]

# KG-registration triple shape — the `<kg_uri> <onto/kg_name> "name"` record in
# the tenant metadata graph that ``list_kgs`` reads to populate the Explorer
# dropdown, and that the ONTA-413 existence probe ASKs for. The predicate + URI
# shape now live in ``graph/queries.py`` (the same layer, no dependency on
# ``api.routes``) so the writer here, ``create_kg``, ``list_kgs`` and the probe
# cannot drift apart.
_KG_NAME_PRED = KG_NAME_PRED
_kg_meta_uri = kg_meta_uri

# A KG name that can legally be created via the Explorer ("New KG" button) and
# that ``kg_graph_uri`` will accept: a name that can't be created via the UI must
# not be allowed to silently corrupt the registration URI (``<{kg_uri}>``
# interpolates the raw name, so a `>` or whitespace would break the URI even when
# the literal is escaped).
#
# ONTA-414 folded this into the ONE predicate in ``graph/queries.py``. The local
# copy that used to live here had already drifted (``$`` instead of ``\Z``, so it
# accepted a trailing newline that ``KGCreate.name`` rejects). Registration stays
# a warn-and-skip rather than a raise, because it is best-effort post-write
# housekeeping that must never fail a write.


async def ensure_kg_registered(neptune, tenant_id: str, kg_name: str) -> None:
    """Idempotently register a KG in the tenant metadata graph.

    Writes the ``<kg_uri> <onto/kg_name> "name"`` record that ``list_kgs`` reads
    to populate the Explorer dropdown — but ONLY if the KG is not already
    registered. Historically this record was written in exactly one place
    (``create_kg``, the Explorer's "New KG" button), so any non-UI writer (agent
    web-discovery, CLI, MCP) that ingested into a brand-new ``kg_name`` left the
    KG invisible. Folding registration into the shared write path fixes that for
    every writer at once.

    Idempotent + non-clobbering by construction: a single
    ``INSERT … WHERE { FILTER NOT EXISTS { <kg_uri> <kg_name> ?n } }`` so it (a)
    never duplicates the registration triple and (b) never overwrites an existing
    registration or its ``kg_description`` (the whole INSERT is skipped when any
    ``kg_name`` already exists for this KG URI).

    Deliberately does NOT write ``kg_triple_count 0``: data has already been
    ingested by the time the shared write path registers, so a literal ``0`` would
    be stale-on-arrival and ``list_kgs`` only live-counts when the count is
    *absent*. Leaving it absent lets ``list_kgs`` lazily compute + persist the
    real count on first read.

    Safety: the literal is escaped via the canonical ``_escape_literal`` (no
    SPARQL-literal breakout on a name containing ``"`` / ``\\`` / newline), and the
    name is validated against the same ``^[a-zA-Z0-9_-]+$`` pattern the UI
    enforces before it's interpolated into the registration URI — a name that
    couldn't be created via the UI is skipped rather than allowed to corrupt the
    URI. Best-effort overall: a failure is logged, never raised, matching the rest
    of the post-write housekeeping.
    """
    if not kg_name:
        return
    if not is_valid_kg_name(kg_name):
        # A name with URI-breaking characters (``>``, whitespace, …) can't be a
        # real KG (the UI rejects it), so don't risk corrupting the metadata
        # graph — log and skip rather than emit a malformed registration.
        logger.warning("ensure_kg_registered_invalid_name", kg_name=kg_name)
        return
    base = tenant_graph_uri(tenant_id)
    kg_uri = _kg_meta_uri(tenant_id, kg_name)
    sparql = (
        f"WITH <{base}>\n"
        f"INSERT {{\n"
        f'  <{kg_uri}> <{_KG_NAME_PRED}> "{_escape_literal(kg_name)}" .\n'
        f"}}\n"
        f"WHERE {{\n"
        f"  FILTER NOT EXISTS {{ <{kg_uri}> <{_KG_NAME_PRED}> ?n }}\n"
        f"}}"
    )
    try:
        await neptune.update(sparql)
    except Exception:  # noqa: BLE001 — never fail a write on a registration hiccup
        logger.warning("ensure_kg_registered_failed", kg_name=kg_name, exc_info=True)


async def _record_value_history(
    neptune,
    instance_graph: str,
    sp_pairs: list[tuple[str, str]],
    new_values: dict[tuple[str, str], str],
) -> None:
    """Legacy SPARQL ValueHistory writer (kept for tests that mock Neptune).

    Production uses :func:`_record_value_history_store` on the GraphStore path
    (ONTA-536). This body remains for hermetic unit tests that still drive the
    SPARQL seam with AsyncMock.
    """
    # Only pairs the caller gave a replacement value for can be a tracked change.
    tracked = [(s, p) for (s, p) in sp_pairs if (s, p) in new_values]
    if not tracked:
        return
    try:
        _, rows = parse_sparql_results(
            await neptune.query(
                select_subject_predicate_objects_query(instance_graph, tracked)
            )
        )
        now = datetime.now(timezone.utc)
        hist_triples: list[Triple] = []
        for row in rows:
            s, p, old = row.get("s", ""), row.get("p", ""), row.get("o", "")
            if not s or not p:
                continue
            new = new_values.get((s, p))
            if new is None:
                continue
            # The reader already returns the LEXICAL form; normalize new the same
            # way so old==new is a true no-op regardless of serialization.
            hist_triples.extend(
                build_value_change_triples(s, p, old, lexical_value(new), changed_at=now)
            )
        if hist_triples:
            hist_graph = history_graph_uri(instance_graph)
            for sparql in batched_insert_triples(hist_graph, hist_triples):
                await neptune.update(sparql)
    except Exception:  # noqa: BLE001 — history is a derived companion, never the write
        logger.warning(
            "value_history_record_failed",
            instance_graph=instance_graph,
            exc_info=True,
        )


async def rewrite_predicates(
    neptune,
    instance_graph: str,
    mapping: dict[str, str],
    *,
    reason: str = "",
) -> int:
    """Re-key instance predicates in place — the predicate-rewrite primitive.

    For each ``old_pred -> new_pred`` pair, moves every ``(s, old_pred, o)``
    triple onto ``new_pred`` server-side (``rewrite_predicate_update``), so the
    object term keeps its exact datatype — never a client-side read-then-reinsert,
    which would strip a typed ``xsd:dateTime`` down to a plain string (the
    ONTA-247 lesson). Idempotent: re-running on already-rewritten data is a no-op.

    Built for the attr_meta companion migration (ONTA-262: legacy
    ``attrs/<attr>_<suffix>`` provenance companions → ``attr_meta/<Type>/<attr>/
    <suffix>``). Deliberately writes NO per-triple provenance events: companions
    are display metadata OF a fact, not facts — a tombstone/rewrite record per
    stamp would inflate the governance graph with zero governance value. Callers
    moving REAL domain predicates should think hard before reusing this.

    Does NOT itself refresh derived state: call :func:`refresh_after_write` with
    the touched types once per migration so a single housekeeping pass recomputes
    stats and invalidates the NL-planning cache. Returns the number of predicate
    pairs rewritten.
    """
    done = 0
    for old_pred, new_pred in mapping.items():
        if not old_pred or not new_pred or old_pred == new_pred:
            continue
        await neptune.update(
            rewrite_predicate_update(instance_graph, old_pred, new_pred)
        )
        done += 1
    if done:
        logger.info(
            "predicates_rewritten",
            instance_graph=instance_graph,
            count=done,
            reason=reason,
        )
    return done
