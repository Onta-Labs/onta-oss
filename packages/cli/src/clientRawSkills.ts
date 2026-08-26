/** Raw / passthrough methods for skills, functions, entity detail, stats.

{@link RawApi} extends this class (which extends {@link RawExtractApi}), so
callers still reach every method through ``client.raw.*``. Paths come from
the Client path builders; each method returns the backend Response VERBATIM.
*/
import { RawExtractApi } from "./clientRawExtract.js";
import type { RawInit } from "./clientTypesExtra.js";

export class RawSkillsApi extends RawExtractApi {
  // -- skills -------------------------------------------------------------- #

  /** `GET /graphs/{tenant}/skills?type_name`. */
  skills(opts: { typeName?: string } = {}, init?: RawInit): Promise<Response> {
    const qs = opts.typeName
      ? `?type_name=${encodeURIComponent(opts.typeName)}`
      : "";
    return this.client.requestRaw("GET", this.client.pSkills(qs), init);
  }

  /** `GET /graphs/{tenant}/skills/{type}/{slug}`. */
  skill(typeName: string, slug: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("GET", this.client.pSkill(typeName, slug), init);
  }

  /** `POST /graphs/{tenant}/skills` — create or replace a tenant skill. */
  createSkill(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pSkills(), { body, ...init });
  }

  /** `PATCH /graphs/{tenant}/skills/{type}/{slug}`. */
  updateSkill(
    typeName: string,
    slug: string,
    body: unknown,
    init?: RawInit,
  ): Promise<Response> {
    return this.client.requestRaw("PATCH", this.client.pSkill(typeName, slug), {
      body,
      ...init,
    });
  }

  /** `DELETE /graphs/{tenant}/skills/{type}/{slug}`. */
  deleteSkill(typeName: string, slug: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw(
      "DELETE",
      this.client.pSkill(typeName, slug),
      init,
    );
  }

  /** `POST /graphs/{tenant}/skills/validate`. */
  validateSkill(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pSkillsValidate(), {
      body,
      ...init,
    });
  }

  /** `GET /graphs/{tenant}/skills/prompt-block?type_name`. */
  skillsPromptBlock(typeNames?: string[], init?: RawInit): Promise<Response> {
    const qs = new URLSearchParams();
    for (const t of typeNames ?? []) qs.append("type_name", t);
    const query = qs.toString() ? `?${qs.toString()}` : "";
    return this.client.requestRaw(
      "GET",
      this.client.pSkillsPromptBlock(query),
      init,
    );
  }

  // -- functions ----------------------------------------------------------- #

  /** `GET /graphs/{tenant}/functions?entity_type`. */
  functions(opts: { entityType?: string } = {}, init?: RawInit): Promise<Response> {
    const qs = opts.entityType
      ? `?entity_type=${encodeURIComponent(opts.entityType)}`
      : "";
    return this.client.requestRaw("GET", this.client.pFunctions(qs), init);
  }

  /** `POST /graphs/{tenant}/functions`. */
  registerFunction(body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pFunctions(), {
      body,
      ...init,
    });
  }

  /** `POST /graphs/{tenant}/functions/{name}/invoke`. */
  invokeFunction(name: string, body: unknown, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pFunctionInvoke(name), {
      body,
      ...init,
    });
  }

  /** `DELETE /graphs/{tenant}/functions/{name}?entity_type=` (required). */
  deleteFunction(
    name: string,
    opts: { entityType: string },
    init?: RawInit,
  ): Promise<Response> {
    const qs = `?entity_type=${encodeURIComponent(opts.entityType)}`;
    return this.client.requestRaw(
      "DELETE",
      `${this.client.pFunction(name)}${qs}`,
      init,
    );
  }

  // -- explore / workspace ------------------------------------------------- #

  /** `GET /graphs/{tenant}/explore/kgs/{kg}/entities/{id}`. */
  exploreEntity(kg: string, entityId: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw(
      "GET",
      this.client.pExploreEntity(kg, entityId),
      init,
    );
  }

  /** `POST /graphs/{tenant}/explore/kgs/{kg}/recompute-stats`. */
  recomputeStats(kg: string, init?: RawInit): Promise<Response> {
    return this.client.requestRaw("POST", this.client.pRecomputeStats(kg), {
      body: {},
      ...init,
    });
  }
}
