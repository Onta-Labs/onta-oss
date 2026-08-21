# Infona API Reference

**Version:** 0.1.0

Living Knowledge Graph Platform

Auto-generated from the OpenAPI spec. Do not edit manually.

## Interactive Docs

When the server is running, visit:
- Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
- ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)

## Actions

### `POST /graphs/{tenant}/actions/find-merge-duplicates`

Find Merge Duplicates

Kick off a dedupe job (second-pass entity resolution) over a KG.

**Request body:** `KGActionRequest`

**202:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/actions/enrich`

Enrich Action

Kick off an enrichment job, reusing the existing EnrichmentExecutor.

Same job-creation + executor wiring as POST /enrich/jobs, but tagged with
``category=enrichment`` and returning the action-shaped response.

**Request body:** `EnrichActionRequest`

**202:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/actions/suggest-relationships`

Suggest Relationships

Kick off a relationship-suggestion (reconciliation) job.

Relationship suggestion is a PREMIUM capability. If no recommender hook is
registered, the job is created and immediately resolved to a terminal
``failed`` state with a clear message — but a ``job_id`` is still returned
so the UI's create-then-poll flow works unchanged. When a premium
recommender is registered (via ``register_relationship_recommender``), the
job runs it in the background and lands in ``review``.

**Request body:** `KGActionRequest`

**202:** Successful Response
**422:** Validation Error

---

## Agent

### `POST /graphs/{tenant}/agent`

Agent Turn

One agent turn: confirm→execute a plan, or classify+respond to a message.

**Write authorization (ONTA-451).** This is the one READ/WRITE MIXED route in
the API: the same endpoint answers a question and ingests a dataset. A
blanket ``Depends(require_tenant_write)`` — the gate every single-purpose
mutating route uses — would therefore 403 a read-only member out of the
read-only turns their role explicitly permits (query / ask / research /
ontology inspection), which is the wrong product behavior, not just a
stricter one.

So the gate sits at CAPABILITY DISPATCH instead: ``get_tenant_with_capability``
resolves the membership capability, ``_build_ctx`` threads it onto
:class:`AgentContext`, and the planner refuses at the two points where a
mutation is actually committed — persisting a mutating plan, and
``execute_plan`` (the only path that runs one). Capability classification is
deny-by-default, so a capability that does not declare ``writes = False`` is
treated as mutating. The resulting
:class:`~infona_client.agent.registry.ReadOnlyMembershipError` is translated
to HTTP 403 here, with the same wording ``require_tenant_write`` uses.

**Request body:** `AgentRequest`

**200:** Successful Response
**422:** Validation Error

---

## Api_Sources

### `GET /graphs/{tenant}/api-sources`

List Api Sources

List the sources visible to this caller.

Visibility is operator-gated (ONTA-234):

* a **regular** caller sees ONLY their own ``tenant_custom`` (editable)
  sources — the GLOBAL catalog (``global_public`` + ``global_enhanced``) is
  never returned, so our vendor stack / coverage is not exposed to tenants.
* an **Infona operator** (``tenant.is_operator``, decided server-side from the
  verified identity) additionally sees the full global catalog, read-only —
  the authoring aid for the operator-curated, PR-reviewed global sources.

The gate is on VISIBILITY only; the discovery / enrichment rails still
execute every enabled global source for every tenant (they consult the
catalog directly, not this route).

The caller's ``user_custom`` sources (registered once, visible in every
workspace they can access) are merged in when the key has an owner
subject (Clerk user or a static-key fingerprint).

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/api-sources`

Create Api Source

Create a tenant-custom source. 403 if the slug shadows a global one is NOT
enforced — a tenant MAY shadow a global slug for its own workspace (that's the
layer's purpose) — but the slug must be a valid tenant slug and the spec must
validate. Secrets are encrypted per tenant.

**Request body:** `CreateApiSourceRequest`

**201:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/api-sources/{slug}`

Get Api Source

Read one source's full spec (secrets REDACTED/omitted) + ``has_secret``.

A GLOBAL slug is only visible to an operator: a regular caller requesting a
global slug gets a 404 — same as a slug that does not exist — so the route
never leaks that a global source exists (ONTA-234).

**200:** Successful Response
**422:** Validation Error

---

### `PATCH /graphs/{tenant}/api-sources/{slug}`

Update Api Source

Edit a tenant-custom source (spec body, enabled, and/or secrets). A global
slug => 403. Missing tenant entry => 404.

**Request body:** `UpdateApiSourceRequest`

**200:** Successful Response
**422:** Validation Error

---

### `DELETE /graphs/{tenant}/api-sources/{slug}`

Delete Api Source

Delete a tenant-custom source + its stored secrets. Global slug => 403,
missing tenant entry => 404.

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/api-sources/validate`

Validate Api Source

Validate a spec against ``ApiSourceSpec`` (schema + URL lint + auth
coherence). No write. Returns structured ``{valid, errors:[{path,message}]}``.

**Request body:** `CreateApiSourceRequest`

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/api-sources/test`

Test Api Source

Run ONE smoke request through the executor (SSRF-guarded). No KG write, no
persistence. Provide an inline ``spec`` OR an existing ``slug``.

For an inline spec that uses a ``secret_ref``, the smoke call resolves the
secret from the tenant's store (if already saved) — so a test never echoes a
secret. Rows are returned; a secret never appears in them (the executor keeps
auth out of provenance/sources).

**Request body:** `TestApiSourceRequest`

**200:** Successful Response
**422:** Validation Error

---

## Ask

### `POST /graphs/{tenant}/ask`

Ask Question

**Request body:** `NLQuery`

**200:** Successful Response
**422:** Validation Error

---

## Conversations

### `GET /graphs/{tenant}/conversations`

List Conversations

List the caller's threads for this tenant, newest-first.

Returns ``{"conversations": []}`` for a request without a subject (the demo
shared key) — history is an authenticated, per-user feature.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/conversations/{session_id}`

Get Conversation

Return one thread's full transcript so the UI can re-render it.

Scoped to the caller's subject: a user can only open their own thread (a
mismatch is a 404, not a 403, so thread ids aren't enumerable).

**200:** Successful Response
**422:** Validation Error

---

## Corrections

### `POST /graphs/{tenant}/corrections`

Create Correction

Apply an A10 user correction and return its A6 receipt.

Validates the body, derives the entity type + literal predicate on the write
side, stamps ``actor`` from the authenticated subject, and calls the shared
:func:`apply_user_assertion` writer (supersede + top-authority provenance,
all through kg_writer). Returns the corrected value now current, the wrong
value(s) retired, and the ``user_assertion`` authority the fix carries.

**Request body:** `CorrectionRequest`

**200:** Successful Response
**422:** Validation Error

---

## Enrich

### `POST /graphs/{tenant}/enrich/jobs`

Create Job

**Request body:** `EnrichRequest`

**202:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/enrich/jobs`

List Jobs

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/enrich/jobs/{job_id}`

Get Job

**200:** Successful Response
**422:** Validation Error

---

### `DELETE /graphs/{tenant}/enrich/jobs/{job_id}`

Cancel Job

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/enrich/jobs/{job_id}/wait`

Wait For Job

Block until a job reaches a TERMINAL state OR a bounded timeout, then
return its current status (COG — persona-eval async-settling blocker).

Web-discovery / enrichment jobs take minutes to settle. The only status
primitive is ``GET .../jobs/{id}``, which returns instantly with
``running`` — so a client that wants to *wait* has no choice but to hammer
it in a tight loop (15 polls in seconds, all ``running``, then gives up)
without ever actually waiting. This route waits SERVER-SIDE with an async
sleep loop (never a busy-wait) and returns as soon as the job is done, or
after at most ``timeout_s`` (clamped to ``WAIT_MAX_TIMEOUT_S``) if it is
still in flight.

Return contract:
- Job already terminal (applied/failed/cancelled/review) → returns
  immediately with the final status.
- Job completes mid-wait → returns promptly once it settles.
- Job still ``queued``/``running`` at the timeout → returns the job with its
  CURRENT (non-terminal) status and HTTP 200. This is NOT an error: the
  caller inspects ``status`` and, if still running, simply calls ``wait``
  again. A few such calls cover a multi-minute job for a few steps total.

Same auth + tenant-scoping + result-truncation as ``get_job``; the only
added behavior is the bounded server-side blocking.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/enrich/jobs/{job_id}/conflicts`

List Conflicts

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/enrich/jobs/{job_id}/apply`

Apply Job

**Request body:** `ApplyRequest`

**200:** Successful Response
**422:** Validation Error

---

## Explore

### `GET /graphs/{tenant}/explore/kgs/{kg_name}/types/{type_name}/summary`

Get Type Summary

Bundle all Explorer panel data for one type in one call.

**GraphStore / Neo4j (P-A1a):** instance inventory via
:func:`infona_client.graph.explore_store.type_summary` — same
``INSTANCE_OF`` count path as type-counts so ``vis`` overview and
``vis <Type>`` drill-in agree. Returns 404 only when the type is neither
declared in the tenant ontology nor has instances in this KG.

**Legacy SPARQL (Neptune):** serves from precomputed stats (fast); falls
back to a live scan if stats for this type are not yet materialized. All
percentages are relative to entity_count.

A ``type_name`` that cannot sit inside an IRI is a 422 (ONTA-425), rejected
here rather than three store round trips later, so the caller is told what is
wrong instead of getting a 500 out of the store's parser.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/explore/kgs/{kg_name}/schema`

Get Kg Schema

Population-aware schema for ONE KG: every type with its POPULATED slots.

The whole-KG counterpart of ``/types/{type}/summary``, same per-type shape,
assembled by the same ``_assemble_summary`` (so the same predicate hygiene
applies: internal ER/batch predicates and legacy per-attribute provenance
companions never surface), but for every type in one request. This is a
BACKEND join on purpose: the stats it reads are already materialized for the
whole KG, so it costs 3-4 queries, where a client-side loop over the per-type
summary would be 1+N round trips.

Declared-but-empty types and attributes are INCLUDED and marked
(``populated: false`` / ``declared_only: true``), never hidden. Hiding them
made agents assert "that type does not exist" or substitute a wrong type
(ONTA-248 / ONTA-258). ``min_coverage`` is the one filter that withholds
slots, and it only acts when the caller explicitly sets it.

**GraphStore / Neo4j:** composes :func:`explore_store.type_counts` +
:func:`explore_store.type_summary` + ontology catalog declarations
(``stats_source=graph_store``). Required under production GraphStore so
MCP ``inspect_graph_schema`` does not hit retired SPARQL (ONTA-534 residual).

Not exposed here: sample VALUES. Nothing serves them over HTTP today (the NL
pipeline computes them inside ``/ask`` only). Deliberately out of scope.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/explore/kgs/{kg_name}/type-edges`

Get Type Edges

Undirected type→type edges for the Explorer overview graph.

Derived from instance data (the precomputed stats graph, with a live-scan
fallback) rather than the ontology's declared ``rdfs:range``. This keeps the
overview consistent with the per-type detail view: a relationship that
exists in the data but whose ontology range was never upgraded to a type
URI (e.g. a predicate first seen as a primitive attribute) is now drawn in
both places. Returns ``[{source, target, weight}]``.

ADR 0004 (flag ``INFONA_DRIFT_CONTROL``): when ON, the stats read also
respects the support floor — a low-support drift edge (e.g.
``ManufacturerPartNumber.issuedby -> Retailer`` at 6% coverage) is excluded
from the overview, while high-coverage and core-slot edges are kept. With
the flag OFF the read is byte-identical to before (no filtering).

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/explore/kgs/{kg_name}/recompute-stats`

Recompute Stats

Schedule a recompute of the precomputed type-stats for a KG.

Returns immediately; the ~15s whole-KG scan runs in the background so it
never hits the ALB response timeout.

Mutating: it rewrites the per-KG stats graph (and schedules a whole-KG
scan), so ``require_tenant_write`` refuses a ``reader`` member with 403
(ONTA-451). Being allowlisted in the write-path convergence guard — it IS
the stats action rather than a writer of instance data — says nothing about
who may trigger it.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/explore/kgs/{kg_name}/drift-history`

Get Drift History

Read the accumulated observe-only drift distribution for a KG (COG-57).

Returns the persisted recompute snapshots (newest first), each with the run's
effective floors, kept/quarantined totals, and the full per-relationship
coverage distribution. This is the durable, queryable replacement for
log-scraping CloudWatch — the data ADR 0004 sets ``INFONA_DRIFT_FLOOR_COV``
from. Raw distribution access only; histogram/floor analysis is done offline.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/explore/kgs/{kg_name}/types/{type_name}/records`

Get Type Records

Paged entity instances for the Explorer Data table (COG-100).

Returns one page of instances of ``type_name``, ordered deterministically
by entity URI (``ORDER BY ?e``) with keyset pagination via ``cursor`` (the
last entity URI from the previous page).  For each entity the endpoint
fetches all attribute values, excluding ``rdf:type`` and
``SYSTEM_PREDICATES``.  Attribute predicates are resolved to display names
via the ontology (same ``attr_def`` query shape as ``get_type_summary``).
The row ``name`` is the declared ``attrs/name`` attribute value when present
(ingest stores the human-readable name there; ``rdfs:label`` holds the
opaque entity-id slug), else ``rdfs:label``, else the entity-URI leaf.

Response shape::

    {
        "columns": ["name", "<attr1>", ...],
        "rows": [{"id": "<uri>", "name": "...", "<attr1>": "...", ...}],
        "total": <int>,
        "next_cursor": "<uri>" | null,
    }

Never errors on an empty/missing type; returns the empty sentinel instead.
A type name that could not exist at all — one carrying a character no IRI may
contain — is a different thing from a type with no rows, and is a 422
(ONTA-425). The sentinel keeps covering every name that is merely absent.

**Dual-backend (E9):** when ``INFONA_GRAPH_BACKEND=neo4j``, reads via
:mod:`infona_client.graph.explore_store`. Default Neptune path unchanged.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/explore/kgs/{kg_name}/entities/{entity_id}`

Get Entity Detail Route

Entity detail (properties + incident relationships).

**Dual-backend (E9):** under ``INFONA_GRAPH_BACKEND=neo4j`` (or an injected
GraphStore) uses :func:`infona_client.graph.explore_store.get_entity_detail`.
On the default Neptune path, assembles the same shape via SPARQL point
lookups on the KG graph.

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/explore/kgs/{kg_name}/er-rebuild`

Er Rebuild

Second-pass entity resolution (MOE-22): collapse intra-batch fragments.

Mutating: a real ER merge (``rewrite_subject``) plus post-write housekeeping,
so ``require_tenant_write`` refuses a ``reader`` member with 403 (ONTA-451).

Re-runs ER over the already-ingested KG so same-entity rows that couldn't
see each other's index triples mid-batch now merge. Runs synchronously and
returns per-type before/after counts (the merge volume is modest). Stale
type-stats are recomputed in the background afterward so the Explorer
reflects the new counts without blocking this response.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/explore/search`

Search Explorer

Search types or attributes by name substring.

kind=type  — returns matching type names + their instance counts.
kind=attr  — returns every type that has an attribute matching the query.

Ontology side is layered (ONTA-397): Public/Enhanced declarations are
visible under the caller's LayerStack; same-name collisions collapse by
first-visible-layer-wins when assembling the result set.

**200:** Successful Response
**422:** Validation Error

---

## Export

### `GET /graphs/{tenant}/kgs/{kg_name}/export`

Export Kg

Export KG instance data as JSON or CSV (OSS launch F10).

**200:** Successful Response
**422:** Validation Error

---

## Functions

### `POST /graphs/{tenant}/functions`

Register Function

Attach a function endpoint URL to a type.

Tenant attachments are the ordinary workspace write path. Enhanced
attachments are operator-only (global-layer authoring); Public is refused
by the writer (ONTA-400).

Mutating: ``require_tenant_write`` refuses a ``reader`` member with 403
(ONTA-451). The ``GET`` listing below stays on plain ``get_tenant``.

**Request body:** `FunctionRegister`

**201:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/functions`

List Functions

List functions in the tenant graph (workspace layer).

Enhanced global functions live in ``graphs/global/enhanced`` and are read
via the operator Global Ontology browser / layered reads (ONTA-397) — they
are not mixed into this tenant-scoped list so a non-entitled workspace
cannot discover them by listing functions.

**200:** Successful Response
**422:** Validation Error

---

## Grep

### `POST /graphs/{tenant}/grep`

Grep Graph

Literal substring scan over one KG's triples. See the module docstring.

Auth is the same ``get_tenant`` dependency as every ``/graphs/{tenant}``
route, and the scanned graph URI is built ONLY from the RESOLVED tenant id
plus a charset-validated KG name — a caller can never widen the scan beyond
the graph its key authorizes. (Explicitly NOT built on
``POST /graphs/{tenant}/query``, which executes caller SPARQL verbatim with
no graph scoping — ONTA-412; a grep layered on that route would inherit its
cross-tenant read hazard.)

``request`` is required positionally by slowapi's ``@limiter.limit``, which
reads the API key off it for the per-key bucket.

**Request body:** `GrepRequest`

**200:** Successful Response
**422:** Validation Error

---

## Health

### `GET /health`

Health

**200:** Successful Response

---

## History

### `GET /graphs/{tenant}/history`

Get Value History

Return dated value entries for a KG, oldest → newest.

Each entry is ``{subject, predicate, old_value, new_value, changed_at}``,
sourced from Assertion provenance in the property-graph store.

**200:** Successful Response
**422:** Validation Error

---

## Ingest

### `POST /graphs/{tenant}/ingest`

Ingest

Ingest raw content into the knowledge graph.

Runs LLM extraction, schema resolution (type matching, attribute
resolution, validation), and inserts validated triples into Neptune.

ONTA-386: opens a tracked ``category=ingest`` job with live stage_trace
(P0/P2/P5/P6; file is A1-like entry so P1 is skipped). Writes still go
only through ``insert_facts`` / ``refresh_after_write``.

**Request body:** `IngestRequest`

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ingest/csv/schema`

Infer Csv Schema

Step 1: Infer column mapping from CSV headers + sample rows.

Default (``INFONA_CSV_INFERENCE_V2`` unset/truthy) is the ADR 0003
evidence-grounded pipeline: a deterministic column profile (Pass A) feeds
a REASON LLM call (Pass B), an adversarial REFUTE LLM call (Pass C), and
a conceptual COMPLETE LLM call (Pass D). The response is the same
``CSVSchemaMapping`` contract as before, extended with optional,
backward-compatible fields: per-decision ``why``/``confidence`` (on
entities/columns), ``key_strategy`` per entity, the refute pass's
``violations``, an ``inference_audit`` block, and the completion pass's
``ontology_extensions`` (dependent-entity promotions, core slots, dataset
constants, rejected candidates). ``INFONA_CSV_INFERENCE_V2=0`` falls back
to the legacy single-LLM-call path.

Confirm gate (COG-52, until COG-56's judge panel lands): promotions and
low-confidence completions come back flagged ``held_for_review`` — the
gate is CLIENT-SIDE. The Explorer asks the user to confirm/edit held
items; whatever mapping the client then posts to ``/ingest/csv/rows`` is
applied as-is.

Latency budget: schema inference is up to 3 sequential LLM calls, once per
CSV file — REASON + REFUTE + COMPLETE (each with at most one validation
retry at temperature 0.3); the Pass A profile is milliseconds. Row
ingestion (``/ingest/csv/rows``) stays LLM-free, so the cost does not
scale with row count. Clients should send the full file (capped at a few
thousand rows) as ``sample_rows`` — profile fidelity, and therefore
mapping quality, depends on it.

**Request body:** `CSVSchemaRequest`

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ingest/csv/rows`

Ingest Csv Rows

Step 2: Insert rows using a pre-inferred mapping. No LLM call.

The mapping is applied AS POSTED — including ``ontology_extensions``
items flagged ``held_for_review`` by ``/ingest/csv/schema``: the confirm
gate is client-side (the Explorer asks the user, then posts the possibly
edited mapping here). Promoted types and their core slots are
pre-registered in the tenant ontology — including slots with zero data,
marked with a ``coreSlot`` triple as declared enrichment targets
(ADR 0003 §3).

ONTA-386: tracked ``category=ingest`` job + live stage_trace (P0/P2/P5/P6).

**Request body:** `CSVRowsRequest`

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/embeddings/build`

Build Embeddings

Trigger a full embedding build for all ontology types in this tenant.

**200:** Successful Response
**422:** Validation Error

---

## Jobs

### `GET /graphs/{tenant}/jobs`

List Jobs

List a tenant's jobs across all categories, newest first.

Pass ``?category=dedupe|enrichment|reconciliation|discovery|ingest`` to
filter. Each item is a ``JobSummary`` carrying the unified fields the Jobs
page renders: category, trigger, last_run, next_run, cost (+ note), status,
and the derived ``progress_pct``.

**200:** Successful Response
**422:** Validation Error

---

### `DELETE /graphs/{tenant}/jobs`

Purge Jobs

Hard-delete every job for this tenant.

Used by demo reset / tenant wipe. Does not touch instance data or the
ontology — only the job store (enrichment, ingest, ask, dedupe, …).
Idempotent: an empty tenant returns ``deleted=0``.

**200:** Successful Response
**422:** Validation Error

---

### `DELETE /graphs/{tenant}/jobs/{job_id}`

Delete Job

Hard-delete one job. 404 if it is missing or belongs to another tenant.

**200:** Successful Response
**422:** Validation Error

---

## Knowledge_Graphs

### `GET /graphs/{tenant}/kgs`

List Kgs

List all knowledge graphs for a tenant, with dashboard-summary stats.

**Neo4j:** registry is ``:KnowledgeGraph`` nodes (see
:mod:`infona_client.graph.kg_registry`). Entity/edge stats still come from
the durable stats store when available.

**Legacy SPARQL:** triple counts are read from the metadata graph (stored
alongside the KG registration) in the SAME query that lists the KGs.

Entity/edge counts come from the durable per-KG stats store (kept fresh by
the shared write/refresh path) — a single relational read, no Neptune. Rows
for KGs that predate the store are backfilled lazily from their existing
precomputed stats graph the first time they're listed (the same lazy
materialization pattern as triple counts). ``status`` is derived live from
the tenant's in-flight enrichment jobs.

A GET that persists (ONTA-452): every lazy materialization on this path is a
WRITE, and the route stays open to readers because listing your graphs is a
read. So the persistence is gated on the caller's write capability instead
of the route: a read-only member gets the SAME numbers, computed live, and
writes back nothing. Without this the route was a bypass of the very
``recompute-stats`` gate this ticket added, since it schedules the identical
recompute.

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/kgs`

Create Kg

Create a new knowledge graph for a tenant.

**Neo4j:** ``:KnowledgeGraph`` registry upsert (idempotent).

**Legacy SPARQL:** guarded with ``FILTER NOT EXISTS`` so calling it twice
never duplicates the registration triples and never clobbers an existing
registration (or its ``kg_description``). This is the same registration the
shared write path performs via ``ensure_kg_registered`` (ONTA-153) — here we
additionally write the description, which only the explicit "New KG" flow
supplies.

On a re-POST of an existing KG the guarded INSERT no-ops; we then return the
*existing* KGInfo (real description + triple count) rather than claiming an
empty/zero KG, so the response never lies about a KG that may already hold
real data.

Safety: ``body.name`` is pattern-validated by ``KGCreate`` (``[a-zA-Z0-9_-]``)
so it's URI-safe, but the free-text ``description`` and (defensively) the name
are escaped via the canonical ``_escape_literal`` before going into a SPARQL
literal — no statement-breakout on a ``"`` / ``\`` / newline.

**Request body:** `KGCreate`

**201:** Successful Response
**422:** Validation Error

---

### `DELETE /graphs/{tenant}/kgs/{kg_name}`

Delete Kg

Delete a knowledge graph and all its data.

Store-specific purge (registry + DETACH on Neo4j; DROP GRAPH + metadata on
SPARQL) runs first. Every derived-state eviction then runs for BOTH backends
(ONTA-532): the Neo4j branch used to early-return after registry / DETACH /
durable-stats and skip semantic clear, spatiotemporal clear, example bank,
NL cache, kg_status, explore stats cache, and reconcile schedule — leaving
stale answers and schedules for a recreated same-name KG.

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/kgs/{kg_name}/search/reindex`

Reindex Kg Semantic

Trigger an on-demand semantic reconcile (= backfill) for one KG.

THE entry point for indexing an already-ingested KG without re-ingesting
(ONTA-181's parliamentary-speeches scenario): the reconciler's first run
against a KG is the backfill. Deliberately NOT an inline long-running
request — it seeds the KG's recurring reconcile schedule row with
``next_run=now`` and returns 202 immediately; the claim-based schedule
runner picks it up within one poll interval, so overlapping ECS tasks never
double-scan. Deployments without a runner (no DSN, scheduler off) fall back
to a fire-and-forget in-process task — single process, so no claim needed.

503 when the semantic index is disabled (``INFONA_SEMANTIC_INDEX_ENABLED``
is the master gate for the write hook AND the reconciler): accepting the
request would acknowledge work that can never run.

**202:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/kgs/{kg_name}/type-counts`

List Type Counts

List every type that has instances in this KG, sorted by entity count.

Tenant-global ontology types with zero instances in this KG are not
returned here — fetch them via /ontology/types if the caller needs the
full schema.

**Dual-backend (E5):** when ``INFONA_GRAPH_BACKEND=neo4j`` (or a process
GraphStore is configured for that backend), counts come from
:func:`infona_client.graph.explore_store.type_counts` instead of SPARQL.
Spatio-temporal index flags are still best-effort from the stats graph
(Neptune path only; Neo4j returns False until stats port).

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/kgs/{kg_name}/types/{type_name}/usage`

Get Type Usage

Per-type breakdown for one type in one KG.

Combines the tenant-global ontology definition (attribute names,
datatypes, parent type) with per-KG instance numbers (entity count,
attribute usage, sample entities) so the caller doesn't have to make
three round-trips and re-join the results client-side.

**GraphStore / Neo4j (ONTA-535):** inventory via
:func:`infona_client.graph.explore_store.type_summary` + sample entities
from :func:`~infona_client.graph.explore_store.list_entities_by_type`.
System/internal keys are already filtered by the summary path (same
``is_internal_property_key`` authority as grep/records); ``include_system``
is a SPARQL-branch opt-in and is ignored on the store path (internals
never surface as domain columns).

**200:** Successful Response
**422:** Validation Error

---

## Lambda_Functions

### `POST /functions/sec-latest-filing`

Sec Latest Filing

Fetch a company's most recent SEC filing from EDGAR.

Input: CIK (Central Index Key) as a string.
Output: latest_filing_date, latest_filing_type, days_since_last_filing, source_url.

**Request body:** `SECFilingRequest`

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/functions/{function_name}/invoke`

Invoke Function

Invoke a registered function for one entity and materialize the result as triples.

Steps:
1. Look up FunctionRef in the tenant ontology graph
2. Resolve the entity's filing_cik attribute from the KG
3. Invoke the function via FunctionExecutor
4. Write result attributes back as triples on the entity

**Request body:** `InvokeRequest`

**200:** Successful Response
**422:** Validation Error

---

### `POST /functions/investor-portfolio`

Investor Portfolio

Query the KG for all companies in an investor's portfolio.

Looks up FundingRound entities where lead_investor matches this investor,
then follows company_name relationships to get Company names and sums amounts.
f

**Request body:** `PortfolioRequest`

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/functions/investor-portfolio/invoke`

Invoke Investor Portfolio

Invoke investor-portfolio for an Investor entity.

Resolves the investor name from the entity URI, queries the KG for
portfolio data, and materializes the results as triples.

**Request body:** `InvokeRequest`

**200:** Successful Response
**422:** Validation Error

---

## Normalize

### `POST /graphs/{tenant}/normalize/rules`

Create Rule

Create a USER-AUTHORED normalization rule directly (no inference).

The id is derived from ``(kg, type, predicate, rule_type)`` via
:func:`make_rule_id`, so it shares ids with inferred rules of the same shape:
creating one whose id already exists UPSERTs (the store clears prior triples
before re-writing), never duplicates. ``created_at`` is stamped by the
:class:`NormalizationRule` model's default_factory. Persists with the
requested ``status`` and returns the persisted rule.

**Request body:** `CreateRuleRequest`

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/normalize/rules`

List Rules

List stored rules, optionally filtered by KG and/or status.

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/normalize/suggest`

Suggest

Infer normalization rules for a type's predicates, persist them as
``suggested``, and return them ranked by confidence (desc).

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/normalize/rules/{rule_id}/confirm`

Confirm Rule

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/normalize/rules/{rule_id}/reject`

Reject Rule

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/normalize/rules/{rule_id}/apply`

Apply Rule Route

Apply a confirmed rule in the background; ack immediately (202).

Apply runs ONLY when the rule is ``confirmed`` (or already ``applied`` — a
re-run is idempotent). ``suggested`` / ``rejected`` rules are refused.
On success the rule's status flips to ``applied`` with ``applied_at`` set.

**202:** Successful Response
**422:** Validation Error

---

## Ontology

### `GET /graphs/{tenant}/ontology`

Get Workspace Ontology

Effective layered ontology for this workspace (ONTA-397).

Canonical full-payload read: layers status + shadowed types. Empty is 200
with ``types: []``. Writes never go through this route.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/ontology/type-counts`

Workspace Type Counts

Workspace-wide union of per-type entity counts (ONTA-409).

Unions ``KgStats.type_breakdown`` across every knowledge graph in this
tenant's durable stats store — one relational read, no SPARQL. Types with
zero instances in every KG are omitted (so the response IS the Active set).

When that union is empty, fall back to live GraphStore counts
(:func:`_live_workspace_type_counts`) — on Neo4j nothing writes
``type_breakdown`` any more, so the durable union alone would report an
empty Active set for every workspace.

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ontology/types`

Create Type

**Request body:** `TypeCreate`

**201:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/ontology/types`

List Types

List effective types (tenant + visible global layers, shadowed).

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/ontology/types/{type_name}`

Get Type

Type detail from the effective (shadowed) layered ontology.

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ontology/types/{type_name}/attributes`

Add Attributes

**Request body:** `AttributeAdd`

**201:** Successful Response
**422:** Validation Error

---

### `DELETE /graphs/{tenant}/ontology/types/{type_name}/attributes/{attr_name}`

Delete Attribute Route

Drop one tenant-catalog attribute declaration.

Instance facts are left untouched. Explorer chips/columns that come from
the declared schema (empty ``lead_sponsor`` after a KG wipe) disappear
once this returns. Evicts the in-process type-summary cache for the
tenant so a refresh does not keep serving the deleted attr for 30 min.

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ontology/types/{type_name}/subtypes`

Add Subtype

**Request body:** `SubtypeAdd`

**201:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/ontology/schema`

Get Full Schema

Complete effective schema (layered + shadowed). Used by the NL pipeline.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/ontology/changelog`

Get Ontology Changelog

Return the workspace ontology changelog, newest first (ONTA-401).

Each entry's ``changes`` list is the ChangeRecord delta written at commit
time — enough to describe the mutation without consulting the live graph.
Scoped exclusively to this tenant's companion changelog graph.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/ontology/base-pin`

Get Workspace Base Pin

Current workspace base pin + revision + upgrade affordance (ONTA-410).

Ensures a pin when missing (soft backfill to latest) so the version strip
always has a defined state. Pin **read** infrastructure failures → 503
(never silent re-pin to latest).

The backfill is a WRITE, so it is gated on the caller's write capability
(ONTA-452): a read-only member sees the same pin, computed ephemerally, and
opening the version strip no longer pins or auto-upgrades their workspace.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/ontology/base-pin/preview`

Preview Workspace Base Upgrade

Preview upgrading the workspace base pin (structural ChangeRecords).

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ontology/base-pin/upgrade`

Post Workspace Base Upgrade

Upgrade the workspace base pin to ``to_version`` (or latest).

**Request body:** `BasePinUpgradeRequest`

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ontology/base-pin/rollback`

Post Workspace Base Rollback

Roll the workspace base pin back to its previous version.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/ontology/history`

Get Ontology History

Grouped (or flat) workspace ontology history (ONTA-410).

Default ``grouped=true`` collapses consecutive ``commit_ontology`` bursts
that share a job identity or fall within a 60s window — hundreds of
automatic mid-ingest revisions become a few history rows. Empty changelog
→ 200 with empty groups/entries, never an error.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/ontology/diff`

Get Ontology Diff

Structural ontology diff as ChangeRecords (ONTA-410).

Reuses ``diff_graphs`` / ``diff_shapes`` so the viewer and the pure
classifier see the same records. Missing snapshot graphs resolve to empty
shapes (clear empty), never a 500. Deep-link a version/revision that does
not exist → empty change list for that scope.

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ontology/aliases`

Register Attribute Alias

Register an attribute alias (old → new) on the tenant ontology graph.

Alias-edge only. Prefer ``POST /aliases/rename`` for a full rename that
also updates the schema declaration (always creates the alias).

**Request body:** `AliasRegister`

**201:** Successful Response
**422:** Validation Error

---

### `DELETE /graphs/{tenant}/ontology/aliases`

Retire Attribute Alias

Retire an alias after backfill — 409 while instance refs remain.

**Request body:** `AliasRetire`

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/ontology/aliases`

List Attribute Aliases

Return the flattened old→new attribute alias map for this tenant graph.

Chains (``a → b → c``) collapse to one hop (``a → c``, ``b → c``). Empty
when no aliases are registered. Cyclic chains are dropped (never hang).

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ontology/aliases/rename`

Rename Attribute With Alias

Full attribute rename — ALWAYS creates an alias (ONTA-407b).

Ensures the new attribute declaration, records ``old aliasOf new``, and
drops the old schema declaration. Instance triples keep the old predicate
until ``POST /aliases/backfill``; retirement refuses while refs remain.

**Request body:** `AliasRename`

**201:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ontology/aliases/backfill`

Backfill Attribute Aliases

Rewrite old-predicate instance triples onto their alias targets.

After a clean backfill (zero remaining refs), call
``DELETE /aliases`` (retire) to drop the alias edge.

**Request body:** `AliasBackfill`

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ontology/resolve`

Resolve Ontology

Resolve a fuzzy NL ask into ontology changes; auto-apply the confident
ones, return ambiguous/new-type ones as proposals for the caller to confirm
via `POST .../ontology/apply`.

`dry_run=True` (the interactive Explorer path) is plan-only: the resolver
runs exactly as below but NOTHING is written — every change (what
would have auto-applied plus the proposals) is returned under `proposals`,
with `applied` empty, so the UI can render one uniform reviewable list.

**Request body:** `ResolveRequest`

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ontology/apply`

Apply Ontology Change

Commit a single proposal previously returned by `/resolve` (stateless —
the caller passes the change object straight back). Idempotent.

Kept for back-compat; to apply several proposals at once use `/apply/batch`
(one round-trip instead of N).

**Request body:** `ResolvedChange`

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/ontology/apply/batch`

Apply Ontology Changes

Commit MANY proposals from one `/resolve` call in a single round-trip.

The canonical batch-apply route: every client (SDK `ontologyApplyBatch`,
MCP `apply_ontology_changes`) rides THIS endpoint as a thin pass-through —
none reimplements the loop client-side (interface convergence, CLAUDE.md).

Semantics — identical, per change, to `/apply` (same `_apply_change`, same
idempotent upserts), so N-in-one is equivalent to N single calls. Changes
apply in the submitted order. Partial-failure is well defined: a change that
raises is reported with `ok=False` + its error and does NOT abort the rest.

**Request body:** `ApplyBatchRequest`

**200:** Successful Response
**422:** Validation Error

---

## Operator

### `GET /operator/ontology/global`

Get Global Ontology

Return the ENTIRE Global ontology — Public + Enhanced layers — at once.

Cross-tenant by design (like the job trace above): the Global layers are the
shared canon, not any one tenant's ontology, so this route deliberately sits
outside ``/graphs/{tenant}/…``. One payload, not paginated: the curated
Global ontology is small, and the browser searches/sorts it client-side.

Never 500s on an empty or partially-unreachable Global ontology — an empty
canon is the expected state today (200 + ``types: []``), and a layer whose
graph errors is reported ``available: false`` while the other still renders.

**200:** Successful Response
**422:** Validation Error

---

### `GET /operator/ontology/global`

Get Global Ontology

Return the ENTIRE Global ontology — Public + Enhanced layers — at once.

Cross-tenant by design (like the job trace above): the Global layers are the
shared canon, not any one tenant's ontology, so this route deliberately sits
outside ``/graphs/{tenant}/…``. One payload, not paginated: the curated
Global ontology is small, and the browser searches/sorts it client-side.

Never 500s on an empty or partially-unreachable Global ontology — an empty
canon is the expected state today (200 + ``types: []``), and a layer whose
graph errors is reported ``available: false`` while the other still renders.

**200:** Successful Response
**422:** Validation Error

---

### `GET /operator/jobs/{job_id}/trace`

Get Job Stage Trace

Return the P0–P9 contract-level stage trace for a job.

Cross-tenant: any job id the store knows about is visible to operators.
Prefer live ``job.stage_trace`` when present; otherwise reconstruct from
manifest / provider_logs / progress so pre-instrumentation jobs still
render.

**Ask/agent answer runs (ONTA-389):** a completed ``/ask`` or agent
``kind:answer`` turn mints a job with ``category=answer`` and returns
``run_id`` (= this ``job_id``) on the answer payload. Open this endpoint with
that id to see live **P7 Answer (A7)** + **P0/A9** coverage on Job Trace.

**200:** Successful Response
**422:** Validation Error

---

### `GET /operator/jobs/{job_id}/trace`

Get Job Stage Trace

Return the P0–P9 contract-level stage trace for a job.

Cross-tenant: any job id the store knows about is visible to operators.
Prefer live ``job.stage_trace`` when present; otherwise reconstruct from
manifest / provider_logs / progress so pre-instrumentation jobs still
render.

**Ask/agent answer runs (ONTA-389):** a completed ``/ask`` or agent
``kind:answer`` turn mints a job with ``category=answer`` and returns
``run_id`` (= this ``job_id``) on the answer payload. Open this endpoint with
that id to see live **P7 Answer (A7)** + **P0/A9** coverage on Job Trace.

**200:** Successful Response
**422:** Validation Error

---

## Query

### `POST /graphs/{tenant}/query`

Execute Query

Gone. Use ``/ask``, ``/agent``, or the explore APIs.

**Request body:** `SPARQLQuery`

**200:** Successful Response
**410:** Gone — raw SPARQL was removed with the Neptune backend. Use agent / SDK / high-level APIs.
**422:** Validation Error

---

### `POST /graphs/{tenant}/update`

Execute Update

Gone. Use ``/kgs`` or ingest for workspace-scoped writes.

**Request body:** `SPARQLUpdate`

**200:** Successful Response
**410:** Gone — raw SPARQL was removed with the Neptune backend. Use agent / SDK / high-level APIs.
**422:** Validation Error

---

## Schedules

### `POST /graphs/{tenant}/schedules`

Create Schedule

Create a recurring schedule and compute its initial ``next_run``.

Exactly one of ``cron`` / ``interval_seconds`` must be set (422 otherwise).
``next_run`` is seeded from ``created_at`` so the firing loop can pick it up
on the next sweep.

**Request body:** `ScheduleCreateRequest`

**201:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/schedules`

List Schedules

List a tenant's schedules, oldest first (creation order).

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/schedules/{schedule_id}`

Get Schedule

Fetch a single schedule by id (scoped to the authorized tenant).

**200:** Successful Response
**422:** Validation Error

---

### `PATCH /graphs/{tenant}/schedules/{schedule_id}`

Update Schedule

Enable/disable or update a schedule.

Only provided fields change. If the recurrence (``cron``/
``interval_seconds``) changes, ``next_run`` is recomputed from now.

System-managed rows (action outside ``USER_SCHEDULABLE_ACTIONS`` — the
per-KG ``semantic-reconcile:{tenant}:{kg}`` rows the reconciler auto-creates
carry the caller's tenant_id, so they ARE reachable here) are tenant-visible
but not tenant-tunable: every PATCH to such a row is rejected with 403.
Rejecting wholesale (rather than field-by-field) keeps the policy simple and
airtight — cadence/action/params are platform-owned (the reconciler re-tunes
them from env knobs), and even an ``enabled`` flip would silently switch off
index maintenance while the row still looks healthy. The 403 (vs 404) is
deliberate: the row is visible via GET/list, so pretending it doesn't exist
would be misleading.

**Request body:** `ScheduleUpdateRequest`

**200:** Successful Response
**422:** Validation Error

---

### `DELETE /graphs/{tenant}/schedules/{schedule_id}`

Delete Schedule

Delete a schedule (scoped to the authorized tenant).

Deleting a system-managed semantic row (action outside
``USER_SCHEDULABLE_ACTIONS``) is allowed but is NOT a durable opt-out: the
reconciler's ensure-* hooks recreate the row on the next KG write or
reindex request. Disabling semantic maintenance is done via its feature
flag, not by deleting rows.

**204:** Successful Response
**422:** Validation Error

---

## Search

### `POST /graphs/{tenant}/search`

Semantic Search

Hybrid semantic search over the tenant's indexed free-text attributes.

Auth is the same ``get_tenant`` dependency as every ``/graphs/{tenant}``
route: the search is ALWAYS scoped to the resolved tenant (a multi-tenant
key requesting an unowned path tenant is a 403 before this body runs), so
the index's tenant-isolation contract starts here. See the module
docstring for the full documented semantics (lexical-degrade when the gate
is off, 400 on blank query, unknown-KG-empty, top_k clamp, type-staleness
caveat).

**Request body:** `SearchRequest`

**200:** Successful Response
**422:** Validation Error

---

## Skills

### `GET /graphs/{tenant}/skills`

List Skills

List the skills visible to this workspace, in precedence order.

Returns the RESOLVED union across every visible layer — tenant-authored
skills plus the curated global ones — because that union is what an agent
actually sees. Global rows come back with ``editable: false``.

Unlike the API-source catalog, the global layers are NOT operator-gated
here: a curated skill is content the workspace is meant to benefit from
(and Global-Enhanced is already gated by entitlement), not a disclosure of
our vendor stack.

**200:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/skills`

Create Skill

Create (or replace) a TENANT-layer skill.

Idempotent on ``(type_name, slug)``: posting the same slug again replaces the
body and bumps ``version``, so a client that retries cannot fork the skill.

Optional ``archive_b64`` + ``filename`` parse a markdown file or skill-package
zip on this same route (no second endpoint). Explicit JSON fields win.

**Request body:** `CreateSkillRequest`

**201:** Successful Response
**422:** Validation Error

---

### `POST /graphs/{tenant}/skills/validate`

Validate Skill Route

Validate a skill without writing it (the authoring pre-flight).

**Request body:** `CreateSkillRequest`

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/skills/prompt-block`

Get Prompt Block

Return the EXACT text an LM agent would be handed for these types.

The canonical read of the injection seam
(``skills.resolve.skills_prompt_block``): clients render nothing themselves,
so the block a CLI or MCP agent sees is byte-identical to the one the
backend's own planner would inject. Empty ``type_name`` → empty text.

**200:** Successful Response
**422:** Validation Error

---

### `GET /graphs/{tenant}/skills/{type_name}/{slug}`

Get Skill

Read one resolved skill (full body), whichever layer wins.

**200:** Successful Response
**422:** Validation Error

---

### `PATCH /graphs/{tenant}/skills/{type_name}/{slug}`

Update Skill

Partially update a TENANT skill. 403 on a curated global skill.

A tenant cannot edit curated content in place; the sanctioned override is to
POST a tenant skill with the SAME slug, which shadows the global one for
this workspace only.

**Request body:** `UpdateSkillRequest`

**200:** Successful Response
**422:** Validation Error

---

### `DELETE /graphs/{tenant}/skills/{type_name}/{slug}`

Delete Skill

Delete a TENANT skill. 403 on a curated global skill, 404 if unknown.

**200:** Successful Response
**422:** Validation Error

---

## Tenants

### `GET /v1/me/tenants`

List Tenants

**200:** Successful Response

---

### `POST /v1/me/tenants`

Add Tenant

**201:** Successful Response
**422:** Validation Error

---

### `PATCH /v1/me/tenants/{tenant_id}`

Rename Tenant

Rename one of the caller's workspaces. The id is immutable — it keys the
graph IRIs — so only the label changes.

**Request body:** `TenantRename`

**200:** Successful Response
**422:** Validation Error

---

### `DELETE /v1/me/tenants/{tenant_id}`

Remove Tenant

**200:** Successful Response
**422:** Validation Error

---

## Triples

### `POST /graphs/{tenant}/triples`

Create Triples

**Request body:** `TripleCreate`

**200:** Successful Response
**410:** Gone — raw triple SPO was removed with the Neptune/SPARQL backend. Use ingest / enrich / agent / KG-scoped writes.
**422:** Validation Error

---

### `GET /graphs/{tenant}/triples`

Get Triples

**200:** Successful Response
**410:** Gone — raw triple SPO was removed with the Neptune/SPARQL backend. Use ingest / enrich / agent / KG-scoped writes.
**422:** Validation Error

---

### `DELETE /graphs/{tenant}/triples`

Remove Triples

**Request body:** `TripleDelete`

**200:** Successful Response
**410:** Gone — raw triple SPO was removed with the Neptune/SPARQL backend. Use ingest / enrich / agent / KG-scoped writes.
**422:** Validation Error

---

## Usage

### `GET /graphs/{tenant}/usage`

Get Usage

Day-aligned usage report for the tenant, newest day last.

``days`` sets the current window; the preceding window of equal length is
aggregated into ``prev_totals`` for period-over-period deltas.

**200:** Successful Response
**422:** Validation Error

---

## User_Api_Sources

### `GET /v1/me/api-sources`

List User Api Sources

List the caller's user-scoped sources only (not tenant_custom, not global).

**200:** Successful Response

---

### `POST /v1/me/api-sources`

Create User Api Source

Create a user-scoped source. Secrets are encrypted under ``user:{subject}``.

**Request body:** `CreateApiSourceRequest`

**201:** Successful Response
**422:** Validation Error

---

### `GET /v1/me/api-sources/{slug}`

Get User Api Source

Read one of the caller's user sources (secrets REDACTED) + ``has_secret``.

**200:** Successful Response
**422:** Validation Error

---

### `PATCH /v1/me/api-sources/{slug}`

Update User Api Source

Edit a user-scoped source (spec body, enabled, and/or secrets).

**Request body:** `UpdateApiSourceRequest`

**200:** Successful Response
**422:** Validation Error

---

### `DELETE /v1/me/api-sources/{slug}`

Delete User Api Source

Delete a user-scoped source + its stored secrets.

**200:** Successful Response
**422:** Validation Error

---

## Workspace

### `POST /v1/me/tenants/{tenant_id}/invites`

Create Invite

**Request body:** `InviteCreate`

**201:** Successful Response
**422:** Validation Error

---

### `GET /v1/me/tenants/{tenant_id}/invites`

List Invites

**200:** Successful Response
**422:** Validation Error

---

### `DELETE /v1/me/tenants/{tenant_id}/invites/{invite_id}`

Revoke Invite

**200:** Successful Response
**422:** Validation Error

---

### `GET /v1/me/tenants/{tenant_id}/members`

List Members

**200:** Successful Response
**422:** Validation Error

---

### `DELETE /v1/me/tenants/{tenant_id}/members/{member_subject}`

Remove Member

**200:** Successful Response
**422:** Validation Error

---

### `GET /v1/me/invites`

My Invites

**200:** Successful Response

---

### `POST /v1/me/invites/{invite_id}/accept`

Accept Invite

**200:** Successful Response
**422:** Validation Error

---

### `POST /v1/me/invites/{invite_id}/decline`

Decline Invite

**200:** Successful Response
**422:** Validation Error

---

### `POST /v1/invites/accept`

Accept Invite By Token

**Request body:** `TokenAccept`

**200:** Successful Response
**422:** Validation Error

---

## Schemas

### AcceptOut

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tenant_id` | string | Yes |  |
| `label` | string | Yes |  |
| `role` | string | Yes |  |
| `capability` | string | No |  |
| `status` | string | Yes |  |

### AgentRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `message` | string | No | The user's message to the agent |
| `context` | #/components/schemas/AgentRequestContext | No |  |
| `session_id` | object | No |  |
| `confirm` | object | No |  |
| `spend_ceiling_usd` | object | No |  |

### AgentRequestContext

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `kg_name` | string | No |  |
| `type_name` | object | No |  |
| `selection` | object | No |  |
| `urls` | array | No |  |
| `medium` | string | No |  |

### AliasBackfill

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `kg_name` | string | Yes | Knowledge graph name whose instance graph is rewritten |
| `old_attr_uri` | object | No | Optional single old attribute IRI to backfill. When omitted, every registered alias on the tenant ontology is backfilled. |
| `batch_size` | integer | No | Triples per DELETE/INSERT batch |

### AliasMapResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `aliases` | object | No | old_attr_iri → new_attr_iri (chains flattened to one hop) |

### AliasRegister

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type_name` | string | Yes | Type owning the OLD attribute (context for bare leaves and ChangeRecord) |
| `from_slot` | string | Yes | Old attribute leaf name, or a full attribute IRI |
| `to_slot` | string | Yes | New attribute leaf name, or a full attribute IRI |
| `to_type` | object | No | Type owning the NEW attribute when different from type_name (hierarchy move, e.g. Guest.phone_num → Person.phone) |

### AliasRename

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type_name` | string | Yes | Type owning the OLD attribute |
| `from_slot` | string | Yes | Old attribute leaf name, or a full attribute IRI |
| `to_slot` | string | Yes | New attribute leaf name, or a full attribute IRI |
| `to_type` | object | No | Type owning the NEW attribute when different from type_name (hierarchy move) |
| `datatype` | object | No | Datatype for the new attribute when it must be minted (default string) |
| `description` | object | No | Optional description written onto the new attribute declaration |

### AliasRetire

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type_name` | string | Yes | Type owning the OLD attribute (for bare leaves) |
| `from_slot` | string | Yes | Old attribute leaf name, or a full attribute IRI |
| `kg_name` | string | Yes | KG whose instance graph is checked for remaining old-predicate triples |

### ApiRequestTrace

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | No |  |
| `params` | object | No |  |
| `status` | object | No |  |
| `records` | integer | No |  |
| `error` | object | No |  |

### ApiSourceSummary

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `slug` | string | Yes |  |
| `title` | string | Yes |  |
| `publisher` | string | Yes |  |
| `description` | string | Yes |  |
| `layer` | string | Yes |  |
| `authority_level` | string | Yes |  |
| `entity_kinds` | array | Yes |  |
| `attributes` | array | Yes |  |
| `enabled` | boolean | Yes |  |
| `editable` | boolean | Yes |  |
| `has_secret` | boolean | Yes |  |

### ApplyBatchRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `changes` | array | Yes | The resolved changes to apply, in order. Idempotent (upserts). |

### ApplyBatchResult

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `results` | array | No |  |
| `applied_count` | integer | No |  |
| `failed_count` | integer | No |  |
| `operations` | integer | No |  |
| `summary` | string | No |  |

### ApplyChangeResult

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `change` | #/components/schemas/ResolvedChange | Yes |  |
| `ok` | boolean | No |  |
| `operations` | integer | No |  |
| `error` | string | No |  |

### ApplyRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `decisions` | array | Yes |  |

### AttributeAdd

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `attributes` | array | Yes |  |

### AttributeDefinition

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `description` | string | No |  |
| `datatype` | string | No | string, integer, float, boolean, datetime, uri, geo (WKT point / 'lat,lon'), or a type name for relationships |

### AttributeUsage

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `datatype` | string | No |  |
| `count` | integer | Yes |  |

### BasePinResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tenant_id` | string | Yes |  |
| `base_layer` | string | No |  |
| `base_version` | object | No | Pinned release number, or null when tracking live |
| `is_live` | boolean | No |  |
| `auto_upgrade` | boolean | No |  |
| `previous_version` | object | No |  |
| `has_previous` | boolean | No |  |
| `updated_at` | object | No |  |
| `workspace_revision` | integer | No | ONTA-403 workspaceRevision counter (0 if never committed) |
| `latest_available` | object | No | Latest published release for the pin's base layer, if any |
| `upgrade_available` | boolean | No | True when latest_available > base_version (or pin is live and a release exists) |

### BasePinUpgradeRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `to_version` | object | No | Target release number; omit to upgrade to latest |

### CSVRowsRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `mapping` | #/components/schemas/CSVSchemaMapping | Yes |  |
| `rows` | array | Yes |  |
| `source` | string | No |  |
| `kg_name` | object | No |  |
| `key_join` | object | No |  |

### CSVSchemaMapping

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_type` | string | Yes |  |
| `columns` | array | Yes |  |
| `entities` | object | No |  |
| `relationships` | object | No |  |
| `violations` | array | No | Structural violations the refute pass found in the proposed schema (already corrected in this mapping) |
| `inference_audit` | object | No | How this mapping was inferred (v2 pipeline only) |
| `ontology_extensions` | object | No | Pass D (COMPLETE) output: dependent-entity promotions, constitutive core slots (max 3/type), dataset constants, and the rejected-candidate audit list. None on the legacy path and on payloads serialized before COG-52. held_for_review items are a client-side confirm gate — /ingest/csv/rows applies whatever the client posts back (judge-panel gating is COG-56). |

### CSVSchemaRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `headers` | array | Yes |  |
| `sample_rows` | array | Yes |  |
| `total_rows` | integer | No |  |

### ChangeKind

### ChangeRecord

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `kind` | #/components/schemas/ChangeKind | Yes |  |
| `type_name` | object | No | Bare type name the change attaches to, when applicable |
| `slot_name` | object | No | Attribute or relationship leaf name, when the change is slot-scoped |
| `parent_type` | object | No | Parent type name for subclass-edge changes |
| `old_value` | object | No |  |
| `new_value` | object | No |  |
| `from_name` | object | No | Prior name for RENAME_WITH_ALIAS |
| `to_name` | object | No | New name for RENAME_WITH_ALIAS |
| `superseded_by` | object | No | Replacement identity for DEPRECATE |

### CleanFact

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `datatype` | string | Yes |  |
| `raw_value` | string | Yes |  |
| `clean_value` | object | Yes |  |
| `outcome` | #/components/schemas/CleanOutcome | Yes |  |
| `conformed` | boolean | No |  |
| `reason` | string | No |  |
| `entity_id` | string | No |  |
| `attribute` | string | No |  |

### CleanOutcome

### CleanReport

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `passed` | array | No |  |
| `transformed` | array | No |  |
| `dropped` | array | No |  |

### CollisionRecordResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type_name` | string | Yes |  |
| `slot_name` | object | No |  |
| `kind` | string | No |  |
| `detail` | string | No |  |

### ColumnMapping

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `column_name` | string | Yes |  |
| `role` | #/components/schemas/ColumnRole | Yes |  |
| `target_type` | object | No |  |
| `datatype` | string | No |  |
| `attribute_name` | object | No |  |
| `entity` | object | No |  |
| `confidence` | object | No | LLM confidence in this column decision (v2 inference) |
| `why` | object | No | Profile-evidence rationale for this column decision (v2 inference) |
| `text_kind` | object | No | 'free_text' when this column holds free-running prose worth semantic indexing (ONTA-177); 'not_text' when a text-shaped column was explicitly adjudicated NOT prose (durable decided-no, ONTA-173); both persisted as an ontology `textKind` marker on the attribute at ingest time; None = undecided |

### ColumnRole

### Confirm

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `plan_id` | string | Yes |  |

### ConflictPolicy

### ConflictReview

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_uri` | string | Yes |  |
| `attribute` | string | Yes |  |
| `existing_value` | string | Yes |  |
| `proposed` | #/components/schemas/Verdict | Yes |  |
| `decision` | object | No |  |
| `existing_source_url` | object | No |  |
| `existing_verified_at` | object | No |  |

### CoreSlot

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `kind` | string | No |  |
| `target_type` | object | No | PascalCase type a relationship-kind slot points at |
| `why` | object | No |  |
| `tests` | object | No | per-test verdicts (existence/identity/universality) |
| `dataset_constant` | object | No |  |
| `confidence` | object | No | optional model confidence in this slot (when emitted) |
| `held_for_review` | boolean | No | True when this slot needs user confirmation before ingest: its confidence (or its dataset constant's) is below 0.7, or the constant carries no confidence at all |

### CoreSlotTests

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `existence` | boolean | No | an instance cannot exist in reality without this slot |
| `identity` | boolean | No | needed to individuate instances, OR the type is a dependent entity existing only relative to the slot's target |
| `universality` | boolean | No | holds for every instance of the concept in any dataset |

### CorrectionRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `kg_name` | string | Yes | KG the entity lives in |
| `subject` | string | Yes | Canonical entity IRI (the record's rec.id) |
| `attribute` | string | Yes | Attribute leaf name (the field's key) |
| `value` | string | Yes | The corrected value |
| `type_name` | string | No | Entity type; derived from subject when omitted |
| `reason` | string | No | Optional note explaining the correction |

### CorrectionResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | string | Yes |  |
| `subject` | string | Yes |  |
| `predicate` | string | Yes |  |
| `value` | string | Yes |  |
| `authority` | string | Yes |  |
| `superseded` | array | Yes |  |
| `run_id` | string | Yes |  |

### CreateApiSourceRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `spec` | object | Yes |  |
| `secrets` | object | No |  |
| `enabled` | object | No |  |

### CreateJobResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | object | No |  |
| `status` | string | Yes |  |
| `matched_entities` | object | No |  |
| `resolved_tier` | object | No |  |
| `routing_note` | object | No |  |
| `needs_clarification` | boolean | No |  |
| `candidates` | object | No |  |

### CreateRuleRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `kg_name` | string | Yes |  |
| `type_name` | string | Yes |  |
| `predicate` | string | Yes |  |
| `target_kind` | string | Yes |  |
| `rule_type` | string | Yes |  |
| `params` | object | No |  |
| `confidence` | number | No |  |
| `rationale` | string | No |  |
| `status` | string | No |  |

### CreateSkillRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `slug` | string | No |  |
| `type_name` | string | Yes |  |
| `body` | string | No | The markdown skill body — this IS the skill. Optional when archive_b64 is set. |
| `title` | string | No |  |
| `summary` | string | No |  |
| `enabled` | boolean | No |  |
| `metadata` | object | No |  |
| `filename` | string | No |  |
| `archive_b64` | string | No |  |

### DatasetConstant

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `value` | string | Yes |  |
| `confidence` | object | No | model confidence that the constant is implied; <0.7 (or absent) holds the slot for review |

### DeleteJobResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `deleted` | boolean | Yes |  |
| `job_id` | string | Yes |  |

### DiscoveredEntity

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `uri` | string | Yes |  |
| `type` | string | Yes |  |
| `name` | string | Yes |  |
| `functions` | array | Yes |  |
| `skills` | array | Yes |  |

### EnrichActionRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type_name` | string | Yes |  |
| `attributes` | array | Yes |  |
| `kg_name` | string | Yes |  |
| `tier` | #/components/schemas/EnrichmentTier | No |  |
| `conflict_policy` | #/components/schemas/ConflictPolicy | No |  |
| `confidence_min` | number | No |  |
| `limit` | object | No |  |
| `scope` | object | No |  |
| `entity_uris` | object | No |  |
| `instructions` | object | No |  |
| `sources` | object | No |  |
| `spend_ceiling_usd` | object | No |  |

### EnrichJob

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes |  |
| `tenant_id` | string | Yes |  |
| `kg_name` | string | Yes |  |
| `type_name` | string | Yes |  |
| `attributes` | array | Yes |  |
| `tier` | #/components/schemas/EnrichmentTier | Yes |  |
| `status` | #/components/schemas/JobStatus | Yes |  |
| `progress` | #/components/schemas/JobProgress | No |  |
| `created_at` | string | Yes |  |
| `started_at` | object | No |  |
| `completed_at` | object | No |  |
| `conflict_policy` | #/components/schemas/ConflictPolicy | Yes |  |
| `confidence_min` | number | No |  |
| `error` | object | No |  |
| `limit` | object | No |  |
| `results` | array | No |  |
| `scope` | object | No |  |
| `entity_uris` | object | No |  |
| `instructions` | object | No |  |
| `sources` | object | No |  |
| `source_urls` | array | No |  |
| `category` | #/components/schemas/JobCategory | No |  |
| `trigger` | #/components/schemas/JobTrigger | No |  |
| `last_run` | object | No |  |
| `next_run` | object | No |  |
| `cost` | object | No |  |
| `cost_note` | object | No |  |
| `spend_ceiling_usd` | object | No |  |
| `result_count` | object | No |  |
| `platforms` | object | No |  |
| `provider_logs` | array | No |  |
| `error_summary` | array | No |  |
| `manifest` | object | No |  |
| `thread_id` | object | No |  |
| `stage_trace` | object | No |  |

### EnrichRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type_name` | string | Yes |  |
| `attributes` | array | Yes |  |
| `tier` | #/components/schemas/EnrichmentTier | No |  |
| `kg_name` | string | Yes |  |
| `conflict_policy` | #/components/schemas/ConflictPolicy | No |  |
| `confidence_min` | number | No |  |
| `limit` | object | No |  |
| `scope` | object | No |  |
| `entity_uris` | object | No |  |
| `instructions` | object | No |  |
| `sources` | object | No |  |
| `target_urls` | object | No |  |
| `thread_id` | object | No |  |
| `spend_ceiling_usd` | object | No |  |

### EnrichScope

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `predicate` | string | Yes |  |
| `value` | string | Yes |  |

### EnrichmentTier

### EntityRelationSpec

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `subject` | string | Yes |  |
| `predicate` | string | Yes |  |
| `object` | string | Yes |  |
| `why` | object | No | Profile-evidence rationale for this edge (v2 inference) |

### EntitySample

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `uri` | string | Yes |  |
| `label` | string | No |  |

### EntitySpec

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `type_name` | string | Yes |  |
| `id_column` | object | No |  |
| `id_from` | object | No |  |
| `key_strategy` | object | No | How this entity is keyed: 'column' = id_column natural key, 'composite' = deterministic id_from composite, 'synthetic' = content-hash key minted per row (ADR 0003 §2). None = legacy mapping that predates the v2 inference pipeline. |
| `confidence` | object | No | LLM confidence in this entity decision (v2 inference) |
| `why` | object | No | Profile-evidence rationale for this entity decision (v2 inference) |

### FactCitation

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `subject` | string | No |  |
| `predicate` | string | No |  |
| `object` | string | No |  |
| `label` | string | No |  |
| `verdict` | string | No |  |
| `confidence` | object | No |  |
| `valid_from` | string | No |  |
| `is_current` | boolean | No |  |
| `source` | string | No |  |
| `truth_verdict` | string | No |  |

### FunctionRef

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `entity_type` | string | Yes |  |
| `description` | string | No |  |
| `endpoint_url` | object | No |  |
| `tier` | #/components/schemas/FunctionTier | No |  |
| `layer` | string | No |  |

### FunctionRegister

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `entity_type` | string | Yes | Type to attach to. Bare name → Tenant; full Enhanced URI or path-shaped 'x/<Type>' → Enhanced; Public is refused (ONTA-400). |
| `endpoint_url` | string | Yes | HTTPS URL or Lambda function ARN (arn:aws:lambda:…) |
| `description` | string | No |  |
| `layer` | object | No |  |

### FunctionTier

### GlobalOntologyAttribute

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `datatype` | string | No | Primitive datatype name: string, integer, float, boolean, datetime, uri, geo |
| `description` | object | No | rdfs:comment on the property URI; null when absent |
| `core_slot` | boolean | No | onto/coreSlot marker — a CONSTITUTIVE slot (ADR 0003 Pass D) |

### GlobalOntologyLayer

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `layer` | string | Yes |  |
| `graph_uri` | string | Yes |  |
| `type_count` | integer | No |  |
| `available` | boolean | No |  |

### GlobalOntologyRelationship

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `target_type` | string | Yes | Bare type NAME the range points at, not a URI |
| `description` | object | No | rdfs:comment on the property URI; null when absent |
| `core_slot` | boolean | No |  |

### GlobalOntologyResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `layers` | array | No |  |
| `types` | array | No |  |

### GlobalOntologySkill

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `slug` | string | Yes | Skill id within (type, layer); URL-safe |
| `type_name` | string | Yes | The type name the skill declares itself attached to. Normally the enclosing type's name, but attachment is matched CASE-INSENSITIVELY, so the two can differ in case — this is the exact spelling the canonical `/graphs/{tenant}/skills/{type_name}/{slug}` read wants. |
| `title` | string | No | Human title; empty ⇒ fall back to the slug |
| `summary` | string | No | The AUTHORED one-line gist (front-matter `summary`), capped at 500 chars by validation. Empty is common — it is optional on the author's side — which is why `excerpt` exists as well. |
| `excerpt` | string | No | First ~400 chars of the markdown body with runs of whitespace collapsed, cut on a word boundary and suffixed with '…' when it was truncated. DERIVED, never authored. Whitespace collapsing means markdown structure (headings, bullets) does not survive it — render it as a plain prose preview, never as markdown. |
| `body_chars` | integer | No | Length of the FULL raw body (not the excerpt). `body_chars > len(excerpt)` ⇒ there is more text behind the canonical read. |
| `layer` | string | No | The SKILL's own ontology layer: "public" or "enhanced". Unlike ``GlobalOntologySource.registry_layer`` this is the SAME axis as ``GlobalOntologyType.layer`` and may be rendered with the same badge — but it can legitimately DIFFER from the enclosing type's layer: skills are looked up by type NAME across both global layers, so a public-layer type can carry a curated enhanced-layer skill (and that skill is only visible to entitled workspaces at resolution time). TENANT-layer skills can never appear here — see ``GlobalOntologyType.skills``. |
| `enabled` | boolean | No | False ⇒ authored but switched off. A disabled skill is still listed (this is the operator's raw browse view) and is NOT injected into any agent prompt; it also SUPPRESSES a same-slug skill from a lower layer rather than falling through to it (`skills.resolve.merge_layers`). |
| `version` | integer | No | Monotonic per-(scope, type, slug) revision, bumped on upsert |

### GlobalOntologySource

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `slug` | string | Yes | Catalog entry id, unique across layers |
| `title` | string | No |  |
| `publisher` | string | No |  |
| `registry_layer` | string | No | The SOURCE CATALOG's layer — NOT an ontology layer. Values are "global_public" / "global_enhanced" (and "tenant_custom", which can never appear here). This is a DIFFERENT AXIS from ``GlobalOntologyType.layer`` ("public" / "enhanced"): different subsystem, different vocabulary, different precedence ranks, no relationship whatsoever between the two. A ``global_public`` API can cover an ``enhanced``-layer type and a ``global_enhanced`` API can cover a ``public`` one. The field is NAMED apart from the type's ``layer`` precisely so the two can never be rendered with the same badge by mistake — do not shorten it back to ``layer``. |
| `authority_level` | string | No | ApiSourceSpec.authority_level, e.g. source_of_truth / authoritative / supplementary |
| `enabled` | boolean | No | False ⇒ the entry is catalogued but not served |
| `verified_at` | string | No | ISO date (YYYY-MM-DD) the entry's call spec was last hand-verified; empty ⇒ never |
| `freshness` | string | No | Verification grade from the EXISTING catalog audit (``api_registry/catalog_audit.py``), never a second health scale: "UNVERIFIED" (no/unparseable verified_at), "STALE" (older than the audit's max age), "FUTURE" (stamp in the future — a typo), or "OK". Live reachability (the audit's optional EMPTY / UNREACHABLE smoke) is deliberately NOT computed here: this read must stay offline. |
| `entity_kinds` | array | No | The entry's declared ``coverage.entity_kinds`` — the EVIDENCE the token match ran against, so an operator can see WHY a source was attached (or was not). |

### GlobalOntologyType

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `layer` | string | Yes | Layer that declares this type: "public" or "enhanced" |
| `description` | object | No |  |
| `parent_type` | object | No | Bare parent type NAME from rdfs:subClassOf, not a URI |
| `subtypes` | array | No | Bare NAMES of types (in EITHER Global layer) whose rdfs:subClassOf points here |
| `attributes` | array | No |  |
| `relationships` | array | No |  |
| `sources` | array | No | Registered API sources that PLAUSIBLY cover this type (fuzzy token match on coverage.entity_kinds — see GlobalOntologySource), sorted by slug. Empty is normal: no covering source, or the registry was unavailable (which degrades to [] and never fails the request). |
| `functions` | array | No | Executable code attached to this type, read from THIS LAYER's graph. Enhanced attachments use ``types/x/<T>`` via ``queries.register_function_triple`` (ONTA-399); Public may not carry functions (ONTA-400). ``entity_type`` is the enclosing type's name; ``tier`` is not stored in the graph and carries the model default, exactly as the tenant ``GET /graphs/{tenant}/functions`` route reports it. |
| `skills` | array | No | Curated GLOBAL-layer skills attached to this type NAME — markdown prose taught to an LM agent, NOT executable code (that is ``functions``). Sorted by slug, with the skill's own layer breaking ties so a slug curated in BOTH global layers lists as two adjacent rows: this is the operator's raw browse view, so that override is SHOWN, not silently resolved (Enhanced wins at resolution time). Bodies are NOT inlined — see ``GlobalOntologySkill``. **Global layers only.** A workspace's private tenant-layer skills live in the durable per-tenant store and are structurally unreachable from here: this reader calls ``skills.registry.global_skills_for_type``, which reads only the process-wide curated registry (whose writer, ``register_skill_layer``, REJECTS ``Layer.TENANT`` and blanks ``tenant_id``) plus the OSS seed directory. It never touches the store and takes no tenant context. Empty is normal: no curated skill for the name, or the skills subsystem was unavailable (which degrades to [] and never fails the request). |

### GrepMatch

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_uri` | string | Yes |  |
| `label` | string | No |  |
| `type` | string | No |  |
| `predicate` | string | Yes |  |
| `attr` | string | Yes |  |
| `value` | string | Yes |  |
| `snippet` | string | Yes |  |

### GrepRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `q` | string | Yes | Substring to look for in literal values. Must contain at least 2 non-whitespace characters. Plain substring matching, NOT a regex or a glob. |
| `kg_name` | string | Yes | REQUIRED context graph to scan. Unlike /search this is not optional: the scan is index-free, so bounding it to one graph is the primary cost control. |
| `type` | object | No | Only match triples whose subject is an instance of this type (bare type name, e.g. 'Person'). Matched across every ontology layer namespace, so a Public/Enhanced-typed instance is included. Must match ^[a-zA-Z0-9_-]+$ — the charset a well-formed type IRI can carry. |
| `predicate` | object | No | Only match this predicate. Accepts a full URI, or a bare leaf name (e.g. 'title') matched against the tail of the predicate URI. |
| `case_sensitive` | boolean | No | Match case-sensitively. Default false (LCASE both sides). |
| `limit` | integer | No | Maximum matches to return. Clamped server-side to [1, 200]; the effective value is echoed in the response. |

### GrepResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `matches` | array | No |  |
| `count` | integer | No |  |
| `limit` | integer | No |  |
| `truncated` | boolean | No |  |

### HTTPValidationError

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `detail` | array | No |  |

### HaltReasonKind

### InferenceAudit

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `pipeline` | string | No | 'reason_refute_v2' (profile → reason → refute → complete; the completion pass's output lives in ontology_extensions) — the legacy single-call path emits no audit |
| `rows_profiled` | integer | No | sample rows Pass A profiled |
| `total_rows` | integer | No | declared full-file size |
| `profile` | object | No | compact Pass A profile (TableProfile.to_prompt_dict) the decisions were grounded in |

### IngestRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `content` | string | Yes | Raw text, JSON, or CSV to ingest |
| `content_type` | string | No | text, json, or csv |
| `source` | string | No | Source identifier for provenance |
| `kg_name` | object | No | Knowledge graph name. If set, data goes into a KG-specific graph. |

### IngestResult

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `batch_id` | string | No | Batch ID for rollback support |
| `job_id` | object | No | Tracked EnrichJob id for this file ingest (category=ingest), when the API recorded a Jobs entry with live stage_trace. None when the job store was unavailable or the call path does not track jobs. |
| `entities_extracted` | integer | No |  |
| `entities_resolved` | integer | No |  |
| `triples_inserted` | integer | No |  |
| `types_created` | array | No |  |
| `attributes_added` | array | No |  |
| `node_target_types` | array | No |  |
| `rejections` | array | No |  |
| `flagged_types` | array | No | Types needing user review |
| `chunks_processed` | integer | No |  |
| `entities_deduplicated` | integer | No |  |
| `rows_in` | integer | No | Input rows received by this ingest call (CSV paths) |
| `rows_dropped` | integer | No | Rows that produced no entity at all — only possible when every owned value in the row is empty (nothing to assert). Never silent: a structured warning is logged whenever this is > 0. |
| `drops_by_entity` | object | No | Skipped entity-instances per mapping entity. Keys are the entity_type in single-entity mode, or the EntitySpec.name in multi-entity mode (where one row can mint some entities while skipping an all-empty one without the row itself being dropped). |
| `free_text_attributes` | array | No | 'Type.attr' entries that received a textKind='free_text' ontology marker during this ingest (schema-time semantic-index candidacy, ONTA-177) |
| `rows_key_merged` | integer | No | Rows whose key value matched an existing entity, so their attributes were merged ONTO that node (no duplicate minted). |
| `rows_key_minted` | integer | No | Rows whose key value matched no existing entity and minted a new node (only when the key-join allows minting unmatched rows). |
| `rows_key_unmatched` | integer | No | Rows whose key value matched no existing entity and were SKIPPED (key-join with mint_unmatched=false). Reported, never silent. |
| `graph_delta` | object | No |  |
| `clean_report` | #/components/schemas/CleanReport | No |  |
| `verified_facts` | array | No |  |

### InviteCreate

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `email` | string | Yes | Invitee email address. |
| `role` | string | No | Invited tenant role: 'writer' (may mutate data/schema) or 'reader' (read-only). Legacy 'member' is accepted and stored as 'writer'. 'owner' cannot be invited. |

### InviteCreateOut

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `invite` | #/components/schemas/InviteOut | Yes |  |
| `accept_token` | string | Yes |  |
| `accept_url` | object | Yes |  |
| `delivery` | string | Yes |  |

### InviteOut

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes |  |
| `tenant_id` | string | Yes |  |
| `email` | string | Yes |  |
| `role` | string | Yes |  |
| `capability` | string | No |  |
| `status` | string | Yes |  |
| `invited_by` | string | Yes |  |
| `created_at` | string | Yes |  |
| `expires_at` | string | Yes |  |
| `email_sent` | boolean | Yes |  |

### InvokeRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_uri` | string | Yes |  |
| `kg_name` | string | Yes |  |

### InvokeResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_uri` | string | Yes |  |
| `function` | string | Yes |  |
| `output` | object | Yes |  |
| `discovered_entities` | array | No |  |
| `duration_ms` | number | Yes |  |

### JobCategory

### JobErrorItem

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `provider` | object | No |  |
| `kind` | string | No |  |
| `message` | string | Yes |  |
| `count` | integer | No |  |

### JobProgress

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `total` | integer | No |  |
| `processed` | integer | No |  |
| `filled` | integer | No |  |
| `verified` | integer | No |  |
| `conflicts` | integer | No |  |
| `skipped` | integer | No |  |
| `no_match` | integer | No |  |
| `cache_hits` | integer | No |  |
| `phase` | string | No |  |

### JobStageTrace

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | Yes |  |
| `tenant_id` | string | Yes |  |
| `kg_name` | string | Yes |  |
| `category` | object | No |  |
| `status` | object | No |  |
| `source` | string | No |  |
| `projects` | array | No |  |
| `summary` | object | No |  |
| `recorded_at` | string | No |  |

### JobStatus

### JobSummary

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes |  |
| `tenant_id` | string | Yes |  |
| `kg_name` | string | Yes |  |
| `type_name` | string | Yes |  |
| `attributes` | array | Yes |  |
| `tier` | #/components/schemas/EnrichmentTier | Yes |  |
| `status` | #/components/schemas/JobStatus | Yes |  |
| `progress` | #/components/schemas/JobProgress | Yes |  |
| `created_at` | string | Yes |  |
| `started_at` | object | No |  |
| `completed_at` | object | No |  |
| `conflict_policy` | #/components/schemas/ConflictPolicy | Yes |  |
| `confidence_min` | number | No |  |
| `error` | object | No |  |
| `category` | #/components/schemas/JobCategory | No |  |
| `trigger` | #/components/schemas/JobTrigger | No |  |
| `last_run` | object | No |  |
| `next_run` | object | No |  |
| `cost` | object | No |  |
| `cost_note` | object | No |  |
| `result_count` | object | No |  |
| `platforms` | object | No |  |
| `progress_pct` | integer | No |  |
| `coverage` | object | No |  |
| `thread_id` | object | No |  |

### JobTrigger

### KGActionRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `kg_name` | string | Yes |  |

### KGCreate

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `description` | string | No |  |

### KGInfo

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `description` | string | No |  |
| `triple_count` | integer | No |  |
| `entity_count` | integer | No |  |
| `edge_count` | integer | No |  |
| `status` | string | No |  |
| `stats_updated_at` | object | No |  |
| `ai_description` | string | No |  |

### KeyJoin

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `key_attribute` | string | Yes | The snake_case attribute name to join on (the attribute the key column maps to). Existing entities of the row's type carrying this attribute equal to the row's key value are merged onto. |
| `mint_unmatched` | boolean | No | When a row's key value matches no existing entity: True (default) mints a new node; False skips the row and reports it unmatched (never silently dropped). |

### ManifestItem

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ref` | string | No |  |
| `status` | string | No |  |
| `retries` | integer | No |  |
| `reason` | object | No |  |
| `spend_usd` | number | No |  |

### MemberOut

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `subject` | string | Yes |  |
| `role` | string | Yes |  |
| `capability` | string | No |  |
| `joined_at` | string | Yes |  |
| `email` | object | No |  |
| `name` | object | No |  |

### MyInviteOut

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes |  |
| `tenant_id` | string | Yes |  |
| `workspace_label` | string | Yes |  |
| `email` | string | Yes |  |
| `role` | string | Yes |  |
| `capability` | string | No |  |
| `created_at` | string | Yes |  |
| `expires_at` | string | Yes |  |

### NLQuery

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `question` | string | Yes |  |
| `kg_name` | object | No | Query a specific knowledge graph |
| `model` | object | No | Override the query generation model (OpenRouter model ID) |
| `exclude_questions` | array | No | Questions to exclude from example bank retrieval (anti-cheat for evals) |

### NLResult

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `answer` | string | Yes |  |
| `sparql` | string | Yes |  |
| `explanation` | string | Yes |  |
| `ontology` | string | No | Ontology summary text passed to the LLM for SPARQL generation |
| `narrative_answer` | string | No | Natural-language summary of the result set, generated by a fast LLM |
| `functions_invoked` | array | No |  |
| `timing` | object | No | Stage latencies in ms and metadata |
| `citations` | array | No | Per-fact verdict/confidence/recency for the facts the answer relies on |
| `coverage_caveat` | string | No | Honest coverage caveat: 'answered from N of M sources; K facts stale' |
| `run_id` | string | No | Answer-run id for operator Job Trace (P7/A7 + P0/A9). Look up via GET /operator/jobs/{run_id}/trace. Empty when tracking is unavailable. |
| `token_usage` | array | No | Per-LLM-call token usage events for this /ask: stage, attempt, model, prompt_tokens, completion_tokens, total_tokens, provider. Empty when no LLM usage was recorded. |
| `query_confidence` | string | No | Plan confidence after constraint coverage: high | medium | low. Empty when not assessed. |
| `query_confidence_reason` | string | No | Short reason for query_confidence (debug / CLI -d). |
| `clarification_prompt` | string | No | When confidence is low / fail-closed, an optional clarification question (which field a filter token should bind to). |

### NormalizationRule

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes |  |
| `kg_name` | string | Yes |  |
| `type_name` | string | Yes |  |
| `predicate` | string | Yes |  |
| `target_kind` | string | Yes |  |
| `rule_type` | string | No |  |
| `params` | object | No |  |
| `confidence` | number | No |  |
| `rationale` | string | No |  |
| `sample_values` | array | No |  |
| `status` | string | No |  |
| `created_at` | string | No |  |
| `applied_at` | object | No |  |

### OkResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ok` | boolean | No |  |

### OntologyChangelogEntry

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entry_uri` | string | Yes |  |
| `action` | string | Yes |  |
| `subject` | string | Yes | Target graph URI for commit_ontology writes; type/shape URI for global governance entries |
| `timestamp` | string | Yes |  |
| `tenant_id` | object | No |  |
| `actor` | object | No |  |
| `message` | object | No |  |
| `version_before` | object | No |  |
| `version_after` | object | No |  |
| `revision` | object | No |  |
| `changes` | array | No |  |

### OntologyChangelogResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tenant_id` | string | Yes |  |
| `graph_uri` | string | Yes |  |
| `count` | integer | Yes |  |
| `offset` | integer | Yes |  |
| `limit` | integer | Yes |  |
| `entries` | array | No |  |

### OntologyDiffResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tenant_id` | string | Yes |  |
| `from_ref` | string | Yes |  |
| `to_ref` | string | Yes |  |
| `from_graph_uri` | string | Yes |  |
| `to_graph_uri` | string | Yes |  |
| `changes` | array | No |  |
| `count` | integer | No |  |
| `compat_class` | object | No | Overall CompatClass from classify_diff (when computed) |
| `requires_major` | boolean | No |  |
| `summary` | array | No |  |

### OntologyExtensions

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `types` | array | No |  |

### OntologyHistoryGroup

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes |  |
| `start` | string | Yes |  |
| `end` | string | Yes |  |
| `count` | integer | Yes |  |
| `actor` | object | No |  |
| `message` | object | No |  |
| `sample_actions` | array | No |  |
| `change_summary_counts` | object | No |  |
| `entries` | array | No | Member entries newest → oldest; expand in the UI |

### OntologyHistoryResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tenant_id` | string | Yes |  |
| `graph_uri` | string | Yes |  |
| `grouped` | boolean | No |  |
| `count` | integer | No |  |
| `offset` | integer | No |  |
| `limit` | integer | No |  |
| `workspace_revision` | integer | No |  |
| `groups` | array | No |  |
| `entries` | array | No | Flat changelog when grouped=false; empty when grouped |

### PortfolioRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `investor_name` | string | Yes |  |

### PortfolioResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `portfolio_count` | integer | Yes |  |
| `companies` | array | Yes |  |
| `total_invested_usd` | object | Yes |  |

### PromptBlockResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | Yes |  |
| `skill_count` | integer | Yes |  |
| `chars` | integer | Yes |  |

### ProviderLog

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `provider` | string | Yes |  |
| `status` | string | No |  |
| `attempts` | integer | No |  |
| `matches` | integer | No |  |
| `no_match` | integer | No |  |
| `errors` | integer | No |  |
| `timeouts` | integer | No |  |
| `cache_hits` | integer | No |  |
| `last_error` | object | No |  |
| `requests` | array | No |  |

### PurgeJobsResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `deleted` | integer | Yes |  |

### ReindexAccepted

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | string | No |  |
| `kg_name` | string | Yes |  |
| `schedule_id` | string | Yes |  |
| `mode` | string | Yes |  |

### RejectedSlot

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `failed_test` | string | No | which test failed: existence, identity, or universality |
| `why` | object | No |  |

### RejectedValue

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_id` | string | Yes |  |
| `attribute` | string | Yes |  |
| `value` | string | Yes |  |
| `expected_datatype` | string | Yes |  |
| `reason` | string | Yes |  |

### RelationshipUsage

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `target_type` | object | No |  |
| `count` | integer | Yes |  |

### ResolutionResult

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `applied` | array | No |  |
| `proposals` | array | No |  |
| `summary` | string | No |  |
| `dry_run` | boolean | No | True when the caller requested plan-only mode: nothing was written and every change is surfaced under `proposals`. |

### ResolveRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ask` | string | Yes | Natural-language ontology-evolution request |
| `knowledge_graph` | object | No | Optional KG scope hint |
| `dry_run` | boolean | No | Plan-only mode. When false (default, the MCP/agent path) the route auto-applies the resolver's high-confidence changes and returns the rest as proposals. When true (the interactive Explorer path) nothing is written: every change — what would have auto-applied plus the proposals — is returned under `proposals`, `applied` is empty. |

### ResolvedChange

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `kind` | string | Yes |  |
| `subject_type` | string | Yes | Resolved type the change attaches to (existing name, or a proposed new one) |
| `name` | string | Yes | Resolved attribute name (attribute) or predicate (relationship), normalized |
| `datatype_or_target` | string | Yes | For an attribute: the primitive datatype (string/integer/float/boolean/datetime/uri). For a relationship: the target type name (its range) the predicate points at. |
| `action` | string | Yes |  |
| `confidence` | number | Yes |  |
| `reason` | string | No | One-line human-readable rationale for the action/gate decision |

### RowResult

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_uri` | string | Yes |  |
| `attribute` | string | Yes |  |
| `existing_value` | object | No |  |
| `verdict` | object | No |  |
| `action` | string | Yes |  |
| `existing_source_url` | object | No |  |
| `existing_verified_at` | object | No |  |

### RunCoverage

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `total` | integer | No |  |
| `completed` | integer | No |  |
| `dropped` | integer | No |  |
| `pending` | integer | No |  |
| `complete` | boolean | No |  |
| `summary` | string | No |  |

### RunManifest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `run_id` | string | Yes |  |
| `stage` | string | No |  |
| `state` | #/components/schemas/RunState | No |  |
| `total` | integer | No |  |
| `completed` | integer | No |  |
| `dropped` | integer | No |  |
| `retries` | integer | No |  |
| `spend_usd` | number | No |  |
| `spend_ceiling_usd` | object | No |  |
| `halt_reason_kind` | #/components/schemas/HaltReasonKind | No |  |
| `halt_reason` | object | No |  |
| `started_at` | object | No |  |
| `ended_at` | object | No |  |
| `items` | array | No |  |

### RunState

### SECFilingRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cik` | string | Yes |  |

### SECFilingResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `latest_filing_date` | object | Yes |  |
| `latest_filing_type` | object | Yes |  |
| `days_since_last_filing` | object | Yes |  |
| `source_url` | string | Yes |  |

### SPARQLQuery

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | SPARQL 1.1 query string |

### SPARQLResult

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `bindings` | array | No |  |
| `vars` | array | No |  |

### SPARQLUpdate

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `update` | string | Yes | SPARQL 1.1 Update string |

### Schedule

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes |  |
| `tenant_id` | string | Yes |  |
| `kg_name` | string | Yes |  |
| `category` | #/components/schemas/JobCategory | Yes |  |
| `action` | string | Yes |  |
| `params` | object | No |  |
| `cron` | object | No |  |
| `interval_seconds` | object | No |  |
| `enabled` | boolean | No |  |
| `next_run` | object | No |  |
| `last_run` | object | No |  |
| `created_at` | string | Yes |  |

### ScheduleCreateRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `kg_name` | string | Yes |  |
| `category` | #/components/schemas/JobCategory | Yes |  |
| `action` | string | Yes |  |
| `params` | object | No |  |
| `cron` | object | No |  |
| `interval_seconds` | object | No |  |
| `enabled` | boolean | No |  |

### ScheduleUpdateRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `kg_name` | object | No |  |
| `category` | object | No |  |
| `action` | object | No |  |
| `params` | object | No |  |
| `cron` | object | No |  |
| `interval_seconds` | object | No |  |
| `enabled` | object | No |  |

### SchemaViolation

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `template` | string | Yes | Which of the structural failure templates fired |
| `location` | string | No | Where in the proposed schema (entity/column/edge) |
| `evidence` | string | No | Profile evidence the reviewer cited |
| `severity` | string | No | Reviewer-assigned severity |

### SearchRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | Free-text query. Must contain at least one non-whitespace character (blank queries are a 400). |
| `kg_name` | object | No | Narrow the search to one knowledge graph. Omit/null/empty = every KG in the tenant. An unknown KG yields empty results (see module docs), never an error. |
| `type` | object | No | Filter hits to entities whose denormalized display type equals this value (e.g. 'Speech'). NOTE: the type is denormalized onto chunks at write time and repaired hourly by the reconciler, so a recent type change may match stale values (ONTA-178 docs). |
| `entity_uris` | object | No | Strict allowlist of entity URIs that may participate in ranking. Omit/null = no URI filter; empty list = zero hits (200); more than 500 unique URIs after blank-strip + dedupe = 400. Combined with kg_name/type via AND; applied inside ranking legs before LIMIT. |
| `top_k` | integer | No | Maximum entities to return. Clamped server-side to [1, 50]; the effective value is echoed in the response. |

### SearchResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `hits` | array | No |  |
| `count` | integer | No |  |
| `degraded` | boolean | No |  |
| `top_k` | integer | No |  |

### SemanticHit

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_uri` | string | Yes |  |
| `attrs` | object | No |  |
| `snippet` | string | No |  |
| `attr` | string | No |  |
| `score` | number | No |  |

### SkillDetail

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `slug` | string | Yes |  |
| `type_name` | string | Yes |  |
| `title` | string | No |  |
| `summary` | string | No |  |
| `layer` | string | Yes |  |
| `enabled` | boolean | No |  |
| `version` | integer | No |  |
| `body_chars` | integer | No |  |
| `editable` | boolean | No |  |
| `body` | string | No |  |
| `metadata` | object | No |  |

### SkillSummary

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `slug` | string | Yes |  |
| `type_name` | string | Yes |  |
| `title` | string | No |  |
| `summary` | string | No |  |
| `layer` | string | Yes |  |
| `enabled` | boolean | No |  |
| `version` | integer | No |  |
| `body_chars` | integer | No |  |
| `editable` | boolean | No |  |

### StageAction

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `detail` | object | No |  |
| `at` | object | No |  |
| `meta` | object | No |  |

### StageProjectId

### StageProjectTrace

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `project_id` | #/components/schemas/StageProjectId | Yes |  |
| `name` | string | Yes |  |
| `status` | #/components/schemas/StageStatus | No |  |
| `contract_goal` | object | No |  |
| `contract_consumes` | object | No |  |
| `contract_emits` | object | No |  |
| `started_at` | object | No |  |
| `completed_at` | object | No |  |
| `duration_ms` | object | No |  |
| `input` | object | No |  |
| `actions` | array | No |  |
| `output` | object | No |  |
| `error` | object | No |  |
| `reconstructed` | boolean | No |  |

### StageStatus

### SubtypeAdd

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `subtype` | string | Yes | Name of the child type |

### TenantCreate

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | object | No | Tenant slug (lowercase, 3–40 chars). Auto-minted if omitted. |
| `label` | object | No | Human-readable label. Defaults to "Untitled workspace N". |

### TenantOut

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes |  |
| `label` | string | Yes |  |
| `role` | string | No |  |
| `capability` | string | No |  |

### TenantRename

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `label` | string | Yes | New human-readable label. |

### TestApiSourceRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `slug` | object | No |  |
| `spec` | object | No |  |
| `sample_params` | object | No |  |

### TestApiSourceResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ok` | boolean | Yes |  |
| `rows` | array | No |  |
| `error` | object | No |  |

### TokenAccept

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `token` | string | Yes | The one-time accept token. |

### Triple

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `subject` | string | Yes | RDF subject URI or blank node |
| `predicate` | string | Yes | RDF predicate URI |
| `object` | string | Yes | RDF object (URI, literal, or blank node) |

### TripleBatch

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `inserted` | integer | No |  |
| `deleted` | integer | No |  |

### TripleCreate

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `triples` | array | Yes |  |

### TripleDelete

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `triples` | array | Yes |  |

### TypeCount

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `entity_count` | integer | Yes |  |
| `spatially_indexed` | boolean | No |  |
| `temporally_indexed` | boolean | No |  |

### TypeCreate

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `description` | string | No |  |
| `parent_type` | object | No | Parent type name for subtype relationship |
| `attributes` | array | No |  |

### TypeExtension

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type_name` | string | Yes |  |
| `promoted_from_attribute` | object | No | the schema attribute this dependent-entity type was promoted from (None = pre-existing type) |
| `core_slots` | array | No | constitutive slots — more than 3 fails validation (ADR 0003 boundedness cap) |
| `rejected` | array | No | considered-but-rejected slot candidates, each with the failed test |
| `confidence` | object | No | optional model confidence in this extension (when emitted) |
| `held_for_review` | boolean | No | True when this extension needs user confirmation before ingest: every promotion is held, as is any extension with confidence < 0.7 |

### TypeResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `description` | string | No |  |
| `parent_type` | object | No |  |
| `attributes` | array | No |  |
| `subtypes` | array | No |  |
| `functions` | array | No |  |

### TypeUsage

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `description` | string | No |  |
| `parent_type` | object | No |  |
| `entity_count` | integer | Yes |  |
| `attributes` | array | No |  |
| `relationships` | array | No |  |
| `samples` | array | No |  |

### UpdateApiSourceRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `spec` | object | No |  |
| `secrets` | object | No |  |
| `enabled` | object | No |  |

### UpdateSkillRequest

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `body` | object | No |  |
| `title` | object | No |  |
| `summary` | object | No |  |
| `enabled` | object | No |  |
| `metadata` | object | No |  |

### UpgradePreviewResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `from_version` | object | No |  |
| `to_version` | object | No |  |
| `base_layer` | string | No |  |
| `changes` | array | No |  |
| `collisions` | array | No |  |
| `deprecated_used` | array | No |  |
| `summary` | array | No |  |
| `from_fingerprint` | object | No |  |
| `to_fingerprint` | object | No |  |

### UsageMetricBlock

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `total` | #/components/schemas/UsageSeries | Yes |  |
| `by_kg` | array | No |  |
| `by_key` | array | No |  |

### UsageReport

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `days` | array | Yes |  |
| `requests` | #/components/schemas/UsageMetricBlock | Yes |  |
| `latency_ms` | #/components/schemas/UsageMetricBlock | Yes |  |
| `cost_usd` | #/components/schemas/UsageMetricBlock | Yes |  |
| `totals` | #/components/schemas/UsageTotals | Yes |  |
| `prev_totals` | #/components/schemas/UsageTotals | Yes |  |
| `route_class_requests` | object | No |  |
| `has_queried` | boolean | No |  |
| `month_requests` | integer | No |  |

### UsageSeries

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `label` | string | Yes |  |
| `values` | array | Yes |  |
| `total` | number | Yes |  |

### UsageTotals

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `requests` | integer | No |  |
| `errors` | integer | No |  |
| `avg_latency_ms` | number | No |  |
| `cost_usd` | number | No |  |

### ValidationError

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `loc` | array | Yes |  |
| `msg` | string | Yes |  |
| `type` | string | Yes |  |
| `input` | object | No |  |
| `ctx` | object | No |  |

### ValidationIssue

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `message` | string | Yes |  |

### Verdict

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `value` | string | Yes |  |
| `confidence` | number | Yes |  |
| `source` | string | Yes |  |
| `source_url` | object | No |  |
| `reasoning` | object | No |  |
| `raw_confidence` | object | No |  |
| `retrieved_at` | object | No |  |
| `source_published_at` | object | No |  |
| `grounding_score` | object | No |  |
| `extraction_method` | object | No |  |
| `calibration_method` | object | No |  |
| `authority` | object | No |  |

### WorkspaceOntologyLayer

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `layer` | string | Yes |  |
| `graph_uri` | string | Yes |  |
| `type_count` | integer | No |  |
| `available` | boolean | No |  |

### WorkspaceOntologyResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tenant_id` | string | No |  |
| `entitled` | boolean | No |  |
| `layers` | array | No |  |
| `types` | array | No |  |

### WorkspaceOntologyType

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `layer` | string | Yes | Winning layer under shadowing: "tenant", "enhanced", or "public" |
| `description` | object | No |  |
| `parent_type` | object | No | Bare parent type NAME from rdfs:subClassOf, not a URI |
| `subtypes` | array | No |  |
| `attributes` | array | No |  |
| `relationships` | array | No |  |
| `sources` | array | No |  |
| `functions` | array | No |  |
| `skills` | array | No |  |

### WorkspaceTypeCount

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes |  |
| `entity_count` | integer | Yes | Sum of entity counts for this type across every KG that has any |
| `by_kg` | object | No | Per-KG breakdown (kg_name → count); omits KGs with zero for this type |

### WorkspaceTypeCountsResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tenant_id` | string | No |  |
| `types` | array | No |  |
| `kg_names` | array | No | KG names that contributed a stats row (may include empty KGs) |

### infona_client__api__routes__api_sources__ValidateResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `valid` | boolean | Yes |  |
| `errors` | array | No |  |

### infona_client__api__routes__skills__ValidateResponse

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `valid` | boolean | Yes |  |
| `errors` | array | No |  |
