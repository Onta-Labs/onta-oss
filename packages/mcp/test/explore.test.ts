import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  listRecordsHandler,
  getEntityHandler,
  typeSummaryHandler,
  listTenantsHandler,
  createTenantHandler,
  recomputeStatsHandler,
} from "../src/index.js";

const here = dirname(fileURLToPath(import.meta.url));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function stub(methods: Record<string, ReturnType<typeof vi.fn>>) {
  return {
    client: methods as unknown as import("@infona-ai/cli").Client,
    ...methods,
  };
}

describe("explore tools — SDK forwarding (no handmade URLs)", () => {
  it("list_records calls exploreRecords with kg, type, limit, cursor", async () => {
    const exploreRecords = vi.fn(async () => ({
      columns: ["name", "color"],
      rows: [{ id: "e1", name: "Ada Example", color: "blue" }],
      total: 1,
      next_cursor: null,
    }));
    const { client } = stub({ exploreRecords });
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    const res = await listRecordsHandler(
      { kg_name: "demo-kg", type_name: "SynthWidget", limit: 10, cursor: "e0" },
      () => client,
    );

    expect(res.isError).toBeUndefined();
    expect(exploreRecords).toHaveBeenCalledTimes(1);
    expect(exploreRecords).toHaveBeenCalledWith("demo-kg", "SynthWidget", {
      limit: 10,
      cursor: "e0",
    });
    expect(fetchSpy).not.toHaveBeenCalled();
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("Ada Example");
    expect(text).toContain("color");
  });

  it("notes relationship vs literal when a column carries kind", async () => {
    const exploreRecords = vi.fn(async () => ({
      columns: [
        { name: "assembled_from", kind: "relationship" },
        { name: "color", kind: "literal" },
      ],
      rows: [{ id: "e1", name: "Ada Example", assembled_from: "Part-1", color: "blue" }],
      total: 1,
      next_cursor: "e1",
    }));
    const { client } = stub({ exploreRecords });
    const res = await listRecordsHandler(
      { kg_name: "demo-kg", type_name: "SynthWidget" },
      () => client,
    );
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("assembled_from (relationship)");
    expect(text).toContain("color (literal)");
    expect(text).toContain("next_cursor: e1");
  });

  it("get_entity calls getEntity", async () => {
    const getEntity = vi.fn(async () => ({
      id: "https://graph.infona.ai/entities/SynthWidget/ada",
      name: "Ada Example",
      primary_type: "SynthWidget",
      properties: { color: "blue" },
      outgoing: [],
      incoming: [],
    }));
    const { client } = stub({ getEntity });
    const res = await getEntityHandler(
      {
        kg_name: "demo-kg",
        entity_id: "https://graph.infona.ai/entities/SynthWidget/ada",
      },
      () => client,
    );
    expect(getEntity).toHaveBeenCalledWith(
      "demo-kg",
      "https://graph.infona.ai/entities/SynthWidget/ada",
    );
    expect(res.content.map((c) => c.text).join("\n")).toContain("Ada Example");
  });

  it("type_summary calls exploreSummary and renders API coverage only", async () => {
    const exploreSummary = vi.fn(async () => ({
      name: "SynthWidget",
      description: "A synthetic widget",
      parent_type: null,
      entity_count: 3,
      attributes: [
        {
          name: "color",
          predicate_uri: "u",
          datatype: "string",
          count: 3,
          coverage_pct: 100,
        },
      ],
      relationships: [],
    }));
    const { client } = stub({ exploreSummary });
    const res = await typeSummaryHandler(
      { kg_name: "demo-kg", type_name: "SynthWidget" },
      () => client,
    );
    expect(exploreSummary).toHaveBeenCalledWith("demo-kg", "SynthWidget");
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("color (string) 100%");
    expect(text).toContain("A synthetic widget");
    expect(text).not.toMatch(/sample/i);
  });

  it("list_tenants / create_tenant / recompute_stats call the SDK methods", async () => {
    const listTenants = vi.fn(async () => [{ id: "ws-ada", label: "Ada Example" }]);
    const createTenant = vi.fn(async () => ({ id: "ws-2", label: "Ada Example" }));
    const recomputeStats = vi.fn(async () => ({ status: "scheduled", kg: "demo-kg" }));
    const listed = await listTenantsHandler({}, () => stub({ listTenants }).client);
    expect(listTenants).toHaveBeenCalledTimes(1);
    expect(listed.content.map((c) => c.text).join("\n")).toContain("INFONA_TENANT");

    const created = await createTenantHandler(
      { label: "Ada Example" },
      () => stub({ createTenant }).client,
    );
    expect(createTenant).toHaveBeenCalledWith({ label: "Ada Example" });
    expect(created.content.map((c) => c.text).join("\n")).toContain("ws-2");

    const recomputed = await recomputeStatsHandler(
      { kg_name: "demo-kg" },
      () => stub({ recomputeStats }).client,
    );
    expect(recomputeStats).toHaveBeenCalledWith("demo-kg");
    expect(recomputed.content.map((c) => c.text).join("\n")).toContain("scheduled");
  });
});

describe("explore tool sources do not hardcode backend paths", () => {
  it("mcpExplore.ts has no fetch( and no path-string /graphs/", () => {
    const src = readFileSync(join(here, "../src/mcpExplore.ts"), "utf8");
    const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
    expect(code).not.toMatch(/["'`][^"'`]*\/graphs\//);
    expect(code).not.toMatch(/\bfetch\s*\(/);
  });
});
