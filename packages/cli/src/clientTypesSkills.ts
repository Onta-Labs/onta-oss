/** Skills, functions, entity-detail, and workspace SDK types.

Re-exported from ``client.ts`` so existing imports keep working.
*/

/** List/summary shape from `GET /graphs/{tenant}/skills`. */
export interface SkillSummary {
  slug: string;
  type_name: string;
  title: string;
  summary: string;
  layer: string;
  enabled: boolean;
  version: number;
  body_chars: number;
  editable: boolean;
}

/** Full skill from get/create/update (`body` is the markdown). */
export interface SkillDetail extends SkillSummary {
  body: string;
  metadata: Record<string, unknown>;
}

/** Body of `POST /graphs/{tenant}/skills` (create-or-replace) and `/validate`. */
export interface SkillWrite {
  slug?: string;
  type_name: string;
  body?: string;
  title?: string;
  summary?: string;
  enabled?: boolean;
  metadata?: Record<string, unknown>;
  filename?: string;
  archive_b64?: string;
}

/** Body of `PATCH /graphs/{tenant}/skills/{type}/{slug}`. */
export interface SkillPatch {
  body?: string | null;
  title?: string | null;
  summary?: string | null;
  enabled?: boolean | null;
  metadata?: Record<string, unknown> | null;
}

export interface SkillValidateResult {
  valid: boolean;
  errors: Array<{ message: string }>;
}

/** Exact agent-injection text from `GET …/skills/prompt-block`. */
export interface SkillsPromptBlock {
  text: string;
  skill_count: number;
  chars: number;
}

/** One registered function from `GET /graphs/{tenant}/functions`. */
export interface FunctionRef {
  name: string;
  entity_type: string;
  description: string;
  endpoint_url?: string | null;
  tier?: string;
  layer?: string;
}

/** Body of `POST /graphs/{tenant}/functions`. `endpoint_url` is https or a Lambda ARN. */
export interface FunctionRegister {
  name: string;
  entity_type: string;
  endpoint_url: string;
  description?: string;
  layer?: "tenant" | "enhanced" | "public";
}

export interface FunctionRegisterResult {
  registered: string;
  entity_type: string;
  layer: string;
  type_uri?: string;
  graph_uri?: string;
  [key: string]: unknown;
}

/** Body of `POST /graphs/{tenant}/functions/{name}/invoke`. */
export interface FunctionInvokeRequest {
  entity_uri: string;
  kg_name: string;
}

export interface FunctionInvokeResult {
  entity_uri: string;
  function: string;
  output: Record<string, unknown>;
  discovered_entities?: Array<Record<string, unknown>>;
  duration_ms: number;
  [key: string]: unknown;
}

export interface EntityRel {
  attr: string;
  rel_type: string;
  other_id: string;
  other_name?: string | null;
  other_type?: string | null;
  direction: string;
}

/** `GET /graphs/{tenant}/explore/kgs/{kg}/entities/{id}`. */
export interface EntityDetail {
  id: string;
  name?: string | null;
  primary_type?: string | null;
  source?: string | null;
  labels?: string[];
  properties?: Record<string, unknown>;
  outgoing?: EntityRel[];
  incoming?: EntityRel[];
  [key: string]: unknown;
}

export interface TenantInfo {
  id: string;
  label: string;
  role?: string;
  capability?: string;
}

export interface RecomputeStatsResult {
  status: string;
  kg: string;
  [key: string]: unknown;
}
