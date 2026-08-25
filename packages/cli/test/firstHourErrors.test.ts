import { describe, expect, it } from "vitest";
import {
  MSG_API_DOWN,
  MSG_LLM_KEY,
  MSG_NEO4J_DOWN,
  MSG_NOT_LOGGED_IN,
  diagnoseAskResult,
  emptyKgMessage,
  fetchHealthSnapshot,
  mapFirstHourError,
} from "../src/firstHourErrors.js";

describe("first-hour diagnoses", () => {
  it("1. API not running (ECONNREFUSED / fetch failed to localhost:8000)", () => {
    const viaFetch = mapFirstHourError({
      message:
        "Network error contacting http://localhost:8000/graphs/default/ask: fetch failed",
      baseUrl: "http://localhost:8000",
    });
    expect(viaFetch).toBe(MSG_API_DOWN);
    expect(viaFetch).toContain("./scripts/oss_up.sh");

    const viaRefused = mapFirstHourError({
      message: "connect ECONNREFUSED 127.0.0.1:8000",
      baseUrl: "http://127.0.0.1:8000",
    });
    expect(viaRefused).toBe(MSG_API_DOWN);
    expect(viaRefused).toContain("./scripts/oss_up.sh");
  });

  it("2. Neo4j down but API up (GET /health neo4j=false or status=degraded)", () => {
    const viaHealth = mapFirstHourError({
      message: "HTTP 500: Internal Server Error",
      status: 500,
      baseUrl: "http://localhost:8000",
      health: { ok: true, status: "degraded", neo4j: false },
    });
    expect(viaHealth).toBe(MSG_NEO4J_DOWN);
    expect(viaHealth).toContain("docker compose up -d neo4j");

    const viaText = mapFirstHourError({
      answer: "Could not answer: Neo4j GraphStore is not configured.",
    });
    expect(viaText).toContain("docker compose up -d neo4j");
  });

  it("2b. HTTP 503 /health JSON is Neo4j down, not API down", async () => {
    const orig = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({ status: "degraded", neo4j: false, backend: "neo4j" }),
        { status: 503, headers: { "Content-Type": "application/json" } },
      )) as typeof fetch;
    try {
      const snap = await fetchHealthSnapshot("http://localhost:8000");
      expect(snap).toEqual({ ok: true, status: "degraded", neo4j: false });
      expect(
        mapFirstHourError({
          status: 503,
          baseUrl: "http://localhost:8000",
          health: snap,
        }),
      ).toBe(MSG_NEO4J_DOWN);
    } finally {
      globalThis.fetch = orig;
    }
  });

  it("3. Missing LLM key when calling /ask", () => {
    const api =
      "Could not answer after 3 attempts. Last error: No LLM API key configured.";
    const msg = mapFirstHourError({ answer: api });
    expect(msg).toContain(api);
    expect(msg).toContain(MSG_LLM_KEY);
    expect(msg).toContain("set OPENROUTER_API_KEY in .env");
    expect(diagnoseAskResult(api)).toContain("OPENROUTER_API_KEY");
  });

  it("4. Empty / unknown KG", () => {
    const unknown = mapFirstHourError({
      status: 404,
      message: "HTTP 404",
      body: JSON.stringify({
        detail: {
          error: "kg_not_found",
          message: "Knowledge graph 'trials' does not exist in this workspace.",
          kg_name: "trials",
          available_kgs: [],
        },
      }),
    });
    expect(unknown).toBe(emptyKgMessage("trials"));
    expect(unknown).toContain("infona ingest examples/trials.csv --kg trials");

    const empty = diagnoseAskResult(
      "Knowledge graph 'trials' exists but contains no data yet, so there is nothing to query. Ingest data into it first.",
      "trials",
    );
    expect(empty).toBe(
      "No data in kg 'trials'. ingest a CSV: infona ingest examples/trials.csv --kg trials",
    );
    expect(empty).toContain("infona ingest examples/trials.csv --kg trials");
  });

  it("5. CLI pointed at hosted default without login", () => {
    const msg = mapFirstHourError({
      status: 401,
      message: 'HTTP 401: {"detail":"Not authenticated"}',
      body: '{"detail":"Not authenticated"}',
      baseUrl: "https://api.infona.ai",
      hasApiKey: false,
    });
    expect(msg).toBe(MSG_NOT_LOGGED_IN);
    expect(msg).toContain("infona init --local");
    expect(msg).toContain("infona login");
  });
});
