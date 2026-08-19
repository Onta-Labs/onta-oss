/** Raw / passthrough API — one method per canonical backend operation.

Each method returns the backend Response VERBATIM (no throw on non-2xx,
no reshape). Paths come from Client path builders so they stay shared
with the typed methods.
*/
import type { Client } from "./client.js";
import type { RawInit } from "./clientTypesExtra.js";

// --- Raw / passthrough API (COG-128) ----------------------------------------- #

/**
 * Raw / passthrough surface — reached via {@link Client.raw}. Each method maps
 * to ONE canonical backend operation, builds the path internally (callers pass
 * NO path string), and returns the backend {@link Response} VERBATIM:
 *
 *  - it does NOT throw on a non-2xx status (a 404/500 resolves as a `Response`
 *    whose `.status` the caller inspects — contrast the typed methods, which
 *    throw {@link InfonaError}); and
 *  - it does NOT parse or reshape the body (the caller gets the unread stream;
 *    contrast e.g. {@link Client.listKgs}, which unwraps `{kgs:[]}`).
 *
 * Every method funnels through {@link Client.requestRaw}, so the base URL,
 * `X-API-Key`, `/graphs/{tenant}` prefix, JSON content-type and timeout are
 * centralized in exactly one place. The only rejection paths are a network
 * failure or a timeout — the cases where there is no HTTP response to return.
 *
 * @example
 * ```ts
 * const client = new Client({ apiKey, tenant });
 * // Webapp proxy pattern: forward the backend response 1:1, no reshaping.
 * const res = await client.raw.enrichJobs(); // GET …/enrich/jobs
 * return new Response(res.body, { status: res.status, headers: res.headers });
 *
 * // A non-2xx is a Response, not a throw:
 * const r = await client.raw.enrichJob("does-not-exist");
 * if (r.status === 404) { ... }            // no try/catch needed
 * ```
 */
export class RawApi {
  constructor(private readonly client: Client) {}

  // -- agent / ask --------------------------------------------------------- #

  /** `POST /graphs/{tenant}/agent` — one turn of the unified Ask-AI agent. */
  agent(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pAgent(), { body, ...init });
  }

  /** `POST /graphs/{tenant}/ask` — natural-language question. */
  ask(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pAsk(), { body, ...init });
  }

  // -- ingest -------------------------------------------------------------- #

  /** `POST /graphs/{tenant}/ingest` — ingest text/json (or csv) content. */
  ingest(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pIngest(), { body, ...init });
  }

  /** `POST /graphs/{tenant}/ingest/csv/schema` — infer a CSV schema mapping. */
  ingestCsvSchema(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pIngestCsvSchema(), { body, ...init });
  }

  /** `POST /graphs/{tenant}/ingest/csv/rows` — write a batch of mapped rows. */
  ingestCsvRows(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pIngestCsvRows(), { body, ...init });
  }

  /** `POST /graphs/{tenant}/ingest/dlt` — extract a REST/SQL source via dlt. */
  ingestDlt(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pIngestDlt(), { body, ...init });
  }

  extractSourcesList(init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pExtractSources(), init);
  }

  extractSourcesGet(slug: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pExtractSource(slug), init);
  }

  extractSourcesCreate(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pExtractSources(), { body, ...init });
  }

  extractSourcesUpdate(slug: string, body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("PATCH", this.client.pExtractSource(slug), {
      body,
      ...init,
    });
  }

  extractSourcesDelete(slug: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("DELETE", this.client.pExtractSource(slug), init);
  }

  extractSourcesRun(slug: string, body?: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pExtractSourceRun(slug), {
      body,
      ...init,
    });
  }

  /** `GET /graphs/{tenant}/extract-sources/catalog` — connector templates. */
  extractCatalog(init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pExtractCatalog(), init);
  }

  /** `PUT /graphs/{tenant}/extract-sources/{slug}/schedule` — set the cadence. */
  extractSourceScheduleSet(slug: string, body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("PUT", this.client.pExtractSourceSchedule(slug), {
      body,
      ...init,
    });
  }

  /** `DELETE /graphs/{tenant}/extract-sources/{slug}/schedule` — stop recurring reads. */
  extractSourceScheduleClear(slug: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("DELETE", this.client.pExtractSourceSchedule(slug), init);
  }

  // -- enrich jobs --------------------------------------------------------- #

  /** `POST /graphs/{tenant}/enrich/jobs` — plan + run an enrichment job. */
  enrichCreateJob(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pEnrichJobs(), { body, ...init });
  }

  /** `GET /graphs/{tenant}/enrich/jobs` — list recent enrichment jobs. */
  enrichJobs(init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pEnrichJobs(), init);
  }

  /** `GET /graphs/{tenant}/jobs?category` — unified jobs list across ALL
   *  categories (dedupe + enrichment + reconciliation), newest first. */
  jobs(opts: { category?: string } = {}, init?: RawInit): Promise<Response> {
    const qs = opts.category
      ? `?category=${encodeURIComponent(opts.category)}`
      : "";
    return this.client.requestRaw("GET", this.client.pJobs(qs), init);
  }

  /** `DELETE /graphs/{tenant}/jobs` — hard-delete every job for the tenant. */
  purgeJobs(init?: RawInit): Promise<Response> {
    return this.client.requestRaw("DELETE", this.client.pJobs(), init);
  }

  /** `DELETE /graphs/{tenant}/jobs/{id}` — hard-delete one job. */
  deleteJob(jobId: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("DELETE", this.client.pJob(jobId), init);
  }

  /** `GET /graphs/{tenant}/usage?days` — per-tenant API-usage report
   *  (day-aligned request/latency/cost series + breakdowns + totals). */
  usage(opts: { days?: number } = {}, init?: RawInit): Promise<Response> {
    const qs = opts.days
      ? `?days=${encodeURIComponent(String(opts.days))}`
      : "";
    return this.client.requestRaw("GET", this.client.pUsage(qs), init);
  }

  /** `POST /graphs/{tenant}/actions/find-merge-duplicates` — start a dedupe
   *  job (second-pass entity resolution). Body `{kg_name}`. */
  actionFindMergeDuplicates(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw(
      "POST",
      this.client.pAction("find-merge-duplicates"),
      { body, ...init },
    );
  }

  /** `POST /graphs/{tenant}/actions/enrich` — start an enrichment job. Body
   *  `{type_name, attributes, kg_name, tier?, …}`. */
  actionEnrich(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pAction("enrich"), {
      body,
      ...init,
    });
  }

  /** `POST /graphs/{tenant}/actions/suggest-relationships` — start a
   *  reconciliation job. Body `{kg_name}`. Premium: degrades to a terminal
   *  failed job when no recommender is registered. */
  actionSuggestRelationships(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw(
      "POST",
      this.client.pAction("suggest-relationships"),
      { body, ...init },
    );
  }

  /** `GET /graphs/{tenant}/enrich/jobs/{id}` — fetch a single job. */
  enrichJob(jobId: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pEnrichJob(jobId), init);
  }

  /** `GET /graphs/{tenant}/enrich/jobs/{id}/wait?timeout_s=` — bounded
   *  server-side long-poll until the job is terminal or the (capped) timeout. */
  waitForJob(
    jobId: string,
    timeoutS?: number,
    init?: RawInit,
  ): Promise<Response> {
    return this.client.requestRaw(
      "GET",
      this.client.pEnrichJobWait(jobId, timeoutS),
      init,
    );
  }

  /** `GET /graphs/{tenant}/enrich/jobs/{id}/conflicts` — conflict review queue. */
  enrichConflicts(jobId: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pEnrichJobConflicts(jobId), init);
  }

  /** `POST /graphs/{tenant}/enrich/jobs/{id}/apply` — apply review decisions. */
  enrichApply(jobId: string, body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pEnrichJobApply(jobId), { body, ...init });
  }

  /** `DELETE /graphs/{tenant}/enrich/jobs/{id}` — cancel a job. */
  enrichCancel(jobId: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("DELETE", this.client.pEnrichJob(jobId), init);
  }

  // -- schedules (COG-135) ------------------------------------------------- #

  /** `GET /graphs/{tenant}/schedules` — list recurring schedules, oldest first. */
  schedules(init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pSchedules(), init);
  }

  /** `POST /graphs/{tenant}/schedules` — create a recurring schedule. The
   *  body's `action` must be a {@link UserSchedulableAction} (the backend
   *  answers 422 for system-managed actions). Body
   *  `{kg_name, category, action, params?, cron?|interval_seconds, enabled?}`. */
  createSchedule(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pSchedules(), { body, ...init });
  }

  /** `PATCH /graphs/{tenant}/schedules/{id}` — enable/disable or update a
   *  schedule. Only provided fields change. System-managed rows (a
   *  non-{@link UserSchedulableAction} `action`, e.g. `semantic-reconcile`)
   *  reject every PATCH with 403. */
  updateSchedule(id: string, body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("PATCH", this.client.pSchedule(id), { body, ...init });
  }

  /** `DELETE /graphs/{tenant}/schedules/{id}` — delete a schedule. */
  deleteSchedule(id: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("DELETE", this.client.pSchedule(id), init);
  }

  // -- ontology ------------------------------------------------------------ #

  /** `GET /graphs/{tenant}/ontology` — effective layered workspace ontology
   *  (ONTA-397/408 browser payload). */
  ontology(init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pOntology(), init);
  }

  /** `GET /graphs/{tenant}/ontology/type-counts` — workspace-wide Active
   *  type counts (ONTA-409 KgStats union). */
  ontologyTypeCounts(init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pOntologyTypeCounts(), init);
  }

  /** `GET /graphs/{tenant}/ontology/base-pin` — current pin + revision (ONTA-410). */
  ontologyBasePin(init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pOntologyBasePin(), init);
  }

  /** `GET /graphs/{tenant}/ontology/base-pin/preview` — upgrade preview. */
  ontologyBasePinPreview(
    query?: string,
    init?: RawInit,
  ): Promise<Response> {
    return this.client.requestRaw(
      "GET",
      this.client.pOntologyBasePinPreview(query),
      init,
    );
  }

  /** `POST /graphs/{tenant}/ontology/base-pin/upgrade`. */
  ontologyBasePinUpgrade(body?: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pOntologyBasePinUpgrade(), {
      body: body ?? {},
      ...init,
    });
  }

  /** `POST /graphs/{tenant}/ontology/base-pin/rollback`. */
  ontologyBasePinRollback(init?: RawInit): Promise<Response> {
    return this.client.requestRaw(
      "POST",
      this.client.pOntologyBasePinRollback(),
      init,
    );
  }

  /** `GET /graphs/{tenant}/ontology/history` — grouped changelog (ONTA-410). */
  ontologyHistory(query?: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw(
      "GET",
      this.client.pOntologyHistory(query),
      init,
    );
  }

  /** `GET /graphs/{tenant}/ontology/diff` — structural ChangeRecords (ONTA-410). */
  ontologyDiff(query?: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw(
      "GET",
      this.client.pOntologyDiff(query),
      init,
    );
  }

  /** `GET /graphs/{tenant}/ontology/types` — list ontology types. */
  ontologyTypes(init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pOntologyTypes(), init);
  }

  /** `POST /graphs/{tenant}/ontology/resolve` — resolve an NL ontology change. */
  ontologyResolve(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pOntologyResolve(), { body, ...init });
  }

  /** `POST /graphs/{tenant}/ontology/recommend` — recommend ontology changes.
   *  Premium route: only mounted on deployments with the proprietary layer,
   *  404s on bare OSS. */
  ontologyRecommend(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pOntologyRecommend(), { body, ...init });
  }

  /** `POST /graphs/{tenant}/ontology/apply` — apply one resolved change. */
  ontologyApply(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pOntologyApply(), { body, ...init });
  }

  /** `POST /graphs/{tenant}/ontology/apply/batch` — apply many resolved changes
   *  in one call. Body: `{ changes: ResolvedChange[] }`. */
  ontologyApplyBatch(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pOntologyApplyBatch(), { body, ...init });
  }

  // -- context graphs ---------------------------------------------------- #

  /** `GET /graphs/{tenant}/kgs` — list context graphs. */
  kgs(init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pKgs(), init);
  }

  /** `POST /graphs/{tenant}/kgs` — create a context graph. */
  createKg(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pKgs(), { body, ...init });
  }

  /** `DELETE /graphs/{tenant}/kgs/{name}` — delete a context graph. */
  deleteKg(name: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("DELETE", this.client.pKg(name), init);
  }

  // -- explore ------------------------------------------------------------- #

  /** `GET /graphs/{tenant}/explore/kgs/{kg}/types/{type}/summary`. */
  exploreSummary(kg: string, typeName: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pExploreSummary(kg, typeName), init);
  }

  /** `GET /graphs/{tenant}/explore/kgs/{kg}/types/{type}/records?limit&cursor`. */
  exploreRecords(
    kg: string,
    typeName: string,
    opts: { limit?: number; cursor?: string } = {},
    init?: RawInit,
  ): Promise<Response> {
    const qs = new URLSearchParams();
    if (opts.limit != null) qs.set("limit", String(opts.limit));
    if (opts.cursor) qs.set("cursor", opts.cursor);
    const query = qs.toString() ? `?${qs.toString()}` : "";
    return this.client.requestRaw("GET", this.client.pExploreRecords(kg, typeName, query), init);
  }

  /** `GET /graphs/{tenant}/explore/kgs/{kg}/type-edges`. */
  exploreTypeEdges(kg: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pExploreTypeEdges(kg), init);
  }

  /** `GET /graphs/{tenant}/explore/kgs/{kg}/schema?type&min_coverage&include_empty&limit`. */
  exploreSchema(
    kg: string,
    opts: {
      types?: string[];
      minCoverage?: number;
      includeEmpty?: boolean;
      limit?: number;
    } = {},
    init?: RawInit,
  ): Promise<Response> {
    const qs = new URLSearchParams();
    for (const t of opts.types ?? []) qs.append("type", t);
    if (opts.minCoverage != null) qs.set("min_coverage", String(opts.minCoverage));
    if (opts.includeEmpty != null) qs.set("include_empty", String(opts.includeEmpty));
    if (opts.limit != null) qs.set("limit", String(opts.limit));
    const query = qs.toString() ? `?${qs.toString()}` : "";
    return this.client.requestRaw("GET", this.client.pExploreSchema(kg, query), init);
  }

  /** `GET /graphs/{tenant}/kgs/{kg}/type-counts`. */
  typeCounts(kg: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pTypeCounts(kg), init);
  }

  /** `GET /graphs/{tenant}/kgs/{kg}/export?format&type&limit` (F10). */
  exportKg(
    kg: string,
    opts: { format?: "json" | "csv"; type?: string; limit?: number } = {},
    init?: RawInit,
  ): Promise<Response> {
    const qs = new URLSearchParams();
    qs.set("format", opts.format ?? "json");
    if (opts.type) qs.set("type", opts.type);
    if (opts.limit != null) qs.set("limit", String(opts.limit));
    return this.client.requestRaw(
      "GET",
      this.client.pExport(kg, `?${qs.toString()}`),
      init,
    );
  }

  /** `POST /graphs/{tenant}/search` — canonical semantic instance search
   *  (ONTA-178). Body `{query, kg_name?, type?, top_k?}`. */
  search(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pSearch(), { body, ...init });
  }

  /** `POST /graphs/{tenant}/grep` — index-free literal scan of ONE KG
   *  (ONTA-416). Body `{q, kg_name, type?, predicate?, case_sensitive?, limit?}`. */
  grep(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pGrep(), { body, ...init });
  }

  /** `GET /graphs/{tenant}/explore/search?kg&q&kind`. */
  exploreSearch(
    kg: string,
    q: string,
    kind: "type" | "attr" = "type",
    init?: RawInit,
  ): Promise<Response> {
    const qs = new URLSearchParams({ kg, q, kind }).toString();
    return this.client.requestRaw("GET", this.client.pExploreSearch(`?${qs}`), init);
  }

  // -- normalize ----------------------------------------------------------- #

  /** `POST /graphs/{tenant}/normalize/suggest?kg&type` — infer + persist rules. */
  normalizeSuggest(kg: string, type: string, init?: RawInit): Promise<Response> {
    const qs = new URLSearchParams({ kg, type }).toString();
    return this.client.requestRaw("POST", this.client.pNormalizeSuggest(`?${qs}`), init);
  }

  /** `GET /graphs/{tenant}/normalize/rules?kg&status` — list stored rules. */
  normalizeRules(opts: { kg?: string; status?: string } = {}, init?: RawInit): Promise<Response> {
    const qs = new URLSearchParams();
    if (opts.kg) qs.set("kg", opts.kg);
    if (opts.status) qs.set("status", opts.status);
    const query = qs.toString() ? `?${qs.toString()}` : "";
    return this.client.requestRaw("GET", this.client.pNormalizeRules(query), init);
  }

  /** `POST /graphs/{tenant}/normalize/rules` — create a user-authored rule. */
  normalizeCreateRule(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pNormalizeRules(), { body, ...init });
  }

  /** `POST /graphs/{tenant}/normalize/rules/{id}/confirm`. */
  normalizeConfirmRule(ruleId: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pNormalizeRule(ruleId, "confirm"), init);
  }

  /** `POST /graphs/{tenant}/normalize/rules/{id}/reject`. */
  normalizeRejectRule(ruleId: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pNormalizeRule(ruleId, "reject"), init);
  }

  /** `POST /graphs/{tenant}/normalize/rules/{id}/apply`. */
  normalizeApplyRule(ruleId: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pNormalizeRule(ruleId, "apply"), init);
  }

  // -- tenants (account-level, NOT tenant-scoped) -------------------------- #

  /** `POST /v1/me/tenants` — create/grant a tenant for the authed user. */
  createTenant(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pTenants(), { body, ...init });
  }

  /** `PATCH /v1/me/tenants/{id}` — rename a tenant (label only; id is fixed). */
  renameTenant(tenantId: string, body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("PATCH", this.client.pTenant(tenantId), { body, ...init });
  }

  /** `DELETE /v1/me/tenants/{id}` — remove a tenant grant. */
  deleteTenant(tenantId: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("DELETE", this.client.pTenant(tenantId), init);
  }

  /** `GET /v1/me/tenants` — list tenants the authed user can access. */
  tenants(init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pTenants(), init);
  }

  // -- api sources (per-tenant registry, ONTA-2xx) ------------------------- #

  /** `GET /graphs/{tenant}/api-sources` — list global (read-only) + tenant-custom
   *  (editable) sources, each flagged by `layer` / `editable` / `has_secret`. */
  apiSourcesList(init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pApiSources(), init);
  }

  /** `GET /graphs/{tenant}/api-sources/{slug}` — read one full spec (secrets
   *  redacted) + `has_secret`. */
  apiSourcesGet(slug: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pApiSource(slug), init);
  }

  /** `POST /graphs/{tenant}/api-sources` — create a tenant-custom source. `body`
   *  is `{spec, secrets?, enabled?}`; `secrets` is write-only, never returned. */
  apiSourcesCreate(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pApiSources(), { body, ...init });
  }

  /** `PATCH /graphs/{tenant}/api-sources/{slug}` — edit a tenant-custom source
   *  (spec / enabled / secrets). A global slug => 403. */
  apiSourcesUpdate(slug: string, body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("PATCH", this.client.pApiSource(slug), { body, ...init });
  }

  /** `DELETE /graphs/{tenant}/api-sources/{slug}` — delete a tenant-custom source
   *  (+ its stored secrets). A global slug => 403. */
  apiSourcesDelete(slug: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("DELETE", this.client.pApiSource(slug), init);
  }

  /** `POST /graphs/{tenant}/api-sources/validate` — validate a spec (no write).
   *  `body` is `{spec}`; returns `{valid, errors:[{path,message}]}`. */
  apiSourcesValidate(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pApiSourcesValidate(), { body, ...init });
  }

  /** `POST /graphs/{tenant}/api-sources/test` — run ONE smoke request (no write,
   *  no persist). `body` is `{slug?, spec?, sample_params}`; a secret is never
   *  echoed. Returns `{ok, rows, error?}`. */
  apiSourcesTest(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pApiSourcesTest(), { body, ...init });
  }
}
