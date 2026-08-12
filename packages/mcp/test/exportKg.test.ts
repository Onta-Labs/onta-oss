import { afterEach, describe, expect, it, vi } from "vitest";
import { exportKgHandler } from "../src/index.js";

// F10: the `export_kg` MCP tool is a thin renderer over the canonical
// `GET /graphs/{tenant}/kgs/{kg}/export` route via Client.exportKg. Two things
// must hold and are easy to regress:
//   1. it forwards EXACTLY what the caller asked for to the SDK (no bespoke
//      endpoint); and
//   2. its rendering never floods the agent context window — a huge dump is
//      truncated with an honest note, and JSON is pretty-printed.

function stubClient(res: Record<string, unknown> | string | (() => never)) {
  const exportKg = vi.fn(async () => {
    if (typeof res === "function") res();
    return res as Record<string, unknown> | string;
  });
  const client = { exportKg } as unknown as import("@infona-ai/cli").Client;
  return { client, exportKg };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("export_kg handler — SDK forwarding", () => {
  it("passes kg_name, format, type and limit through to Client.exportKg", async () => {
    const { client, exportKg } = stubClient({ rows: [] });

    await exportKgHandler(
      {
        kg_name: "bookstore",
        format: "csv",
        type: "Book",
        limit: 50,
      },
      () => client,
    );

    expect(exportKg).toHaveBeenCalledWith("bookstore", {
      format: "csv",
      type: "Book",
      limit: 50,
    });
  });

  it("defaults format to json when omitted", async () => {
    const { client, exportKg } = stubClient({});
    await exportKgHandler({ kg_name: "bookstore" }, () => client);
    expect(exportKg).toHaveBeenCalledWith("bookstore", {
      format: "json",
      type: undefined,
      limit: undefined,
    });
  });

  it("rejects an empty kg_name without calling the SDK", async () => {
    const { client, exportKg } = stubClient(() => {
      throw new Error("SDK exportKg must not be called without kg_name");
    });

    const res = await exportKgHandler({ kg_name: "  " }, () => client);

    expect(res.isError).toBe(true);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toMatch(/kg_name/i);
    expect(exportKg).not.toHaveBeenCalled();
  });
});

describe("export_kg handler — rendering", () => {
  it("pretty-prints a JSON object body", async () => {
    const payload = { entities: [{ id: "b1", title: "Dune" }] };
    const { client } = stubClient(payload);

    const res = await exportKgHandler(
      { kg_name: "bookstore", format: "json" },
      () => client,
    );

    expect(res.isError).toBeUndefined();
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toBe(JSON.stringify(payload, null, 2));
  });

  it("returns a CSV string body as-is", async () => {
    const csv = "title,author\nDune,Herbert\n";
    const { client } = stubClient(csv);

    const res = await exportKgHandler(
      { kg_name: "bookstore", format: "csv" },
      () => client,
    );

    expect(res.isError).toBeUndefined();
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toBe(csv);
  });

  it("truncates a huge body and says so", async () => {
    // Just over the 100_000-char MCP cap.
    const huge = "x".repeat(100_050);
    const { client } = stubClient(huge);

    const res = await exportKgHandler({ kg_name: "bookstore" }, () => client);

    expect(res.isError).toBeUndefined();
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("truncated");
    expect(text).toContain("100050");
    // Whole response stays under the cap (note included).
    expect(text.length).toBeLessThanOrEqual(100_000);
    expect(text.length).toBeLessThan(huge.length);
    // Body starts with the original content, not a rewritten summary.
    expect(text.startsWith("x".repeat(1000))).toBe(true);
  });

  it("surfaces InfonaError via errorResult", async () => {
    const { InfonaError } = await import("@infona-ai/cli");
    const { client } = stubClient(() => {
      throw new InfonaError("kg not found", { status: 404 });
    });

    const res = await exportKgHandler(
      { kg_name: "missing-kg" },
      () => client,
    );

    expect(res.isError).toBe(true);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toMatch(/kg not found/i);
  });
});
