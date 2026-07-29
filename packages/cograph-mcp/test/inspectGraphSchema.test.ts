import { afterEach, describe, expect, it, vi } from "vitest";
import { inspectGraphSchemaHandler } from "../src/index.js";
import type { KgSchema } from "@onta/cli";

// ONTA-418: the tool exists so an agent stops GUESSING attribute names
// ("fda_indications" vs "indications"). Two properties are load-bearing:
// the call is ONE backend request (never a per-type fan-out from here), and a
// declared-but-empty slot is rendered as EMPTY rather than dropped, dropping
// it made agents assert the attribute/type does not exist at all.

function stubClient(schema: KgSchema) {
  const kgSchema = vi.fn(async () => schema);
  const client = { kgSchema } as unknown as import("@onta/cli").Client;
  return { client, kgSchema };
}

function schemaFixture(over: Partial<KgSchema> = {}): KgSchema {
  return {
    kg: "pharma",
    types: [
      {
        name: "Drug",
        description: "A pharmaceutical product",
        parent_type: null,
        entity_count: 120,
        populated: true,
        declared_only: false,
        attributes_withheld: 0,
        relationships_withheld: 0,
        attributes: [
          {
            name: "brand_name",
            predicate_uri: "u1",
            datatype: "string",
            count: 120,
            coverage_pct: 100,
            populated: true,
          },
          {
            name: "indications",
            predicate_uri: "u2",
            datatype: "string",
            count: 90,
            coverage_pct: 75,
            populated: true,
          },
          {
            name: "fda_indications",
            predicate_uri: "u3",
            datatype: "string",
            count: 0,
            coverage_pct: 0,
            populated: false,
          },
        ],
        relationships: [
          {
            name: "manufacturer",
            predicate_uri: "u4",
            target_type: "Company",
            count: 60,
            coverage_pct: 50,
            avg_degree: 0.5,
            populated: true,
          },
        ],
      },
    ],
    total_types: 1,
    truncated: false,
    omitted_type_names: [],
    stats_source: "precomputed",
    coverage_note: "coverage_pct is relative to entity_count.",
    ...over,
  };
}

afterEach(() => vi.restoreAllMocks());

describe("inspect_graph_schema handler", () => {
  it("renders real coverage per slot in ONE backend call", async () => {
    const { client, kgSchema } = stubClient(schemaFixture());

    const res = await inspectGraphSchemaHandler({ kg_name: "pharma" }, () => client);

    expect(res.isError).toBeUndefined();
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("Drug: 120 entities");
    expect(text).toContain("brand_name (string) 100%");
    expect(text).toContain("indications (string) 75%");
    expect(text).toContain("manufacturer -> Company 50%");
    // The whole reason this is a backend endpoint: one round trip regardless of
    // how many types the graph has.
    expect(kgSchema).toHaveBeenCalledTimes(1);
    expect(kgSchema).toHaveBeenCalledWith("pharma", {
      types: undefined,
      minCoverage: undefined,
    });
  });

  it("shows a declared-but-empty attribute as EMPTY instead of dropping it", async () => {
    const { client } = stubClient(schemaFixture());

    const res = await inspectGraphSchemaHandler({ kg_name: "pharma" }, () => client);
    const text = res.content.map((c) => c.text).join("\n");

    // Present (so the agent knows the name exists) and unmistakably unpopulated
    // (so it does not query it expecting rows).
    expect(text).toContain("fda_indications (string) EMPTY");
    expect(text).not.toContain("fda_indications (string) 0%");
  });

  it("marks a declared type with no instances in this graph", async () => {
    const schema = schemaFixture();
    schema.types.push({
      name: "ClinicalTrial",
      description: "",
      parent_type: null,
      entity_count: 0,
      populated: false,
      declared_only: true,
      attributes_withheld: 0,
      relationships_withheld: 0,
      attributes: [],
      relationships: [],
    });
    schema.total_types = 2;
    const { client } = stubClient(schema);

    const res = await inspectGraphSchemaHandler({ kg_name: "pharma" }, () => client);
    const text = res.content.map((c) => c.text).join("\n");

    expect(text).toContain("ClinicalTrial: 0 entities");
    expect(text).toMatch(/ClinicalTrial: 0 entities .*NO instances in this graph/);
  });

  it("forwards type + min_coverage and reports withheld slots", async () => {
    const schema = schemaFixture();
    schema.types[0]!.attributes_withheld = 2;
    schema.types[0]!.relationships_withheld = 1;
    const { client, kgSchema } = stubClient(schema);

    const res = await inspectGraphSchemaHandler(
      { kg_name: "pharma", type: ["Drug"], min_coverage: 50 },
      () => client,
    );

    expect(kgSchema).toHaveBeenCalledWith("pharma", {
      types: ["Drug"],
      minCoverage: 50,
    });
    const text = res.content.map((c) => c.text).join("\n");
    // Never a silently shortened list.
    expect(text).toContain("3 more below the min_coverage floor");
  });

  it("names capped types so a truncated schema never reads as 'does not exist'", async () => {
    const { client } = stubClient(
      schemaFixture({
        truncated: true,
        total_types: 3,
        omitted_type_names: ["Trial", "Company"],
      }),
    );

    const res = await inspectGraphSchemaHandler({ kg_name: "pharma" }, () => client);
    const text = res.content.map((c) => c.text).join("\n");

    expect(text).toContain("Trial, Company");
    expect(text).toContain("pass type= to drill in");
  });

  it("surfaces backend errors instead of an empty schema", async () => {
    const kgSchema = vi.fn(async () => {
      throw new Error("boom");
    });
    const client = { kgSchema } as unknown as import("@onta/cli").Client;

    const res = await inspectGraphSchemaHandler({ kg_name: "pharma" }, () => client);

    expect(res.isError).toBe(true);
    expect(res.content.map((c) => c.text).join("\n")).toContain("boom");
  });
});
