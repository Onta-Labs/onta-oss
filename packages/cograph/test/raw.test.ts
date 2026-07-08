import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  Client,
  CographError,
  isTerminalJobStatus,
  TERMINAL_JOB_STATUSES,
} from "../src/client.js";

// --- fetch mock -------------------------------------------------------------- #
// Every test installs a single fetch spy and asserts on the (url, init) it was
// called with. We never hit the network. The mock returns a real `Response` so
// the raw-passthrough contract (return the Response verbatim) is exercised
// against the genuine WHATWG type, not a stub.

const BASE = "https://api.example.test";
const TENANT = "acme-tenant";
const API_KEY = "test-key-123";

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

function makeClient(): Client {
  return new Client({ apiKey: API_KEY, baseUrl: BASE, tenant: TENANT });
}

/** The tenant-scoped prefix every per-tenant op must carry. */
const PREFIX = `${BASE}/graphs/${TENANT}`;

function headerOf(init: RequestInit, name: string): string | undefined {
  const h = (init.headers ?? {}) as Record<string, string>;
  return h[name];
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("requestRaw — base contract", () => {
  it("sends method, body, X-API-Key, JSON content-type and the encoded path", async () => {
    const { calls } = installFetch(new Response("{}", { status: 200 }));
    const client = makeClient();

    const res = await client.raw.agent({ message: "hi" });

    expect(res).toBeInstanceOf(Response);
    expect(calls).toHaveLength(1);
    const { url, init } = calls[0]!;
    expect(url).toBe(`${PREFIX}/agent`);
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ message: "hi" }));
    expect(headerOf(init, "X-API-Key")).toBe(API_KEY);
    expect(headerOf(init, "Content-Type")).toBe("application/json");
  });

  it("omits X-API-Key when the client has no key", async () => {
    const { calls } = installFetch(new Response("{}", { status: 200 }));
    const client = new Client({ baseUrl: BASE, tenant: TENANT });
    await client.raw.kgs();
    expect(headerOf(calls[0]!.init, "X-API-Key")).toBeUndefined();
  });

  it("does not stringify a string body (passes it through verbatim)", async () => {
    const { calls } = installFetch(new Response("{}", { status: 200 }));
    const client = makeClient();
    await client.raw.ingest("raw text body");
    expect(calls[0]!.init.body).toBe("raw text body");
  });

  it("merges per-call header overrides over the defaults", async () => {
    const { calls } = installFetch(new Response("{}", { status: 200 }));
    const client = makeClient();
    await client.raw.kgs({ headers: { "X-Trace": "abc" } });
    expect(headerOf(calls[0]!.init, "X-Trace")).toBe("abc");
    expect(headerOf(calls[0]!.init, "X-API-Key")).toBe(API_KEY);
  });
});

describe("non-throwing + non-reshaping passthrough", () => {
  it("returns a non-2xx as a Response WITHOUT throwing", async () => {
    installFetch(new Response("nope", { status: 500 }));
    const client = makeClient();

    // No try/catch: a 5xx must resolve, not reject.
    const res = await client.raw.enrichJobs();
    expect(res).toBeInstanceOf(Response);
    expect(res.status).toBe(500);
    expect(await res.text()).toBe("nope");
  });

  it("returns a 404 Response WITHOUT throwing (contrast: typed method throws)", async () => {
    // Same backend 404 drives both calls; the raw method resolves, the typed
    // method rejects with a CographError carrying the status + body.
    installFetch(new Response('{"detail":"not found"}', { status: 404 }));
    const client = makeClient();

    const raw = await client.raw.enrichJob("missing");
    expect(raw.status).toBe(404);
    expect(await raw.text()).toBe('{"detail":"not found"}'); // body UNPARSED / unreshaped

    await expect(client.enrichJob("missing")).rejects.toBeInstanceOf(CographError);
  });

  it("does not reshape a 2xx body — caller gets the raw envelope", async () => {
    // listKgs() unwraps {kgs:[...]} to the inner array; raw.kgs() must NOT.
    const envelope = { kgs: [{ name: "k1" }, { name: "k2" }] };
    installFetch(
      new Response(JSON.stringify(envelope), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const client = makeClient();

    const res = await client.raw.kgs();
    expect(await res.json()).toEqual(envelope); // envelope intact, not unwrapped

    // sanity: the typed method DOES unwrap to the inner array.
    installFetch(
      new Response(JSON.stringify(envelope), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const typed = await client.listKgs();
    expect(typed).toEqual(envelope.kgs);
  });

  it("still rejects on a network error (no Response to return)", async () => {
    const spy = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    vi.stubGlobal("fetch", spy);
    const client = makeClient();
    await expect(client.raw.kgs()).rejects.toBeInstanceOf(CographError);
  });

  it("maps an abort/timeout to CographError (no Response to return)", async () => {
    // When the timeout fires, the AbortController aborts the fetch and the
    // platform rejects with a DOMException/Error named "AbortError". requestRaw
    // surfaces that one case as a thrown CographError (there is no Response).
    const spy = vi.fn(async (_input: unknown, init?: RequestInit) => {
      const signal = init?.signal;
      throw await new Promise<never>((_resolve, reject) => {
        const fail = () => {
          const err = new Error("The operation was aborted");
          err.name = "AbortError";
          reject(err);
        };
        if (signal?.aborted) fail();
        else signal?.addEventListener("abort", fail, { once: true });
      });
    });
    vi.stubGlobal("fetch", spy);
    const client = makeClient();
    // 0ms timeout → controller aborts on the next tick → fetch rejects AbortError.
    await expect(client.raw.kgs({ timeoutMs: 0 })).rejects.toBeInstanceOf(CographError);
  });
});

describe("canonical paths + methods for every covered op", () => {
  // One table-driven assertion per op: invoke the raw method, assert the exact
  // HTTP method + canonical URL the SDK built. Bodies are covered above.
  const ENC = encodeURIComponent;

  type Case = { name: string; run: (c: Client) => Promise<Response>; method: string; url: string };

  const cases: Case[] = [
    { name: "agent", run: (c) => c.raw.agent({}), method: "POST", url: `${PREFIX}/agent` },
    { name: "ask", run: (c) => c.raw.ask({}), method: "POST", url: `${PREFIX}/ask` },
    { name: "ingest", run: (c) => c.raw.ingest({}), method: "POST", url: `${PREFIX}/ingest` },
    {
      name: "ingestCsvSchema",
      run: (c) => c.raw.ingestCsvSchema({}),
      method: "POST",
      url: `${PREFIX}/ingest/csv/schema`,
    },
    {
      name: "ingestCsvRows",
      run: (c) => c.raw.ingestCsvRows({}),
      method: "POST",
      url: `${PREFIX}/ingest/csv/rows`,
    },
    {
      name: "enrichCreateJob",
      run: (c) => c.raw.enrichCreateJob({}),
      method: "POST",
      url: `${PREFIX}/enrich/jobs`,
    },
    { name: "enrichJobs", run: (c) => c.raw.enrichJobs(), method: "GET", url: `${PREFIX}/enrich/jobs` },
    { name: "jobs", run: (c) => c.raw.jobs(), method: "GET", url: `${PREFIX}/jobs` },
    {
      name: "actionFindMergeDuplicates",
      run: (c) => c.raw.actionFindMergeDuplicates({}),
      method: "POST",
      url: `${PREFIX}/actions/find-merge-duplicates`,
    },
    {
      name: "actionEnrich",
      run: (c) => c.raw.actionEnrich({}),
      method: "POST",
      url: `${PREFIX}/actions/enrich`,
    },
    {
      name: "actionSuggestRelationships",
      run: (c) => c.raw.actionSuggestRelationships({}),
      method: "POST",
      url: `${PREFIX}/actions/suggest-relationships`,
    },
    {
      name: "enrichJob",
      run: (c) => c.raw.enrichJob("job 1"),
      method: "GET",
      url: `${PREFIX}/enrich/jobs/${ENC("job 1")}`,
    },
    {
      name: "enrichConflicts",
      run: (c) => c.raw.enrichConflicts("j1"),
      method: "GET",
      url: `${PREFIX}/enrich/jobs/j1/conflicts`,
    },
    {
      name: "enrichApply",
      run: (c) => c.raw.enrichApply("j1", {}),
      method: "POST",
      url: `${PREFIX}/enrich/jobs/j1/apply`,
    },
    {
      name: "enrichCancel",
      run: (c) => c.raw.enrichCancel("j1"),
      method: "DELETE",
      url: `${PREFIX}/enrich/jobs/j1`,
    },
    {
      name: "schedules",
      run: (c) => c.raw.schedules(),
      method: "GET",
      url: `${PREFIX}/schedules`,
    },
    {
      name: "createSchedule",
      run: (c) => c.raw.createSchedule({}),
      method: "POST",
      url: `${PREFIX}/schedules`,
    },
    {
      name: "updateSchedule",
      run: (c) => c.raw.updateSchedule("s 1", {}),
      method: "PATCH",
      url: `${PREFIX}/schedules/${ENC("s 1")}`,
    },
    {
      name: "deleteSchedule",
      run: (c) => c.raw.deleteSchedule("s 1"),
      method: "DELETE",
      url: `${PREFIX}/schedules/${ENC("s 1")}`,
    },
    {
      name: "ontologyTypes",
      run: (c) => c.raw.ontologyTypes(),
      method: "GET",
      url: `${PREFIX}/ontology/types`,
    },
    {
      name: "ontologyResolve",
      run: (c) => c.raw.ontologyResolve({}),
      method: "POST",
      url: `${PREFIX}/ontology/resolve`,
    },
    {
      name: "ontologyRecommend",
      run: (c) => c.raw.ontologyRecommend({}),
      method: "POST",
      url: `${PREFIX}/ontology/recommend`,
    },
    {
      name: "ontologyApply",
      run: (c) => c.raw.ontologyApply({}),
      method: "POST",
      url: `${PREFIX}/ontology/apply`,
    },
    {
      name: "ontologyApplyBatch",
      run: (c) => c.raw.ontologyApplyBatch({ changes: [] }),
      method: "POST",
      url: `${PREFIX}/ontology/apply/batch`,
    },
    { name: "kgs", run: (c) => c.raw.kgs(), method: "GET", url: `${PREFIX}/kgs` },
    { name: "createKg", run: (c) => c.raw.createKg({}), method: "POST", url: `${PREFIX}/kgs` },
    {
      name: "deleteKg",
      run: (c) => c.raw.deleteKg("my kg"),
      method: "DELETE",
      url: `${PREFIX}/kgs/${ENC("my kg")}`,
    },
    {
      name: "exploreSummary",
      run: (c) => c.raw.exploreSummary("kg1", "Person"),
      method: "GET",
      url: `${PREFIX}/explore/kgs/kg1/types/Person/summary`,
    },
    {
      name: "exploreTypeEdges",
      run: (c) => c.raw.exploreTypeEdges("kg1"),
      method: "GET",
      url: `${PREFIX}/explore/kgs/kg1/type-edges`,
    },
    {
      name: "typeCounts",
      run: (c) => c.raw.typeCounts("kg1"),
      method: "GET",
      url: `${PREFIX}/kgs/kg1/type-counts`,
    },
    {
      name: "normalizeCreateRule",
      run: (c) => c.raw.normalizeCreateRule({}),
      method: "POST",
      url: `${PREFIX}/normalize/rules`,
    },
    {
      name: "createTenant",
      run: (c) => c.raw.createTenant({}),
      method: "POST",
      url: `${BASE}/v1/me/tenants`,
    },
    {
      name: "deleteTenant",
      run: (c) => c.raw.deleteTenant("t 1"),
      method: "DELETE",
      url: `${BASE}/v1/me/tenants/${ENC("t 1")}`,
    },
    { name: "tenants", run: (c) => c.raw.tenants(), method: "GET", url: `${BASE}/v1/me/tenants` },
    // ONTA-178: the canonical semantic instance search — one route for every
    // interface (the MCP `search` tool rides this exact path via the SDK).
    { name: "search", run: (c) => c.raw.search({ query: "q" }), method: "POST", url: `${PREFIX}/search` },
  ];

  for (const tc of cases) {
    it(`${tc.name} → ${tc.method} ${tc.url.replace(BASE, "")}`, async () => {
      const { calls } = installFetch(new Response("{}", { status: 200 }));
      await tc.run(makeClient());
      expect(calls).toHaveLength(1);
      expect(calls[0]!.init.method).toBe(tc.method);
      expect(calls[0]!.url).toBe(tc.url);
      // Tenant prefix invariant for per-tenant ops (tenant CRUD is account-level).
      if (!tc.url.includes("/v1/me/tenants")) {
        expect(calls[0]!.url.startsWith(PREFIX)).toBe(true);
      }
    });
  }
});

describe("missing methods build URLs incl. query params + encoding", () => {
  const ENC = encodeURIComponent;

  it("exploreRecords encodes path segments and forwards limit + cursor", async () => {
    const { calls } = installFetch(new Response("{}", { status: 200 }));
    const client = makeClient();
    await client.raw.exploreRecords("kg/1", "Type Name", {
      limit: 25,
      cursor: "urn:x?y&z",
    });
    const url = calls[0]!.url;
    expect(url).toBe(
      `${PREFIX}/explore/kgs/${ENC("kg/1")}/types/${ENC("Type Name")}/records?limit=25&cursor=${ENC("urn:x?y&z")}`,
    );
    expect(calls[0]!.init.method).toBe("GET");
  });

  it("exploreRecords omits the query string entirely when no opts given", async () => {
    const { calls } = installFetch(new Response("{}", { status: 200 }));
    const client = makeClient();
    await client.raw.exploreRecords("kg1", "Person");
    expect(calls[0]!.url).toBe(`${PREFIX}/explore/kgs/kg1/types/Person/records`);
  });

  it("exploreTypeEdges builds the type-edges path", async () => {
    const { calls } = installFetch(new Response("{}", { status: 200 }));
    await makeClient().raw.exploreTypeEdges("kg with space");
    expect(calls[0]!.url).toBe(`${PREFIX}/explore/kgs/${ENC("kg with space")}/type-edges`);
  });

  it("exploreSearch builds ?kg&q&kind with encoding", async () => {
    const { calls } = installFetch(new Response("[]", { status: 200 }));
    await makeClient().raw.exploreSearch("kg1", "a&b c", "attr");
    const url = new URL(calls[0]!.url);
    expect(url.pathname.endsWith("/explore/search")).toBe(true);
    expect(url.searchParams.get("kg")).toBe("kg1");
    expect(url.searchParams.get("q")).toBe("a&b c");
    expect(url.searchParams.get("kind")).toBe("attr");
  });

  it("normalizeSuggest → POST /normalize/suggest?kg&type", async () => {
    const { calls } = installFetch(new Response("[]", { status: 200 }));
    await makeClient().raw.normalizeSuggest("kg1", "Person");
    const url = new URL(calls[0]!.url);
    expect(calls[0]!.init.method).toBe("POST");
    expect(url.pathname.endsWith("/normalize/suggest")).toBe(true);
    expect(url.searchParams.get("kg")).toBe("kg1");
    expect(url.searchParams.get("type")).toBe("Person");
  });

  it("normalizeRules → GET /normalize/rules?kg&status (filters)", async () => {
    const { calls } = installFetch(new Response("[]", { status: 200 }));
    await makeClient().raw.normalizeRules({ kg: "kg1", status: "suggested" });
    const url = new URL(calls[0]!.url);
    expect(calls[0]!.init.method).toBe("GET");
    expect(url.searchParams.get("kg")).toBe("kg1");
    expect(url.searchParams.get("status")).toBe("suggested");
  });

  it("normalizeRules → GET /normalize/rules with NO query when unfiltered", async () => {
    const { calls } = installFetch(new Response("[]", { status: 200 }));
    await makeClient().raw.normalizeRules();
    expect(calls[0]!.url).toBe(`${PREFIX}/normalize/rules`);
  });

  it("normalize confirm/reject/apply encode the rule id in the path", async () => {
    for (const [action, fn] of [
      ["confirm", (c: Client) => c.raw.normalizeConfirmRule("r/1")],
      ["reject", (c: Client) => c.raw.normalizeRejectRule("r/1")],
      ["apply", (c: Client) => c.raw.normalizeApplyRule("r/1")],
    ] as const) {
      const { calls } = installFetch(new Response("{}", { status: 200 }));
      await fn(makeClient());
      expect(calls[0]!.init.method).toBe("POST");
      expect(calls[0]!.url).toBe(`${PREFIX}/normalize/rules/${ENC("r/1")}/${action}`);
      vi.unstubAllGlobals();
    }
  });

  it("jobs → GET /jobs?category when filtered, no query when not", async () => {
    {
      const { calls } = installFetch(new Response("[]", { status: 200 }));
      await makeClient().raw.jobs({ category: "dedupe" });
      const url = new URL(calls[0]!.url);
      expect(calls[0]!.init.method).toBe("GET");
      expect(url.pathname.endsWith("/jobs")).toBe(true);
      expect(url.searchParams.get("category")).toBe("dedupe");
    }
    vi.unstubAllGlobals();
    {
      const { calls } = installFetch(new Response("[]", { status: 200 }));
      await makeClient().raw.jobs();
      expect(calls[0]!.url).toBe(`${PREFIX}/jobs`);
    }
  });

  it("ontologyRecommend → POST /ontology/recommend", async () => {
    const { calls } = installFetch(new Response("{}", { status: 200 }));
    await makeClient().raw.ontologyRecommend({ kg_name: "kg1" });
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.url).toBe(`${PREFIX}/ontology/recommend`);
    expect(calls[0]!.init.body).toBe(JSON.stringify({ kg_name: "kg1" }));
  });
});

describe("new typed parsed variants of the missing methods", () => {
  it("exploreRecords (typed) returns the parsed page shape", async () => {
    const page = {
      columns: ["name", "age"],
      rows: [{ id: "u1", name: "Ada", age: "36" }],
      total: 1,
      next_cursor: null,
    };
    installFetch(
      new Response(JSON.stringify(page), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const client = makeClient();
    const got = await client.exploreRecords("kg1", "Person", { limit: 10 });
    expect(got).toEqual(page);
  });

  it("exploreTypeEdges (typed) returns [] for a non-array body", async () => {
    installFetch(
      new Response(JSON.stringify({ unexpected: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const got = await makeClient().exploreTypeEdges("kg1");
    expect(got).toEqual([]);
  });

  it("normalizeSuggest (typed) returns the rule array", async () => {
    const rules = [
      { id: "r1", kg_name: "kg1", type_name: "Person", predicate: "p", rule_type: "strip_emoji", status: "suggested" },
    ];
    installFetch(
      new Response(JSON.stringify(rules), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const got = await makeClient().normalizeSuggest("kg1", "Person");
    expect(got).toEqual(rules);
  });

  it("typed missing method throws CographError on non-2xx", async () => {
    installFetch(new Response("boom", { status: 503 }));
    await expect(makeClient().exploreTypeEdges("kg1")).rejects.toBeInstanceOf(CographError);
  });

  it("search (typed, ONTA-178) maps opts to the canonical body and parses the envelope", async () => {
    // Locks the SDK↔route field mapping the MCP tool depends on:
    // kg → kg_name, type → type, topK → top_k, and the response envelope
    // (hits/count/degraded/top_k) passed through unreshaped.
    const envelope = {
      hits: [
        {
          entity_uri: "e:solar",
          attrs: { label: "Solar", type: "Speech" },
          snippet: "rooftop solar…",
          attr: "transcript",
          score: 0.032,
        },
      ],
      count: 1,
      degraded: false,
      top_k: 5,
    };
    const { calls } = installFetch(
      new Response(JSON.stringify(envelope), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const got = await makeClient().search("solar subsidies", {
      kg: "parliament",
      type: "Speech",
      topK: 5,
    });
    expect(got).toEqual(envelope);
    expect(calls[0]!.url).toBe(`${PREFIX}/search`);
    expect(calls[0]!.init.method).toBe("POST");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({
      query: "solar subsidies",
      kg_name: "parliament",
      type: "Speech",
      top_k: 5,
    });
  });

  it("search (typed) omits optional fields when not given", async () => {
    const { calls } = installFetch(
      new Response(JSON.stringify({ hits: [], count: 0, degraded: true, top_k: 10 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await makeClient().search("anything");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ query: "anything" });
  });

  it("search (typed) surfaces the disabled-deployment 503 as CographError", async () => {
    installFetch(
      new Response('{"detail":"… COGRAPH_SEMANTIC_INDEX_ENABLED …"}', { status: 503 }),
    );
    await expect(makeClient().search("x")).rejects.toBeInstanceOf(CographError);
  });

  it("ontologyApplyBatch (typed) wraps changes in {changes}, hits the canonical batch path, and passes the envelope through", async () => {
    // Locks the batch-apply contract the MCP `apply_ontology_changes` tool
    // rides: N changes → ONE POST to /ontology/apply/batch, body {changes:[...]},
    // per-change result envelope returned unreshaped.
    const changes = [
      { kind: "attribute", subject_type: "Person", name: "email", datatype_or_target: "string", action: "extend", confidence: 0.9, reason: "x" },
      { kind: "relationship", subject_type: "Person", name: "works_at", datatype_or_target: "Company", action: "create", confidence: 0.9, reason: "y" },
    ];
    const envelope = {
      results: [
        { change: changes[0], ok: true, operations: 1, error: "" },
        { change: changes[1], ok: true, operations: 3, error: "" },
      ],
      applied_count: 2,
      failed_count: 0,
      operations: 4,
      summary: "Applied 2/2 change(s)",
    };
    const { calls } = installFetch(
      new Response(JSON.stringify(envelope), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    // deliberately loosen the change type for the test literals
    const got = await makeClient().ontologyApplyBatch(changes as never);
    expect(got).toEqual(envelope);
    expect(calls).toHaveLength(1); // ONE round-trip for N changes
    expect(calls[0]!.url).toBe(`${PREFIX}/ontology/apply/batch`);
    expect(calls[0]!.init.method).toBe("POST");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ changes });
  });

  it("ontologyApplyBatch (typed) surfaces a non-2xx as CographError", async () => {
    installFetch(new Response("boom", { status: 500 }));
    await expect(makeClient().ontologyApplyBatch([] as never)).rejects.toBeInstanceOf(CographError);
  });
});

describe("waitForJob — bounded server-side long-poll", () => {
  const jobBody = (status: string) =>
    JSON.stringify({ id: "j1", tenant_id: TENANT, status });

  it("hits GET …/enrich/jobs/{id}/wait with the timeout_s query", async () => {
    const { calls } = installFetch(
      new Response(jobBody("running"), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const job = await makeClient().waitForJob("j1", 90);
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe(`${PREFIX}/enrich/jobs/j1/wait?timeout_s=90`);
    expect(calls[0]!.init.method).toBe("GET");
    // Thin pass-through: it returns the job envelope verbatim (running is a
    // valid, non-error timeout result — the typed method must NOT throw).
    expect(job.status).toBe("running");
  });

  it("omits the query string when no timeout is given", async () => {
    const { calls } = installFetch(
      new Response(jobBody("applied"), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await makeClient().waitForJob("j1");
    expect(calls[0]!.url).toBe(`${PREFIX}/enrich/jobs/j1/wait`);
  });

  it("URL-encodes the job id (path is built through the SDK, not hand-rolled)", async () => {
    const { calls } = installFetch(
      new Response(jobBody("applied"), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await makeClient().waitForJob("a/b c");
    expect(calls[0]!.url).toBe(`${PREFIX}/enrich/jobs/a%2Fb%20c/wait`);
  });

  it("raw.waitForJob returns the Response verbatim without throwing on 404", async () => {
    installFetch(new Response('{"detail":"job not found"}', { status: 404 }));
    const res = await makeClient().raw.waitForJob("missing", 30);
    expect(res.status).toBe(404);
    expect(await res.text()).toBe('{"detail":"job not found"}');
  });

  it("isTerminalJobStatus mirrors the backend terminal set", () => {
    expect(isTerminalJobStatus("queued")).toBe(false);
    expect(isTerminalJobStatus("running")).toBe(false);
    for (const s of TERMINAL_JOB_STATUSES) {
      expect(isTerminalJobStatus(s)).toBe(true);
    }
    expect([...TERMINAL_JOB_STATUSES].sort()).toEqual(
      ["applied", "cancelled", "failed", "review"].sort(),
    );
  });
});
