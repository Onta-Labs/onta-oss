import { afterEach, describe, expect, it, vi } from "vitest";
import { Client, InfonaError } from "../src/client.js";

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

describe("skills — path builders + typed methods", () => {
  it("listSkills → GET /skills and optional type_name", async () => {
    const rows = [{ slug: "how-to-query", type_name: "SynthWidget", title: "Query" }];
    const { calls } = installFetch(jsonResponse(rows));
    const got = await makeClient().listSkills("SynthWidget");
    expect(got).toEqual(rows);
    expect(calls[0]!.init.method).toBe("GET");
    expect(calls[0]!.url).toBe(`${PREFIX}/skills?type_name=SynthWidget`);
  });

  it("getSkill / createSkill / updateSkill / deleteSkill hit canonical paths", async () => {
    const detail = { slug: "how-to-query", type_name: "SynthWidget", body: "# Ada" };
    const calls: FetchArgs[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: unknown, init?: RequestInit) => {
        calls.push({ url: String(input), init: init ?? {} });
        return jsonResponse(detail);
      }),
    );
    const c = makeClient();
    await c.getSkill("SynthWidget", "how-to-query");
    await c.createSkill({ type_name: "SynthWidget", slug: "how-to-query", body: "# Ada" });
    await c.updateSkill("SynthWidget", "how-to-query", { title: "Ada Example" });
    await c.deleteSkill("Synth Widget", "how to query");
    expect(calls.map((x) => x.init.method)).toEqual(["GET", "POST", "PATCH", "DELETE"]);
    expect(calls[0]!.url).toBe(`${PREFIX}/skills/SynthWidget/how-to-query`);
    expect(calls[1]!.url).toBe(`${PREFIX}/skills`);
    expect(calls[3]!.url).toBe(
      `${PREFIX}/skills/${encodeURIComponent("Synth Widget")}/${encodeURIComponent("how to query")}`,
    );
  });

  it("validateSkill → POST /skills/validate", async () => {
    const { calls } = installFetch(jsonResponse({ valid: true, errors: [] }));
    const got = await makeClient().validateSkill({
      type_name: "SynthWidget",
      slug: "s",
      body: "x",
    });
    expect(got.valid).toBe(true);
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.url).toBe(`${PREFIX}/skills/validate`);
  });

  it("skillsPromptBlock repeats type_name and does not invent text", async () => {
    const block = { text: "## SynthWidget\nAda Example", skill_count: 1, chars: 28 };
    const { calls } = installFetch(jsonResponse(block));
    const got = await makeClient().skillsPromptBlock(["SynthWidget", "Ada"]);
    expect(got).toEqual(block);
    expect(calls[0]!.url).toBe(
      `${PREFIX}/skills/prompt-block?type_name=SynthWidget&type_name=Ada`,
    );
  });

  it("typed skill methods throw InfonaError on non-2xx", async () => {
    installFetch(new Response("nope", { status: 404 }));
    await expect(makeClient().getSkill("SynthWidget", "missing")).rejects.toBeInstanceOf(
      InfonaError,
    );
  });
});

describe("functions — path builders + typed methods", () => {
  it("listFunctions / registerFunction / invokeFunction / deleteFunction", async () => {
    const { calls } = installFetch(jsonResponse([]));
    await makeClient().listFunctions("SynthWidget");
    expect(calls[0]!.url).toBe(`${PREFIX}/functions?entity_type=SynthWidget`);
    expect(calls[0]!.init.method).toBe("GET");
  });

  it("registerFunction POSTs the body to /functions", async () => {
    const body = {
      name: "widget-lookup",
      entity_type: "SynthWidget",
      endpoint_url: "https://example.test/fn",
    };
    const { calls } = installFetch(jsonResponse({ registered: "widget-lookup" }));
    await makeClient().registerFunction(body);
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.url).toBe(`${PREFIX}/functions`);
    expect(calls[0]!.init.body).toBe(JSON.stringify(body));
  });

  it("invokeFunction POSTs to /functions/{name}/invoke", async () => {
    const { calls } = installFetch(
      jsonResponse({ entity_uri: "e1", function: "widget-lookup", output: {}, duration_ms: 1 }),
    );
    await makeClient().invokeFunction("widget-lookup", {
      entity_uri: "https://graph.infona.ai/entities/SynthWidget/ada",
      kg_name: "demo-kg",
    });
    expect(calls[0]!.url).toBe(`${PREFIX}/functions/widget-lookup/invoke`);
    expect(calls[0]!.init.method).toBe("POST");
  });

  it("deleteFunction encodes name and always sends entity_type", async () => {
    const { calls } = installFetch(jsonResponse({ ok: true }));
    await makeClient().deleteFunction("widget lookup", { entityType: "SynthWidget" });
    expect(calls[0]!.init.method).toBe("DELETE");
    expect(calls[0]!.url).toBe(
      `${PREFIX}/functions/${encodeURIComponent("widget lookup")}?entity_type=SynthWidget`,
    );
  });
});

describe("explore / workspace — typed methods", () => {
  it("exploreSummary aliases typeSummary on the summary path", async () => {
    const summary = { name: "SynthWidget", description: "", parent_type: null, entity_count: 2, attributes: [], relationships: [] };
    const { calls } = installFetch(jsonResponse(summary));
    const got = await makeClient().exploreSummary("demo-kg", "SynthWidget");
    expect(got.entity_count).toBe(2);
    expect(calls[0]!.url).toBe(
      `${PREFIX}/explore/kgs/demo-kg/types/SynthWidget/summary`,
    );
  });

  it("getEntity encodes kg and entity id", async () => {
    const { calls } = installFetch(jsonResponse({ id: "e1", name: "Ada Example" }));
    await makeClient().getEntity("kg 1", "https://graph.infona.ai/entities/SynthWidget/ada");
    expect(calls[0]!.init.method).toBe("GET");
    expect(calls[0]!.url).toBe(
      `${PREFIX}/explore/kgs/${encodeURIComponent("kg 1")}/entities/${encodeURIComponent("https://graph.infona.ai/entities/SynthWidget/ada")}`,
    );
  });

  it("createTenant POSTs /v1/me/tenants (account-level, not /graphs)", async () => {
    const { calls } = installFetch(jsonResponse({ id: "ws-1", label: "Ada Example" }));
    const got = await makeClient().createTenant({ label: "Ada Example" });
    expect(got.label).toBe("Ada Example");
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.url).toBe(`${BASE}/v1/me/tenants`);
    expect(calls[0]!.url.startsWith(PREFIX)).toBe(false);
  });

  it("recomputeStats POSTs explore/…/recompute-stats", async () => {
    const { calls } = installFetch(jsonResponse({ status: "scheduled", kg: "demo-kg" }));
    const got = await makeClient().recomputeStats("demo-kg");
    expect(got.status).toBe("scheduled");
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.url).toBe(`${PREFIX}/explore/kgs/demo-kg/recompute-stats`);
  });
});

describe("raw passthrough shares the same path builders", () => {
  const ENC = encodeURIComponent;
  const cases: Array<{
    name: string;
    run: (c: Client) => Promise<Response>;
    method: string;
    url: string;
  }> = [
    {
      name: "skills",
      run: (c) => c.raw.skills({ typeName: "SynthWidget" }),
      method: "GET",
      url: `${PREFIX}/skills?type_name=SynthWidget`,
    },
    {
      name: "createSkill",
      run: (c) => c.raw.createSkill({ type_name: "SynthWidget" }),
      method: "POST",
      url: `${PREFIX}/skills`,
    },
    {
      name: "validateSkill",
      run: (c) => c.raw.validateSkill({}),
      method: "POST",
      url: `${PREFIX}/skills/validate`,
    },
    {
      name: "skillsPromptBlock",
      run: (c) => c.raw.skillsPromptBlock(["SynthWidget"]),
      method: "GET",
      url: `${PREFIX}/skills/prompt-block?type_name=SynthWidget`,
    },
    {
      name: "functions",
      run: (c) => c.raw.functions({ entityType: "SynthWidget" }),
      method: "GET",
      url: `${PREFIX}/functions?entity_type=SynthWidget`,
    },
    {
      name: "registerFunction",
      run: (c) => c.raw.registerFunction({ name: "f" }),
      method: "POST",
      url: `${PREFIX}/functions`,
    },
    {
      name: "invokeFunction",
      run: (c) => c.raw.invokeFunction("f", { kg_name: "kg" }),
      method: "POST",
      url: `${PREFIX}/functions/f/invoke`,
    },
    {
      name: "deleteFunction",
      run: (c) => c.raw.deleteFunction("f n", { entityType: "SynthWidget" }),
      method: "DELETE",
      url: `${PREFIX}/functions/${ENC("f n")}?entity_type=SynthWidget`,
    },
    {
      name: "exploreEntity",
      run: (c) => c.raw.exploreEntity("kg1", "e/1"),
      method: "GET",
      url: `${PREFIX}/explore/kgs/kg1/entities/${ENC("e/1")}`,
    },
    {
      name: "recomputeStats",
      run: (c) => c.raw.recomputeStats("kg1"),
      method: "POST",
      url: `${PREFIX}/explore/kgs/kg1/recompute-stats`,
    },
  ];

  for (const tc of cases) {
    it(`raw.${tc.name} → ${tc.method} ${tc.url.replace(BASE, "")}`, async () => {
      const { calls } = installFetch(new Response("{}", { status: 200 }));
      const res = await tc.run(makeClient());
      expect(res).toBeInstanceOf(Response);
      expect(calls).toHaveLength(1);
      expect(calls[0]!.init.method).toBe(tc.method);
      expect(calls[0]!.url).toBe(tc.url);
    });
  }

  it("raw does not throw on non-2xx (contrast typed methods)", async () => {
    installFetch(new Response("missing", { status: 404 }));
    const res = await makeClient().raw.skill("SynthWidget", "nope");
    expect(res.status).toBe(404);
  });
});
