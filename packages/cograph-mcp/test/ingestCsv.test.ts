import { mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ingestCsvHandler } from "../src/index.js";

// ONTA-253: `ingest_csv`'s contract is "ingest a CSV FILE". A path that does not
// resolve to a readable file must be a CLEAR error, and the SDK/HTTP `ingest`
// call must NEVER fire — otherwise the backend LLM-extracts phantom entities out
// of the path string and the tool reports a fabricated "N entities resolved"
// success (the persona-eval trust bug). These tests assert on the mechanism with
// an invented KG name so nothing overfits to a specific dataset.

function stubClient(ingestImpl: (...args: unknown[]) => unknown) {
  const ingest = vi.fn(ingestImpl);
  // Only the `ingest` method is exercised by the handler.
  const client = { ingest } as unknown as import("@onta/cli").Client;
  return { client, ingest };
}

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "cograph-mcp-ingest-"));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
  vi.restoreAllMocks();
});

describe("ingest_csv handler — missing file (ONTA-253)", () => {
  it("returns an error naming the missing file and NEVER calls the SDK ingest", async () => {
    const missing = join(dir, "does-not-exist.csv");
    const { client, ingest } = stubClient(() => {
      throw new Error("SDK ingest must not be called for a missing file");
    });

    const res = await ingestCsvHandler(
      { file_path: missing, kg_name: "widget-catalog" },
      () => client,
    );

    // Clear error, not a fabricated success.
    expect(res.isError).toBe(true);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain(missing);
    expect(text.toLowerCase()).toContain("not found");
    // No fabricated "entities resolved" success.
    expect(text).not.toMatch(/entities resolved/i);
    // The load-bearing assertion: the SDK/HTTP ingest never fired.
    expect(ingest).not.toHaveBeenCalled();
  });

  it("a path that exists but is a DIRECTORY is also rejected without ingesting", async () => {
    const { client, ingest } = stubClient(() => {
      throw new Error("SDK ingest must not be called for a directory");
    });

    const res = await ingestCsvHandler(
      { file_path: dir, kg_name: "widget-catalog" },
      () => client,
    );

    expect(res.isError).toBe(true);
    expect(ingest).not.toHaveBeenCalled();
  });
});

// ONTA-415: the not-found error grew a "did you mean" suffix driven by the SAME
// containment-guarded primitive as `list_local_files`. The suffix must be
// CONDITIONAL on a root being configured: naming a tool that is absent from
// tools/list is the ONTA-243 failure class (the model is sent after a capability
// it cannot call).
describe("ingest_csv handler: did-you-mean hint (ONTA-415)", () => {
  it("suggests real local files and names list_local_files WHEN a root is configured", async () => {
    const root = realpathSync(dir);
    const real = join(root, "tecentriq-label-demo.csv");
    writeFileSync(real, "sku,color\nW-1,red\n", "utf-8");
    const { client, ingest } = stubClient(() => {
      throw new Error("SDK ingest must not be called for a missing file");
    });

    const res = await ingestCsvHandler(
      { file_path: "/workspace/tecentriq-label-demo.csv", kg_name: "widget-catalog" },
      () => client,
      [root],
    );

    expect(res.isError).toBe(true);
    const text = res.content.map((c) => c.text).join("\n");
    // Still a clear failure, never a fabricated success (ONTA-253 holds).
    expect(text).toContain("/workspace/tecentriq-label-demo.csv");
    expect(text).not.toMatch(/entities resolved/i);
    expect(ingest).not.toHaveBeenCalled();
    // The actionable part: the real absolute path, plus the tool to see more.
    expect(text).toContain(real);
    expect(text).toContain("list_local_files");
  });

  it("says NOTHING about list_local_files when no root is configured", async () => {
    const missing = join(dir, "does-not-exist.csv");
    const { client } = stubClient(() => {
      throw new Error("SDK ingest must not be called for a missing file");
    });

    const res = await ingestCsvHandler(
      { file_path: missing, kg_name: "widget-catalog" },
      () => client,
      [],
    );

    const text = res.content.map((c) => c.text).join("\n");
    expect(res.isError).toBe(true);
    // The tool is not registered in this configuration, so pointing the model at
    // it would strand the task.
    expect(text).not.toContain("list_local_files");
    expect(text.toLowerCase()).toContain("not found");
  });

  it("does not leak paths from outside the configured root", async () => {
    const root = realpathSync(mkdtempSync(join(tmpdir(), "cograph-mcp-root-")));
    const outsideFile = join(realpathSync(dir), "secret.csv");
    writeFileSync(outsideFile, "a,b\n1,2\n", "utf-8");
    const { client } = stubClient(() => {
      throw new Error("SDK ingest must not be called for a missing file");
    });

    try {
      const res = await ingestCsvHandler(
        { file_path: join(root, "nope.csv"), kg_name: "widget-catalog" },
        () => client,
        [root],
      );
      const text = res.content.map((c) => c.text).join("\n");
      expect(text).not.toContain("secret.csv");
      expect(text).not.toContain(outsideFile);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("ingest_csv handler — real file (happy path)", () => {
  it("ingests an existing CSV with asFile:true and reports the real counts", async () => {
    const csv = join(dir, "widgets.csv");
    writeFileSync(csv, "sku,color\nW-1,red\nW-2,blue\n", "utf-8");

    const { client, ingest } = stubClient(async () => ({
      entities_resolved: 2,
      triples_inserted: 6,
    }));

    const res = await ingestCsvHandler(
      { file_path: csv, kg_name: "widget-catalog" },
      () => client,
    );

    expect(res.isError).toBeUndefined();
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("2 entities resolved");
    expect(text).toContain("6 triples inserted");
    // The tool must ingest in FILE mode (asFile:true) so a vanish-after-stat
    // still hard-errors in the SDK rather than degrading to text.
    expect(ingest).toHaveBeenCalledTimes(1);
    const [pathArg, opts] = ingest.mock.calls[0]!;
    expect(pathArg).toBe(csv);
    expect(opts).toMatchObject({ kg: "widget-catalog", asFile: true });
    // No key-join unless the caller asked for one.
    expect((opts as Record<string, unknown>).keyJoin).toBeUndefined();
  });

  it("forwards join_on to the SDK as keyJoin.keyAttribute (ONTA-250)", async () => {
    const csv = join(dir, "widgets.csv");
    writeFileSync(csv, "sku,region\nW-1,west\n", "utf-8");

    const { client, ingest } = stubClient(async () => ({
      entities_resolved: 1,
      triples_inserted: 1,
    }));

    await ingestCsvHandler(
      { file_path: csv, kg_name: "widget-catalog", join_on: "sku" },
      () => client,
    );

    const [, opts] = ingest.mock.calls[0]!;
    expect(opts).toMatchObject({
      kg: "widget-catalog",
      asFile: true,
      keyJoin: { keyAttribute: "sku" },
    });
  });
});
