/**
 * First-run connect wizard + `infona init` (ONTA-540 / P-K0).
 *
 * Dual audience:
 *  1. Repo OSS setup (`scripts/oss_setup.sh`) writes local open-access config
 *     non-interactively after a health probe.
 *  2. npm CLI alone: empty config → interactive menu (local / browser / API key).
 *     Never silent cloud browser login; never silent force-local on global install.
 *
 * Precedence (caller-enforced): flags (one-off, no silent config wipe)
 *   > env > config file > wizard when empty.
 *
 * Hard rules:
 *  - Never open a browser for open-access local.
 *  - Never force local mode for pure cloud npm users (global install alone).
 *  - OSS only — no proprietary imports.
 *  - Never leave an owned readline open: non-interactive paths use write-only
 *    IO (no readline); interactive paths close in try/finally so `init --local`
 *    and first-run shell do not hang / dual-readline (ONTA-540 review P0).
 */

import { createInterface, type Interface as ReadlineInterface } from "node:readline";
import { stdin, stdout } from "node:process";
import {
  LOCAL_API_URL,
  LOCAL_DEFAULT_TENANT,
  configHasConnection,
  configPathForDisplay,
  hasResolvedConnection,
  isLocalhostUrl,
  readConfig,
  writeConfig,
  writeLocalOpenAccessConfig,
  type InfonaConfig,
} from "./config.js";

export type ConnectChoice = "local" | "browser" | "apikey" | "cancel";

export interface ProbeResult {
  ok: boolean;
  requiresAuth: boolean;
  url: string;
  error?: string;
}

export interface ConnectIo {
  /** Read one line; tests inject a scripted sequence. */
  question: (prompt: string) => Promise<string>;
  write: (s: string) => void;
  /** Open the browser login flow. Injected so tests never spawn a server. */
  runBrowserLogin?: () => Promise<void>;
  /** Health/auth probe. Defaults to fetch against `/health` + `/kgs`. */
  probe?: (baseUrl: string, tenant?: string) => Promise<ProbeResult>;
  /** Whether stdin is a TTY (wizard requires interactive). */
  isTty?: boolean;
  /**
   * Release owned resources (readline). Idempotent. Callers that create
   * interactive IO via `defaultIo()` must close in a finally block; injected
   * test IO typically omits this.
   */
  close?: () => void;
}

const DIM = "\x1b[2m";
const BOLD = "\x1b[1m";
const GREEN = "\x1b[32m";
const YELLOW = "\x1b[33m";
const CYAN = "\x1b[36m";
const RESET = "\x1b[0m";
const useColor = (): boolean =>
  Boolean(stdout.isTTY) && !process.env.NO_COLOR;

function c(code: string, s: string): string {
  return useColor() ? `${code}${s}${RESET}` : s;
}

/** Default health probe — same semantics as Client.healthCheck, no Client dep. */
export async function probeBackend(
  baseUrl: string,
  tenant: string = LOCAL_DEFAULT_TENANT,
): Promise<ProbeResult> {
  const url = baseUrl.replace(/\/+$/, "");
  const healthUrl = `${url}/health`;
  try {
    const res = await fetch(healthUrl, {
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) {
      try {
        const data = (await res.json()) as { neo4j?: unknown; status?: unknown };
        if (typeof data.neo4j === "boolean") {
          return { ok: true, requiresAuth: false, url };
        }
      } catch {
        /* not JSON */
      }
      return { ok: false, requiresAuth: false, url, error: `HTTP ${res.status}` };
    }
  } catch (err) {
    return {
      ok: false,
      requiresAuth: false,
      url,
      error: err instanceof Error ? err.message : String(err),
    };
  }
  // Probe whether endpoints require auth by hitting /kgs without X-API-Key.
  try {
    const res = await fetch(`${url}/graphs/${tenant}/kgs`, {
      headers: { "Content-Type": "application/json" },
      signal: AbortSignal.timeout(5000),
    });
    return {
      ok: true,
      requiresAuth: res.status === 401,
      url,
    };
  } catch {
    // Reachable health but kgs probe failed — treat as auth-required to be safe
    // for cloud; local open-access still works if health alone succeeded and
    // caller already chose local.
    return { ok: true, requiresAuth: true, url };
  }
}

/**
 * Write-only IO — no readline. Use for non-interactive local paths
 * (`connectLocal`, `init --local`, probes that only write status) so the
 * process can exit cleanly without an open stdin handle.
 */
export function writeOnlyIo(): ConnectIo {
  return {
    question: async () => {
      throw new Error(
        "writeOnlyIo does not support questions (non-interactive path)",
      );
    },
    write: (s: string) => {
      stdout.write(s);
    },
    isTty: Boolean(stdin.isTTY && stdout.isTTY),
    probe: probeBackend,
  };
}

/**
 * Interactive IO with an owned readline interface.
 * Caller MUST call `close()` (prefer try/finally) so the process can exit and
 * so the first-run shell does not stack a second readline on top of this one.
 *
 * Pass an existing `rl` to borrow (e.g. tests); then `close` is a no-op.
 */
export function defaultIo(rl?: ReadlineInterface): ConnectIo {
  const ownRl =
    rl ??
    createInterface({
      input: stdin,
      output: stdout,
      terminal: Boolean(stdin.isTTY),
    });
  const ownsRl = !rl;
  let closed = false;
  return {
    question: (prompt: string) =>
      new Promise((resolve) => {
        ownRl.question(prompt, (ans) => {
          resolve(ans);
        });
      }),
    write: (s: string) => {
      stdout.write(s);
    },
    isTty: Boolean(stdin.isTTY && stdout.isTTY),
    probe: probeBackend,
    // lazy import to avoid pulling http server into pure unit paths
    runBrowserLogin: async () => {
      const { runLogin } = await import("./login.js");
      await runLogin();
    },
    close: () => {
      if (ownsRl && !closed) {
        closed = true;
        ownRl.close();
      }
    },
  };
}

/** Run `fn` with either injected IO or a freshly created factory IO; always close owned. */
async function withIo<T>(
  provided: ConnectIo | undefined,
  factory: () => ConnectIo,
  fn: (io: ConnectIo) => Promise<T>,
): Promise<T> {
  if (provided) {
    return fn(provided);
  }
  const io = factory();
  try {
    return await fn(io);
  } finally {
    io.close?.();
  }
}

/**
 * If replace would clobber an existing saved connection, require confirm (TTY)
 * or `--force` (non-TTY / scripts). Returns an error string when blocked,
 * otherwise null (caller may proceed).
 *
 * Pure local re-writes of the same open-access shape are allowed without force
 * so `init --local` / oss_setup remain idempotent.
 */
export async function confirmReplaceConnection(opts: {
  io: ConnectIo;
  force?: boolean;
  /** Target local open-access URL (for same-shape skip). */
  apiUrl?: string;
  tenant?: string;
}): Promise<string | null> {
  const existing = readConfig();
  if (!configHasConnection(existing)) {
    return null;
  }
  const apiUrl = opts.apiUrl ?? LOCAL_API_URL;
  const tenant = opts.tenant ?? LOCAL_DEFAULT_TENANT;
  const sameLocalOpenAccess =
    !existing.apiKey &&
    isLocalhostUrl(existing.apiUrl) &&
    (existing.apiUrl ?? "").replace(/\/+$/, "") === apiUrl.replace(/\/+$/, "") &&
    (existing.tenant ?? LOCAL_DEFAULT_TENANT) === tenant;
  if (sameLocalOpenAccess) {
    return null;
  }

  if (opts.force) {
    return null;
  }

  const isTty = opts.io.isTty ?? Boolean(stdin.isTTY && stdout.isTTY);
  if (!isTty) {
    return (
      `Existing connection in ${configPathForDisplay()} ` +
      `(apiUrl=${existing.apiUrl ?? "(default)"}, ` +
      `apiKey=${existing.apiKey ? "set" : "none"}). ` +
      `Pass --force to overwrite in non-interactive mode ` +
      `(or run \`infona init\` in a TTY to confirm).`
    );
  }

  opts.io.write(
    `\n  ${c(YELLOW, "Existing connection")}:\n` +
      `    apiUrl=${existing.apiUrl ?? c(DIM, "(default cloud)")}\n` +
      `    tenant=${existing.tenant ?? c(DIM, "(default)")}\n` +
      `    apiKey=${existing.apiKey ? c(DIM, "(set)") : c(DIM, "(none)")}\n` +
      `    file=${configPathForDisplay()}\n\n`,
  );
  const ans = (
    await opts.io.question(
      `  ${c(YELLOW, "Overwrite")} saved credentials with local open-access? [y/N]: `,
    )
  )
    .trim()
    .toLowerCase();
  if (ans !== "y" && ans !== "yes") {
    opts.io.write(`  ${c(DIM, "Kept existing config.")}\n`);
    return "cancelled";
  }
  return null;
}

/** Parse wizard menu choice from a raw line. */
export function parseConnectChoice(raw: string): ConnectChoice | null {
  const a = raw.trim().toLowerCase();
  if (a === "1" || a === "l" || a === "local") return "local";
  if (a === "2" || a === "b" || a === "browser" || a === "cloud" || a === "login")
    return "browser";
  if (a === "3" || a === "k" || a === "key" || a === "apikey" || a === "api-key" || a === "api key")
    return "apikey";
  if (a === "q" || a === "c" || a === "cancel" || a === "quit" || a === "n")
    return "cancel";
  return null;
}

/**
 * Persist local open-access connection after a successful probe.
 * Refuses to open a browser. If the local server requires auth, returns an
 * error (user should pick API key or fix INFONA_API_KEYS).
 *
 * Defaults to **write-only IO** (no readline) so `infona init --local` and
 * scripted callers exit cleanly. Pass an interactive `io` only when you need
 * clobber confirm prompts.
 */
export async function connectLocal(opts?: {
  apiUrl?: string;
  tenant?: string;
  io?: ConnectIo;
  /** When true, overwrite the whole config file (init confirm path). */
  replace?: boolean;
  /** Skip the live probe (oss_setup already probed, or tests). */
  skipProbe?: boolean;
  /**
   * Skip the existing-connection clobber gate (wizard already confirmed, or
   * explicit `init --local --force`).
   */
  force?: boolean;
  /**
   * When true (default for `replace` without prior wizard confirm), gate
   * overwriting a different saved connection: TTY confirm, non-TTY needs
   * `force`. Set false when the wizard already confirmed overwrite.
   */
  confirmClobber?: boolean;
}): Promise<{ ok: true; config: InfonaConfig } | { ok: false; error: string }> {
  const apiUrl = opts?.apiUrl ?? LOCAL_API_URL;
  const tenant = opts?.tenant ?? LOCAL_DEFAULT_TENANT;
  // Prefer write-only: this path never needs questions unless confirmClobber
  // requires a TTY prompt. When confirm may need a prompt, use interactive
  // only if no io was injected and we are on a TTY with an existing connection.
  const needsConfirmGate =
    Boolean(opts?.replace) &&
    opts?.confirmClobber !== false &&
    !opts?.force &&
    configHasConnection();

  const factory = (): ConnectIo => {
    if (needsConfirmGate && stdin.isTTY && stdout.isTTY) {
      return defaultIo();
    }
    return writeOnlyIo();
  };

  return withIo(opts?.io, factory, async (io) => {
    if (opts?.replace && opts?.confirmClobber !== false) {
      const blocked = await confirmReplaceConnection({
        io,
        force: opts?.force,
        apiUrl,
        tenant,
      });
      if (blocked === "cancelled") {
        return { ok: false, error: "cancelled" };
      }
      if (blocked) {
        return { ok: false, error: blocked };
      }
    }

    const probe = io.probe ?? probeBackend;

    if (!opts?.skipProbe) {
      const health = await probe(apiUrl, tenant);
      if (!health.ok) {
        return {
          ok: false,
          error:
            `Could not reach ${apiUrl}` +
            (health.error ? ` (${health.error})` : "") +
            `. Is the API running? (e.g. uvicorn … --port 8000)`,
        };
      }
      // Hard rule: never browser for open-access local. If local requires auth,
      // tell the user to use the API-key path instead of opening a browser.
      if (health.requiresAuth) {
        return {
          ok: false,
          error:
            `Local server at ${apiUrl} requires authentication. ` +
            `Choose the API key option (or unset INFONA_API_KEYS for open access).`,
        };
      }
    }

    const config = writeLocalOpenAccessConfig({
      apiUrl,
      tenant,
      replace: opts?.replace,
    });
    io.write(
      `  ${c(GREEN, "✓")} Local open-access saved to ${configPathForDisplay()}\n` +
        `    apiUrl=${config.apiUrl}  tenant=${config.tenant}\n` +
        `    Bare ${c(BOLD, "infona")} works without --local.\n\n`,
    );
    return { ok: true, config };
  });
}

/**
 * Prompt for an API key (+ optional URL / tenant) and write config.
 * Never opens a browser.
 */
export async function connectApiKey(opts?: {
  io?: ConnectIo;
  replace?: boolean;
  /** Non-interactive values (tests / scripted). */
  values?: { apiKey?: string; apiUrl?: string; tenant?: string };
}): Promise<{ ok: true; config: InfonaConfig } | { ok: false; error: string }> {
  const needsQuestions = !opts?.values?.apiKey;
  const factory = (): ConnectIo =>
    needsQuestions ? defaultIo() : writeOnlyIo();

  return withIo(opts?.io, factory, async (io) => {
    let apiKey = opts?.values?.apiKey?.trim() ?? "";
    if (!apiKey) {
      apiKey = (await io.question("  API key: ")).trim();
    }
    if (!apiKey) {
      return { ok: false, error: "No API key entered." };
    }

    let apiUrl: string;
    if (opts?.values?.apiUrl !== undefined) {
      apiUrl =
        opts.values.apiUrl.trim() || "https://api.infona.ai";
    } else {
      apiUrl =
        (
          await io.question(
            `  API URL [${c(DIM, "https://api.infona.ai")}]: `,
          )
        ).trim() || "https://api.infona.ai";
    }
    apiUrl = apiUrl.replace(/\/+$/, "");

    let tenant: string;
    if (opts?.values?.tenant !== undefined) {
      tenant =
        opts.values.tenant.trim() ||
        (isLocalhostUrl(apiUrl) ? LOCAL_DEFAULT_TENANT : "demo-tenant");
    } else {
      tenant =
        (
          await io.question(
            `  Tenant / workspace id [${c(DIM, isLocalhostUrl(apiUrl) ? LOCAL_DEFAULT_TENANT : "demo-tenant")}]: `,
          )
        ).trim() ||
        (isLocalhostUrl(apiUrl) ? LOCAL_DEFAULT_TENANT : "demo-tenant");
    }

    const patch: InfonaConfig = { apiKey, apiUrl, tenant };
    if (opts?.replace) {
      writeConfig(patch, { replace: true });
    } else {
      writeConfig(patch);
    }
    io.write(
      `  ${c(GREEN, "✓")} Credentials saved to ${configPathForDisplay()}\n` +
        `    apiUrl=${apiUrl}  tenant=${tenant}\n\n`,
    );
    return { ok: true, config: { ...readConfig() } };
  });
}

/**
 * Interactive connect menu. Used on first run (empty connection) and by
 * `infona init` (force / re-init with confirm).
 *
 * Returns `"cancelled"` if the user backs out without writing, `"ok"` on
 * success, `"non-interactive"` when stdin is not a TTY (caller should print
 * a hint and exit).
 *
 * Owns and closes readline when `io` is not injected — safe for shell
 * first-run before the shell creates its own interface.
 */
export async function runConnectWizard(opts?: {
  /** Re-init path: show confirm when credentials already exist. */
  force?: boolean;
  io?: ConnectIo;
  /** Pre-select a choice (tests). */
  choice?: ConnectChoice;
}): Promise<"ok" | "cancelled" | "non-interactive"> {
  return withIo(opts?.io, defaultIo, async (io) => {
    const isTty = io.isTty ?? Boolean(stdin.isTTY && stdout.isTTY);

    if (!isTty && opts?.choice === undefined) {
      io.write(
        `  No connection configured. Run ${c(BOLD, "infona init")} in a terminal,\n` +
          `  or set INFONA_API_URL / INFONA_API_KEY, or pass --local.\n`,
      );
      return "non-interactive";
    }

    if (opts?.force || configHasConnection()) {
      const existing = readConfig();
      if (configHasConnection(existing)) {
        io.write(
          `\n  ${c(YELLOW, "Existing connection")}:\n` +
            `    apiUrl=${existing.apiUrl ?? c(DIM, "(default cloud)")}\n` +
            `    tenant=${existing.tenant ?? c(DIM, "(default)")}\n` +
            `    apiKey=${existing.apiKey ? c(DIM, "(set)") : c(DIM, "(none)")}\n` +
            `    file=${configPathForDisplay()}\n\n`,
        );
        const ans = (
          await io.question(
            `  ${c(YELLOW, "Overwrite")} saved credentials? [y/N]: `,
          )
        )
          .trim()
          .toLowerCase();
        if (ans !== "y" && ans !== "yes") {
          io.write(`  ${c(DIM, "Kept existing config.")}\n`);
          return "cancelled";
        }
      }
    }

    io.write(`\n  ${c(BOLD, "Connect Infona")}\n`);
    io.write(
      `  ${c(DIM, "How do you want to reach a backend?")}\n\n` +
        `    ${c(CYAN, "1")}) Local open-access  ${c(DIM, `(${LOCAL_API_URL}, tenant ${LOCAL_DEFAULT_TENANT})`)}\n` +
        `    ${c(CYAN, "2")}) Browser sign-in    ${c(DIM, "(hosted Infona cloud)")}\n` +
        `    ${c(CYAN, "3")}) API key            ${c(DIM, "(paste key + optional URL)")}\n` +
        `    ${c(CYAN, "q")}) Cancel\n\n`,
    );

    let choice: ConnectChoice | null = opts?.choice ?? null;
    if (!choice) {
      const raw = await io.question("  Choice [1/2/3/q]: ");
      choice = parseConnectChoice(raw);
    }
    if (!choice || choice === "cancel") {
      io.write(`  ${c(DIM, "Cancelled.")}\n`);
      return "cancelled";
    }

    if (choice === "local") {
      const result = await connectLocal({
        io,
        replace: true, // clobber credentials after confirm / on first write
        // Wizard already confirmed overwrite when force/existing.
        confirmClobber: false,
        force: true,
      });
      if (!result.ok) {
        io.write(`  ${c(YELLOW, "✗")} ${result.error}\n`);
        return "cancelled";
      }
      return "ok";
    }

    if (choice === "browser") {
      // Browser is for hosted cloud only — never for open-access local.
      const runLogin = io.runBrowserLogin;
      if (!runLogin) {
        io.write(
          `  ${c(YELLOW, "✗")} Browser login is not available in this environment.\n`,
        );
        return "cancelled";
      }
      io.write(
        `  ${c(DIM, "Opening browser sign-in for hosted Infona…")}\n`,
      );
      await runLogin();
      // runLogin writes apiKey (+ tenant) via writeConfig merge. Ensure we don't
      // leave a stale local apiUrl pointing at localhost if the user had one.
      const after = readConfig();
      if (after.apiKey && isLocalhostUrl(after.apiUrl)) {
        writeConfig({ apiUrl: "https://api.infona.ai" });
      }
      return after.apiKey ? "ok" : "cancelled";
    }

    // apikey
    const result = await connectApiKey({ io, replace: true });
    if (!result.ok) {
      io.write(`  ${c(YELLOW, "✗")} ${result.error}\n`);
      return "cancelled";
    }
    return "ok";
  });
}

/**
 * Ensure a connection exists before entering the interactive shell.
 *
 * Flags (`local` / `noLogin`) are one-off: they do NOT write config and do
 * NOT run the wizard. Env / existing config skip the wizard. Empty → wizard
 * on TTY.
 *
 * Returns false when the user cancelled or non-interactive with no config
 * (caller should exit). Does not open a browser for open-access local.
 *
 * Closes any owned readline before returning so the shell can open its own
 * interface without dual-readline.
 */
export async function ensureConnected(opts?: {
  local?: boolean;
  noLogin?: boolean;
  io?: ConnectIo;
}): Promise<boolean> {
  // One-off flags: proceed without wizard / without config write.
  if (opts?.local || opts?.noLogin) {
    return true;
  }
  if (hasResolvedConnection()) {
    return true;
  }
  return withIo(opts?.io, defaultIo, async (io) => {
    io.write(
      `\n  ${c(DIM, "First run — no connection configured yet.")}\n`,
    );
    const result = await runConnectWizard({ io, force: false });
    return result === "ok" || hasResolvedConnection();
  });
}

/**
 * Non-interactive local setup used by `scripts/oss_setup.sh`.
 * Probes health, writes config, never opens a browser.
 * No readline — safe for shell scripts.
 */
export async function setupLocalNonInteractive(opts?: {
  apiUrl?: string;
  tenant?: string;
  probe?: (baseUrl: string, tenant?: string) => Promise<ProbeResult>;
  write?: typeof writeLocalOpenAccessConfig;
}): Promise<{ ok: true; config: InfonaConfig } | { ok: false; error: string }> {
  const apiUrl = opts?.apiUrl ?? LOCAL_API_URL;
  const tenant = opts?.tenant ?? LOCAL_DEFAULT_TENANT;
  const probe = opts?.probe ?? probeBackend;
  const health = await probe(apiUrl, tenant);
  if (!health.ok) {
    return {
      ok: false,
      error: `Health probe failed for ${apiUrl}` + (health.error ? `: ${health.error}` : ""),
    };
  }
  if (health.requiresAuth) {
    return {
      ok: false,
      error:
        `Local server requires auth — not writing open-access config. ` +
        `Use \`infona init\` and pick API key, or clear INFONA_API_KEYS.`,
    };
  }
  const write = opts?.write ?? writeLocalOpenAccessConfig;
  const config = write({ apiUrl, tenant, replace: true });
  return { ok: true, config };
}
