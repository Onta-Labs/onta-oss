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

Layering note: this module sits in ``graph/`` and must stay importable without
pulling in ``nlp`` or the API routes, so the embedding-service / ontology-cache /
stats-recompute dependencies are imported lazily inside
:func:`refresh_after_write` (they live in higher layers). Housekeeping is
best-effort — embedding/stats failures are logged, never raised — matching the
non-blocking behavior the ingest routes already had.
"""


from __future__ import annotations

from infona_client.graph.iri import IRI_BASE
import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Optional, Sequence

import structlog

from infona_client.graph.history import (
    build_value_change_triples,
    history_graph_uri,
    lexical_value,
)
from infona_client.pipeline.envelope import derive_fact_id
from infona_client.graph.parser import parse_sparql_results
from infona_client.graph.queries import (
    KG_NAME_PRED,
    _escape_literal,
    batched_insert_triples,
    is_valid_kg_name,
    kg_meta_uri,
    parse_kg_graph_uri,
    rewrite_predicate_update,
    select_subject_predicate_objects_query,
    tenant_graph_uri,
)
from infona_client.graph.facts import Fact, triples_to_facts
from infona_client.graph.scope import GraphScope, GraphScopeError

# NOTE: `graph_backend` used to be defined here as well as in `graph/store.py`.
# Two copies of a backend switch is exactly the drift ONTA-527 removes — the one
# switch now lives in `graph.store` and this module does not re-export it.

if TYPE_CHECKING:
    from infona_client.graph.store import GraphSession, GraphStore

logger = structlog.stdlib.get_logger("infona.graph.kg_writer")

Triple = tuple[str, str, str]

# Hard cap on the synchronous spatio-temporal index upsert inside insert_facts.
# The index is a DERIVED, eventually-consistent companion store; Neptune (the
# source of truth) is already written by the time we reach it. Catching exceptions
# isn't enough — a hung/partitioned Postgres (pool exhaustion, Aurora failover)
# would otherwise block the KG-write request on this await with no exception. The
# timeout converts a hang into a caught TimeoutError → logged, index skipped, the
# write proceeds. Env-overridable for ops.
_INDEX_UPSERT_TIMEOUT_S = float(
    os.environ.get("INFONA_SPATIOTEMPORAL_UPSERT_TIMEOUT_S", "10")
)


def _semantic_upsert_timeout_s() -> float:
    """Timeout for the semantic-index write hook (ONTA-181) — the same hang-to-
    TimeoutError conversion as ``_INDEX_UPSERT_TIMEOUT_S``, with its own knob
    because the semantic hook does strictly more work per write (marker-map
    read + touched-entity re-read + chunk upsert + empty-doc deletes). Read per
    call so tests/ops can tune it without re-importing the module."""
    return float(os.environ.get("INFONA_SEMANTIC_UPSERT_TIMEOUT_S", "10"))


def _semantic_hook_max_entities() -> int:
    """Cap on TOUCHED ENTITIES the semantic hook re-reads from Neptune per
    write. The touched-entity fetch is one VALUES-scoped SELECT, so its cost
    scales with the entity count of the write; a huge ingest batch must not
    turn the hook into a full-graph scan. Overflow is logged (never silent)
    and repaired by the reconciler's next full scan. Read per call so
    tests/ops can tune it without re-importing the module."""
    try:
        return int(float(os.environ.get("INFONA_SEMANTIC_HOOK_MAX_ENTITIES", "500")))
    except ValueError:
        return 500


def _provenance_enabled(*, store_path: bool = False) -> bool:
    """Whether removal/rename primitives write companion-graph provenance events.

    Gated by the same ``INFONA_PROVENANCE_ENABLED`` env var the ingest path uses
    for assertion provenance (default OFF), so tombstone/rewrite events only land
    when governance/undo is switched on.

    E8 store-path optional always-on: when ``store_path=True`` and
    ``INFONA_PROVENANCE_STORE_ALWAYS=1``, provenance events fire on the
    property-graph path even if the global flag is off (useful for hermetic
    isolation QC / Neo4j local without enabling Neptune companion graphs).
    """
    if os.environ.get("INFONA_PROVENANCE_ENABLED", "0") == "1":
        return True
    if store_path and os.environ.get("INFONA_PROVENANCE_STORE_ALWAYS", "0") == "1":
        return True
    return False


def _resolve_graph_session(
    *,
    store: Optional["GraphStore"] = None,
    session: Optional["GraphSession"] = None,
    instance_graph: str | None = None,
    tenant_id: str | None = None,
    kg_name: str | None = None,
) -> "GraphSession":
    """Return the scoped GraphSession for this write.

    Priority:
    1. Explicit ``session``
    2. Explicit ``store`` + scope derived from graph URI or tenant/kg
    3. Process :func:`get_optional_graph_store` (Neo4j / injected test store)

    Never returns ``None``: Neo4j is the only backend (ONTA-527), so there is
    no SPARQL path to fall back to. Raises :class:`GraphConfigError` when no
    store is configured and :class:`GraphScopeError` when the scope cannot be
    derived — fail closed rather than write nowhere.
    """
    if session is not None:
        # When both an explicit session and instance_graph are supplied, fail
        # closed if they disagree — silent cross-scope writes are worse than a
        # loud GraphScopeError (isolation / ADR 0012).
        if instance_graph:
            scope_pair = parse_kg_graph_uri(instance_graph)
            sess_scope = getattr(session, "scope", None)
            if (
                scope_pair is not None
                and sess_scope is not None
                and (
                    getattr(sess_scope, "tenant_id", None) != scope_pair[0]
                    or getattr(sess_scope, "kg", None) != scope_pair[1]
                )
            ):
                raise GraphScopeError(
                    f"session scope ({getattr(sess_scope, 'tenant_id', None)!r}/"
                    f"{getattr(sess_scope, 'kg', None)!r}) does not match "
                    f"instance_graph ({scope_pair[0]!r}/{scope_pair[1]!r})"
                )
        return session
    if store is None:
        from infona_client.graph.store import get_optional_graph_store

        store = get_optional_graph_store()
    tid, kg = tenant_id, kg_name
    if (not tid or not kg) and instance_graph:
        scope_pair = parse_kg_graph_uri(instance_graph)
        if scope_pair is None:
            raise GraphScopeError(
                f"Cannot derive tenant/kg scope from instance_graph={instance_graph!r}; "
                "pass tenant_id+kg_name or a per-KG graph URI"
            )
        tid, kg = scope_pair
    if not tid or not kg:
        raise GraphScopeError(
            "Neo4j write path requires tenant_id and kg (or a parseable instance_graph)"
        )
    return store.session(GraphScope.for_instance(tid, kg))


def _value_history_enabled() -> bool:
    """Whether an attribute UPDATE records a dated value-history entry (ONTA-236).

    Gated by ``INFONA_VALUE_HISTORY_ENABLED`` (default OFF) so bulk ingest stays
    byte-stable and the extra read-before-delete + companion-graph write are only
    paid where "what changed, old→new, when" matters. When ON, ``delete_facts``
    reads the prior value of each predicate-scoped clear it is given a NEW value
    for, and versions any genuine change (see :func:`_record_value_history`). The
    mechanism is GENERAL — it versions ANY attribute of ANY type, with zero
    domain knowledge.
    """
    return os.environ.get("INFONA_VALUE_HISTORY_ENABLED", "0") == "1"


def _chunk(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def _count_matching(neptune, count_sparql: str) -> int:
    """Best-effort ``SELECT (COUNT(*) AS ?n)`` → int (0 on any failure).

    Used by :func:`delete_facts` to return an accurate removed-triple count for
    the pattern-based (subject / predicate-scoped) removals, whose count can't be
    known up front the way a concrete-triple list's can. Best-effort because the
    count is informational — a hiccup here must never fail the delete."""
    try:
        _, rows = parse_sparql_results(await neptune.query(count_sparql))
        return int(rows[0].get("n", 0)) if rows else 0
    except Exception:  # noqa: BLE001 — the count is informational, never load-bearing
        return 0

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


# --- A6 Graph Delta (ONTA-271) ------------------------------------------------
#
# Predicates PROJECTED OUT of a deterministic Graph Delta: the write-path
# bookkeeping NONCES that qc/boundary also omits from its byte-stable A2/A4/A5
# fixtures. ``ingested_at`` is a wall-clock stamp and ``batch_id`` a per-run
# token; neither is a domain fact, and including either would make a replayed
# run's delta differ even when every real fact is identical. Excluding them is
# what lets a byte-identical Graph Delta prove an upstream replay reproduced the
# graph (the P6 determinism the ticket requires). The instance-graph values are
# additionally made replay-stable at the source (batch_id derived from run_id,
# ingested_at sourced from the runf's observed_at), so the store write itself is
# idempotent — the exclusion here is belt-and-suspenders + a clean fact-level
# delta.
DELTA_NONCE_PREDICATES = frozenset(
    {
        f"{IRI_BASE}/onto/ingested_at",
        f"{IRI_BASE}/onto/batch_id",  # == graph.queries.BATCH_PREDICATE
    }
)


@dataclass(frozen=True)
class GraphDelta:
    """A6 — a deterministic, replay-stable receipt of the domain facts a write applied.

    Mirrors qc/boundary's determinism discipline: the de-duplicated, SORTED set
    of instance ``(subject, predicate, object)`` triples with the bookkeeping
    NONCES (``DELTA_NONCE_PREDICATES``) projected out, each stamped with the
    stable per-subject ``fact_id`` (a pure function of ``run_id`` + subject URI,
    the content-stable ``local_key`` that flows A2→A6). Two runs of the SAME
    facts under the SAME ``run_id`` therefore produce byte-identical
    :meth:`canonical_bytes`, so P6 can dedupe an upstream replay instead of
    duplicating the graph (ONTA-271).

    ``fan_in`` records source-fact → canonical-node merges (ER auto-merge,
    key-join, in-run same-key dedup) as sorted ``(source_fact_id,
    canonical_fact_id)`` pairs — the mapping is otherwise invisible once several
    source facts collapse onto one node.
    """

    run_id: Optional[str]
    instance_graph: str
    facts: tuple[tuple[str, str, str, str], ...]  # (fact_id, s, p, o), sorted
    fan_in: tuple[tuple[str, str], ...] = ()  # (source_fact_id, canonical_fact_id), sorted

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "instance_graph": self.instance_graph,
            "facts": [list(f) for f in self.facts],
            "fan_in": [list(p) for p in self.fan_in],
        }

    def canonical_bytes(self) -> bytes:
        """Byte-stable serialization for replay comparison (sorted keys, UTF-8)."""
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def build_graph_delta(
    instance_graph: str,
    instance_triples: list[Triple],
    *,
    run_id: Optional[str] = None,
    fan_in: Optional[dict[str, str]] = None,
) -> GraphDelta:
    """Project written instance triples into a deterministic A6 :class:`GraphDelta`.

    Drops the bookkeeping nonces (``DELTA_NONCE_PREDICATES``), de-dups + SORTS the
    remaining ``(s, p, o)``, and stamps each with the stable per-subject
    ``fact_id = derive_fact_id(run_id, stage="A6", local_key=subject)``. Pure and
    deterministic: the same triples under the same ``run_id`` always yield
    byte-identical :meth:`GraphDelta.canonical_bytes`, so a caller can PROVE an
    upstream replay reproduced the graph exactly (ONTA-271).

    ``fan_in`` maps ``{source_subject_uri: canonical_subject_uri}`` for facts that
    merged onto one node; both sides are resolved to their stable fact_ids and
    recorded, sorted. ``run_id=None`` still yields a valid (run-agnostic) delta.
    """

    def _fid(subject: str) -> str:
        return derive_fact_id(run_id=run_id or "", stage="A6", local_key=subject)

    domain = {
        (s, p, o)
        for (s, p, o) in instance_triples
        if s and p and p not in DELTA_NONCE_PREDICATES
    }
    facts = tuple(sorted((_fid(s), s, p, o) for (s, p, o) in domain))
    fan_pairs = tuple(
        sorted(
            (_fid(src), _fid(dst))
            for src, dst in (fan_in or {}).items()
            if src and dst and src != dst
        )
    )
    return GraphDelta(
        run_id=run_id, instance_graph=instance_graph, facts=facts, fan_in=fan_pairs
    )


def _warn_unported_companions(
    instance_graph: str,
    *,
    validity_triples: Optional[list[Triple]] = None,
    suppression_triples: Optional[list[Triple]] = None,
    reopen_facts: Optional[list[Triple]] = None,
) -> None:
    """Log once per write when a caller passes an unported companion payload.

    Valid-time and suppression companions were named-graph SPARQL writes. Their
    property-graph node ports are E7, so on Neo4j these payloads have been
    dropped on the floor since the cutover. Warning here turns a silent no-op
    into something greppable (ONTA-527).
    """
    unported = [
        name
        for name, payload in (
            ("validity_triples", validity_triples),
            ("suppression_triples", suppression_triples),
            ("reopen_facts", reopen_facts),
        )
        if payload
    ]
    if unported:
        logger.warning(
            "insert_facts_companion_payload_not_ported",
            instance_graph=instance_graph,
            payloads=unported,
            detail=(
                "valid-time / suppression companions have no property-graph "
                "port yet (E7); payload ignored"
            ),
        )


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

    **Ignored RDF companion payloads** (``provenance_triples`` /
    ``validity_triples`` / ``suppression_triples`` / ``reopen_facts``): these
    were named-graph writes on the deleted SPARQL path. The property-graph
    equivalents are companion NODES, and only the provenance half is ported —
    ``:ProvEvent`` assert hooks fire inside the store path when
    ``INFONA_PROVENANCE_ENABLED=1``. Callers still passing validity /
    suppression payloads get a warning rather than silence, because "wrote
    nothing" was already the Neo4j behavior and it should be visible (validity /
    suppression node ports are E7).

    ``run_id`` (ONTA-271): when given, returns a deterministic A6
    :class:`GraphDelta` over the *triple* form of the write (Fact-only writes
    without triples yield an empty domain set unless triples are also supplied).
    """
    _warn_unported_companions(
        instance_graph,
        validity_triples=validity_triples,
        suppression_triples=suppression_triples,
        reopen_facts=reopen_facts,
    )
    instance_triples = list(instance_triples or [])
    gs = _resolve_graph_session(
        store=store, session=session, instance_graph=instance_graph
    )
    return await _insert_facts_store(
        gs,
        instance_graph,
        instance_triples=instance_triples,
        facts=facts,
        run_id=run_id,
    )


async def _insert_facts_store(
    session: "GraphSession",
    instance_graph: str,
    *,
    instance_triples: list[Triple],
    facts: Optional[Sequence[Fact]],
    run_id: Optional[str],
) -> Optional[GraphDelta]:
    """Property-graph insert path (Memory / Neo4j). Template/native ops only."""
    from infona_client.graph import pg_ops

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
    if fact_list:
        await pg_ops.apply_facts(
            session,
            fact_list,
            provenance_enabled=_provenance_enabled(store_path=True),
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
    # Secondary indexes still key off graph URI + triple-shaped payloads when
    # available (best-effort).
    #
    # GAP (pre-dates ONTA-527, made visible by it): the SEMANTIC index write
    # hook (`_index_semantic`, ONTA-181/421) is NOT called here. It rebuilds
    # each touched entity's docs from a re-read of the store, and that re-read
    # is SPARQL — so the hook has been dead in production since the Neo4j
    # cutover, not merely since this deletion. Freshness now depends on the
    # claim-based reconciler (`semantic/reconciler.py`) alone until the hook is
    # ported to GraphStore.
    if instance_triples:
        await _index_spatiotemporal(instance_graph, instance_triples)
    if run_id is not None:
        return build_graph_delta(instance_graph, instance_triples, run_id=run_id)
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
            # _INDEX_UPSERT_TIMEOUT_S note); TimeoutError is caught below.
            await asyncio.wait_for(
                get_spatiotemporal_index().upsert_many(facts),
                timeout=_INDEX_UPSERT_TIMEOUT_S,
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
    of marked free-text attributes land in the same request that wrote Neptune,
    with ``embedding=NULL`` — the store-side generated tsvector makes them
    lexically searchable instantly; vector recall follows within one embed-fill
    sweep. Kept HERE in the single insertion primitive (like the
    spatio-temporal hook above) so EVERY converged writer auto-indexes with no
    per-caller wiring.

    Env-gated OFF by default (``INFONA_SEMANTIC_INDEX_ENABLED`` — cost/rollout
    control: indexing implies embedding spend and index growth). Marker-driven:
    only predicates the tenant's textKind map (``graph/text_markers.py``) marks
    ``free_text`` are extracted — free text has no distinguishing datatype, so
    unlike the spatio-temporal hook this one needs the ``neptune`` handle to
    consult the (TTL-cached) marker map.

    ONE exception, marker-INDEPENDENT (ONTA-421): every named entity also gets
    an identity doc, so a write that carries only ``rdfs:label`` / ``name`` /
    ``title`` now touches the index where it previously touched nothing. That is
    the cost side of the fix: a KG with no marked attribute at all used to pay
    zero Neptune re-reads per write and now pays one bounded, VALUES-scoped
    SELECT (still capped by ``INFONA_SEMANTIC_HOOK_MAX_ENTITIES``, still under
    the one timeout, still best-effort). ``INFONA_SEMANTIC_IDENTITY_INDEX=0``
    restores the old write-path behavior — but note it is not free to flip: the
    next reconcile of each KG then sees every identity doc as a ghost and
    batch-deletes it (symmetric and self-healing on flip-back, but a mass delete
    to expect rather than discover).

    Completeness contract (the ONTA-173 partial-doc fix): the write's triples
    only tell the hook WHICH (entity, marked attr) docs were touched — the docs
    themselves are rebuilt from a re-read of the touched entities' FULL current
    triples in Neptune (the write has already been committed by the time the
    hook runs, so the re-read includes it). Upsert is replace-per-doc and the
    reconciler builds docs from the full KG, so indexing only the write's
    triples would (a) wipe the previously indexed tail of a multi-valued attr
    that just got one value appended, and (b) feed the intra-entity cross-attr
    dedup a different input than the reconciler sees, making mirrored attrs
    flip-flop between hook and reconcile runs.

    Empty-doc contract: a marked attr on a touched entity whose RE-READ
    canonicalized doc came out empty (or was deduped away because it mirrors
    another attr's doc) gets ``delete(entity, tenant, kg_name=…, attr=…)`` —
    per the ONTA-175 upsert contract an empty doc has no chunk rows to carry
    its key, so the hook must issue the delete explicitly.

    Best-effort and time-bounded exactly like ``_index_spatiotemporal``: the
    whole body (marker read + entity re-read + upsert + deletes + schedule
    ensure) runs under one ``asyncio.wait_for`` so a hung index backend can't
    block the KG write, and ANY failure is logged, never raised — the KG write
    must NEVER fail on an index hiccup (Neptune is already the source of truth
    at this point).
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
            _index_semantic_inner(
                neptune, instance_graph, tenant_id, kg_name, instance_triples
            ),
            timeout=_semantic_upsert_timeout_s(),
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
    """The unguarded body of :func:`_index_semantic` (wrapped in one timeout).

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
    # nor a name still costs zero extra Neptune reads.
    touched = indexable_doc_keys(instance_triples, marked)
    if touched:
        entity_uris = sorted({entity_uri for entity_uri, _ in touched})
        cap = _semantic_hook_max_entities()
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

        # Completeness re-read (see _index_semantic's docstring): rebuild the
        # touched entities' docs from their FULL current triples in Neptune —
        # NEVER from the write's own (possibly partial) triples. On failure we
        # SKIP indexing for this write (the reconciler repairs on its cadence);
        # degrading to write-local partial docs would reintroduce the exact
        # partial-doc-wipe bug this re-read exists to fix.
        try:
            fetched = await _fetch_touched_entity_triples(
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
    """Fetch the full current ``?e ?p ?o`` triples of the given entities.

    One VALUES-scoped SELECT over the touched entity URIs against the instance
    graph (the same VALUES batching style as ``batch_entity_exists_query`` and
    the reconciler's scan). ALL predicates are fetched — marked attrs plus
    ``rdf:type`` / label predicates for the extractor's denormalized display
    fields; the extractor itself filters to the marked set, exactly as it does
    for the reconciler's scan, so the two can never disagree on matching.

    Ordered like the reconciler's scan (``ORDER BY ?e ?p ?o``, re-sorted
    client-side to be safe) so the extractor's first-attr-wins intra-entity
    dedup picks the SAME winner the reconciler's full scan picks — otherwise
    mirrored attrs flip-flop between hook writes and reconcile runs.
    """
    from infona_client.graph.parser import parse_sparql_results

    values = " ".join(f"<{u}>" for u in entity_uris)
    sparql = (
        f"SELECT ?e ?p ?o FROM <{instance_graph}> WHERE {{\n"
        f"  VALUES ?e {{ {values} }}\n"
        f"  ?e ?p ?o .\n"
        f"}} ORDER BY ?e ?p ?o"
    )
    _, rows = parse_sparql_results(await neptune.query(sparql))
    triples: list[Triple] = []
    for row in rows:
        e, p = row.get("e", ""), row.get("p", "")
        if e and p:
            triples.append((e, p, row.get("o", "")))
    triples.sort()
    return triples


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

    gs = _resolve_graph_session(
        store=store, session=session, instance_graph=instance_graph
    )
    return await _delete_facts_store(
        gs,
        instance_graph,
        subjects=subjects,
        concrete=concrete,
        sp_pairs=sp_pairs,
        all_triples=all_triples,
        reason=reason,
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
) -> int:
    """Property-graph delete path."""
    from infona_client.graph import pg_ops
    from infona_client.graph.facts import classify_triple
    from infona_client.graph.iri import ONTO_PRED_PREFIX

    removed = 0
    prov_on = _provenance_enabled(store_path=True)

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


async def _record_value_history(
    neptune,
    instance_graph: str,
    sp_pairs: list[tuple[str, str]],
    new_values: dict[tuple[str, str], str],
) -> None:
    """Version any genuine value CHANGE among ``sp_pairs`` before they're cleared.

    Called by :func:`delete_facts` (gated by ``INFONA_VALUE_HISTORY_ENABLED``)
    for the predicate-scoped-delete step of an attribute UPDATE — the one place
    that sees both the OLD value (still in the graph) and the NEW value the caller
    is about to write. For every ``(s, p)`` in this chunk that the caller declared
    a ``new_value`` for, it reads the current object(s), and for each old value
    that actually differs from the new one it builds an ``old → new`` version node
    (:func:`build_value_change_triples`) and writes it to the companion HISTORY
    graph through the SAME shared batched-insert seam every other write uses —
    never a bespoke writer.

    General by construction (no attribute is special) and correct on the two
    no-record cases the ticket calls out:

    * **First insert** — a brand-new ``(s, p)`` has no current object, so the read
      returns nothing and no entry is recorded (a value appearing for the first
      time is not a *change*).
    * **Unchanged value** — old == new (compared on the lexical axis via
      ``build_value_change_triples``) yields an empty triple list, so re-writing
      the same value records nothing.

    Best-effort and fully isolated (its own try/except, only reachable inside
    ``delete_facts``'s own flow): a history hiccup must NEVER fail the update —
    the primary KG write is the source of truth; history is a derived companion
    exactly like provenance and the secondary indexes.
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
    ``INFONA_PROVENANCE_ENABLED``.

    Does NOT itself touch derived secondary indexes: call
    :func:`refresh_after_write` with ``rewritten_subjects={old: new}`` once per
    rebuild batch so a single housekeeping pass re-keys them.
    """
    if not old_uri or not new_uri or old_uri == new_uri:
        return

    gs = _resolve_graph_session(
        store=store, session=session, instance_graph=instance_graph
    )
    from infona_client.graph import pg_ops

    await pg_ops.rewrite_entity_id(gs, old_uri, new_uri)
    if _provenance_enabled(store_path=True):
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
    types = list(dict.fromkeys(t for t in affected_types if t))
    if types:
        try:
            from infona_client.nlp.pipeline import get_embedding_service

            svc = get_embedding_service()
            if svc is not None:
                await svc.embed_types(onto_graph, types, neptune)
        except Exception:  # noqa: BLE001 — non-blocking, mirrors the ingest routes
            logger.warning("embed_types_failed", types=types, exc_info=True)

    # 3. KG registration in the tenant metadata graph (ONTA-153) was a SPARQL-only
    #    step and is GONE with the Neptune path (ONTA-527). The property-graph KG
    #    registry is deferred (model B7 / E4), so a non-UI writer's KG is not
    #    auto-registered for list_kgs today. `ensure_kg_registered` is kept as an
    #    unwired reference for that port and must not be called from here.

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
    await _deindex_secondary(
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
        # _INDEX_UPSERT_TIMEOUT_S note); TimeoutError is caught below.
        await asyncio.wait_for(_work(), timeout=_INDEX_UPSERT_TIMEOUT_S)
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

        await asyncio.wait_for(_semantic_evict(), timeout=_semantic_upsert_timeout_s())
    except Exception:  # noqa: BLE001 — never fail a write on a derived-index hiccup
        logger.warning(
            "semantic_index_deindex_failed",
            tenant_id=tenant_id,
            kg_name=kg_name,
            exc_info=True,
        )
