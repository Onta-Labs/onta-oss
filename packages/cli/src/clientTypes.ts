/** Shared Client / RawApi TypeScript types.

Re-exported from ``client.ts`` so existing ``from "./client.js"`` imports
keep working. No runtime behavior.
*/
export interface ClientOptions {
  apiKey?: string;
  baseUrl?: string;
  tenant?: string;
}

export interface IngestOptions {
  kg?: string;
  contentType?: "text" | "csv" | "json" | string;
  /** Treat `pathOrText` as a FILE PATH, not as raw text. When set, a path that
   *  does not resolve to a readable file throws a `InfonaError` instead of
   *  silently POSTing the path string itself as text content (ONTA-253: a
   *  file-intent caller — e.g. the MCP `ingest_csv` tool — must never fabricate
   *  a success by LLM-extracting entities out of a nonexistent filename). The
   *  dual-mode default (`asFile` unset) keeps the CLI's intentional
   *  `ingest <raw text>` path working. */
  asFile?: boolean;
  /** Treat `pathOrText` as RAW TEXT even if the string happens to resolve to an
   *  existing file path. Use from text-intent callers (e.g. MCP `ingest_text`)
   *  so a note that looks like a path is never silently re-read from disk.
   *  Mutually exclusive with `asFile` — when both are set, `asFile` wins. */
  asText?: boolean;
  /** Rows per batch for CSV ingest. Default 200. Larger = fewer round-trips
   *  but higher per-request memory; 200 is a good balance for typical KGs. */
  batchSize?: number;
  /** Max number of batches in flight at once. Default 4. Higher saturates
   *  the backend faster but risks 429s on large ingests. */
  concurrency?: number;
  /** Called after each batch completes during CSV ingest, in batch order.
   *  Use for progress UI. Not invoked for text/json ingest. */
  onProgress?: (progress: IngestProgress) => void;
  /** CSV only. Join-by-exact-key ingest mode (ONTA-250): match each row to an
   *  EXISTING entity by an exact key attribute and merge the row's attributes
   *  ONTO that node instead of minting a duplicate. `keyAttribute` is the
   *  snake_case attribute the key column maps to (e.g. an id column); when a
   *  row's key matches no existing entity it mints a new node unless
   *  `mintUnmatched` is false (then it is skipped and reported). A thin
   *  pass-through of the `/ingest/csv/rows` route's `key_join` field — the
   *  server does the matching. General over any (type, key). */
  keyJoin?: { keyAttribute: string; mintUnmatched?: boolean };
  /** CSV only. Called once after schema inference and BEFORE any rows are
   *  written, with the inferred mapping. Return the (possibly edited/approved)
   *  mapping to ingest, or `null` to cancel without writing anything. When
   *  omitted the inferred mapping is applied as-is (non-interactive). This is
   *  the same confirm/override gate the Explorer surfaces in its review step. */
  onSchemaInferred?: (
    mapping: Record<string, unknown>,
    info: { totalRows: number; rowsProfiled: number },
  ) => Promise<Record<string, unknown> | null>;
  /** CSV only. Deterministic ingest: skip LLM schema inference entirely and
   *  map columns VERBATIM under this entity type — the first column becomes
   *  the entity name (`type_id`), every other column a literal attribute named
   *  exactly like its header. The predictable counterpart to the inferred flow
   *  for when column names are load-bearing (e.g. an attribute another rail
   *  binds on, like an external series/id column an enrichment source joins
   *  against). `onSchemaInferred` is not called in this mode — there is
   *  nothing inferred to review. */
  typeName?: string;
}

export interface IngestProgress {
  rowsProcessed: number;
  totalRows: number;
  entitiesResolved: number;
  triplesInserted: number;
}

export interface AskOptions {
  kg?: string;
  model?: string;
}
export interface ResolvedChange {
  kind: "attribute" | "relationship";
  subject_type: string;
  name: string;
  datatype_or_target: string;
  action: "reuse" | "extend" | "create";
  confidence: number;
  reason: string;
}

export interface OntologyResolveResult {
  applied: ResolvedChange[];
  proposals: ResolvedChange[];
  summary: string;
}

export interface OntologyApplyResult {
  applied: ResolvedChange;
  operations: number;
  summary: string;
}

/** One change's outcome inside an {@link OntologyApplyBatchResult}. */
export interface OntologyApplyChangeResult {
  change: ResolvedChange;
  /** false ⇒ this change raised; see `error`. The rest of the batch still ran. */
  ok: boolean;
  operations: number;
  error: string;
}

/** Response of {@link Client.ontologyApplyBatch} — one entry per submitted change. */
export interface OntologyApplyBatchResult {
  results: OntologyApplyChangeResult[];
  applied_count: number;
  failed_count: number;
  operations: number;
  summary: string;
}

export interface TypeCount {
  name: string;
  entity_count: number;
  /** Instances carry geometry, so the type is in the spatio-temporal index.
   *  The backend has always returned this; the type just never declared it. */
  spatially_indexed?: boolean;
  /** Instances carry validity (an explicit bound, or a start+end pair). */
  temporally_indexed?: boolean;
}

export interface AttributeUsage {
  name: string;
  datatype: string;
  count: number;
}

export interface RelationshipUsage {
  name: string;
  target_type: string | null;
  count: number;
}

export interface EntitySample {
  uri: string;
  label: string;
}

export interface TypeUsage {
  name: string;
  description: string;
  parent_type: string | null;
  entity_count: number;
  attributes: AttributeUsage[];
  relationships: RelationshipUsage[];
  samples: EntitySample[];
}

export interface AttributeSummary {
  name: string;
  predicate_uri: string;
  datatype: string;
  count: number;
  coverage_pct: number;
}

export interface RelationshipSummary {
  name: string;
  predicate_uri: string;
  target_type: string | null;
  count: number;
  coverage_pct: number;
  avg_degree: number;
}

export interface TypeSummary {
  name: string;
  description: string;
  parent_type: string | null;
  entity_count: number;
  attributes: AttributeSummary[];
  relationships: RelationshipSummary[];
  /** See {@link TypeCount.spatially_indexed} (also returned here). */
  spatially_indexed?: boolean;
  /** See {@link TypeCount.temporally_indexed} (also returned here). */
  temporally_indexed?: boolean;
}

/** An {@link AttributeSummary} inside a {@link KgSchema}, annotated with whether
 *  any instance in this KG actually carries it. A declared attribute with no
 *  data is returned with `populated: false` rather than dropped: a count of 0
 *  is indistinguishable from a transient backend throttle, so dropping makes
 *  slots flicker across identical calls. */
export interface SchemaAttribute extends AttributeSummary {
  populated: boolean;
}

/** A {@link RelationshipSummary} inside a {@link KgSchema}. See
 *  {@link SchemaAttribute} for the `populated` semantics. */
export interface SchemaRelationship extends RelationshipSummary {
  populated: boolean;
}

/** One type inside a {@link KgSchema}: the {@link TypeSummary} shape plus the
 *  population annotations that make declared-but-empty schema visible instead
 *  of hidden. */
export interface KgSchemaType extends TypeSummary {
  attributes: SchemaAttribute[];
  relationships: SchemaRelationship[];
  /** This KG has at least one instance of the type. */
  populated: boolean;
  /** Declared in the ontology but with no instances in THIS KG. */
  declared_only: boolean;
  /** How many slots the `minCoverage` floor withheld (0 when unfiltered). */
  attributes_withheld: number;
  relationships_withheld: number;
}

/** Response of {@link Client.kgSchema}: the whole KG's population-aware schema. */
export interface KgSchema {
  kg: string;
  /** Types sorted by `entity_count` descending, capped by `limit`. */
  types: KgSchemaType[];
  total_types: number;
  truncated: boolean;
  /** Names of the types the `limit` cap withheld, so a capped type is still
   *  known to EXIST and can be fetched with `types: [...]`. */
  omitted_type_names: string[];
  /** Populated ONLY when a `types` filter matched nothing: every type name the
   *  graph does have, so a typo reads as "you meant one of these" rather than
   *  "that type does not exist". Empty otherwise. */
  available_type_names: string[];
  /** `"precomputed"` (materialized stats) or `"live_scan"` (legacy KG). */
  stats_source: string;
  /** How `coverage_pct` is computed, including the multi-typed-entity caveat. */
  coverage_note: string;
}

export type EnrichmentTier = "auto" | "lite" | "base" | "core" | "pro";
export type JobStatus =
  | "queued"
  | "running"
  | "review"
  | "applied"
  | "cancelled"
  | "failed";

/** The job statuses that mean "stopped doing work, will not advance on its own"
 *  — the mirror of the backend's `JobStatus.is_terminal()`. `queued`/`running`
 *  are the only in-flight states; everything else is settled (`review` is a
 *  finished run parked for human conflict decisions). Kept in lockstep with the
 *  server so a `waitForJob` caller and the wait route agree on when to stop. */
export const TERMINAL_JOB_STATUSES: readonly JobStatus[] = [
  "review",
  "applied",
  "cancelled",
  "failed",
] as const;

/** True when a job status is terminal (see {@link TERMINAL_JOB_STATUSES}). */
export function isTerminalJobStatus(status: JobStatus): boolean {
  return (TERMINAL_JOB_STATUSES as readonly string[]).includes(status);
}
/** The kind of work a tracked job performs — the unified `/jobs` feed spans all
 *  categories. Existing enrichment jobs default to `enrichment` server-side.
 *  `discovery` is a web-discovery ingest (the `web_ingest` capability): it
 *  CREATES a new record set from the web rather than filling/merging an existing
 *  one. `ingest` is file CSV/JSON/text ingest (ONTA-386): an A1-like entry that
 *  maps/extracts, places against the ontology, and writes via insert_facts.
 *  `answer` is a read-only NL ask / agent Q&A turn (P7 Answer / A7 + P0/A9
 *  stage_trace; ONTA-389) — not every chat message, only meaningful completions. */
export type JobCategory =
  | "dedupe"
  | "enrichment"
  | "reconciliation"
  | "discovery"
  | "ingest"
  | "answer";
export type JobTrigger = "manual" | "scheduled" | "webhook";
export type ConflictPolicy = "skip" | "verify" | "overwrite" | "stage";
export type RowAction =
  | "filled"
  | "verified"
  | "conflict"
  | "skipped"
  | "no_match";
export type ReviewDecision = "accept" | "reject" | "skip";

export interface EnrichRequest {
  type_name: string;
  attributes: string[];
  tier?: EnrichmentTier;
  kg_name: string;
  conflict_policy?: ConflictPolicy;
  confidence_min?: number;
  limit?: number;
  /** Chat provenance: the conversation/thread id this job is kicked off from, so
   *  the created job is traceable back to its conversation. Omit for non-chat
   *  (direct API / CLI / scheduled) callers. */
  thread_id?: string;
}

export interface EnrichJobCreate {
  /** Null when the backend needs the client to clarify the source before a job
   *  is created (see {@link needs_clarification}). */
  job_id: string | null;
  /** Either a real {@link JobStatus} (e.g. "queued") or the routing sentinel
   *  "needs_clarification" when the backend wants the client to pick a tier. */
  status: "queued" | "needs_clarification" | JobStatus | string;
  /** The concrete tier a job was actually created at — e.g. "lite" (Wikidata,
   *  free) or "core" (live web search) — once the backend's "auto" routing
   *  resolves. Null/absent when {@link needs_clarification}. */
  resolved_tier?: EnrichmentTier | null;
  /** Short human reason for the routing decision, e.g. "Wikidata is thin for
   *  these attributes — using web search". */
  routing_note?: string | null;
  /** True when the backend could not confidently route "auto" and wants the
   *  client to choose among {@link candidates}; no job was created. */
  needs_clarification?: boolean;
  /** The tiers to offer the user when {@link needs_clarification}, e.g.
   *  ["lite","core"]. */
  candidates?: string[] | null;
  estimated_cost_usd?: number;
  total_entities?: number;
}

export interface Verdict {
  value: string;
  confidence: number;
  source: string;
  source_url?: string | null;
  reasoning?: string | null;
}

export interface JobProgress {
  total: number;
  processed: number;
  filled: number;
  verified: number;
  conflicts: number;
  skipped: number;
  no_match: number;
  cache_hits: number;
  /** Coarse WHAT-is-happening-now label for a running job (ONTA-238): discovery
   *  sets it through the run ("searching" → "ingesting" → "done" / "failed");
   *  enrichment/dedupe leave it "". Optional for back-compat with older payloads
   *  that predate the field. The MCP `get_job` tool surfaces it. */
  phase?: string;
}

export interface RowResult {
  entity_uri: string;
  attribute: string;
  existing_value: string | null;
  verdict: Verdict | null;
  action: RowAction;
}

/** One day-aligned usage line: a breakdown member or the total. `total` is the
 *  window aggregate (sum for requests/cost; weighted average for latency). */
export interface UsageSeries {
  label: string;
  values: number[];
  total: number;
}

/** A usage metric's total line plus its per-KG / per-API-key breakdowns. */
export interface UsageMetricBlock {
  total: UsageSeries;
  by_kg: UsageSeries[];
  by_key: UsageSeries[];
}

export interface UsageTotals {
  requests: number;
  errors: number;
  avg_latency_ms: number;
  cost_usd: number;
}

/** `GET /graphs/{tenant}/usage` — the dashboard usage panel's one payload:
 *  day-aligned series for requests / latency / cost, current + previous
 *  window totals (for deltas), route-class request counts, and the
 *  month-to-date request count (quota "used"). */
export interface UsageReport {
  days: string[];
  requests: UsageMetricBlock;
  latency_ms: UsageMetricBlock;
  cost_usd: UsageMetricBlock;
  totals: UsageTotals;
  prev_totals: UsageTotals;
  route_class_requests: Record<string, number>;
  has_queried: boolean;
  month_requests: number;
}

export interface JobSummary {
  id: string;
  tenant_id: string;
  kg_name: string;
  type_name: string;
  attributes: string[];
  tier: EnrichmentTier;
  status: JobStatus;
  progress: JobProgress;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  conflict_policy: ConflictPolicy;
  confidence_min: number;
  error?: string | null;
  // COG-101 unified-jobs fields. Present on the GET /jobs summary across all
  // categories; optional because the same shape also models a plain enrichment
  // job (which predates these fields and defaults them server-side).
  category?: JobCategory;
  trigger?: JobTrigger;
  last_run?: string | null;
  next_run?: string | null;
  cost?: number | null;
  cost_note?: string | null;
  /** Discovery/web-ingest summary fields. `result_count` is the headline "how
   *  many records were found" number; `platforms` are the web sources/providers
   *  consulted during the run. Both null/absent for non-discovery jobs. */
  result_count?: number | null;
  platforms?: string[] | null;
  /** Derived 0-100 completion percentage from progress.processed/total. */
  progress_pct?: number;
  /** Chat provenance: the conversation/thread id this job was created from, when
   *  it was kicked off from the Ask-AI chat (null/absent for non-chat jobs). */
  thread_id?: string | null;
}

export interface EnrichJob extends JobSummary {
  results?: RowResult[];
  limit?: number | null;
}

export interface ConflictReview {
  entity_uri: string;
  attribute: string;
  existing_value: string;
  proposed: Verdict;
  decision?: ReviewDecision | null;
}

