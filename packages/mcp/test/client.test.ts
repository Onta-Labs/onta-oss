import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { InfonaError } from "@infona-ai/cli";
import {
  HOSTED_KEY_REQUIRED,
  assertClientAccess,
  client,
  isLocalApiUrl,
} from "../src/mcpShared.js";

// Local OSS is open-access: the root README MCP JSON (localhost + tenant
// default, no INFONA_API_KEY) must construct. Hosted without a key must fail
// with a one-liner — not a later 401. Isolate HOME so ~/.infona/config.json
// from the developer machine cannot leak a key or a hosted URL into these cases.

let home: string;
const originalHome = process.env.HOME;
const originalUserProfile = process.env.USERPROFILE;
const originalApiKey = process.env.INFONA_API_KEY;
const originalApiUrl = process.env.INFONA_API_URL;
const originalTenant = process.env.INFONA_TENANT;

function resetEnvConnection(): void {
  delete process.env.INFONA_API_KEY;
  delete process.env.INFONA_API_URL;
  delete process.env.INFONA_TENANT;
}

function writeIsolatedConfig(cfg: Record<string, unknown>): void {
  const dir = join(home, ".infona");
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "config.json"), JSON.stringify(cfg, null, 2) + "\n");
}

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), "infona-mcp-client-"));
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  resetEnvConnection();
});

afterEach(() => {
  if (originalHome === undefined) delete process.env.HOME;
  else process.env.HOME = originalHome;
  if (originalUserProfile === undefined) delete process.env.USERPROFILE;
  else process.env.USERPROFILE = originalUserProfile;
  if (originalApiKey === undefined) delete process.env.INFONA_API_KEY;
  else process.env.INFONA_API_KEY = originalApiKey;
  if (originalApiUrl === undefined) delete process.env.INFONA_API_URL;
  else process.env.INFONA_API_URL = originalApiUrl;
  if (originalTenant === undefined) delete process.env.INFONA_TENANT;
  else process.env.INFONA_TENANT = originalTenant;
  try {
    rmSync(home, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
});

describe("isLocalApiUrl", () => {
  it("matches localhost and 127.0.0.1 only", () => {
    expect(isLocalApiUrl("http://localhost:8000")).toBe(true);
    expect(isLocalApiUrl("http://127.0.0.1:8000")).toBe(true);
    expect(isLocalApiUrl("https://localhost")).toBe(true);
    expect(isLocalApiUrl("https://api.infona.ai")).toBe(false);
    expect(isLocalApiUrl(undefined)).toBe(false);
  });
});

describe("assertClientAccess", () => {
  it("does not throw for localhost without a key", () => {
    expect(() =>
      assertClientAccess({ apiKey: undefined, baseUrl: "http://localhost:8000" }),
    ).not.toThrow();
  });

  it("throws a one-liner for hosted without a key", () => {
    expect(() =>
      assertClientAccess({ apiKey: undefined, baseUrl: "https://api.infona.ai" }),
    ).toThrow(InfonaError);
    expect(() =>
      assertClientAccess({ apiKey: undefined, baseUrl: "https://api.infona.ai" }),
    ).toThrow(HOSTED_KEY_REQUIRED);
  });
});

describe("client() — local OSS / hosted key", () => {
  it("constructs with local URL and no key (README JSON)", () => {
    process.env.INFONA_API_URL = "http://localhost:8000";
    process.env.INFONA_TENANT = "default";

    expect(() => client()).not.toThrow();
    const c = client();
    expect(c.baseUrl).toBe("http://localhost:8000");
    expect(c.tenant).toBe("default");
    expect(c.apiKey).toBeUndefined();
  });

  it("constructs with 127.0.0.1 and no key; tenant defaults to default", () => {
    process.env.INFONA_API_URL = "http://127.0.0.1:8000";

    const c = client();
    expect(c.baseUrl).toBe("http://127.0.0.1:8000");
    expect(c.tenant).toBe("default");
    expect(c.apiKey).toBeUndefined();
  });

  it("throws a clear error for hosted URL with no key", () => {
    process.env.INFONA_API_URL = "https://api.infona.ai";

    expect(() => client()).toThrow(InfonaError);
    expect(() => client()).toThrow(/INFONA_API_KEY/);
    expect(() => client()).toThrow(/localhost:8000/);
  });

  it("lets INFONA_API_URL and INFONA_TENANT override ~/.infona/config.json", () => {
    writeIsolatedConfig({
      apiUrl: "https://api.infona.ai",
      tenant: "cloud-ws",
      apiKey: "cloud-secret",
    });
    process.env.INFONA_API_URL = "http://localhost:8000";
    process.env.INFONA_TENANT = "default";

    const c = client();
    expect(c.baseUrl).toBe("http://localhost:8000");
    expect(c.tenant).toBe("default");
  });

  it("process INFONA_TENANT is the only workspace selector (no per-tool override)", () => {
    // A tool argument cannot retarget the Client; pointing INFONA_TENANT at a
    // foreign workspace is the only way MCP would even *attempt* /graphs/victim-ws.
    // The backend still 403s that (OSS test_mcp_tools_contract).
    process.env.INFONA_API_URL = "http://localhost:8000";
    process.env.INFONA_API_KEY = "stranger-key";
    process.env.INFONA_TENANT = "victim-ws";

    const c = client();
    expect(c.tenant).toBe("victim-ws");
    expect(c.apiKey).toBe("stranger-key");
  });
});
