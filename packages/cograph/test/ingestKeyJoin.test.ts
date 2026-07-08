import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Client } from "../src/client.js";

// ONTA-250: the SDK forwards join-by-exact-key mode to the canonical
// `/ingest/csv/rows` route as `key_join` (snake_case per the route contract).
// It stays a THIN pass-through — the SDK never matches/merges itself; the server
// does. Asserted with an invented key (`sku`) so nothing overfits.

const BASE = "https://api.example.test";
const TENANT = "acme-tenant";
const API_KEY = "test-key-123";

type FetchArgs = { url: string; init: RequestInit };

/** Route the CSV two-step: schema inference returns a trivial mapping, then the
 *  rows POST echoes counts. Records every call so we can assert the rows body. */
function installCsvFetch(): { calls: FetchArgs[] } {
  const calls: FetchArgs[] = [];
  const spy = vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init: init ?? {} });
    if (url.endsWith("/ingest/csv/schema")) {
      return new Response(
        JSON.stringify({
          entity_type: "Widget",
          columns: [
            { column_name: "sku", role: "type_id", datatype: "string" },
            { column_name: "region", role: "attribute", datatype: "string" },
          ],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    // /ingest/csv/rows and the best-effort recompute-stats
    return new Response(
      JSON.stringify({ entities_resolved: 1, triples_inserted: 2 }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  });
  vi.stubGlobal("fetch", spy);
  return { calls };
}

function makeClient(): Client {
  return new Client({ apiKey: API_KEY, baseUrl: BASE, tenant: TENANT });
}

let dir: string;
let csv: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "cograph-keyjoin-"));
  csv = join(dir, "referrals.csv");
  writeFileSync(csv, "sku,region\nW-1,west\n", "utf-8");
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function rowsBody(calls: FetchArgs[]): Record<string, unknown> {
  const rowsCall = calls.find((c) => c.url.endsWith("/ingest/csv/rows"));
  expect(rowsCall).toBeDefined();
  return JSON.parse(String(rowsCall!.init.body));
}

describe("Client.ingest CSV — keyJoin pass-through (ONTA-250)", () => {
  it("forwards key_join with snake_case key_attribute to /ingest/csv/rows", async () => {
    const { calls } = installCsvFetch();
    await makeClient().ingest(csv, {
      kg: "providers",
      asFile: true,
      keyJoin: { keyAttribute: "sku" },
    });
    const body = rowsBody(calls);
    expect(body.key_join).toEqual({ key_attribute: "sku" });
  });

  it("forwards mint_unmatched when provided", async () => {
    const { calls } = installCsvFetch();
    await makeClient().ingest(csv, {
      kg: "providers",
      asFile: true,
      keyJoin: { keyAttribute: "sku", mintUnmatched: false },
    });
    expect(rowsBody(calls).key_join).toEqual({
      key_attribute: "sku",
      mint_unmatched: false,
    });
  });

  it("omits key_join entirely for an ordinary ingest (no keyJoin)", async () => {
    const { calls } = installCsvFetch();
    await makeClient().ingest(csv, { kg: "providers", asFile: true });
    expect(rowsBody(calls).key_join).toBeUndefined();
  });
});
