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

function defaultIo(rl?: ReadlineInterface): ConnectIo {
  const ownRl =
    rl ??
    createInterface({
      input: stdin,
      output: stdout,
      terminal: Boolean(stdin.isTTY),
    });
  const ownsRl = !rl;
  return {
    question: (prompt: string) =>
      new Promise((resolve) => {
        ownRl.question(prompt, (ans) => {
          if (ownsRl) {
            // keep open across multi-step wizard; caller closes via closeIo
          }
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
  };
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
 */
export async function connectLocal(opts?: {
  apiUrl?: string;
  tenant?: string;
  io?: ConnectIo;
  /** When true, overwrite the whole config file (init confirm path). */
  replace?: boolean;
  /** Skip the live probe (oss_setup already probed, or tests). */
  skipProbe?: boolean;
}): Promise<{ ok: true; config: InfonaConfig } | { ok: false; error: string }> {
  const apiUrl = opts?.apiUrl ?? LOCAL_API_URL;
  const tenant = opts?.tenant ?? LOCAL_DEFAULT_TENANT;
  const io = opts?.io ?? defaultIo();
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
  const io = opts?.io ?? defaultIo();
  let apiKey = opts?.values?.apiKey?.trim() ?? "";
  if (!apiKey) {
    apiKey = (await io.question("  API key: ")).trim();
  }
  if (!apiKey) {
    return { ok: false, error: "No API key entered." };
  }

  let apiUrl =
    opts?.values?.apiUrl?.trim() ||
    (await io.question(
      `  API URL [${c(DIM, "https://api.infona.ai")}]: `,
    )).trim() ||
    "https://api.infona.ai";
  apiUrl = apiUrl.replace(/\/+$/, "");

  let tenant =
    opts?.values?.tenant?.trim() ||
    (await io.question(
      `  Tenant / workspace id [${c(DIM, isLocalhostUrl(apiUrl) ? LOCAL_DEFAULT_TENANT : "demo-tenant")}]: `,
    )).trim() ||
    (isLocalhostUrl(apiUrl) ? LOCAL_DEFAULT_TENANT : "demo-tenant");

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
}

/**
 * Interactive connect menu. Used on first run (empty connection) and by
 * `infona init` (force / re-init with confirm).
 *
 * Returns `"cancelled"` if the user backs out without writing, `"ok"` on
 * success, `"non-interactive"` when stdin is not a TTY (caller should print
 * a hint and exit).
 */
export async function runConnectWizard(opts?: {
  /** Re-init path: show confirm when credentials already exist. */
  force?: boolean;
  io?: ConnectIo;
  /** Pre-select a choice (tests). */
  choice?: ConnectChoice;
}): Promise<"ok" | "cancelled" | "non-interactive"> {
  const io = opts?.io ?? defaultIo();
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
      io.write(`  ${c(YELLOW, "✗")} Browser login is not available in this environment.\n`);
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
  const io = opts?.io ?? defaultIo();
  io.write(
    `\n  ${c(DIM, "First run — no connection configured yet.")}\n`,
  );
  const result = await runConnectWizard({ io, force: false });
  return result === "ok" || hasResolvedConnection();
}

/**
 * Non-interactive local setup used by `scripts/oss_setup.sh`.
 * Probes health, writes config, never opens a browser.
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
