import { afterEach, describe, expect, it, vi } from "vitest";
import { ingestTextHandler } from "../src/index.js";

// Dogfood S1: `ingest_text` posts free-form content through the SDK's existing
// `ingest` → canonical `POST /graphs/{tenant}/ingest` path. No file on disk,
// no bespoke endpoint. These tests pin the forwarding contract and the empty-
// input guard so a missing file is never fabricated into a text success the
// way ONTA-253 was for CSV.

function stubClient(ingestImpl: (...args: unknown[]) => unknown) {
  const ingest = vi.fn(ingestImpl);
  const client = { ingest } as unknown as import("@infona-ai/cli").Client;
  return { client, ingest };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ingest_text handler — input guards", () => {
  it("rejects empty text without calling the SDK", async () => {
    const { client, ingest } = stubClient(() => {
      throw new Error("SDK ingest must not be called for empty text");
    });

    const res = await ingestTextHandler(
      { text: "   ", kg_name: "notes" },
      () => client,
    );

    expect(res.isError).toBe(true);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text.toLowerCase()).toContain("non-empty");
    expect(ingest).not.toHaveBeenCalled();
  });

  it("rejects missing kg_name without calling the SDK", async () => {
    const { client, ingest } = stubClient(() => {
      throw new Error("SDK ingest must not be called without kg_name");
    });

    const res = await ingestTextHandler(
      { text: "Alice works at Acme.", kg_name: "  " },
      () => client,
    );

    expect(res.isError).toBe(true);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("kg_name");
    expect(ingest).not.toHaveBeenCalled();
  });
});

describe("ingest_text handler — SDK forwarding", () => {
  it("posts raw text with asText:true and reports extraction + write counts", async () => {
    const { client, ingest } = stubClient(async () => ({
      entities_extracted: 2,
      entities_resolved: 2,
      triples_inserted: 5,
    }));

    const note = "Alice is CEO of Acme Corp. Bob reports to Alice.";
    const res = await ingestTextHandler(
      { text: note, kg_name: "org-notes" },
      () => client,
    );

    expect(res.isError).toBeUndefined();
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("2 entities extracted");
    expect(text).toContain("2 entities resolved");
    expect(text).toContain('5 triples inserted into "org-notes"');

    expect(ingest).toHaveBeenCalledTimes(1);
    const [contentArg, opts] = ingest.mock.calls[0]!;
    expect(contentArg).toBe(note);
    expect(opts).toMatchObject({
      kg: "org-notes",
      contentType: "text",
      asText: true,
    });
    // Never force file mode on a text-intent call.
    expect((opts as Record<string, unknown>).asFile).toBeUndefined();
  });

  it("forwards format=json as contentType", async () => {
    const { client, ingest } = stubClient(async () => ({
      entities_resolved: 1,
      triples_inserted: 1,
    }));

    const payload = JSON.stringify({ name: "Acme", industry: "software" });
    await ingestTextHandler(
      { text: payload, kg_name: "companies", format: "json" },
      () => client,
    );

    const [contentArg, opts] = ingest.mock.calls[0]!;
    expect(contentArg).toBe(payload);
    expect(opts).toMatchObject({
      kg: "companies",
      contentType: "json",
      asText: true,
    });
  });

  it("surfaces SDK/backend errors as tool errors", async () => {
    const { client } = stubClient(async () => {
      throw new Error("rate limited");
    });

    const res = await ingestTextHandler(
      { text: "hello", kg_name: "notes" },
      () => client,
    );

    expect(res.isError).toBe(true);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("rate limited");
  });
});
