import { afterEach, describe, expect, it, vi } from "vitest";
import { isClerkUserId } from "../src/config.js";
import { Client } from "../src/client.js";

describe("isClerkUserId", () => {
  it("matches Clerk subject shape", () => {
    expect(isClerkUserId("user_3CErfj5NUOstcrdPxAm8InEdW4r")).toBe(true);
    expect(isClerkUserId("july-2")).toBe(false);
    expect(isClerkUserId("demo-tenant")).toBe(false);
    expect(isClerkUserId("user_")).toBe(false);
    expect(isClerkUserId(undefined)).toBe(false);
  });
});

describe("Client tenant heal", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("rewrites /graphs/user_… to the first real workspace", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.endsWith("/v1/me/tenants")) {
        return new Response(
          JSON.stringify([
            // Looks like a Clerk subject — heal must skip it.
            { id: "user_ABC123xyz", label: "bad" },
            { id: "july-2", label: "July2" },
          ]),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/graphs/july-2/kgs")) {
        return new Response(JSON.stringify([{ name: "kg1" }]), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(`unexpected ${url}`, { status: 500 });
    }) as typeof fetch;

    const client = new Client({
      apiKey: "test-key",
      baseUrl: "https://api.example",
      tenant: "user_3CErfj5NUOstcrdPxAm8InEdW4r",
    });
    const kgs = await client.listKgs();
    expect(kgs).toEqual([{ name: "kg1" }]);
    expect(client.tenant).toBe("july-2");
    expect(calls.some((u) => u.includes("/v1/me/tenants"))).toBe(true);
    expect(calls.some((u) => u.includes("/graphs/july-2/kgs"))).toBe(true);
    expect(calls.some((u) => u.includes("/graphs/user_"))).toBe(false);
  });
});
