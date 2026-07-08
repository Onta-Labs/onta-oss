import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Client, CographError } from "../src/client.js";

// ONTA-253: `ingest(path, {asFile:true})` is the FILE-intent mode. A missing
// path must reject with a CographError and issue NO HTTP POST — never degrade to
// POSTing the path string as text (which makes the backend LLM-extract phantom
// entities out of a filename). The dual-mode default (`asFile` unset) still
// text-ingests raw text, so the CLI's `ingest <text>` path keeps working.

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

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "cograph-ingest-asfile-"));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Client.ingest — asFile hard-errors on a missing path (ONTA-253)", () => {
  it("rejects with CographError and issues NO HTTP POST for a missing file", async () => {
    const { calls } = installFetch(new Response("{}", { status: 200 }));
    const missing = join(dir, "nope.csv");

    await expect(
      makeClient().ingest(missing, { kg: "widget-catalog", asFile: true }),
    ).rejects.toBeInstanceOf(CographError);

    // The load-bearing assertion: nothing was POSTed — no fabricated ingest.
    expect(calls).toHaveLength(0);
  });

  it("the CographError message names the missing path", async () => {
    installFetch(new Response("{}", { status: 200 }));
    const missing = join(dir, "referrals.csv");
    await makeClient()
      .ingest(missing, { asFile: true })
      .then(
        () => {
          throw new Error("expected a rejection");
        },
        (err: unknown) => {
          expect(err).toBeInstanceOf(CographError);
          expect((err as CographError).message).toContain(missing);
        },
      );
  });

  it("a real file with asFile:true still ingests (POSTs) — text/json path", async () => {
    // A .txt file exercises the non-CSV file branch: read + POST /ingest.
    const { calls } = installFetch(
      new Response(JSON.stringify({ entities_resolved: 1, triples_inserted: 2 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const file = join(dir, "note.txt");
    writeFileSync(file, "a note about a widget", "utf-8");

    await makeClient().ingest(file, { kg: "widget-catalog", asFile: true });
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe(`${BASE}/graphs/${TENANT}/ingest`);
  });
});

describe("Client.ingest — text back-compat (asFile unset)", () => {
  it("text-ingests raw text (POSTs the content) when asFile is not set", async () => {
    const { calls } = installFetch(
      new Response(JSON.stringify({ entities_resolved: 0, triples_inserted: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await makeClient().ingest("a widget named Foo with sku W-1");
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe(`${BASE}/graphs/${TENANT}/ingest`);
    const body = JSON.parse(String(calls[0]!.init.body));
    expect(body.content).toBe("a widget named Foo with sku W-1");
    expect(body.content_type).toBe("text");
  });

  it("a path-looking string with asFile UNSET degrades to text (unchanged CLI behavior)", async () => {
    const { calls } = installFetch(
      new Response(JSON.stringify({ entities_resolved: 0, triples_inserted: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const missing = join(dir, "still-missing.csv");

    // Without asFile, the legacy dual-mode path treats a nonexistent path as raw
    // text and POSTs it (this is the CLI's intentional behavior we preserve).
    await makeClient().ingest(missing);
    expect(calls).toHaveLength(1);
    const body = JSON.parse(String(calls[0]!.init.body));
    expect(body.content).toBe(missing);
  });
});
