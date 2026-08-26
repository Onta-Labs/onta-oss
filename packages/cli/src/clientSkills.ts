/** Skills, functions, entity detail, and workspace methods for the SDK client.

Path builders live here (not on ClientHttp) so that file stays under the
size budget. RawApi reaches the same builders via ``this.client.pSkills`` etc.
*/
import { ClientExplore } from "./clientExplore.js";
import type { TypeSummary } from "./clientTypes.js";
import type {
  EntityDetail,
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

export class ClientSkills extends ClientExplore {
  // --- Path builders (shared with RawApi) --------------------------------- #

  /** @internal */ pSkills(query?: string): string {
    return `${this.base()}/skills${query ?? ""}`;
  }
  /** @internal */ pSkillsValidate(): string {
    return `${this.base()}/skills/validate`;
  }
  /** @internal */ pSkillsPromptBlock(query?: string): string {
    return `${this.base()}/skills/prompt-block${query ?? ""}`;
  }
  /** @internal */ pSkill(typeName: string, slug: string): string {
    return `${this.base()}/skills/${encodeURIComponent(typeName)}/${encodeURIComponent(slug)}`;
  }
  /** @internal */ pFunctions(query?: string): string {
    return `${this.base()}/functions${query ?? ""}`;
  }
  /** @internal */ pFunction(name: string): string {
    return `${this.base()}/functions/${encodeURIComponent(name)}`;
  }
  /** @internal */ pFunctionInvoke(name: string): string {
    return `${this.pFunction(name)}/invoke`;
  }
  /** @internal */ pExploreEntity(kg: string, entityId: string): string {
    return `${this.base()}/explore/kgs/${encodeURIComponent(kg)}/entities/${encodeURIComponent(entityId)}`;
  }
  /** @internal */ pRecomputeStats(kg: string): string {
    return `${this.base()}/explore/kgs/${encodeURIComponent(kg)}/recompute-stats`;
  }

  // --- Skills ------------------------------------------------------------- #

  /** List resolved skills (`GET /graphs/{tenant}/skills`). */
  async listSkills(typeName?: string): Promise<SkillSummary[]> {
    const qs = typeName
      ? `?type_name=${encodeURIComponent(typeName)}`
      : "";
    const data = await this.request<unknown>(
      "GET",
      this.pSkills(qs),
      undefined,
      15_000,
    );
    return Array.isArray(data) ? (data as SkillSummary[]) : [];
  }

  /** Read one skill, full body (`GET …/skills/{type}/{slug}`). */
  async getSkill(typeName: string, slug: string): Promise<SkillDetail> {
    return this.request<SkillDetail>(
      "GET",
      this.pSkill(typeName, slug),
      undefined,
      15_000,
    );
  }

  /** Create or replace a tenant skill (`POST /graphs/{tenant}/skills`). */
  async createSkill(body: SkillWrite): Promise<SkillDetail> {
    return this.request<SkillDetail>("POST", this.pSkills(), body, 15_000);
  }

  /** Partially update a tenant skill (`PATCH …/skills/{type}/{slug}`). */
  async updateSkill(
    typeName: string,
    slug: string,
    body: SkillPatch,
  ): Promise<SkillDetail> {
    return this.request<SkillDetail>(
      "PATCH",
      this.pSkill(typeName, slug),
      body,
      15_000,
    );
  }

  /** Delete a tenant skill (`DELETE …/skills/{type}/{slug}`). */
  async deleteSkill(
    typeName: string,
    slug: string,
  ): Promise<{ ok: boolean }> {
    return this.request<{ ok: boolean }>(
      "DELETE",
      this.pSkill(typeName, slug),
      undefined,
      15_000,
    );
  }

  /** Validate a skill body with no write (`POST …/skills/validate`). */
  async validateSkill(body: SkillWrite): Promise<SkillValidateResult> {
    return this.request<SkillValidateResult>(
      "POST",
      this.pSkillsValidate(),
      body,
      15_000,
    );
  }

  /**
   * Exact text an agent is handed for these types
   * (`GET …/skills/prompt-block`). Clients must not re-render locally.
   */
  async skillsPromptBlock(typeNames?: string[]): Promise<SkillsPromptBlock> {
    const qs = new URLSearchParams();
    for (const t of typeNames ?? []) qs.append("type_name", t);
    const query = qs.toString() ? `?${qs.toString()}` : "";
    return this.request<SkillsPromptBlock>(
      "GET",
      this.pSkillsPromptBlock(query),
      undefined,
      15_000,
    );
  }

  // --- Functions ---------------------------------------------------------- #

  /** List function attachments (`GET /graphs/{tenant}/functions`). */
  async listFunctions(entityType?: string): Promise<FunctionRef[]> {
    const qs = entityType
      ? `?entity_type=${encodeURIComponent(entityType)}`
      : "";
    const data = await this.request<unknown>(
      "GET",
      this.pFunctions(qs),
      undefined,
      15_000,
    );
    return Array.isArray(data) ? (data as FunctionRef[]) : [];
  }

  /** Attach an endpoint URL to a type (`POST /graphs/{tenant}/functions`). */
  async registerFunction(body: FunctionRegister): Promise<FunctionRegisterResult> {
    return this.request<FunctionRegisterResult>(
      "POST",
      this.pFunctions(),
      body,
      15_000,
    );
  }

  /** Invoke a registered function (`POST …/functions/{name}/invoke`). */
  async invokeFunction(
    name: string,
    body: FunctionInvokeRequest,
  ): Promise<FunctionInvokeResult> {
    return this.request<FunctionInvokeResult>(
      "POST",
      this.pFunctionInvoke(name),
      body,
      60_000,
    );
  }

  /**
   * Delete a function attachment (`DELETE …/functions/{name}`).
   * Optional `entityType` is forwarded as `?entity_type=`.
   */
  async deleteFunction(
    name: string,
    opts: { entityType?: string } = {},
  ): Promise<Record<string, unknown>> {
    const qs = opts.entityType
      ? `?entity_type=${encodeURIComponent(opts.entityType)}`
      : "";
    return this.request<Record<string, unknown>>(
      "DELETE",
      `${this.pFunction(name)}${qs}`,
      undefined,
      15_000,
    );
  }

  // --- Explore / workspace ------------------------------------------------ #

  /** Alias of {@link typeSummary} — same `GET …/types/{type}/summary` route. */
  async exploreSummary(kg: string, typeName: string): Promise<TypeSummary> {
    return this.typeSummary(kg, typeName);
  }

  /** Entity detail (`GET …/explore/kgs/{kg}/entities/{id}`). */
  async getEntity(kg: string, entityId: string): Promise<EntityDetail> {
    return this.request<EntityDetail>(
      "GET",
      this.pExploreEntity(kg, entityId),
      undefined,
      30_000,
    );
  }

  /** Create a workspace (`POST /v1/me/tenants`). Empty body mints Untitled N. */
  async createTenant(body: { label?: string; id?: string } = {}): Promise<TenantInfo> {
    return this.request<TenantInfo>("POST", this.pTenants(), body, 15_000);
  }

  /**
   * Schedule a type-stats recompute (`POST …/explore/kgs/{kg}/recompute-stats`).
   * Rewrites the stats graph, not instance data.
   */
  async recomputeStats(kg: string): Promise<RecomputeStatsResult> {
    return this.request<RecomputeStatsResult>(
      "POST",
      this.pRecomputeStats(kg),
      {},
      30_000,
    );
  }
}
