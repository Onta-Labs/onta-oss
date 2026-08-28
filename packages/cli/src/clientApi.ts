/** Ask, KG lifecycle, ontology, and job methods for the SDK client. */
import { InfonaError } from "./clientError.js";
import { ClientIngest } from "./clientIngest.js";
import type {
  AskOptions,
  ConflictReview,
  EnrichJob,
  EnrichJobCreate,
  EnrichRequest,
  JobCategory,
  JobSummary,
  OntologyApplyBatchResult,
  OntologyApplyResult,
  OntologyResolveResult,
  ResolvedChange,
  ReviewDecision,
  TypeCount,
  TypeUsage,
  UsageReport,
} from "./clientTypes.js";
import type { AgentResult, AgentTurnOptions } from "./clientTypesExtra.js";

export class ClientApi extends ClientIngest {
  async ask(
    question: string,
    opts: AskOptions = {},
  ): Promise<Record<string, unknown>> {
    const body: Record<string, unknown> = { question };
    if (opts.kg) body.kg_name = opts.kg;
    if (opts.model) body.model = opts.model;
    return this.request("POST", `${this.base()}/ask`, body, 60_000);
  }

  /**
   * One turn of the unified Ask-AI agent — the SINGLE conversational surface
   * (`POST /graphs/{tenant}/agent`, COG-118). Mirrors the HTTP contract exactly:
   *
   *  - `confirmPlanId` set → the server runs `execute_plan` (the only mutating
   *    path) and returns `{kind:"result", steps}`. Confirms are ONE-SHOT
   *    server-side: a plan executes exactly once, so a duplicate confirm (a
   *    retry after a gateway timeout, an auto-confirm double-fire) never
   *    re-runs the steps — it replays the same result marked `replayed: true`
   *    once finished, or errors with `code:"plan_already_executing"` while the
   *    first confirm is still in flight.
   *  - otherwise → the server runs `planner.handle(message)` and returns one of
   *    `{kind:"answer"}` / `{kind:"clarify"}` / `{kind:"plan"}`.
   *
   * The agent classifies intent server-side and drives the underlying engines
   * through its capability registry — the client never talks to `/ask`,
   * `/enrich/*` etc. for an agent turn. ENTITLEMENT for any paid step a plan
   * contains is enforced server-side at execute time (the same authorization the
   * direct paid routes apply), so confirming a plan here cannot bypass a gate the
   * direct path enforces — the gate lives behind the endpoint, not in this client.
   */
  async agent(opts: AgentTurnOptions): Promise<AgentResult> {
    const context: Record<string, unknown> = {
      kg_name: opts.kgName ?? "",
      type_name: opts.typeName ?? null,
    };
    // Explicit links the user attached for this turn. Threaded into the request
    // context so the server routes/extracts from these pages; omitted entirely
    // when none are given so the body is unchanged for existing callers.
    if (opts.urls && opts.urls.length) context.urls = opts.urls;
    const body: Record<string, unknown> = {
      message: opts.message ?? "",
      context,
    };
    if (opts.sessionId) body.session_id = opts.sessionId;
    // Per-run HARD spend ceiling (ONTA-378): forwarded only when the caller set
    // it, so the body is byte-identical for existing callers. `!= null` keeps an
    // explicit 0 (unlimited) while dropping undefined/null.
    if (opts.spendCeilingUsd != null) body.spend_ceiling_usd = opts.spendCeilingUsd;
    // confirm.plan_id present → the server routes to execute_plan (mutating).
    if (opts.confirmPlanId) body.confirm = { plan_id: opts.confirmPlanId };
    return this.request<AgentResult>(
      "POST",
      `${this.base()}/agent`,
      body,
      // Generous: a confirmed plan can kick off enrichment/dedup work, and a
      // question turn runs an LLM round-trip server-side.
      120_000,
    );
  }

  /** List the tenants the authenticated user can access (GET /v1/me/tenants).
   *  Keyed by the API key (X-API-Key → user), so it's independent of the active
   *  tenant. Throws InfonaError with status 501 on deployments without a tenant
   *  provider (e.g. OSS-only). */
  async listTenants(): Promise<Array<{ id: string; label: string }>> {
    return this.request<Array<{ id: string; label: string }>>(
      "GET",
      `${this.baseUrl}/v1/me/tenants`,
      undefined,
      15_000,
    );
  }

  /** List all context graphs for the current tenant. */
  async listKgs(): Promise<Array<Record<string, unknown>>> {
    const data = await this.request<unknown>(
      "GET",
      `${this.base()}/kgs`,
      undefined,
      15_000,
    );
    if (Array.isArray(data)) return data as Array<Record<string, unknown>>;
    if (data && typeof data === "object" && "kgs" in data) {
      const kgs = (data as { kgs?: unknown }).kgs;
      if (Array.isArray(kgs)) return kgs as Array<Record<string, unknown>>;
    }
    return [];
  }

  /** Create a context graph. */
  async createKg(
    name: string,
    description?: string,
  ): Promise<Record<string, unknown>> {
    const body: Record<string, unknown> = { name };
    if (description) body.description = description;
    return this.request("POST", `${this.base()}/kgs`, body, 15_000);
  }

  /** Delete a context graph by name. */
  async deleteKg(name: string): Promise<Record<string, unknown>> {
    return this.request(
      "DELETE",
      `${this.base()}/kgs/${encodeURIComponent(name)}`,
      undefined,
      30_000,
    );
  }

  /**
   * Export KG instance data (`GET /kgs/{kg}/export`).
   * JSON → parsed object; CSV → raw string body.
   */
  async exportKg(
    kg: string,
    opts: { format?: "json" | "csv"; type?: string; limit?: number } = {},
  ): Promise<Record<string, unknown> | string> {
    const qs = new URLSearchParams();
    qs.set("format", opts.format ?? "json");
    if (opts.type) qs.set("type", opts.type);
    if (opts.limit != null) qs.set("limit", String(opts.limit));
    return this.request<Record<string, unknown> | string>(
      "GET",
      this.pExport(kg, `?${qs.toString()}`),
      undefined,
      120_000,
    );
  }

  /**
   * Export a live KG as a Blueprint package
   * (`POST /graphs/{tenant}/kgs/{kg}/blueprint/export`, INF-565).
   * Returns the validated manifest plus directory files (`blueprint.yaml`).
   * This is not {@link exportKg} — that dumps instance rows.
   */
  async exportBlueprint(
    kg: string,
    opts: {
      namespace?: string;
      version?: string;
      license?: string;
      attribution?: string;
      name?: string;
      packageId?: string;
      acquisitionRevision?: number;
    } = {},
  ): Promise<{ kg: string; manifest: Record<string, unknown>; files: Record<string, string> }> {
    const body: Record<string, unknown> = {};
    if (opts.namespace) body.namespace = opts.namespace;
    if (opts.version) body.version = opts.version;
    if (opts.license) body.license = opts.license;
    if (opts.attribution) body.attribution = opts.attribution;
    if (opts.name) body.name = opts.name;
    if (opts.packageId) body.package_id = opts.packageId;
    if (opts.acquisitionRevision != null) {
      body.acquisition_revision = opts.acquisitionRevision;
    }
    return this.request(
      "POST",
      this.pBlueprintExport(kg),
      body,
      60_000,
    );
  }

  /**
   * Validate a Blueprint document via the canonical route
   * (`POST /graphs/{tenant}/blueprint/validate`).
   */
  async validateBlueprint(opts: {
    manifest?: Record<string, unknown>;
    files?: Record<string, string>;
  }): Promise<{ errors: string[] }> {
    return this.request(
      "POST",
      this.pBlueprintValidate(),
      opts,
      20_000,
    );
  }

  /**
   * Effective workspace ontology — layered C+A/B read with shadowing applied
   * (`GET /graphs/{tenant}/ontology`, ONTA-397/408). Returns the full browser
   * payload (`tenant_id`, `entitled`, `layers`, `types` with sources/skills
   * overlays). Empty is a normal `{ types: [] }`, never an error.
   */
  async ontology(): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      "GET",
      this.pOntology(),
      undefined,
      20_000,
    );
  }

  /**
   * Workspace-wide Active type counts — union of `KgStats.type_breakdown`
   * across every KG in the tenant (`GET /graphs/{tenant}/ontology/type-counts`,
   * ONTA-409). Powers the Ontology viewer's Active / All pills. Types with
   * zero instances everywhere are omitted. Empty is `{ types: [] }`.
   */
  async ontologyTypeCounts(): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      "GET",
      this.pOntologyTypeCounts(),
      undefined,
      15_000,
    );
  }

  /** List ontology types. */
  async ontologyTypes(): Promise<Array<Record<string, unknown>>> {
    const data = await this.request<unknown>(
      "GET",
      `${this.base()}/ontology/types`,
      undefined,
      15_000,
    );
    return Array.isArray(data) ? (data as Array<Record<string, unknown>>) : [];
  }

  /**
   * Resolve a natural-language ontology change against the existing ontology.
   * The caller does not need to know exact type/attribute/relationship names —
   * the server matches the plain-language `ask` to the current schema and
   * returns auto-applied changes plus proposals that need confirmation.
   */
  async ontologyResolve(
    ask: string,
    opts: { knowledge_graph?: string } = {},
  ): Promise<OntologyResolveResult> {
    const body: Record<string, unknown> = { ask };
    if (opts.knowledge_graph) body.knowledge_graph = opts.knowledge_graph;
    return this.request<OntologyResolveResult>(
      "POST",
      `${this.base()}/ontology/resolve`,
      body,
      60_000,
    );
  }

  /**
   * Apply a single resolved ontology change — one of the `proposals` returned
   * by {@link ontologyResolve}. Pass the proposal object through unchanged.
   */
  async ontologyApply(
    proposal: ResolvedChange,
  ): Promise<OntologyApplyResult> {
    return this.request<OntologyApplyResult>(
      "POST",
      `${this.base()}/ontology/apply`,
      proposal,
      60_000,
    );
  }

  /**
   * Apply MANY resolved changes in a single round-trip — the canonical
   * batch-apply route. Equivalent to calling {@link ontologyApply} once per
   * change (same idempotent upserts, applied in order) but one HTTP call
   * instead of N. Partial failure is well defined: the response reports each
   * change with `ok`/`error`, and a failed change does not abort the rest.
   *
   * Thin pass-through — the loop lives server-side; the client never
   * reimplements it (interface convergence).
   */
  async ontologyApplyBatch(
    changes: ResolvedChange[],
  ): Promise<OntologyApplyBatchResult> {
    return this.request<OntologyApplyBatchResult>(
      "POST",
      `${this.base()}/ontology/apply/batch`,
      { changes },
      120_000,
    );
  }

  /**
   * Second-pass entity resolution: re-run ER over an already-ingested KG to
   * collapse intra-batch fragments. Synchronous on the server; returns a
   * per-type before/after report. Generous timeout — it rewrites triples.
   */
  async erRebuild(kg: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      "POST",
      `${this.base()}/explore/kgs/${encodeURIComponent(kg)}/er-rebuild`,
      {},
      300_000,
    );
  }

  /** Per-KG type counts: every type with ≥1 instance, sorted desc. */
  async typeCounts(kg: string): Promise<TypeCount[]> {
    const data = await this.request<unknown>(
      "GET",
      `${this.base()}/kgs/${encodeURIComponent(kg)}/type-counts`,
      undefined,
      30_000,
    );
    return Array.isArray(data) ? (data as TypeCount[]) : [];
  }

  /** Plan + run an enrichment job. Returns immediately with the job id. */
  async enrichRun(req: EnrichRequest): Promise<EnrichJobCreate> {
    return this.request<EnrichJobCreate>(
      "POST",
      `${this.base()}/enrich/jobs`,
      req,
      30_000,
    );
  }

  /** List recent enrichment jobs for the current tenant. */
  async enrichJobs(): Promise<JobSummary[]> {
    const data = await this.request<unknown>(
      "GET",
      `${this.base()}/enrich/jobs`,
      undefined,
      15_000,
    );
    return Array.isArray(data) ? (data as JobSummary[]) : [];
  }

  /**
   * List ALL of a tenant's tracked jobs — dedupe, enrichment AND reconciliation
   * — newest first (`GET /graphs/{tenant}/jobs`, COG-101). This is the unified
   * feed the Jobs page renders; contrast {@link enrichJobs}, which lists only
   * enrichment jobs (`/enrich/jobs`). Pass `category` to filter to one kind.
   * Each item carries the unified summary fields (category, trigger, last_run,
   * next_run, cost(+note), status, progress_pct).
   */
  async jobs(opts: { category?: JobCategory } = {}): Promise<JobSummary[]> {
    const qs = opts.category
      ? `?category=${encodeURIComponent(opts.category)}`
      : "";
    const data = await this.request<unknown>(
      "GET",
      this.pJobs(qs),
      undefined,
      15_000,
    );
    return Array.isArray(data) ? (data as JobSummary[]) : [];
  }

  /** Hard-delete every job for this tenant (`DELETE /graphs/{tenant}/jobs`). */
  async purgeJobs(): Promise<{ deleted: number }> {
    return this.request<{ deleted: number }>(
      "DELETE",
      this.pJobs(),
      undefined,
      30_000,
    );
  }

  /** Hard-delete one job (`DELETE /graphs/{tenant}/jobs/{id}`). */
  async deleteJob(jobId: string): Promise<{ deleted: boolean; job_id: string }> {
    return this.request<{ deleted: boolean; job_id: string }>(
      "DELETE",
      this.pJob(jobId),
      undefined,
      15_000,
    );
  }

  /**
   * Per-tenant API-usage report (`GET /graphs/{tenant}/usage?days=`): the
   * dashboard's usage panel data — day-aligned request / latency / cost
   * series with per-KG and per-API-key breakdowns, window + previous-window
   * totals for deltas, route-class counts and the month-to-date request
   * count. `days` defaults server-side to 30 (max 90).
   */
  async usage(opts: { days?: number } = {}): Promise<UsageReport> {
    const qs = opts.days ? `?days=${encodeURIComponent(String(opts.days))}` : "";
    return this.request<UsageReport>("GET", this.pUsage(qs), undefined, 15_000);
  }

  /** Fetch a single enrichment job (with truncated results). */
  async enrichJob(jobId: string): Promise<EnrichJob> {
    return this.request<EnrichJob>(
      "GET",
      `${this.base()}/enrich/jobs/${encodeURIComponent(jobId)}`,
      undefined,
      15_000,
    );
  }

  /**
   * Wait for a job to settle, then return it (`GET …/enrich/jobs/{id}/wait`).
   *
   * The backend blocks SERVER-SIDE (async, never busy-waiting) until the job is
   * terminal or the bounded timeout elapses, then returns the job with its
   * current status. This is the efficient alternative to hammering
   * {@link enrichJob} in a client-side poll loop: web discovery / enrichment
   * jobs take minutes to settle, and one `waitForJob` call covers a whole
   * server-side wait window.
   *
   * @param timeoutS how long the SERVER should block, in seconds. Clamped
   *   server-side to a hard cap (120s); omit to use the server default (60s).
   * @returns the job. If it is still `running`/`queued` when the server window
   *   elapses, it comes back with that (non-terminal) status — NOT an error —
   *   so a caller loops: `while (!isTerminalJobStatus(job.status)) job =
   *   await client.waitForJob(job.id);`. A few iterations cover a multi-minute
   *   job. Inspect {@link isTerminalJobStatus} to decide when to stop.
   */
  async waitForJob(jobId: string, timeoutS?: number): Promise<EnrichJob> {
    // The client timeout must outlast the server's max wait window (120s) so
    // the long-poll completes on the server, never aborts client-side first.
    return this.request<EnrichJob>(
      "GET",
      this.pEnrichJobWait(jobId, timeoutS),
      undefined,
      130_000,
    );
  }

  /** Fetch the conflict review queue for a job. */
  async enrichConflicts(jobId: string): Promise<ConflictReview[]> {
    const data = await this.request<unknown>(
      "GET",
      `${this.base()}/enrich/jobs/${encodeURIComponent(jobId)}/conflicts`,
      undefined,
      30_000,
    );
    return Array.isArray(data) ? (data as ConflictReview[]) : [];
  }

  /** Apply a set of conflict review decisions to a job. */
  async enrichApply(
    jobId: string,
    decisions: ConflictReview[],
  ): Promise<{ applied: number }> {
    return this.request<{ applied: number }>(
      "POST",
      `${this.base()}/enrich/jobs/${encodeURIComponent(jobId)}/apply`,
      { decisions },
      60_000,
    );
  }

  /** Cancel an enrichment job. */
  async enrichCancel(jobId: string): Promise<void> {
    await this.request<void>(
      "DELETE",
      `${this.base()}/enrich/jobs/${encodeURIComponent(jobId)}`,
      undefined,
      15_000,
    );
  }

  /** Per-type breakdown for one type in one KG: definition + counts + samples.
   *
   * System predicates (rdfs:label, ingested_at, source) are hidden by default
   * — they're attached to every entity at 100% and drown out the columns the
   * user cares about. Pass `includeSystem: true` to see them. */
  async typeUsage(
    kg: string,
    typeName: string,
    opts: { includeSystem?: boolean } = {},
  ): Promise<TypeUsage> {
    const qs = opts.includeSystem ? "?include_system=true" : "";
    return this.request<TypeUsage>(
      "GET",
      `${this.base()}/kgs/${encodeURIComponent(kg)}/types/${encodeURIComponent(typeName)}/usage${qs}`,
      undefined,
      30_000,
    );
  }
}
