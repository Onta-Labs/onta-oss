import { afterEach, describe, expect, it, vi } from "vitest";
import { searchHandler } from "../src/index.js";
import type { SemanticSearchResponse } from "@infona-ai/cli";

// ONTA-178 honesty: when the semantic index is off / embedding unavailable the
// backend still answers 200 with degraded:true. An empty page under that mode
// must not read as "nothing exists" — it may simply be unindexed. Point agents
// at `grep` for an index-free literal scan.

function stubClient(res: SemanticSearchResponse | (() => never)) {
  const search = vi.fn(async () => {
    if (typeof res === "function") res();
    return res as SemanticSearchResponse;
  });
  const client = { search } as unknown as import("@infona-ai/cli").Client;
  return { client, search };
}

function response(
  over: Partial<SemanticSearchResponse> = {},
): SemanticSearchResponse {
  return { hits: [], count: 0, degraded: false, top_k: 10, ...over };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("search tool — degraded honesty", () => {
  it("mentions reduced recall + grep even when hits are empty", async () => {
    const { client, search } = stubClient(response({ degraded: true }));

    const res = await searchHandler({ query: "privacy" }, () => client);
    const text = res.content.map((c) => c.text).join("\n");

    expect(res.isError).toBeUndefined();
    expect(text).toContain("No matching entities found");
    expect(text.toLowerCase()).toMatch(/keyword-only|reduced recall/);
    expect(text).toContain("grep");
    expect(search).toHaveBeenCalledWith("privacy", {
      kg: undefined,
      type: undefined,
      entityUris: undefined,
      topK: undefined,
    });
  });

  it("stays quiet about degradation when the index is healthy and empty", async () => {
    const { client } = stubClient(response({ degraded: false }));

    const res = await searchHandler({ query: "privacy" }, () => client);
    const text = res.content.map((c) => c.text).join("\n");

    expect(text).toBe("No matching entities found.");
    expect(text).not.toContain("grep");
  });

  it("appends the degraded note after non-empty hits", async () => {
    const { client } = stubClient(
      response({
        degraded: true,
        count: 1,
        hits: [
          {
            entity_uri: "https://graph.infona.ai/entities/Speech/s1",
            attr: "transcript",
            snippet: "…privacy…",
            score: 0.4,
            attrs: { label: "Keynote", type: "Speech" },
          },
        ],
      }),
    );

    const res = await searchHandler(
      { query: "privacy", kg_name: "speeches", top_k: 5 },
      () => client,
    );
    const text = res.content.map((c) => c.text).join("\n");

    expect(text).toContain("Keynote");
    expect(text.toLowerCase()).toMatch(/keyword-only|reduced recall/);
    expect(text).toContain("grep");
  });
});

describe("search tool — SDK forwarding", () => {
  it("forwards query, kg, type, entity_uris and top_k", async () => {
    const { client, search } = stubClient(response());
    await searchHandler(
      {
        query: "foo",
        kg_name: "kg1",
        type: "Person",
        entity_uris: ["e:a", "e:b"],
        top_k: 3,
      },
      () => client,
    );
    expect(search).toHaveBeenCalledWith("foo", {
      kg: "kg1",
      type: "Person",
      entityUris: ["e:a", "e:b"],
      topK: 3,
    });
  });

  it("forwards empty entity_uris as [] (strict empty allowlist)", async () => {
    const { client, search } = stubClient(response());
    await searchHandler({ query: "foo", entity_uris: [] }, () => client);
    expect(search).toHaveBeenCalledWith("foo", {
      kg: undefined,
      type: undefined,
      entityUris: [],
      topK: undefined,
    });
  });
});
