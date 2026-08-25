import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { erRebuildHandler } from "../src/index.js";

// `er_rebuild` is a thin renderer over Client.erRebuild — the SAME
// `POST /graphs/{tenant}/explore/kgs/{kg}/er-rebuild` path the CLI's
// `infona er rebuild` uses. No bespoke HTTP. These tests pin the
// forwarding contract and the empty-kg guard.

const here = dirname(fileURLToPath(import.meta.url));

function stubClient(impl: (...args: unknown[]) => unknown) {
  const erRebuild = vi.fn(impl);
  const client = { erRebuild } as unknown as import("@infona-ai/cli").Client;
  return { client, erRebuild };
}

const REPORT = {
  types: [
    {
      type: "Supplier",
      entities_before: 6,
      entities_after: 3,
      fragments_absorbed: 3,
      clusters_merged: 2,
    },
  ],
  fragments_absorbed_total: 3,
  merges: [
    {
      winner: "https://graph.infona.ai/entities/Supplier/ERP-1001",
      losers: [
        "https://graph.infona.ai/entities/Supplier/CRM-4402",
        "https://graph.infona.ai/entities/Supplier/DIR-8891",
      ],
      reason: "signal-richest",
    },
  ],
  unresolved: [
    {
      field: "credit_rating",
      entity: "https://graph.infona.ai/entities/Supplier/ERP-1001",
      flagged: "equal-trust sources — not silently guessed",
    },
  ],
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("er_rebuild handler — SDK forwarding", () => {
  it("passes kg_name through to Client.erRebuild and no other method", async () => {
    const { client, erRebuild } = stubClient(async () => REPORT);

    const res = await erRebuildHandler({ kg_name: "suppliers" }, () => client);

    expect(res.isError).toBeUndefined();
    expect(erRebuild).toHaveBeenCalledTimes(1);
    expect(erRebuild).toHaveBeenCalledWith("suppliers");
  });

  it("rejects an empty kg_name without calling the SDK", async () => {
    const { client, erRebuild } = stubClient(() => {
      throw new Error("SDK erRebuild must not be called without kg_name");
    });

    const res = await erRebuildHandler({ kg_name: "  " }, () => client);

    expect(res.isError).toBe(true);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toMatch(/kg_name/i);
    expect(erRebuild).not.toHaveBeenCalled();
  });
});

describe("er_rebuild handler — rendering", () => {
  it("reports per-type before/after counts and fragments absorbed", async () => {
    const { client } = stubClient(async () => REPORT);

    const res = await erRebuildHandler({ kg_name: "suppliers" }, () => client);

    expect(res.isError).toBeUndefined();
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("Rebuilding entity resolution for suppliers");
    expect(text).toContain("Supplier");
    expect(text).toContain("6 → 3");
    expect(text).toContain("−3 fragments");
    expect(text).toContain("Done. 3 fragments absorbed.");
    expect(text).toContain("merge  https://graph.infona.ai/entities/Supplier/ERP-1001");
    expect(text).toContain("unresolved  credit_rating");
  });

  it("still prints counts when the server omits merge extras", async () => {
    const { client } = stubClient(async () => ({
      types: [
        {
          type: "Supplier",
          entities_before: 2,
          entities_after: 2,
          fragments_absorbed: 0,
          clusters_merged: 0,
        },
      ],
      fragments_absorbed_total: 0,
    }));

    const res = await erRebuildHandler({ kg_name: "suppliers" }, () => client);
    const text = res.content.map((c) => c.text).join("\n");

    expect(text).toContain("2 → 2");
    expect(text).toContain("Done. 0 fragments absorbed.");
    expect(text).not.toContain("merge");
  });

  it("surfaces backend errors instead of a fabricated success", async () => {
    const { client, erRebuild } = stubClient(async () => {
      throw new Error("rebuild timed out");
    });

    const res = await erRebuildHandler({ kg_name: "suppliers" }, () => client);

    expect(res.isError).toBe(true);
    expect(res.content.map((c) => c.text).join("\n")).toContain("rebuild timed out");
    expect(erRebuild).toHaveBeenCalledTimes(1);
  });
});

describe("er_rebuild — interface convergence", () => {
  it("rides Client.erRebuild; does not invent an HTTP path", () => {
    const src = readFileSync(join(here, "../src/mcpErRebuild.ts"), "utf8");
    expect(src).toContain(".erRebuild(");
    expect(src).not.toMatch(/makeClient\(\)\.request\b/);
    expect(src).not.toMatch(/fetch\(/);
  });

  it("is registered next to ingest/schema tools from index.ts", () => {
    const src = readFileSync(join(here, "../src/index.ts"), "utf8");
    expect(src).toContain("registerErRebuildTools");
    expect(src).toContain("erRebuildHandler");
    const ingest = src.indexOf("registerIngestTools");
    const schema = src.indexOf("registerSchemaTools");
    const er = src.indexOf("registerErRebuildTools(server)");
    expect(ingest).toBeGreaterThan(-1);
    expect(schema).toBeGreaterThan(-1);
    expect(er).toBeGreaterThan(schema);
  });
});
