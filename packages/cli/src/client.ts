/** Infona SDK client — typed + raw access to the canonical backend.

Implementation lives in sibling ``client*.ts`` modules. Every previously
importable name is re-exported here. Interface/endpoint convergence:
clients reach the backend through these path builders, not hand-rolled
URLs.
*/
export { InfonaError } from "./clientError.js";
export { SCHEMA_SAMPLE_CAP, parseCsv } from "./clientCsv.js";
export { RawApi } from "./clientRaw.js";
export { RawExtractApi } from "./clientRawExtract.js";
export { RawSkillsApi } from "./clientRawSkills.js";
export type {
  AskOptions,
  AttributeSummary,
  AttributeUsage,
  ClientOptions,
  ConflictPolicy,
  ConflictReview,
  EnrichJob,
  EnrichJobCreate,
  EnrichRequest,
  EnrichmentTier,
  EntitySample,
  IngestOptions,
  IngestProgress,
  JobCategory,
  JobProgress,
  JobStatus,
  JobSummary,
  JobTrigger,
  KgSchema,
  KgSchemaType,
  OntologyApplyBatchResult,
  OntologyApplyChangeResult,
  OntologyApplyResult,
  OntologyResolveResult,
  RelationshipSummary,
  RelationshipUsage,
  ResolvedChange,
  ReviewDecision,
  RowAction,
  RowResult,
  SchemaAttribute,
  SchemaRelationship,
  TypeCount,
  TypeSummary,
  TypeUsage,
  UsageMetricBlock,
  UsageReport,
  UsageSeries,
  UsageTotals,
  Verdict,
} from "./clientTypes.js";
export type {
  AgentResult,
  AgentTurnOptions,
  ApiSourceSummary,
  ApiSourceTestResult,
  ApiSourceValidateResult,
  ApiSourceValidationError,
  ApiSourceWrite,
  DltAuthSpec,
  DltAuthType,
  DltIngestRequest,
  DltResourceMap,
  DltSourceKind,
  DltSourceSpec,
  ConnectorTemplate,
  ExtractSchedule,
  ExtractScheduleWrite,
  ExtractSourceSummary,
  ExtractSourceWrite,
  GrepMatch,
  GrepResponse,
  NormalizationRule,
  RawInit,
  Schedule,
  ScheduleAction,
  SemanticSearchHit,
  SemanticSearchOptions,
  SemanticSearchResponse,
  TypeEdge,
  TypeRecord,
  TypeRecordsPage,
  UserSchedulableAction,
} from "./clientTypesExtra.js";
export type {
  EntityDetail,
  EntityRel,
  FunctionInvokeRequest,
  FunctionInvokeResult,
  FunctionRef,
  FunctionRegister,
  FunctionRegisterResult,
  RecomputeStatsResult,
  SkillDetail,
  SkillPatch,
  SkillSummary,
  SkillValidateResult,
  SkillWrite,
  SkillsPromptBlock,
  TenantInfo,
} from "./clientTypesSkills.js";
export { TERMINAL_JOB_STATUSES, isTerminalJobStatus } from "./clientTypes.js";
export { USER_SCHEDULABLE_ACTIONS } from "./clientTypesExtra.js";

import { ClientBlueprints } from "./clientBlueprints.js";
import { RawApi } from "./clientRaw.js";
import type { ClientOptions } from "./clientTypes.js";

export type {
  BlueprintCard,
  BlueprintInstallBody,
  BlueprintInstallResult,
  BlueprintInstallStatus,
  BlueprintUninstallResult,
} from "./clientBlueprints.js";
export { splitBlueprintId } from "./clientBlueprints.js";

export class Client extends ClientBlueprints {
  /**
   * Raw / passthrough API — one method per canonical backend operation, with
   * the path encoded inside the SDK. Each method returns the backend
   * Response VERBATIM: it does NOT throw on non-2xx and does NOT reshape
   * the body.
   */
  readonly raw: RawApi;

  constructor(opts: ClientOptions = {}) {
    super(opts);
    this.raw = new RawApi(this);
  }
}
