/** Agent, schedule, explore, search, and grep SDK types.

Re-exported from ``client.ts`` so existing imports keep working.
*/
import type { JobCategory } from "./clientTypes.js";

// --- Recurring schedules (COG-135) ------------------------------------------- #

/** Actions a tenant may CREATE or UPDATE through the schedules CRUD routes —
 *  mirrors the Ask-AI action endpoints: find-merge-duplicates (dedupe), enrich
 *  (enrichment), suggest-relationships (reconciliation), plus `notify` (ONTA-235):
 *  a standing-alert / weekly-refresh that snapshots a watched value each fire,
 *  diffs it against the previous fire, and delivers a change payload out through
 *  a delivery sink ONLY when it changed. A schedule's `category` agrees with its
 *  `action`. The backend rejects any other action on create/update with a 422 —
 *  see {@link ScheduleAction} for the system-managed values that can still APPEAR
 *  in list/get responses. */
export type UserSchedulableAction =
  | "find-merge-duplicates"
  | "enrich"
  | "suggest-relationships"
  | "notify";

/** The action a {@link Schedule} fires — the FULL read-side vocabulary, a
 *  superset of {@link UserSchedulableAction}. `semantic-embed-fill` /
 *  `semantic-reconcile` (ONTA-181) are SYSTEM-MANAGED semantic-index
 *  maintenance rows the backend creates internally; they show up in a tenant's
 *  schedule list/get responses, but create/update accept only the
 *  user-schedulable subset (422 otherwise) and PATCHing a system row is a 403.
 *  Exhaustive consumers of `Schedule.action` must handle all five arms. */
export type ScheduleAction =
  | UserSchedulableAction
  | "semantic-embed-fill"
  | "semantic-reconcile";

/** Runtime companion of {@link UserSchedulableAction} (e.g. for building a
 *  create-schedule action picker) — mirrors the backend's
 *  `USER_SCHEDULABLE_ACTIONS` allowlist in `scheduling/models.py`. */
export const USER_SCHEDULABLE_ACTIONS: readonly UserSchedulableAction[] = [
  "find-merge-duplicates",
  "enrich",
  "suggest-relationships",
  "notify",
] as const;

/**
 * A recurring-action schedule for a tenant's KG (COG-135). Recurs on EXACTLY
 * one of `cron` / `interval_seconds` (the backend rejects both/neither). This
 * is the scheduling DATA shape — the firing loop that turns a due schedule into
 * a job is server-side and separate. `params` carries the action-specific job
 * payload (e.g. `type_name`/`attributes`/`tier`/`conflict_policy` for enrich).
 */
export interface Schedule {
  id: string;
  tenant_id: string;
  kg_name: string;
  category: JobCategory;
  action: ScheduleAction;
  params: Record<string, unknown>;
  cron?: string | null;
  interval_seconds?: number | null;
  enabled: boolean;
  next_run?: string | null;
  last_run?: string | null;
  created_at: string;
}

// --- Unified Ask-AI agent (COG-118 / COG-125) -------------------------------- #

/** Inputs to {@link Client.agent} — mirror the `/agent` HTTP body. */
export interface AgentTurnOptions {
  /** The user's natural-language message. Optional when `confirmPlanId` is set
   *  (a confirm turn carries no new message). */
  message?: string;
  /** Context graph the turn operates within. */
  kgName?: string;
  /** Optional active type scope (needed for enrich/clean/dedup planning). */
  typeName?: string;
  /** Optional explicit links to parse for this turn (threaded into the request
   *  `context.urls`). The server routes a URL-bearing turn to enrich existing
   *  entities or discover new ones, then extracts records from these pages. */
  urls?: string[];
  /** Optional conversation/session id for multi-turn continuity. */
  sessionId?: string;
  /** When set, the server CONFIRMS + EXECUTES this previously-proposed plan
   *  (the only mutating path) instead of classifying a new message. */
  confirmPlanId?: string;
  /** Optional HARD per-run spend ceiling (USD) for any enrichment/discovery job
   *  this turn kicks off (ONTA-282/ONTA-378). Threaded into the request body as
   *  `spend_ceiling_usd`; the server stamps it onto the job it creates so the
   *  executor's ceiling override bounds that single run. Omit for the deployment
   *  default (unchanged behavior); a value of 0 means unlimited. */
  spendCeilingUsd?: number;
}

/**
 * The kind-tagged result of one agent turn. The server returns exactly one of:
 *  - `answer`  — a read-only answer (questions; an ontology INSPECT) with SPARQL.
 *  - `clarify` — the agent needs more detail; ask the user `question`.
 *  - `plan`    — a proposed (un-executed) plan with `plan_id` + `steps`; confirm
 *                by calling `agent({ confirmPlanId: plan_id })`.
 *  - `result`  — the outcome of executing a confirmed plan, per-step. A
 *                duplicate confirm of a finished plan returns the SAME result
 *                with `replayed: true` (the plan is never run twice).
 *  - `error`   — e.g. an unknown/expired plan_id on confirm, or a duplicate
 *                confirm that can't replay yet: `code:"plan_already_executing"`
 *                (first confirm still in flight) / `"plan_already_executed"`
 *                (finished with no replayable result).
 * Extra fields vary by kind (answer/sparql/rows; question; plan_id/steps;
 * steps), so this is intentionally open beyond the discriminant.
 */
export interface AgentResult {
  kind: "answer" | "clarify" | "plan" | "result" | "error";
  [key: string]: unknown;
}

// --- New typed shapes (COG-128) ---------------------------------------------- #

/** One row in the Explorer Data table — an entity instance with its attribute
 *  values. `id` is the entity URI; `name` is the display name; the remaining
 *  keys are per-attribute values (all stringly-typed for display). */
export interface TypeRecord {
  id: string;
  name: string;
  [attr: string]: string;
}

/** A page of {@link TypeRecord}s returned by {@link Client.exploreRecords}.
 *  `next_cursor` is the last entity URI of this page; pass it back as `cursor`
 *  to fetch the following page, or `null` when there are no more rows. */
export interface TypeRecordsPage {
  columns: string[];
  rows: TypeRecord[];
  total: number;
  next_cursor: string | null;
}

/** An undirected type→type edge in the Explorer overview graph, weighted by the
 *  number of instance relationships it summarizes. */
export interface TypeEdge {
  source: string;
  target: string;
  weight: number;
}

/** A stored normalization rule (suggested / confirmed / rejected / applied).
 *  Open beyond the documented fields because the rule's `params` shape varies by
 *  `rule_type` (e.g. `strip_emoji`, `list_explode`). */
export interface NormalizationRule {
  id: string;
  kg_name: string;
  type_name: string;
  predicate: string;
  rule_type: string;
  target_kind?: string;
  params?: Record<string, unknown>;
  confidence?: number;
  rationale?: string;
  status: "suggested" | "confirmed" | "rejected" | "applied" | string;
  created_at?: string;
  applied_at?: string | null;
  [key: string]: unknown;
}

// --- API source registry (ONTA-2xx) ------------------------------------------- #

/** The list/summary shape for a registered API source. Secret-free by
 *  construction: only `has_secret` is exposed, never a value. `editable` is true
 *  only for `tenant_custom` entries (global entries are read-only). */
export interface ApiSourceSummary {
  slug: string;
  title: string;
  publisher: string;
  description: string;
  layer: "global_public" | "global_enhanced" | "tenant_custom" | string;
  authority_level: string;
  entity_kinds: string[];
  attributes: string[];
  enabled: boolean;
  editable: boolean;
  has_secret: boolean;
}

/** One structured validation error from the validate route. */
export interface ApiSourceValidationError {
  path: string;
  message: string;
}

/** Response of `POST /api-sources/validate`. */
export interface ApiSourceValidateResult {
  valid: boolean;
  errors: ApiSourceValidationError[];
}

/** Response of `POST /api-sources/test` — the smoke-call result. A secret is
 *  never echoed here; `rows` carry no auth material. */
export interface ApiSourceTestResult {
  ok: boolean;
  rows: Record<string, string>[];
  error?: string | null;
}

/** Create/update body. `spec` is an `ApiSourceSpec` JSON object; `secrets` is a
 *  write-only logical-name→value map (never returned); `enabled` toggles the
 *  row. On update, all fields are optional (e.g. flip `enabled` alone). */
export interface ApiSourceWrite {
  spec?: Record<string, unknown>;
  secrets?: Record<string, string>;
  enabled?: boolean;
}

// --- Semantic instance search (ONTA-178) -------------------------------------- #

/** One entity-grouped hit from the canonical `/search` route: the entity that
 *  matched, small denormalized display fields (`attrs.label`, `attrs.type`, …),
 *  and the best-matching chunk's snippet + source attribute so a UI can show
 *  WHERE the match happened without a follow-up fetch. */
export interface SemanticSearchHit {
  entity_uri: string;
  attrs: Record<string, unknown>;
  snippet: string;
  attr: string;
  score: number;
}

/** Options for {@link Client.search} (`POST /graphs/{tenant}/search`). */
export interface SemanticSearchOptions {
  /** Restrict to one knowledge graph (`kg_name` in the body). */
  kg?: string;
  /** Restrict to one entity type (AND with other filters). */
  type?: string;
  /**
   * Strict entity-URI allowlist (`entity_uris` in the body) — structured
   * pre-filter before hybrid ranking. Omit = unrestricted; `[]` = zero hits;
   * server blanks-strip + dedupes and 400s above 500 unique URIs.
   */
  entityUris?: string[];
  /** Max entities to return (server clamps to 1..50; default 10). */
  topK?: number;
}

/** The `/search` response envelope. `degraded: true` means the query ran
 *  lexical-only (no query embedding was available) — reduced recall that must
 *  be surfaced, never hidden. `top_k` echoes the server-side clamped value
 *  (1..50) actually used. */
export interface SemanticSearchResponse {
  hits: SemanticSearchHit[];
  count: number;
  degraded: boolean;
  top_k: number;
}

// --- Index-free literal grep (ONTA-416) --------------------------------------- #

/** ONE matching triple from the `/grep` route. The unit is a TRIPLE, not an
 *  entity (contrast {@link SemanticSearchHit}): the same entity appears once per
 *  matching attribute, because "which field did this match in?" is the point of
 *  a grep. `value` is the literal (truncated), `snippet` a bounded window
 *  centered on the match, `attr` the predicate's leaf name. `label` / `type` are
 *  empty when the subject carries neither. */
export interface GrepMatch {
  entity_uri: string;
  label: string;
  type: string;
  predicate: string;
  attr: string;
  value: string;
  snippet: string;
}

/** The `/grep` response envelope. `truncated: true` means the scan hit `limit`
 *  and more matches exist (the server over-fetches by one row, so this is
 *  observed, not inferred). `limit` echoes the server-side clamped value
 *  (1..200) actually used. */
export interface GrepResponse {
  matches: GrepMatch[];
  count: number;
  limit: number;
  truncated: boolean;
}

/** Per-call overrides for a RawApi method — extra/override headers and a
 *  custom timeout. A body here is ignored by methods that take an explicit
 *  body argument (they set it themselves). */
export interface RawInit {
  headers?: Record<string, string>;
  timeoutMs?: number;
}

/** Frozen ONTA-553 Wave-1 extract contract (`POST /graphs/{tenant}/ingest/dlt`). */
export type DltSourceKind = "rest_api" | "sql";
export type DltAuthType = "bearer" | "basic" | "api_key" | "none";

export interface DltAuthSpec {
  type?: DltAuthType;
  /** BYOK: `env:VAR` (CLI substitutes locally) or `store:<slug>/<logical>`. */
  secret_ref?: string;
  /** Write-only inline token. Never echoed. */
  token?: string;
  username?: string;
  api_key_header?: string;
}

export interface DltSourceSpec {
  kind: DltSourceKind;
  base_url?: string;
  dsn?: string;
  auth?: DltAuthSpec;
  resources: string[];
  headers?: Record<string, string>;
  limit?: number;
}

export interface DltResourceMap {
  type: string;
  id_field?: string;
  attributes?: string[];
}

export interface DltIngestRequest {
  source: DltSourceSpec;
  map: Record<string, DltResourceMap>;
  kg?: string;
}

export interface ExtractSourceSummary {
  slug: string;
  title: string;
  kind: DltSourceKind;
  enabled: boolean;
  has_secret: boolean;
  resources: string[];
  mapped: string[];
  kg?: string | null;
}

export interface ExtractSourceWrite {
  slug?: string;
  title?: string;
  source?: DltSourceSpec;
  map?: Record<string, DltResourceMap>;
  kg?: string;
  enabled?: boolean;
  secrets?: Record<string, string>;
}
