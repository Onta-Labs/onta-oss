export {
  Client,
  CographError,
  RawApi,
  USER_SCHEDULABLE_ACTIONS,
  // The terminal job-status set + predicate — the TS mirror of the backend's
  // `JobStatus.is_terminal()`. A `wait_for_job` caller uses these to decide
  // when a job has settled vs. needs another wait; single source of truth so
  // the terminal set never drifts between the SDK and the wait route.
  TERMINAL_JOB_STATUSES,
  isTerminalJobStatus,
} from "./client.js";
export type {
  ClientOptions,
  IngestOptions,
  AskOptions,
  AgentTurnOptions,
  AgentResult,
  ResolvedChange,
  OntologyResolveResult,
  OntologyApplyResult,
  OntologyApplyChangeResult,
  OntologyApplyBatchResult,
  EnrichRequest,
  EnrichJob,
  EnrichJobCreate,
  JobProgress,
  JobSummary,
  Verdict,
  ConflictReview,
  RowResult,
  EnrichmentTier,
  JobStatus,
  // The kind of work a background job performs (dedupe / enrichment /
  // reconciliation / discovery). Re-exported so a consumer (e.g. the MCP server's
  // `list_jobs` category filter) can source its allowed-category list from the
  // canonical SDK type instead of hand-maintaining a twin that silently drifts
  // from the backend `JobCategory` enum (ONTA-243).
  JobCategory,
  ConflictPolicy,
  RowAction,
  ReviewDecision,
  // COG-128 — raw/passthrough API + newly-added typed shapes
  RawInit,
  TypeRecord,
  TypeRecordsPage,
  TypeEdge,
  NormalizationRule,
  // ONTA-178 — canonical semantic instance search
  SemanticSearchHit,
  SemanticSearchResponse,
  // ONTA-2xx — per-tenant API source registry
  ApiSourceSummary,
  ApiSourceValidationError,
  ApiSourceValidateResult,
  ApiSourceTestResult,
  ApiSourceWrite,
  // ONTA-173 — schedules: user-schedulable vs system-managed action split
  Schedule,
  ScheduleAction,
  UserSchedulableAction,
  // Per-tenant API usage metering (dashboard usage panel)
  UsageSeries,
  UsageMetricBlock,
  UsageTotals,
  UsageReport,
} from "./client.js";
