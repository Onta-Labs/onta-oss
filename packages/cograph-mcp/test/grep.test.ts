import { afterEach, describe, expect, it, vi } from "vitest";
import { grepHandler } from "../src/index.js";
import type { GrepResponse } from "@onta/cli";

// ONTA-416: the `grep` MCP tool is a thin renderer over the canonical
// `POST /graphs/{tenant}/grep` route. Two things must hold and are easy to
// regress:
//   1. it forwards EXACTLY what the caller asked for to the SDK (no bespoke
//      endpoint, no client-side re-implementation of the scan); and
//   2. its rendering never misleads the agent — a TRUNCATED page must say so
//      (otherwise "only N exist" is a false negative the agent will act on), and
//      no unbounded literal may reach the context window.

function stubClient(res: GrepResponse | (() => never)) {
  const grep = vi.fn(async () => {
    if (typeof res === "function") res();
    return res as GrepResponse;
  });
  const client = { grep } as unknown as import("@onta/cli").Client;
  return { client, grep };
}

function response(over: Partial<GrepResponse> = {}): GrepResponse {
  return { matches: [], count: 0, limit: 50, truncated: false, ...over };
}

const MATCH = {
  entity_uri: "https://cograph.tech/entities/Movie/m1",
  label: "The Matrix",
  type: "Movie",
  predicate: "https://cograph.tech/onto/title",
  attr: "title",
  value: "The Matrix",
  snippet: "The Matrix",
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("grep tool — SDK forwarding", () => {
  it("passes the needle, the required kg and every optional filter through", async () => {
    const { client, grep } = stubClient(response({ matches: [MATCH], count: 1 }));

    await grepHandler(
      {
        q: "matrix",
        kg_name: "movies",
        type: "Movie",
        predicate: "title",
        case_sensitive: true,
        limit: 10,
      },
      () => client,
    );

    expect(grep).toHaveBeenCalledWith("matrix", "movies", {
      type: "Movie",
      predicate: "title",
      caseSensitive: true,
      limit: 10,
    });
  });

  it("omits unset filters rather than sending nulls", async () => {
    const { client, grep } = stubClient(response());
    await grepHandler({ q: "matrix", kg_name: "movies" }, () => client);
    expect(grep).toHaveBeenCalledWith("matrix", "movies", {
      type: undefined,
      predicate: undefined,
      caseSensitive: undefined,
      limit: undefined,
    });
  });
});

describe("grep tool — rendering", () => {
  it("renders label, type, uri and the matching attribute + snippet", async () => {
    const { client } = stubClient(response({ matches: [MATCH], count: 1 }));
    const res = await grepHandler({ q: "matrix", kg_name: "movies" }, () => client);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("The Matrix");
    expect(text).toContain("(Movie)");
    expect(text).toContain(MATCH.entity_uri);
    expect(text).toContain("[title]");
    expect(res.isError).toBeFalsy();
  });

  it("falls back to the entity URI when the subject has no label", async () => {
    const bare = { ...MATCH, label: "", type: "" };
    const { client } = stubClient(response({ matches: [bare], count: 1 }));
    const res = await grepHandler({ q: "matrix", kg_name: "movies" }, () => client);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain(bare.entity_uri);
    expect(text).not.toContain("()");
  });

  it("says NO MATCHES plainly, naming the needle and the graph", async () => {
    const { client } = stubClient(response());
    const res = await grepHandler({ q: "zzz", kg_name: "movies" }, () => client);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("No literal matches");
    expect(text).toContain("movies");
    expect(res.isError).toBeFalsy();
  });

  it("a TRUNCATED page is flagged so the agent never reads it as exhaustive", async () => {
    const { client } = stubClient(
      response({ matches: [MATCH], count: 1, limit: 1, truncated: true }),
    );
    const res = await grepHandler({ q: "matrix", kg_name: "movies" }, () => client);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("MORE EXIST");
    expect(text).toContain("1");
  });

  it("a complete page carries NO truncation note", async () => {
    const { client } = stubClient(response({ matches: [MATCH], count: 1 }));
    const res = await grepHandler({ q: "matrix", kg_name: "movies" }, () => client);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).not.toContain("MORE EXIST");
  });

  it("renders the SNIPPET, never the raw value, so context stays bounded", async () => {
    const huge = { ...MATCH, value: "x".repeat(5000), snippet: "…matrix…" };
    const { client } = stubClient(response({ matches: [huge], count: 1 }));
    const res = await grepHandler({ q: "matrix", kg_name: "movies" }, () => client);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("…matrix…");
    expect(text.length).toBeLessThan(1000);
  });

  it("surfaces a backend error (e.g. the disabled-surface 503) as isError", async () => {
    const { client } = stubClient(() => {
      throw new Error("Onta API error 503: literal grep is disabled");
    });
    const res = await grepHandler({ q: "matrix", kg_name: "movies" }, () => client);
    expect(res.isError).toBe(true);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("503");
  });
});
