import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

// Regression for the entrypoint-detection bug: `npx -y @onta/mcp` and a global
// `npm i -g @onta/mcp` invoke the package's `bin` through a SYMLINK. The old
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
  dir = mkdtempSync(join(tmpdir(), "cograph-mcp-entry-"));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

/** Spawn `node <entry>` as an MCP stdio server, send one `initialize` request,
 *  and resolve with the parsed response (or reject on timeout / early exit). */
function handshake(entry: string): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [entry], {
      stdio: ["pipe", "pipe", "pipe"],
      // No backend needed: MCP `initialize` is a local handshake. Provide dummy
      // env so nothing in module init throws on a missing var.
      env: {
        ...process.env,
        COGRAPH_API_URL: "http://localhost:1",
        COGRAPH_API_KEY: "test",
        COGRAPH_TENANT: "test",
      },
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
    const link = join(dir, "onta-mcp");
    symlinkSync(distEntry, link);

    const res = await handshake(link);
    expect((res.result as Record<string, unknown>) ?? {}).toMatchObject({
      serverInfo: { name: "cograph" },
    });
  });

  it("starts when invoked DIRECTLY (node dist/index.js)", async () => {
    const res = await handshake(distEntry);
    expect((res.result as Record<string, unknown>) ?? {}).toMatchObject({
      serverInfo: { name: "cograph" },
    });
  });
});
