import { afterEach, describe, expect, it, vi } from "vitest";
import { Client, splitBlueprintId } from "../src/client.js";

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

describe("blueprints — one path family", () => {
  it("splitBlueprintId requires namespace/name", () => {
    expect(splitBlueprintId("infona/clinical-trials")).toEqual({
      namespace: "infona",
      name: "clinical-trials",
    });
    expect(() => splitBlueprintId("clinical-trials")).toThrow(/namespace\/name/);
  });

  it("install / inspect / uninstall / fork hit canonical routes", async () => {
    const card = {
      status: "installed",
      blueprint_id: "infona/clinical-trials",
      sample_is_current: false,
    };
    const calls: FetchArgs[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: unknown, init?: RequestInit) => {
        calls.push({ url: String(input), init: init ?? {} });
        return jsonResponse(card);
      }),
    );
    const c = makeClient();
    await c.installBlueprint({ kg: "clinical-trials", manifest: { id: "infona/clinical-trials" } });
    await c.inspectBlueprint("infona/clinical-trials");
    await c.uninstallBlueprint("infona/clinical-trials");
    await c.forkBlueprint("infona/clinical-trials", { as: "acme/clinical-trials" });
    await c.extendBlueprint("infona/clinical-trials", { overlay: { concepts: [] } });
    await c.updateBlueprint("infona/clinical-trials", { manifest: { id: "infona/clinical-trials" } });
    await c.firstRunBlueprint("infona/clinical-trials", {});
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.url).toBe(`${PREFIX}/blueprints/install`);
    expect(calls[1]!.init.method).toBe("GET");
    expect(calls[1]!.url).toBe(`${PREFIX}/blueprints/infona/clinical-trials`);
    expect(calls[2]!.init.method).toBe("DELETE");
    expect(calls[2]!.url).toBe(`${PREFIX}/blueprints/infona/clinical-trials`);
    expect(calls[3]!.init.method).toBe("POST");
    expect(calls[3]!.url).toBe(`${PREFIX}/blueprints/infona/clinical-trials/fork`);
    expect(JSON.parse(String(calls[3]!.init.body))).toEqual({
      as: "acme/clinical-trials",
    });
    expect(calls[4]!.url).toBe(`${PREFIX}/blueprints/infona/clinical-trials/extend`);
    expect(calls[5]!.url).toBe(`${PREFIX}/blueprints/infona/clinical-trials/update`);
    expect(calls[6]!.url).toBe(`${PREFIX}/blueprints/infona/clinical-trials/first-run`);
  });

  it("raw passthrough uses the same builders", async () => {
    const { calls } = installFetch(new Response("{}", { status: 200 }));
    const c = makeClient();
    await c.raw.installBlueprint({ kg: "k" });
    await c.raw.blueprint("infona", "clinical-trials");
    await c.raw.uninstallBlueprint("infona", "clinical-trials");
    await c.raw.forkBlueprint("infona", "clinical-trials");
    await c.raw.extendBlueprint("infona", "clinical-trials", { overlay: {} });
    await c.raw.updateBlueprint("infona", "clinical-trials", { manifest: {} });
    await c.raw.firstRunBlueprint("infona", "clinical-trials", {});
    expect(calls.map((x) => [x.init.method, x.url])).toEqual([
      ["POST", `${PREFIX}/blueprints/install`],
      ["GET", `${PREFIX}/blueprints/infona/clinical-trials`],
      ["DELETE", `${PREFIX}/blueprints/infona/clinical-trials`],
      ["POST", `${PREFIX}/blueprints/infona/clinical-trials/fork`],
      ["POST", `${PREFIX}/blueprints/infona/clinical-trials/extend`],
      ["POST", `${PREFIX}/blueprints/infona/clinical-trials/update`],
      ["POST", `${PREFIX}/blueprints/infona/clinical-trials/first-run`],
    ]);
  });
});
