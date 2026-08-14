"""Constants and regexes for the semantic reconciler.

Patchable knobs (``_MAX_SCAN_PAGES``, ``_MAX_CANDIDACY_ATTRS_PER_RUN``,
``_UPSERT_BATCH_CHUNKS``, ``_ASSERTION_HISTORY_HARD_CAP``) are rebound on
the public ``reconciler`` facade. Siblings must read those via ``_host()``.
"""

from __future__ import annotations

import re

from infona_client.graph.iri import IRI_BASE

Triple = tuple[str, str, str]

#: Schedule actions dispatched to this module (also members of
#: ``scheduling.models.ScheduleAction``).
SEMANTIC_EMBED_FILL_ACTION = "semantic-embed-fill"
SEMANTIC_RECONCILE_ACTION = "semantic-reconcile"

#: Deterministic id of the single global embed-fill schedule row. Deterministic
#: ids make the ensure-* helpers idempotent across processes: N racing creates
#: converge on one row (the Postgres store's ``create`` is an UPSERT by id).
EMBED_FILL_SCHEDULE_ID = "semantic-embed-fill"

#: Sentinel tenant for the global sweep row. Never a real tenant (real tenant
#: slugs are user-facing); the tenant-scoped schedule CRUD routes can't list or
#: touch it, and the sweep itself spans tenants via ``fetch_pending``'s
#: maintenance-only ``tenant_id=None`` exception.
_SYSTEM_TENANT = "_system"
_GLOBAL_KG = "*"

#: ``https://graph.infona.ai/types/{Type}/attrs/{name}`` — the only predicate
#: shape the candidacy heuristic may classify (system predicates like
#: ``rdfs:label`` / ``onto/ingested_at`` never carry a textKind verdict).
_ATTR_URI_RE = re.compile(
    rf"^{re.escape(IRI_BASE)}/types/(?P<type>[^/]+)/attrs/(?P<attr>[^/]+)$"
)

#: Durable decided-no verdict written by the default heuristic. Anything other
#: than ``free_text`` reads back as ``is_free_text=False`` in
#: ``get_free_text_map`` — the point is that the attribute is DECIDED (absence
#: would mean "undecided" and get re-sampled every reconcile).
TEXT_KIND_NOT_TEXT = "not_text"

_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
#: Local names treated as display-label sources by the extractor; their
#: predicates are included in the reconcile scan so reconciler-written rows
#: carry the same denormalized ``attrs`` a hook-written row would.
_LABEL_LOCALS = {"label", "name", "title"}

#: Hard bounds on one reconcile run — a runaway KG must exhaust the page cap,
#: not the worker's memory. Truncation is logged, never silent.
_MAX_SCAN_PAGES = 200
#: Cap on attributes the default heuristic samples per run (each is one
#: bounded SPARQL sample query; the rest wait for the next hourly run).
_MAX_CANDIDACY_ATTRS_PER_RUN = 50
_CANDIDACY_SAMPLE_SIZE = 100
#: Chunk-upsert batch budget. Batches are packed on DOC boundaries — the
#: protocol's complete-document contract forbids splitting one (entity, attr)
#: doc across calls — so a batch may exceed this by one doc's tail.
_UPSERT_BATCH_CHUNKS = 500
#: Defensive bound on sweep iterations (each drains up to ``limit`` rows).
_MAX_SWEEP_ITERATIONS = 1000
#: Hard ceiling on :meth:`GraphSession.read_assertion_history` pages.
#: MemoryGraphStore and Neo4jGraphStore both clamp ``limit`` with
#: ``min(limit, 10000)`` — requesting more is silently truncated to this
#: value. Detection must use THIS constant (or ``len(rows) == hard_cap``),
#: not a larger logical budget like ``page_size * max_pages``: a full hard-cap
#: page means the store may have more rows and the scan is PARTIAL (ghost
#: deletion must fail closed). Monkeypatched in tests.
_ASSERTION_HISTORY_HARD_CAP = 10000
