import { afterEach, describe, expect, it, vi } from "vitest";
import { InfonaError } from "@infona-ai/cli";
import type { DltIngestRequest } from "@infona-ai/cli";
import { ingestDltHandler } from "../src/index.js";

// ONTA-553 / COG-128: `ingest_dlt` posts the frozen `{source, map, kg}` body
// through the SDK's `ingestDlt` (the ONE `POST /graphs/{tenant}/ingest/dlt`
// route the CLI and Explorer ride). A missing `infona-client[dlt]` extra on
// the backend must surface the pip-install hint, never a fabricated success.

const FROZEN: DltIngestRequest = {
  source: {
    kind: "rest_api",
    base_url: "https://api.example.com",
    auth: { type: "bearer", secret_ref: "env:EXAMPLE_TOKEN" },
    resources: ["v1/contacts"],
    limit: 1000,
  },
  map: { "v1/contacts": { type: "Contact", id_field: "id" } },
  kg: "crm",
};

function stubClient(impl: (...args: unknown[]) => unknown) {
  const ingestDlt = vi.fn(impl);
  const client = { ingestDlt } as unknown as import("@infona-ai/cli").Client;
  return { client, ingestDlt };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ingest_dlt handler — input guards", () => {
  it("rejects a missing kg without calling the SDK", async () => {
    const { client, ingestDlt } = stubClient(() => {
      throw new Error("SDK ingestDlt must not be called without kg");
    });

    const res = await ingestDltHandler(
      { source: FROZEN.source, map: FROZEN.map },
      () => client,
    );

    expect(res.isError).toBe(true);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("kg");
    expect(ingestDlt).not.toHaveBeenCalled();
  });

  it("rejects an empty map without calling the SDK", async () => {
    const { client, ingestDlt } = stubClient(() => {
      throw new Error("SDK ingestDlt must not be called without map");
    });

    const res = await ingestDltHandler(
      { source: FROZEN.source, map: {}, kg: "crm" },
      () => client,
    );

    expect(res.isError).toBe(true);
    expect(ingestDlt).not.toHaveBeenCalled();
  });
});

describe("ingest_dlt handler — SDK forwarding", () => {
  it("POSTs the frozen {source, map, kg} body and reports row counts", async () => {
    const { client, ingestDlt } = stubClient(async () => ({
      rows_in: 3,
      entities_resolved: 3,
      triples_inserted: 9,
    }));

    const res = await ingestDltHandler(
      { source: FROZEN.source, map: FROZEN.map, kg: "crm" },
      () => client,
    );

    expect(res.isError).toBeUndefined();
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("3 rows in");
    expect(text).toContain("3 entities resolved");
    expect(text).toContain('9 triples inserted into "crm"');

    expect(ingestDlt).toHaveBeenCalledTimes(1);
    const [body] = ingestDlt.mock.calls[0]!;
    expect(body).toEqual({
      source: FROZEN.source,
      map: FROZEN.map,
      kg: "crm",
    });
  });

  it("accepts kg_name as an alias for kg", async () => {
    const { client, ingestDlt } = stubClient(async () => ({
      rows_in: 1,
      entities_resolved: 1,
      triples_inserted: 3,
    }));

    await ingestDltHandler(
      { source: FROZEN.source, map: FROZEN.map, kg_name: "people" },
      () => client,
    );

    const [body] = ingestDlt.mock.calls[0]!;
    expect(body).toMatchObject({ kg: "people" });
  });

  it("degrades with the pip-install hint when the dlt extra is missing", async () => {
    const { client } = stubClient(async () => {
      throw new InfonaError(
        "HTTP 503: dlt is not installed. Install the optional extra: pip install 'infona-client[dlt]'",
        { status: 503 },
      );
    });

    const res = await ingestDltHandler(
      { source: FROZEN.source, map: FROZEN.map, kg: "crm" },
      () => client,
    );

    expect(res.isError).toBe(true);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("pip install infona-client[dlt]");
    expect(text).not.toMatch(/rows in/i);
  });
});
