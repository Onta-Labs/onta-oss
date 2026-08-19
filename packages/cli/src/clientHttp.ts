/** HTTP transport + canonical path builders for the Infona SDK client.

``request`` / ``requestRaw`` are the ONE place headers, tenant-heal, and
timeouts live. Path builders (``pAsk``, ``pIngest``, …) are the single
source of truth for backend URLs — RawApi and typed methods share them.
*/
import { isClerkUserId, readConfig, writeConfig } from "./config.js";
import { InfonaError, envVar } from "./clientError.js";
import type { ClientOptions } from "./clientTypes.js";
import { mapFirstHourError } from "./firstHourErrors.js";

export class ClientHttp {
  apiKey: string | undefined;
  baseUrl: string;
  tenant: string;

  /** In-flight heal for configs that still have tenant = Clerk user id. */
  protected tenantHealPromise: Promise<void> | null = null;

  constructor(opts: ClientOptions = {}) {
    // Resolution order for each field: explicit opts → env var → ~/.infona/config.json
    // (written by `infona login`) → built-in default. Reading the config eagerly
    // is cheap (small JSON file) and lets users skip env vars entirely after login.
    const cfg = readConfig();
    this.apiKey = opts.apiKey ?? envVar("API_KEY") ?? cfg.apiKey;
    const url =
      opts.baseUrl ?? envVar("API_URL") ?? cfg.apiUrl ?? "https://api.infona.ai";
    this.baseUrl = url.replace(/\/+$/, "");
    // Local/self-hosted open-access uses tenant "default". Hosted SDK default
    // remains demo-tenant. --local passes baseUrl=http://localhost:8000.
    const isLocalHost = /^(https?:\/\/)?(localhost|127\.0\.0\.1)(:|\/|$)/i.test(
      this.baseUrl,
    );
    // May still be a Clerk user id from a legacy login; healTenantIfNeeded()
    // rewrites it to a real workspace on the first graph request.
    this.tenant =
      opts.tenant ??
      envVar("TENANT") ??
      cfg.tenant ??
      (isLocalHost ? "default" : "demo-tenant");
  }

  /**
   * If `this.tenant` is a Clerk user id (legacy login bug), resolve the first
   * real workspace via GET /v1/me/tenants and rewrite config + this.tenant.
   */
  protected async healTenantIfNeeded(): Promise<void> {
    if (!isClerkUserId(this.tenant)) return;
    if (!this.apiKey) {
      throw new InfonaError(
        `Configured tenant "${this.tenant}" looks like a Clerk user id, not a workspace. ` +
          `Set INFONA_TENANT to a workspace id from the dashboard (or re-run \`infona login\`).`,
      );
    }
    if (!this.tenantHealPromise) {
      const bogus = this.tenant;
      this.tenantHealPromise = (async () => {
        const res = await fetch(`${this.baseUrl}/v1/me/tenants`, {
          headers: {
            "X-API-Key": this.apiKey!,
            "Content-Type": "application/json",
          },
          signal: AbortSignal.timeout(15_000),
        });
        if (!res.ok) {
          throw new InfonaError(
            `Configured tenant "${bogus}" is a user id, not a workspace, and ` +
              `listing workspaces failed (HTTP ${res.status}). Set INFONA_TENANT ` +
              `to a real workspace id.`,
            { status: res.status },
          );
        }
        const data = (await res.json()) as unknown;
        const list = Array.isArray(data) ? data : [];
        let first: string | undefined;
        for (const entry of list) {
          const id =
            typeof entry === "string"
              ? entry
              : entry &&
                  typeof entry === "object" &&
                  typeof (entry as { id?: unknown }).id === "string"
                ? (entry as { id: string }).id
                : "";
          if (id && !isClerkUserId(id)) {
            first = id;
            break;
          }
        }
        if (!first) {
          throw new InfonaError(
            `Configured tenant "${bogus}" is a user id, and this key has no workspaces. ` +
              `Create a workspace in the dashboard, then set INFONA_TENANT.`,
          );
        }
        this.tenant = first;
        try {
          writeConfig({ tenant: first });
        } catch {
          // best-effort migrate of ~/.infona/config.json
        }
      })();
    }
    await this.tenantHealPromise;
  }

  protected headers(): Record<string, string> {
    const h: Record<string, string> = { "Content-Type": "application/json" };
    if (this.apiKey) h["X-API-Key"] = this.apiKey;
    return h;
  }

  protected base(): string {
    return `${this.baseUrl}/graphs/${this.tenant}`;
  }

  // --- Path builders -------------------------------------------------------- #
  // SINGLE source of truth for every canonical backend path. Both the raw API
  // and the new typed parsed methods build URLs through these, so a path lives
  // in exactly one place. Tenant-scoped paths hang off `base()`
  // (`{baseUrl}/graphs/{tenant}`); the handful of account-level paths
  // (e.g. tenant CRUD) hang off `baseUrl` directly.
  //
  // These are marked `@internal` (not part of the public SDK surface) but are
  // not `private`, so the sibling {@link RawApi} can build the same canonical
  // paths without duplicating them.

  /** @internal */
  pAgent(): string {
    return `${this.base()}/agent`;
  }
  /** @internal */ pAsk(): string {
    return `${this.base()}/ask`;
  }
  /** @internal */ pIngest(): string {
    return `${this.base()}/ingest`;
  }
  /** @internal */ pIngestCsvSchema(): string {
    return `${this.base()}/ingest/csv/schema`;
  }
  /** @internal */ pIngestCsvRows(): string {
    return `${this.base()}/ingest/csv/rows`;
  }
  /** @internal ONTA-553: `POST /graphs/{tenant}/ingest/dlt`. */
  pIngestDlt(): string {
    return `${this.base()}/ingest/dlt`;
  }
  /** @internal ONTA-554 persist family — not `/api-sources`. */
  pExtractSources(): string {
    return `${this.base()}/extract-sources`;
  }
  /** @internal */ pExtractSource(slug: string): string {
    return `${this.base()}/extract-sources/${encodeURIComponent(slug)}`;
  }
  /** @internal */ pExtractSourceRun(slug: string): string {
    return `${this.pExtractSource(slug)}/run`;
  }
  /** @internal */ pEnrichJobs(): string {
    return `${this.base()}/enrich/jobs`;
  }
  /** @internal */ pEnrichJob(jobId: string): string {
    return `${this.base()}/enrich/jobs/${encodeURIComponent(jobId)}`;
  }
  /** @internal Bounded server-side long-poll: waits until the job is terminal
   *  or a capped timeout, then returns its current status. `timeoutS` is baked
   *  into the query string by the caller. */
  pEnrichJobWait(jobId: string, timeoutS?: number): string {
    const qs =
      timeoutS === undefined
        ? ""
        : `?timeout_s=${encodeURIComponent(String(timeoutS))}`;
    return `${this.pEnrichJob(jobId)}/wait${qs}`;
  }
  /** @internal */ pEnrichJobConflicts(jobId: string): string {
    return `${this.pEnrichJob(jobId)}/conflicts`;
  }
  /** @internal */ pEnrichJobApply(jobId: string): string {
    return `${this.pEnrichJob(jobId)}/apply`;
  }
  /** @internal Full workspace ontology (layered C+A/B effective payload). */
  pOntology(): string {
    return `${this.base()}/ontology`;
  }
  /** @internal Workspace-wide Active type counts (ONTA-409 KgStats union). */
  pOntologyTypeCounts(): string {
    return `${this.base()}/ontology/type-counts`;
  }
  /** @internal Workspace base pin + revision (ONTA-410). */
  pOntologyBasePin(): string {
    return `${this.base()}/ontology/base-pin`;
  }
  /** @internal Base-pin upgrade preview (ONTA-410). */
  pOntologyBasePinPreview(query?: string): string {
    return `${this.base()}/ontology/base-pin/preview${query ?? ""}`;
  }
  /** @internal Upgrade workspace base pin (ONTA-410). */
  pOntologyBasePinUpgrade(): string {
    return `${this.base()}/ontology/base-pin/upgrade`;
  }
  /** @internal Rollback workspace base pin (ONTA-410). */
  pOntologyBasePinRollback(): string {
    return `${this.base()}/ontology/base-pin/rollback`;
  }
  /** @internal Grouped ontology history (ONTA-410). */
  pOntologyHistory(query?: string): string {
    return `${this.base()}/ontology/history${query ?? ""}`;
  }
  /** @internal Structural ontology diff (ONTA-410). */
  pOntologyDiff(query?: string): string {
    return `${this.base()}/ontology/diff${query ?? ""}`;
  }
  /** @internal */ pOntologyTypes(): string {
    return `${this.base()}/ontology/types`;
  }
  /** @internal */ pOntologyResolve(): string {
    return `${this.base()}/ontology/resolve`;
  }
  /** @internal Targets the premium ontology-recommender route, mounted only on
   *  deployments with the proprietary layer — 404s on bare OSS. */
  pOntologyRecommend(): string {
    return `${this.base()}/ontology/recommend`;
  }
  /** @internal */ pOntologyApply(): string {
    return `${this.base()}/ontology/apply`;
  }
  /** @internal The canonical batch-apply route (apply N resolved changes in one
   *  round-trip). Every client rides this exact path — no client-side loop. */
  pOntologyApplyBatch(): string {
    return `${this.base()}/ontology/apply/batch`;
  }
  /** @internal Unified jobs list (dedupe + enrichment + reconciliation),
   *  newest first. Optional `?category=` filter is baked into `query`. */
  pJobs(query?: string): string {
    return `${this.base()}/jobs${query ?? ""}`;
  }
  /** @internal Single unified job (`DELETE /graphs/{tenant}/jobs/{id}`). */
  pJob(jobId: string): string {
    return `${this.base()}/jobs/${encodeURIComponent(jobId)}`;
  }
  /** @internal Per-tenant API-usage report (requests / latency / cost,
   *  day-aligned series + breakdowns). Optional `?days=` is baked into `query`. */
  pUsage(query?: string): string {
    return `${this.base()}/usage${query ?? ""}`;
  }
  /** @internal Job-creating action endpoints (COG-99): find-merge-duplicates,
   *  enrich, suggest-relationships. Each creates a tracked job and returns
   *  `{job_id, status, poll_url}`. `name` is fixed by the calling raw method. */
  pAction(name: string): string {
    return `${this.base()}/actions/${name}`;
  }
  /** @internal Recurring-action schedules (COG-135). Optional `?...` filter is
   *  baked into `query` by the caller. */
  pSchedules(query?: string): string {
    return `${this.base()}/schedules${query ?? ""}`;
  }
  /** @internal A single schedule by id. */
  pSchedule(id: string): string {
    return `${this.base()}/schedules/${encodeURIComponent(id)}`;
  }
  /** @internal */ pKgs(): string {
    return `${this.base()}/kgs`;
  }
  /** @internal */ pKg(name: string): string {
    return `${this.base()}/kgs/${encodeURIComponent(name)}`;
  }
  /** @internal */ pTypeCounts(kg: string): string {
    return `${this.pKg(kg)}/type-counts`;
  }
  /** @internal KG export (F10) — JSON or CSV of instance rows. */
  pExport(kg: string, query?: string): string {
    return `${this.pKg(kg)}/export${query ?? ""}`;
  }
  /** @internal */ pExploreSummary(kg: string, typeName: string): string {
    return `${this.base()}/explore/kgs/${encodeURIComponent(kg)}/types/${encodeURIComponent(typeName)}/summary`;
  }
  /** @internal */ pExploreRecords(kg: string, typeName: string, query?: string): string {
    return `${this.base()}/explore/kgs/${encodeURIComponent(kg)}/types/${encodeURIComponent(typeName)}/records${query ?? ""}`;
  }
  /** @internal */ pExploreTypeEdges(kg: string): string {
    return `${this.base()}/explore/kgs/${encodeURIComponent(kg)}/type-edges`;
  }
  /** @internal KG-scoped, population-aware whole schema (ONTA-418). */
  pExploreSchema(kg: string, query?: string): string {
    return `${this.base()}/explore/kgs/${encodeURIComponent(kg)}/schema${query ?? ""}`;
  }
  /** @internal */ pExploreSearch(query: string): string {
    return `${this.base()}/explore/search${query}`;
  }
  /** @internal Canonical semantic instance search (ONTA-178) — hybrid
   *  lexical+vector search over marked free-text attributes, grouped by
   *  entity. ONE route for every interface (webapp/CLI/MCP). */
  pSearch(): string {
    return `${this.base()}/search`;
  }
  /** @internal Index-free literal grep over ONE KG (ONTA-416) — a live triple
   *  scan, deliberately a SEPARATE route from `/search` (whose contract it
   *  inverts on every axis). ONE route for every interface (webapp/CLI/MCP). */
  pGrep(): string {
    return `${this.base()}/grep`;
  }
  /** @internal */ pNormalizeSuggest(query: string): string {
    return `${this.base()}/normalize/suggest${query}`;
  }
  /** @internal */ pNormalizeRules(query?: string): string {
    return `${this.base()}/normalize/rules${query ?? ""}`;
  }
  /** @internal */ pNormalizeRule(ruleId: string, action: "confirm" | "reject" | "apply"): string {
    return `${this.base()}/normalize/rules/${encodeURIComponent(ruleId)}/${action}`;
  }
  /** @internal */ pTenants(): string {
    return `${this.baseUrl}/v1/me/tenants`;
  }
  /** @internal */ pTenant(tenantId: string): string {
    return `${this.baseUrl}/v1/me/tenants/${encodeURIComponent(tenantId)}`;
  }
  /** @internal Per-tenant API source registry collection (ONTA-2xx). */
  pApiSources(): string {
    return `${this.base()}/api-sources`;
  }
  /** @internal A single API source by slug. */
  pApiSource(slug: string): string {
    return `${this.base()}/api-sources/${encodeURIComponent(slug)}`;
  }
  /** @internal Validate a spec (collection-level, no write). */
  pApiSourcesValidate(): string {
    return `${this.base()}/api-sources/validate`;
  }
  /** @internal Smoke-test a source (collection-level; slug OR inline spec). */
  pApiSourcesTest(): string {
    return `${this.base()}/api-sources/test`;
  }

  /**
   * Low-level passthrough request. Centralizes the absolute URL (already built
   * by a path-builder, so it carries the base URL + `/graphs/{tenant}` prefix),
   * the `X-API-Key` header, JSON content-type, body stringification, and a
   * timeout/abort — then returns the backend {@link Response} UNCHANGED.
   *
   * Unlike {@link request}, this does NOT inspect `res.ok` and does NOT parse or
   * reshape the body. A 4xx/5xx comes back as a resolved `Response` (the caller
   * reads `.status`/`.headers`/`.body`), NOT a thrown {@link InfonaError}. The
   * only rejection paths are a genuine network failure or a timeout abort —
   * exactly the cases where there is no HTTP response to hand back.
   *
   * `init.headers` is merged last so a caller can add/override headers; `init.body`,
   * when a non-string is passed, is JSON-stringified for convenience.
   */
  async requestRaw(
    method: string,
    path: string,
    init: { body?: unknown; headers?: Record<string, string>; timeoutMs?: number } = {},
  ): Promise<Response> {
    const timeoutMs = init.timeoutMs ?? 120_000;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    // Always a string (or undefined): we stringify non-string bodies here, so we
    // never depend on the DOM `BodyInit` type (this package builds with the Node
    // lib only, no `dom` lib).
    let body: string | undefined;
    if (init.body !== undefined) {
      body = typeof init.body === "string" ? init.body : JSON.stringify(init.body);
    }

    try {
      return await fetch(path, {
        method,
        headers: { ...this.headers(), ...(init.headers ?? {}) },
        body,
        signal: controller.signal,
      });
    } catch (err) {
      // A network error or timeout abort means there is NO Response to return,
      // so this is the one case we surface as a thrown error. A non-2xx HTTP
      // status is NOT an error here — it resolves above as a Response.
      if (err instanceof Error && err.name === "AbortError") {
        throw new InfonaError(`Request to ${path} timed out after ${timeoutMs}ms`);
      }
      const raw = err instanceof Error ? err.message : String(err);
      throw new InfonaError(
        mapFirstHourError({
          message: raw,
          baseUrl: this.baseUrl,
          hasApiKey: Boolean(this.apiKey),
        }) ?? `Network error contacting ${path}: ${raw}`,
      );
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Probe the backend to determine reachability and whether endpoints
   * require an X-API-Key header. Used at shell startup to distinguish
   * cloud (auth required) from self-hosted open-access deployments.
   */
  async healthCheck(): Promise<{
    ok: boolean;
    requiresAuth: boolean;
    url: string;
    neo4j?: boolean;
    status?: string;
  }> {
    const healthUrl = `${this.baseUrl}/health`;
    let neo4j: boolean | undefined;
    let hstatus: string | undefined;
    try {
      const res = await fetch(healthUrl, {
        signal: AbortSignal.timeout(5000),
      });
      if (!res.ok) return { ok: false, requiresAuth: false, url: this.baseUrl };
      try {
        const data = (await res.json()) as { status?: unknown; neo4j?: unknown };
        hstatus = typeof data.status === "string" ? data.status : undefined;
        neo4j = typeof data.neo4j === "boolean" ? data.neo4j : undefined;
      } catch {
        // body not JSON — still treat the process as reachable
      }
    } catch {
      return { ok: false, requiresAuth: false, url: this.baseUrl };
    }
    // Probe whether endpoints require auth by hitting /kgs without X-API-Key.
    // 401 = requires auth; 200/empty = open access; anything else = treat as
    // auth-required to be safe.
    try {
      const res = await fetch(`${this.base()}/kgs`, {
        headers: { "Content-Type": "application/json" },
        signal: AbortSignal.timeout(5000),
      });
      return {
        ok: true,
        requiresAuth: res.status === 401,
        url: this.baseUrl,
        neo4j,
        status: hstatus,
      };
    } catch {
      return { ok: true, requiresAuth: true, url: this.baseUrl, neo4j, status: hstatus };
    }
  }

  protected async request<T = unknown>(
    method: string,
    url: string,
    body?: unknown,
    timeoutMs: number = 120_000,
  ): Promise<T> {
    // Rewrite legacy tenant=userId before hitting /graphs/{tenant}/…
    if (isClerkUserId(this.tenant) || /\/graphs\/user_[A-Za-z0-9]+(?:\/|$)/.test(url)) {
      const before = this.tenant;
      await this.healTenantIfNeeded();
      if (before !== this.tenant) {
        url = url.replace(
          /\/graphs\/user_[A-Za-z0-9]+/g,
          `/graphs/${this.tenant}`,
        );
      }
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let res: Response;
    try {
      res = await fetch(url, {
        method,
        headers: this.headers(),
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (err) {
      clearTimeout(timer);
      if (err instanceof Error && err.name === "AbortError") {
        throw new InfonaError(`Request to ${url} timed out after ${timeoutMs}ms`);
      }
      const raw = err instanceof Error ? err.message : String(err);
      throw new InfonaError(
        mapFirstHourError({
          message: raw,
          baseUrl: this.baseUrl,
          hasApiKey: Boolean(this.apiKey),
        }) ?? `Network error contacting ${url}: ${raw}`,
      );
    }
    clearTimeout(timer);

    if (!res.ok) {
      let text = "";
      try {
        text = await res.text();
      } catch {
        // ignore
      }
      // Friendlier hint when a stale config still points at a user id path.
      if (
        res.status === 403 &&
        /grant access to tenant ['"]user_/i.test(text)
      ) {
        throw new InfonaError(
          `HTTP 403: ${text}\n` +
            `Hint: INFONA_TENANT / config tenant is set to a Clerk user id. ` +
            `Set it to a workspace id (dashboard → workspace switcher) or re-run \`infona login\`.`,
          { status: res.status, body: text },
        );
      }
      throw new InfonaError(
        mapFirstHourError({
          message: `HTTP ${res.status}: ${text}`,
          status: res.status,
          body: text,
          baseUrl: this.baseUrl,
          hasApiKey: Boolean(this.apiKey),
        }) ?? `HTTP ${res.status}: ${text}`,
        { status: res.status, body: text },
      );
    }

    // 204 No Content
    if (res.status === 204) return undefined as T;

    const ct = res.headers.get("content-type") ?? "";
    if (ct.includes("application/json")) {
      return (await res.json()) as T;
    }
    // fall back to text
    const text = await res.text();
    try {
      return JSON.parse(text) as T;
    } catch {
      return text as unknown as T;
    }
  }
}
