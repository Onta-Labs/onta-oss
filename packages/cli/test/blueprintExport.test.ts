import { afterEach, describe, expect, it, vi } from "vitest";
import { Client } from "../src/client.js";

const BASE = "https://api.example.test";
const TENANT = "acme-tenant";
const API_KEY = "test-key-123";
const PREFIX = `${BASE}/graphs/${TENANT}`;

type FetchArgs = { url: string; init: RequestInit };

function installFetch(response: Response): { calls: FetchArgs[] } {
  const calls: FetchArgs[] = [];
  const spy = vi.fn(async (input: unknown, init?: RequestInit) => {
    calls.push({ url: String(input), init: init ?? {} });
    return response;
  });
  vi.stubGlobal("fetch", spy);
  return { calls };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function makeClient(): Client {
  return new Client({ apiKey: API_KEY, baseUrl: BASE, tenant: TENANT });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("blueprint export — canonical route via shared SDK", () => {
  it("exportBlueprint → POST /kgs/{kg}/blueprint/export", async () => {
    const payload = {
      kg: "clinical-trials",
      manifest: { id: "infona/clinical-trials" },
      files: { "blueprint.yaml": "schema_version: \"1\"\n" },
    };
    const { calls } = installFetch(jsonResponse(payload));
    const got = await makeClient().exportBlueprint("clinical-trials");
    expect(got).toEqual(payload);
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.url).toBe(`${PREFIX}/kgs/clinical-trials/blueprint/export`);
  });

  it("validateBlueprint → POST /blueprint/validate", async () => {
    const { calls } = installFetch(jsonResponse({ errors: [] }));
    const got = await makeClient().validateBlueprint({
      manifest: { id: "infona/clinical-trials" },
    });
    expect(got).toEqual({ errors: [] });
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.url).toBe(`${PREFIX}/blueprint/validate`);
  });

  it("raw methods share the same path builders", async () => {
    const { calls } = installFetch(jsonResponse({ errors: [] }));
    const client = makeClient();
    await client.raw.exportBlueprint("clinical-trials", {});
    await client.raw.validateBlueprint({ files: { "blueprint.yaml": "x" } });
    expect(calls[0]!.url).toBe(`${PREFIX}/kgs/clinical-trials/blueprint/export`);
    expect(calls[1]!.url).toBe(`${PREFIX}/blueprint/validate`);
  });

  it("instance exportKg stays on GET /kgs/{kg}/export", async () => {
    const { calls } = installFetch(jsonResponse({ kg: "clinical-trials", types: [] }));
    await makeClient().exportKg("clinical-trials");
    expect(calls[0]!.init.method).toBe("GET");
    expect(calls[0]!.url).toContain(`${PREFIX}/kgs/clinical-trials/export`);
    expect(calls[0]!.url).not.toContain("blueprint");
  });
});
