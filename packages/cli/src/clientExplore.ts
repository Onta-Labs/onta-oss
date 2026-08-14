/** Explore, normalize, and API-source registry methods for the SDK client. */
import { ClientApi } from "./clientApi.js";
import type { KgSchema, TypeSummary, TypeUsage } from "./clientTypes.js";
import type {
  ApiSourceSummary,
  ApiSourceTestResult,
  ApiSourceValidateResult,
  ApiSourceWrite,
  GrepResponse,
  NormalizationRule,
  SemanticSearchOptions,
  SemanticSearchResponse,
  TypeEdge,
  TypeRecordsPage,
} from "./clientTypesExtra.js";

export class ClientExplore extends ClientApi {
  async typeSummary(kg: string, typeName: string): Promise<TypeSummary> {
    return this.request<TypeSummary>(
      "GET",
      this.pExploreSummary(kg, typeName),
      undefined,
      30_000,
    );
  }

  /**
   * Population-aware schema for ONE context graph (`GET
   * /graphs/{tenant}/explore/kgs/{kg}/schema`, ONTA-418): every type with the
   * attributes/relationships that are actually POPULATED in that KG, with real
   * coverage percentages.
   *
   * Complements {@link ontologyTypes} (tenant-wide, declaration-only): this one
   * is KG-scoped and tells you which of the declared slots carry data, so a
   * caller never has to guess between similar names.
   *
   * Declared-but-empty types and attributes are returned MARKED
   * (`populated: false` / `declared_only: true`), never omitted. `minCoverage`
   * is the only filter that withholds slots, and the response reports how many
   * it withheld.
   *
   * A backend join by design (the interface-convergence rule): the whole-KG
   * stats are already materialized server-side, so this is one request rather
   * than a per-type fan-out.
   */
  async kgSchema(
    kg: string,
    opts: {
      types?: string[];
      minCoverage?: number;
      includeEmpty?: boolean;
      limit?: number;
    } = {},
  ): Promise<KgSchema> {
    const qs = new URLSearchParams();
    for (const t of opts.types ?? []) qs.append("type", t);
    if (opts.minCoverage != null) qs.set("min_coverage", String(opts.minCoverage));
    if (opts.includeEmpty != null) qs.set("include_empty", String(opts.includeEmpty));
    if (opts.limit != null) qs.set("limit", String(opts.limit));
    const query = qs.toString() ? `?${qs.toString()}` : "";
    return this.request<KgSchema>(
      "GET",
      this.pExploreSchema(kg, query),
      undefined,
      30_000,
    );
  }

  /**
   * Semantic instance search (`POST /graphs/{tenant}/search`, ONTA-178) —
   * "which entities talk about X?" answered by the backend's hybrid
   * lexical+vector index over marked free-text attributes, grouped by entity.
   *
   * This is THE canonical search operation every interface rides (the
   * interface-convergence rule): the MCP `search` tool, the CLI and the
   * webapp all call this method / route — never a bespoke endpoint. The
   * backend embeds the query server-side; when it can't (embedding service
   * down/unconfigured) it answers lexical-only and sets `degraded: true` —
   * surface that to users as "reduced recall", never silently.
   *
   * `topK` is clamped server-side to 1..50 (the response echoes the effective
   * value). An unknown `kg` yields empty hits, not an error. A deployment with
   * the semantic index gate (`INFONA_SEMANTIC_INDEX_ENABLED`) OFF does NOT
   * error: the vector leg is simply never populated, so the route degrades to
   * the keyword leg and answers 200 with `degraded: true`. (It historically
   * 503'd there; that dead-ended callers for no correctness benefit.)
   *
   * Both legs read the derived chunk index, so `search` cannot see a value that
   * has not been indexed yet — use {@link Client.grep} for an index-free literal
   * scan of a single KG when you need "is this exact string in my graph?".
   *
   * `entityUris` is a structured pre-filter (filter-then-semantic): pass entity
   * URIs from SPARQL / your own logic so hybrid ranking only considers that
   * set. Omit/`undefined` = no URI filter; `[]` = zero hits (strict empty
   * allowlist); server blanks-strip + dedupes and 400s above 500 unique URIs.
   * Combined with `kg` / `type` via AND, applied inside ranking legs before
   * LIMIT (not a post-hoc top_k shrink).
   */
  async search(
    query: string,
    opts: SemanticSearchOptions = {},
  ): Promise<SemanticSearchResponse> {
    const body: Record<string, unknown> = { query };
    if (opts.kg) body.kg_name = opts.kg;
    if (opts.type) body.type = opts.type;
    // != null so [] is forwarded (empty allowlist → zero hits), while omit
    // leaves the field off the body (unrestricted).
    if (opts.entityUris != null) body.entity_uris = opts.entityUris;
    if (opts.topK != null) body.top_k = opts.topK;
    return this.request<SemanticSearchResponse>(
      "POST",
      this.pSearch(),
      body,
      30_000,
    );
  }

  /**
   * Index-free literal grep over ONE knowledge graph
   * (`POST /graphs/{tenant}/grep`, ONTA-416) — "is this exact string anywhere in
   * my graph?" answered by a live SPARQL scan of the KG's triples.
   *
   * The debugging counterpart to {@link Client.search}, and a SEPARATE canonical
   * route because its contract inverts search's on every axis: it reads the
   * triple store (not the derived chunk index), so it finds values that were
   * never indexed; `kg` is REQUIRED (an index-free scan must be bounded); a hit
   * is ONE matching triple, not a ranked entity; and results are in scan order,
   * not ranked.
   *
   * Because the scan has no supporting index, the server guards it: the needle
   * must carry >= 2 non-whitespace characters (else 400), `limit` is clamped to
   * 1..200 and echoed, the scan runs under a short dedicated timeout, and the
   * route is rate-limited. `truncated: true` means the limit was hit and more
   * matches exist. A deployment may disable the surface entirely
   * (`INFONA_GREP_ENABLED=false`), which answers 503 naming the gate (thrown
   * here as an InfonaError).
   */
  async grep(
    q: string,
    kg: string,
    opts: {
      type?: string;
      predicate?: string;
      caseSensitive?: boolean;
      limit?: number;
    } = {},
  ): Promise<GrepResponse> {
    const body: Record<string, unknown> = { q, kg_name: kg };
    if (opts.type) body.type = opts.type;
    if (opts.predicate) body.predicate = opts.predicate;
    if (opts.caseSensitive != null) body.case_sensitive = opts.caseSensitive;
    if (opts.limit != null) body.limit = opts.limit;
    return this.request<GrepResponse>("POST", this.pGrep(), body, 30_000);
  }

  /** Search types or attributes by name substring within a KG. */
  async exploreSearch(
    kg: string,
    q: string,
    kind: "type" | "attr" = "type",
  ): Promise<Array<Record<string, unknown>>> {
    const qs = new URLSearchParams({ kg, q, kind }).toString();
    const data = await this.request<unknown>(
      "GET",
      this.pExploreSearch(`?${qs}`),
      undefined,
      15_000,
    );
    return Array.isArray(data) ? (data as Array<Record<string, unknown>>) : [];
  }

  // --- New typed methods (COG-128) ------------------------------------------ #
  // Parsed/throwing variants of the previously-MISSING ops, sharing the same
  // path-builders as the raw API. These follow the existing typed-method
  // contract (throw on non-2xx, light reshape) — the raw equivalents under
  // `client.raw.*` are the non-throwing, non-reshaping passthrough versions.

  /**
   * One page of entity instances of a type for the Explorer Data table
   * (`GET /explore/kgs/{kg}/types/{type}/records`). Keyset-paginated by entity
   * URI: pass the previous page's `next_cursor` as `cursor`. `limit` is clamped
   * server-side to 1..200 (default 50).
   */
  async exploreRecords(
    kg: string,
    typeName: string,
    opts: { limit?: number; cursor?: string } = {},
  ): Promise<TypeRecordsPage> {
    const qs = new URLSearchParams();
    if (opts.limit != null) qs.set("limit", String(opts.limit));
    if (opts.cursor) qs.set("cursor", opts.cursor);
    const query = qs.toString() ? `?${qs.toString()}` : "";
    return this.request<TypeRecordsPage>(
      "GET",
      this.pExploreRecords(kg, typeName, query),
      undefined,
      30_000,
    );
  }

  /** Undirected type→type edges for the Explorer overview graph
   *  (`GET /explore/kgs/{kg}/type-edges`). Returns `[{source, target, weight}]`. */
  async exploreTypeEdges(kg: string): Promise<TypeEdge[]> {
    const data = await this.request<unknown>(
      "GET",
      this.pExploreTypeEdges(kg),
      undefined,
      30_000,
    );
    return Array.isArray(data) ? (data as TypeEdge[]) : [];
  }

  /** Infer + persist normalization rules for a type's predicates, returned ranked
   *  by confidence desc (`POST /normalize/suggest?kg&type`). */
  async normalizeSuggest(kg: string, type: string): Promise<NormalizationRule[]> {
    const qs = new URLSearchParams({ kg, type }).toString();
    const data = await this.request<unknown>(
      "POST",
      this.pNormalizeSuggest(`?${qs}`),
      undefined,
      60_000,
    );
    return Array.isArray(data) ? (data as NormalizationRule[]) : [];
  }

  /** List stored normalization rules, optionally filtered by KG and/or status
   *  (`GET /normalize/rules?kg&status`). */
  async normalizeRules(
    opts: { kg?: string; status?: string } = {},
  ): Promise<NormalizationRule[]> {
    const qs = new URLSearchParams();
    if (opts.kg) qs.set("kg", opts.kg);
    if (opts.status) qs.set("status", opts.status);
    const query = qs.toString() ? `?${qs.toString()}` : "";
    const data = await this.request<unknown>(
      "GET",
      this.pNormalizeRules(query),
      undefined,
      15_000,
    );
    return Array.isArray(data) ? (data as NormalizationRule[]) : [];
  }

  /** Confirm a suggested normalization rule (`POST /normalize/rules/{id}/confirm`). */
  async normalizeConfirmRule(ruleId: string): Promise<NormalizationRule> {
    return this.request<NormalizationRule>(
      "POST",
      this.pNormalizeRule(ruleId, "confirm"),
      undefined,
      15_000,
    );
  }

  /** Reject a suggested normalization rule (`POST /normalize/rules/{id}/reject`). */
  async normalizeRejectRule(ruleId: string): Promise<NormalizationRule> {
    return this.request<NormalizationRule>(
      "POST",
      this.pNormalizeRule(ruleId, "reject"),
      undefined,
      15_000,
    );
  }

  /** Apply a confirmed normalization rule in the background; the server acks 202
   *  (`POST /normalize/rules/{id}/apply`). */
  async normalizeApplyRule(ruleId: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      "POST",
      this.pNormalizeRule(ruleId, "apply"),
      {},
      60_000,
    );
  }

  /** Recommend ontology relationships/changes for the active KG
   *  (`POST /ontology/recommend`). Body shape is passed through unchanged.
   *
   *  NOTE: this targets the *premium* ontology-recommender route, which is only
   *  mounted on deployments carrying the proprietary layer. It 404s on a bare
   *  OSS deployment. */
  async ontologyRecommend(
    body: Record<string, unknown> = {},
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      "POST",
      this.pOntologyRecommend(),
      body,
      60_000,
    );
  }

  // --- API source registry (ONTA-2xx) ------------------------------------- #
  // Typed convenience over the raw `api-sources` routes. The tenant is the
  // client's configured tenant (`this.tenant`); the backend authorizes it and
  // returns 403 for an unowned tenant / a global-slug edit. Secrets are
  // write-only on create/update and never returned by list/get/test.

  /** List global (read-only) + tenant-custom (editable) sources. */
  async apiSourcesList(): Promise<ApiSourceSummary[]> {
    const data = await this.request<unknown>("GET", this.pApiSources(), undefined, 15_000);
    return Array.isArray(data) ? (data as ApiSourceSummary[]) : [];
  }

  /** Read one source's full spec (secrets redacted) + `has_secret` / `editable`. */
  async apiSourcesGet(slug: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("GET", this.pApiSource(slug), undefined, 15_000);
  }

  /** Create a tenant-custom source. `body` is `{spec, secrets?, enabled?}`. */
  async apiSourcesCreate(body: ApiSourceWrite): Promise<ApiSourceSummary> {
    return this.request<ApiSourceSummary>("POST", this.pApiSources(), body, 15_000);
  }

  /** Edit a tenant-custom source (spec / enabled / secrets). Global slug => 403. */
  async apiSourcesUpdate(slug: string, body: ApiSourceWrite): Promise<ApiSourceSummary> {
    return this.request<ApiSourceSummary>("PATCH", this.pApiSource(slug), body, 15_000);
  }

  /** Convenience: enable/disable a tenant-custom source (folds into update). */
  async apiSourcesSetEnabled(slug: string, enabled: boolean): Promise<ApiSourceSummary> {
    return this.apiSourcesUpdate(slug, { enabled });
  }

  /** Delete a tenant-custom source (+ its secrets). Global slug => 403. */
  async apiSourcesDelete(slug: string): Promise<{ ok: boolean }> {
    return this.request<{ ok: boolean }>("DELETE", this.pApiSource(slug), undefined, 15_000);
  }

  /** Validate a spec (no write). `body` is `{spec}`. */
  async apiSourcesValidate(spec: Record<string, unknown>): Promise<ApiSourceValidateResult> {
    return this.request<ApiSourceValidateResult>(
      "POST",
      this.pApiSourcesValidate(),
      { spec },
      15_000,
    );
  }

  /** Run ONE smoke request (no write, no persist). Provide `slug` OR inline
   *  `spec` (+ `sample_params`). A secret is never echoed. */
  async apiSourcesTest(body: {
    slug?: string;
    spec?: Record<string, unknown>;
    sample_params?: Record<string, string>;
  }): Promise<ApiSourceTestResult> {
    return this.request<ApiSourceTestResult>("POST", this.pApiSourcesTest(), body, 30_000);
  }
}
