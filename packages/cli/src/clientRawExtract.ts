/** Raw / passthrough methods for the 3rd-party extract family (ONTA-553/554/555).

A sibling of `clientRaw.ts` — that file sits at its size pin, and this family is
a self-contained seam: the frozen `POST /ingest/dlt` execute route, the
`/extract-sources` persist CRUD, the connector catalog and a source's cadence.
{@link RawApi} extends this class, so callers still reach every method through
`client.raw.*` and nothing about the surface changes.

Each method returns the backend Response VERBATIM (no throw on non-2xx, no
reshape) and builds its path from the Client path builders, exactly like the
rest of the raw surface.
*/
import type { Client } from "./client.js";
import type { RawInit } from "./clientTypesExtra.js";

export class RawExtractApi {
  constructor(protected readonly client: Client) {}

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
}
