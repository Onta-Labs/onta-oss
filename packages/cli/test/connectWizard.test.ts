/**
 * Hermetic tests for ONTA-540 (P-K0) CLI connect wizard + local open-access.
 *
 * Success criteria (SC1–SC10):
 *  SC1  setupLocalNonInteractive / writeLocalOpenAccessConfig writes
 *       apiUrl=http://localhost:8000, tenant=default after open-access probe
 *  SC2  config.apiUrl local → Client baseUrl is localhost (bare infona, no --local)
 *  SC3  empty connection + TTY → wizard runs (not silent browser login)
 *  SC4  wizard local path probes, writes config, never opens browser
 *  SC5  wizard browser path calls runBrowserLogin only (cloud)
 *  SC6  wizard apikey path writes key + url + tenant
 *  SC7  force/init with existing config requires confirm before clobber
 *  SC8  hasResolvedConnection: env > config; flags are caller one-off
 *  SC9  --local / ensureConnected({local:true}) does not write config
 *  SC10 pure empty install never auto-writes local; non-interactive empty → hint
 *  P0  connectLocal / init --local must not hang (no orphan readline)
 *  P1  writeLocalOpenAccessConfig preserves defaultKg even with replace
 *  P2  init --local clobber gate: non-TTY needs --force for different connection
 */

import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import {
  mkdtempSync,
  readFileSync,
  existsSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { join, dirname } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { execFileSync, spawn } from "node:child_process";

import {
  LOCAL_API_URL,
  LOCAL_DEFAULT_TENANT,
  clearConfigFile,
  configFileExists,
  configHasConnection,
  envHasConnection,
  hasResolvedConnection,
  isLocalhostUrl,
  readConfig,
  writeConfig,
  writeLocalOpenAccessConfig,
} from "../src/config.js";
import {
  connectApiKey,
  connectLocal,
  ensureConnected,
  parseConnectChoice,
  runConnectWizard,
  setupLocalNonInteractive,
  writeOnlyIo,
  type ConnectIo,
  type ProbeResult,
} from "../src/connect.js";
import { Client } from "../src/client.js";

// --- harness: isolate ~/.infona via HOME ------------------------------------ #

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

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), "infona-connect-"));
  process.env.HOME = home;
  process.env.USERPROFILE = home; // windows-ish
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
  vi.restoreAllMocks();
});

function scriptedIo(answers: string[], probes?: ProbeResult[]): {
  io: ConnectIo;
  writes: string[];
  /** Mutable counters — read `.browser` / `.probes` after the call. */
  counts: { browser: number; probes: string[] };
} {
  const writes: string[] = [];
  let ai = 0;
  let pi = 0;
  const counts = { browser: 0, probes: [] as string[] };
  const io: ConnectIo = {
    isTty: true,
    write: (s) => {
      writes.push(s);
    },
    question: async () => {
      const a = answers[ai++] ?? "";
      return a;
    },
    probe: async (url) => {
      counts.probes.push(url);
      if (probes && probes[pi] !== undefined) return probes[pi++]!;
      return { ok: true, requiresAuth: false, url };
    },
    runBrowserLogin: async () => {
      counts.browser += 1;
      writeConfig({
        apiKey: "browser-key",
        tenant: "ws-from-browser",
        apiUrl: "https://api.infona.ai",
      });
    },
  };
  return { io, writes, counts };
}

function readConfigFile(): Record<string, unknown> {
  const p = join(home, ".infona", "config.json");
  expect(existsSync(p)).toBe(true);
  return JSON.parse(readFileSync(p, "utf-8")) as Record<string, unknown>;
}

// --- config helpers --------------------------------------------------------- #

describe("config connection helpers (SC1/SC8)", () => {
  it("configHasConnection is false for empty / defaultKg-only", () => {
    expect(configHasConnection({})).toBe(false);
    expect(configHasConnection({ defaultKg: "books" })).toBe(false);
    expect(configHasConnection({ apiUrl: LOCAL_API_URL })).toBe(true);
    expect(configHasConnection({ apiKey: "k" })).toBe(true);
  });

  it("envHasConnection respects INFONA_API_KEY / INFONA_API_URL", () => {
    expect(envHasConnection({})).toBe(false);
    expect(envHasConnection({ INFONA_API_KEY: "k" })).toBe(true);
    expect(envHasConnection({ INFONA_API_URL: "http://x" })).toBe(true);
  });

  it("hasResolvedConnection: env wins over empty file (SC8)", () => {
    expect(hasResolvedConnection({ env: {}, cfg: {} })).toBe(false);
    expect(
      hasResolvedConnection({
        env: { INFONA_API_URL: "http://localhost:8000" },
        cfg: {},
      }),
    ).toBe(true);
    writeLocalOpenAccessConfig({ replace: true });
    expect(hasResolvedConnection({ env: {}, cfg: readConfig() })).toBe(true);
  });

  it("isLocalhostUrl", () => {
    expect(isLocalhostUrl("http://localhost:8000")).toBe(true);
    expect(isLocalhostUrl("http://127.0.0.1:8000")).toBe(true);
    expect(isLocalhostUrl("https://api.infona.ai")).toBe(false);
  });
});

// --- SC1: local open-access write shape ------------------------------------- #

describe("writeLocalOpenAccessConfig / setupLocalNonInteractive (SC1)", () => {
  it("writes apiUrl + tenant default and clears cloud apiKey (P1: keeps defaultKg)", () => {
    writeConfig({ apiKey: "cloud-secret", email: "a@b.c", defaultKg: "keep-me" });
    const cfg = writeLocalOpenAccessConfig({ replace: true });
    expect(cfg).toEqual({
      apiUrl: LOCAL_API_URL,
      tenant: LOCAL_DEFAULT_TENANT,
      defaultKg: "keep-me",
    });
    const disk = readConfigFile();
    expect(disk).toEqual({
      apiUrl: "http://localhost:8000",
      tenant: "default",
      defaultKg: "keep-me",
    });
    expect(disk.apiKey).toBeUndefined();
    expect(disk.email).toBeUndefined();
  });

  it("setupLocalNonInteractive probes then writes (SC1)", async () => {
    const probes: ProbeResult[] = [
      { ok: true, requiresAuth: false, url: LOCAL_API_URL },
    ];
    let i = 0;
    const result = await setupLocalNonInteractive({
      probe: async (url) => {
        expect(url).toBe(LOCAL_API_URL);
        return probes[i++]!;
      },
    });
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.config.apiUrl).toBe(LOCAL_API_URL);
      expect(result.config.tenant).toBe("default");
    }
    expect(readConfigFile()).toMatchObject({
      apiUrl: "http://localhost:8000",
      tenant: "default",
    });
  });

  it("setupLocalNonInteractive refuses auth-required local (no silent browser)", async () => {
    const result = await setupLocalNonInteractive({
      probe: async () => ({
        ok: true,
        requiresAuth: true,
        url: LOCAL_API_URL,
      }),
    });
    expect(result.ok).toBe(false);
    expect(configFileExists()).toBe(false);
  });

  it("setupLocalNonInteractive fails closed on unreachable health", async () => {
    const result = await setupLocalNonInteractive({
      probe: async () => ({
        ok: false,
        requiresAuth: false,
        url: LOCAL_API_URL,
        error: "ECONNREFUSED",
      }),
    });
    expect(result.ok).toBe(false);
    expect(configFileExists()).toBe(false);
  });
});

// --- SC2: Client uses config apiUrl (bare infona, no --local) --------------- #

describe("Client respects saved local apiUrl (SC2)", () => {
  it("reads apiUrl and tenant from config without flags", () => {
    writeLocalOpenAccessConfig({ replace: true });
    const c = new Client();
    expect(c.baseUrl).toBe("http://localhost:8000");
    expect(c.tenant).toBe("default");
    expect(c.apiKey).toBeUndefined();
  });
});

// --- parse choices ---------------------------------------------------------- #

describe("parseConnectChoice", () => {
  it("maps menu inputs", () => {
    expect(parseConnectChoice("1")).toBe("local");
    expect(parseConnectChoice("local")).toBe("local");
    expect(parseConnectChoice("2")).toBe("browser");
    expect(parseConnectChoice("cloud")).toBe("browser");
    expect(parseConnectChoice("3")).toBe("apikey");
    expect(parseConnectChoice("key")).toBe("apikey");
    expect(parseConnectChoice("q")).toBe("cancel");
    expect(parseConnectChoice("nope")).toBeNull();
  });
});

// --- SC3/SC4/SC5/SC6 wizard paths ------------------------------------------- #

describe("runConnectWizard (SC3–SC7)", () => {
  it("SC3/SC4: local choice probes, writes config, never opens browser", async () => {
    const { io, counts } = scriptedIo([], [
      { ok: true, requiresAuth: false, url: LOCAL_API_URL },
    ]);
    const result = await runConnectWizard({ io, choice: "local" });
    expect(result).toBe("ok");
    expect(counts.browser).toBe(0);
    expect(counts.probes).toEqual([LOCAL_API_URL]);
    expect(readConfigFile()).toEqual({
      apiUrl: "http://localhost:8000",
      tenant: "default",
    });
  });

  it("SC4: local with requiresAuth does not write and does not browser", async () => {
    const { io, counts } = scriptedIo([], [
      { ok: true, requiresAuth: true, url: LOCAL_API_URL },
    ]);
    const result = await runConnectWizard({ io, choice: "local" });
    expect(result).toBe("cancelled");
    expect(counts.browser).toBe(0);
    expect(configFileExists()).toBe(false);
  });

  it("SC5: browser choice calls runBrowserLogin only", async () => {
    const { io, counts } = scriptedIo([]);
    const result = await runConnectWizard({ io, choice: "browser" });
    expect(result).toBe("ok");
    expect(counts.browser).toBe(1);
    expect(readConfig().apiKey).toBe("browser-key");
  });

  it("SC6: apikey path writes key + url + tenant", async () => {
    const { io, counts } = scriptedIo([
      "sk-test-key",
      "https://api.example.com",
      "my-ws",
    ]);
    const result = await runConnectWizard({ io, choice: "apikey" });
    expect(result).toBe("ok");
    expect(counts.browser).toBe(0);
    expect(readConfigFile()).toMatchObject({
      apiKey: "sk-test-key",
      apiUrl: "https://api.example.com",
      tenant: "my-ws",
    });
  });

  it("SC7: force re-init with existing config requires confirm; N keeps config", async () => {
    writeLocalOpenAccessConfig({ replace: true });
    const before = readConfigFile();
    const { io, counts } = scriptedIo(["n"]); // decline overwrite
    const result = await runConnectWizard({ io, force: true, choice: "browser" });
    // choice is ignored when confirm is N — cancelled before menu action
    expect(result).toBe("cancelled");
    expect(counts.browser).toBe(0);
    expect(readConfigFile()).toEqual(before);
  });

  it("SC7: force re-init Y then local clobbers prior apiKey", async () => {
    writeConfig({
      apiKey: "old-cloud",
      apiUrl: "https://api.infona.ai",
      tenant: "old-ws",
    });
    const { io } = scriptedIo(
      ["y"], // confirm overwrite
      [{ ok: true, requiresAuth: false, url: LOCAL_API_URL }],
    );
    const result = await runConnectWizard({ io, force: true, choice: "local" });
    expect(result).toBe("ok");
    const disk = readConfigFile();
    expect(disk.apiKey).toBeUndefined();
    expect(disk.apiUrl).toBe(LOCAL_API_URL);
    expect(disk.tenant).toBe("default");
  });

  it("SC10: non-interactive empty → non-interactive (no silent local write)", async () => {
    const writes: string[] = [];
    const io: ConnectIo = {
      isTty: false,
      write: (s) => writes.push(s),
      question: async () => {
        throw new Error("should not prompt");
      },
    };
    const result = await runConnectWizard({ io });
    expect(result).toBe("non-interactive");
    expect(configFileExists()).toBe(false);
    expect(writes.join("")).toMatch(/infona init/);
  });
});

// --- SC9: flags are one-off, no silent config wipe -------------------------- #

describe("ensureConnected flags (SC9)", () => {
  it("--local proceeds without writing config", async () => {
    const writes: string[] = [];
    const io: ConnectIo = {
      isTty: true,
      write: (s) => writes.push(s),
      question: async () => {
        throw new Error("wizard must not run for --local");
      },
    };
    const ok = await ensureConnected({ local: true, io });
    expect(ok).toBe(true);
    expect(configFileExists()).toBe(false);
  });

  it("--no-login proceeds without writing config", async () => {
    const ok = await ensureConnected({
      noLogin: true,
      io: {
        isTty: true,
        write: () => {},
        question: async () => {
          throw new Error("no wizard");
        },
      },
    });
    expect(ok).toBe(true);
    expect(configFileExists()).toBe(false);
  });

  it("existing env skips wizard", async () => {
    process.env.INFONA_API_KEY = "from-env";
    const ok = await ensureConnected({
      io: {
        isTty: true,
        write: () => {},
        question: async () => {
          throw new Error("no wizard when env set");
        },
      },
    });
    expect(ok).toBe(true);
    expect(configFileExists()).toBe(false);
  });

  it("empty connection runs wizard (SC3)", async () => {
    const { io } = scriptedIo([], [
      { ok: true, requiresAuth: false, url: LOCAL_API_URL },
    ]);
    // ensureConnected will call runConnectWizard; inject choice via wrapping
    // — script answers: menu "1"
    const answers = ["1"];
    let ai = 0;
    io.question = async () => answers[ai++] ?? "";
    const ok = await ensureConnected({ io });
    expect(ok).toBe(true);
    expect(readConfig().apiUrl).toBe(LOCAL_API_URL);
  });
});

// --- connectLocal / connectApiKey unit -------------------------------------- #

describe("connectLocal / connectApiKey", () => {
  it("connectLocal skipProbe writes without fetch", async () => {
    const { io, counts } = scriptedIo([]);
    const result = await connectLocal({
      io,
      skipProbe: true,
      replace: true,
      force: true,
    });
    expect(result.ok).toBe(true);
    expect(counts.probes).toHaveLength(0);
    expect(readConfigFile().apiUrl).toBe(LOCAL_API_URL);
  });

  it("connectLocal default path uses write-only IO (no hang) + preserves defaultKg", async () => {
    writeConfig({ defaultKg: "books", apiKey: "x", apiUrl: "https://api.infona.ai" });
    // force so we don't need TTY confirm; no custom io → writeOnlyIo factory
    const result = await connectLocal({
      skipProbe: true,
      replace: true,
      force: true,
    });
    expect(result.ok).toBe(true);
    const disk = readConfigFile();
    expect(disk.apiUrl).toBe(LOCAL_API_URL);
    expect(disk.defaultKg).toBe("books");
    expect(disk.apiKey).toBeUndefined();
  });

  it("P2: non-TTY connectLocal refuses clobber of different connection without force", async () => {
    writeConfig({
      apiKey: "cloud-secret",
      apiUrl: "https://api.infona.ai",
      tenant: "ws",
    });
    const writes: string[] = [];
    const io: ConnectIo = {
      isTty: false,
      write: (s) => writes.push(s),
      question: async () => {
        throw new Error("non-TTY must not prompt");
      },
    };
    const result = await connectLocal({
      io,
      skipProbe: true,
      replace: true,
      // force omitted
    });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).toMatch(/--force/);
    }
    // Config unchanged
    expect(readConfig().apiKey).toBe("cloud-secret");
  });

  it("P2: same local open-access rewrite is idempotent without force", async () => {
    writeLocalOpenAccessConfig({ replace: true });
    const io = writeOnlyIo();
    // Override isTty false to simulate script
    const scripted: ConnectIo = {
      ...io,
      isTty: false,
      write: () => {},
      probe: async () => ({ ok: true, requiresAuth: false, url: LOCAL_API_URL }),
    };
    const result = await connectLocal({
      io: scripted,
      skipProbe: true,
      replace: true,
    });
    expect(result.ok).toBe(true);
  });

  it("connectApiKey with values is non-interactive", async () => {
    const { io } = scriptedIo([]);
    const result = await connectApiKey({
      io,
      replace: true,
      values: {
        apiKey: "k",
        apiUrl: "http://127.0.0.1:9000",
        tenant: "t1",
      },
    });
    expect(result.ok).toBe(true);
    expect(readConfigFile()).toMatchObject({
      apiKey: "k",
      apiUrl: "http://127.0.0.1:9000",
      tenant: "t1",
    });
  });
});

// --- P0: no orphan readline hang (child process + timeout) ------------------ #

describe("init --local does not hang (P0 readline regression)", () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const pkgRoot = join(here, "..");
  const cliPath = join(pkgRoot, "dist", "cli.js");

  beforeAll(() => {
    // Exercise the real published bin (same as cliSymlink tests).
    execFileSync("npm", ["run", "build"], { cwd: pkgRoot, stdio: "pipe" });
    if (!existsSync(cliPath)) {
      throw new Error(`expected built CLI at ${cliPath}`);
    }
  }, 120_000);

  it("infona init --local exits within timeout (success or probe-fail)", async () => {
    const testHome = mkdtempSync(join(tmpdir(), "infona-hang-"));
    try {
      // Intentionally leave stdin open (pipe) — the old defaultIo() hang only
      // shows up when readline owns an open stdin and is never closed.
      const child = spawn(process.execPath, [cliPath, "init", "--local", "--force"], {
        env: {
          ...process.env,
          HOME: testHome,
          USERPROFILE: testHome,
          NO_COLOR: "1",
          // Ensure we don't inherit a real connection from the parent env.
          INFONA_API_KEY: "",
          INFONA_API_URL: "",
          INFONA_TENANT: "",
        },
        stdio: ["pipe", "pipe", "pipe"],
      });

      let stdout = "";
      let stderr = "";
      child.stdout?.on("data", (d: Buffer) => {
        stdout += d.toString();
      });
      child.stderr?.on("data", (d: Buffer) => {
        stderr += d.toString();
      });

      const exitCode = await new Promise<number | null>((resolve, reject) => {
        const timer = setTimeout(() => {
          child.kill("SIGKILL");
          reject(
            new Error(
              `init --local hung >2s (readline leak?). stdout=${stdout} stderr=${stderr}`,
            ),
          );
        }, 2000);
        child.on("error", (err) => {
          clearTimeout(timer);
          reject(err);
        });
        child.on("close", (code) => {
          clearTimeout(timer);
          resolve(code);
        });
      });

      // 0 if a local API happens to be up; 1 on probe failure. Either is fine —
      // the regression is hanging forever after printing success/error.
      expect(exitCode === 0 || exitCode === 1).toBe(true);
    } finally {
      rmSync(testHome, { recursive: true, force: true });
    }
  }, 15_000);

  it("connectLocal(skipProbe) child exits 0 with open stdin", async () => {
    const testHome = mkdtempSync(join(tmpdir(), "infona-hang2-"));
    const { readdirSync } = await import("node:fs");
    const distFiles = readdirSync(join(pkgRoot, "dist"));
    const connectChunk = distFiles.find(
      (f) => f.startsWith("connect-") && f.endsWith(".js") && !f.endsWith(".map"),
    );
    expect(connectChunk).toBeTruthy();

    const harness = join(testHome, "hang-check.mjs");
    writeFileSync(
      harness,
      `import { connectLocal } from ${JSON.stringify(join(pkgRoot, "dist", connectChunk!))};
const r = await connectLocal({ skipProbe: true, replace: true, force: true });
if (!r.ok) { console.error(r.error); process.exit(2); }
process.exit(0);
`,
      "utf-8",
    );

    try {
      const child = spawn(process.execPath, [harness], {
        env: {
          ...process.env,
          HOME: testHome,
          USERPROFILE: testHome,
          NO_COLOR: "1",
        },
        stdio: ["pipe", "pipe", "pipe"], // stdin open — hang repro condition
        cwd: pkgRoot,
      });
      let stderr = "";
      child.stderr?.on("data", (d: Buffer) => {
        stderr += d.toString();
      });
      const exitCode = await new Promise<number | null>((resolve, reject) => {
        const timer = setTimeout(() => {
          child.kill("SIGKILL");
          reject(new Error(`connectLocal child hung >2s. stderr=${stderr}`));
        }, 2000);
        child.on("close", (code) => {
          clearTimeout(timer);
          resolve(code);
        });
        child.on("error", (err) => {
          clearTimeout(timer);
          reject(err);
        });
      });
      expect(exitCode, `stderr=${stderr}`).toBe(0);
    } finally {
      rmSync(testHome, { recursive: true, force: true });
    }
  }, 15_000);
});

// --- writeConfig replace / clear -------------------------------------------- #

describe("writeConfig replace semantics", () => {
  it("merge keeps prior fields; replace overwrites", () => {
    writeConfig({ apiKey: "a", tenant: "t", defaultKg: "kg1" });
    writeConfig({ apiUrl: "http://x" });
    expect(readConfig()).toMatchObject({
      apiKey: "a",
      tenant: "t",
      apiUrl: "http://x",
      defaultKg: "kg1",
    });
    writeConfig({ apiUrl: LOCAL_API_URL, tenant: "default" }, { replace: true });
    expect(readConfig()).toEqual({
      apiUrl: LOCAL_API_URL,
      tenant: "default",
    });
  });

  it("clearConfigFile removes the file", () => {
    writeLocalOpenAccessConfig({ replace: true });
    expect(configFileExists()).toBe(true);
    clearConfigFile();
    expect(configFileExists()).toBe(false);
  });
});
