import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

// Regression for the entrypoint-detection bug: `npx -y @infona-ai/mcp` and a global
// `npm i -g @infona-ai/mcp` invoke the package's `bin` through a SYMLINK. The old
// guard compared `import.meta.url` to `pathToFileURL(process.argv[1]).href`
// WITHOUT resolving symlinks, so through the bin symlink the two never matched,
// `main()` never ran, and the server silently exited 0 with no output — a
// configured-but-dead MCP server. This test spawns the built server THROUGH a
// symlink (the exact broken path) and asserts it completes the MCP handshake.
//
// Runs against the built dist/ (CI builds all workspaces before `npm test`).

const here = dirname(fileURLToPath(import.meta.url));
const distEntry = join(here, "..", "dist", "index.js");

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "infona-mcp-entry-"));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

/** Spawn `node <entry>` as an MCP stdio server, send one `initialize` request,
 *  and resolve with the parsed response (or reject on timeout / early exit). */
function handshake(entry: string): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const env = {
      ...process.env,
      // Exact root-README MCP JSON: local OSS, no INFONA_API_KEY.
      INFONA_API_URL: "http://localhost:8000",
      INFONA_TENANT: "default",
    };
    delete env.INFONA_API_KEY;
    const child = spawn(process.execPath, [entry], {
      stdio: ["pipe", "pipe", "pipe"],
      env,
    });
    let out = "";
    let settled = false;
    const done = (fn: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.kill();
      fn();
    };
    const timer = setTimeout(
      () => done(() => reject(new Error("timeout: server never responded to initialize"))),
      15_000,
    );
    child.on("exit", (code) =>
      done(() => reject(new Error(`server exited (code ${code}) before responding — the entrypoint guard did not fire`))),
    );
    child.stdout.on("data", (chunk) => {
      out += chunk.toString();
      const line = out.split("\n").find((l) => l.trim().startsWith("{"));
      if (!line) return;
      try {
        const msg = JSON.parse(line);
        if (msg.id === 1) done(() => resolve(msg));
      } catch {
        /* partial line — keep buffering */
      }
    });
    child.stdin.write(
      JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "entrypoint-test", version: "0.0.0" },
        },
      }) + "\n",
    );
  });
}

describe("stdio entrypoint auto-start", () => {
  it("starts when invoked THROUGH A SYMLINK (npx / npm i -g path)", async () => {
    if (!existsSync(distEntry)) {
      throw new Error(`built entry missing at ${distEntry} — run \`npm run build\` first`);
    }
    const link = join(dir, "infona-mcp");
    symlinkSync(distEntry, link);

    const res = await handshake(link);
    expect((res.result as Record<string, unknown>) ?? {}).toMatchObject({
      serverInfo: { name: "infona" },
    });
  });

  it("starts when invoked DIRECTLY (node dist/index.js)", async () => {
    if (!existsSync(distEntry)) {
      throw new Error(`built entry missing at ${distEntry} — run \`npm run build\` first`);
    }
    const res = await handshake(distEntry);
    expect((res.result as Record<string, unknown>) ?? {}).toMatchObject({
      serverInfo: { name: "infona" },
    });
  });

  it("exits with a one-liner when hosted URL has no key", async () => {
    if (!existsSync(distEntry)) {
      throw new Error(`built entry missing at ${distEntry} — run \`npm run build\` first`);
    }
    const emptyHome = mkdtempSync(join(tmpdir(), "infona-mcp-hosted-"));
    try {
      const env = {
        ...process.env,
        HOME: emptyHome,
        USERPROFILE: emptyHome,
        INFONA_API_URL: "https://api.infona.ai",
      };
      delete env.INFONA_API_KEY;
      delete env.INFONA_TENANT;
      const { code, stderr } = await new Promise<{
        code: number | null;
        stderr: string;
      }>((resolve, reject) => {
        const child = spawn(process.execPath, [distEntry], {
          stdio: ["pipe", "pipe", "pipe"],
          env,
        });
        let stderr = "";
        child.stderr.on("data", (c) => {
          stderr += c.toString();
        });
        const timer = setTimeout(() => {
          child.kill();
          reject(new Error("timeout: hosted-without-key process did not exit"));
        }, 15_000);
        child.on("exit", (code) => {
          clearTimeout(timer);
          resolve({ code, stderr });
        });
      });
      expect(code).toBe(1);
      expect(stderr).toMatch(/INFONA_API_KEY/);
      expect(stderr).toMatch(/localhost:8000/);
    } finally {
      rmSync(emptyHome, { recursive: true, force: true });
    }
  });
});
