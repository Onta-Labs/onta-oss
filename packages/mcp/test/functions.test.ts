import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  listFunctionsHandler,
  registerFunctionHandler,
  invokeFunctionHandler,
  deleteFunctionHandler,
} from "../src/index.js";

const here = dirname(fileURLToPath(import.meta.url));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function stub(methods: Record<string, ReturnType<typeof vi.fn>>) {
  return methods as unknown as import("@infona-ai/cli").Client;
}

describe("function tools — SDK forwarding", () => {
  it("list_functions forwards entity_type", async () => {
    const listFunctions = vi.fn(async () => [
      {
        name: "widget-lookup",
        entity_type: "SynthWidget",
        description: "Ada Example lookup",
        endpoint_url: "https://example.test/fn",
      },
    ]);
    const res = await listFunctionsHandler(
      { entity_type: "SynthWidget" },
      () => stub({ listFunctions }),
    );
    expect(listFunctions).toHaveBeenCalledWith("SynthWidget");
    expect(res.content.map((c) => c.text).join("\n")).toContain("widget-lookup");
  });

  it("register_function / invoke_function / delete_function call Client methods", async () => {
    const registerFunction = vi.fn(async () => ({
      registered: "widget-lookup",
      entity_type: "SynthWidget",
      layer: "tenant",
    }));
    const invokeFunction = vi.fn(async () => ({
      entity_uri: "https://graph.infona.ai/entities/SynthWidget/ada",
      function: "widget-lookup",
      output: { ok: true },
      duration_ms: 12,
    }));
    const deleteFunction = vi.fn(async () => ({ ok: true }));
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    await registerFunctionHandler(
      {
        name: "widget-lookup",
        entity_type: "SynthWidget",
        endpoint_url: "https://example.test/fn",
        description: "Ada Example",
      },
      () => stub({ registerFunction }),
    );
    expect(registerFunction).toHaveBeenCalledWith({
      name: "widget-lookup",
      entity_type: "SynthWidget",
      endpoint_url: "https://example.test/fn",
      description: "Ada Example",
    });

    await invokeFunctionHandler(
      {
        name: "widget-lookup",
        entity_uri: "https://graph.infona.ai/entities/SynthWidget/ada",
        kg_name: "demo-kg",
      },
      () => stub({ invokeFunction }),
    );
    expect(invokeFunction).toHaveBeenCalledWith("widget-lookup", {
      entity_uri: "https://graph.infona.ai/entities/SynthWidget/ada",
      kg_name: "demo-kg",
    });

    await deleteFunctionHandler(
      { name: "widget-lookup", entity_type: "SynthWidget" },
      () => stub({ deleteFunction }),
    );
    expect(deleteFunction).toHaveBeenCalledWith("widget-lookup", {
      entityType: "SynthWidget",
    });
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("function tool sources do not hardcode backend paths", () => {
  it("mcpFunctions.ts has no fetch( and no path-string /graphs/", () => {
    const src = readFileSync(join(here, "../src/mcpFunctions.ts"), "utf8");
    const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
    expect(code).not.toMatch(/["'`][^"'`]*\/graphs\//);
    expect(code).not.toMatch(/\bfetch\s*\(/);
  });
});
