/**
 * First-run opt-in for anonymous job telemetry (ONTA-548).
 *
 * The Python server is what sends events. This module only asks once (TTY)
 * and writes ``~/.infona/telemetry.json``. ``INFONA_TELEMETRY=0`` still wins
 * on the server even after a yes here.
 */
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
  chmodSync,
} from "node:fs";
import { createInterface } from "node:readline";
import { randomUUID } from "node:crypto";
import { stdin, stdout } from "node:process";

export const TELEMETRY_ENV = "INFONA_TELEMETRY";
export const TELEMETRY_STATE_ENV = "INFONA_TELEMETRY_STATE";

export interface TelemetryState {
  opt_in: boolean;
  asked: boolean;
  install_id: string;
}

export interface TelemetryPromptIo {
  question: (prompt: string) => Promise<string>;
  write: (s: string) => void;
  isTty?: boolean;
}

const _OFF = new Set(["0", "false", "off", "no"]);
const _ON = new Set(["1", "true", "on", "yes"]);

export const CONSENT_BLURB =
  "Infona can send anonymous job telemetry to help improve the product.\n" +
  "\n" +
  "What is sent: job type (ingest / ask / er rebuild / export), a row-count\n" +
  "bucket (not the exact count), source type (csv / json / … — never a\n" +
  "filename), and an error class (exception type or HTTP family — never the\n" +
  "message).\n" +
  "\n" +
  "What is never sent: your data, column names, file names, graph content,\n" +
  "workspace ids, prompts, answers, Cypher, or emails.\n" +
  "\n" +
  "Disabled by default. Turn it off anytime with INFONA_TELEMETRY=0.\n";

export function telemetryStatePath(
  env: NodeJS.ProcessEnv = process.env,
): string {
  const override = (env[TELEMETRY_STATE_ENV] ?? "").trim();
  if (override) return override;
  return join(homedir(), ".infona", "telemetry.json");
}

export function envTelemetryOverride(
  env: NodeJS.ProcessEnv = process.env,
): boolean | null {
  const raw = (env[TELEMETRY_ENV] ?? "").trim().toLowerCase();
  if (_OFF.has(raw)) return false;
  if (_ON.has(raw)) return true;
  return null;
}

export function readTelemetryState(
  env: NodeJS.ProcessEnv = process.env,
): TelemetryState | null {
  const path = telemetryStatePath(env);
  if (!existsSync(path)) return null;
  try {
    const parsed = JSON.parse(readFileSync(path, "utf-8")) as unknown;
    if (!parsed || typeof parsed !== "object") return null;
    const o = parsed as Record<string, unknown>;
    return {
      opt_in: o.opt_in === true,
      asked: o.asked === true,
      install_id:
        typeof o.install_id === "string" && o.install_id
          ? o.install_id
          : randomUUID(),
    };
  } catch {
    return null;
  }
}

export function writeTelemetryState(
  state: TelemetryState,
  env: NodeJS.ProcessEnv = process.env,
): void {
  const path = telemetryStatePath(env);
  const dir = dirname(path);
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true, mode: 0o700 });
  }
  writeFileSync(path, JSON.stringify(state, null, 2) + "\n", "utf-8");
  try {
    chmodSync(path, 0o600);
  } catch {
    // best-effort
  }
}

export function isTelemetryEnabled(
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  const forced = envTelemetryOverride(env);
  if (forced === false) return false;
  if (forced === true) return true;
  return readTelemetryState(env)?.opt_in === true;
}

function defaultIo(): TelemetryPromptIo {
  const rl = createInterface({ input: stdin, output: stdout });
  return {
    question: (prompt) =>
      new Promise((resolve) => {
        rl.question(prompt, (ans) => {
          rl.close();
          resolve(ans);
        });
      }),
    write: (s) => {
      stdout.write(s);
    },
    isTty: Boolean(stdin.isTTY && stdout.isTTY),
  };
}

/**
 * Ask once on a TTY when env does not already decide. Non-interactive and
 * already-asked installs are a no-op. Never throws.
 */
export async function maybePromptTelemetryConsent(opts?: {
  io?: TelemetryPromptIo;
  env?: NodeJS.ProcessEnv;
}): Promise<TelemetryState | null> {
  const env = opts?.env ?? process.env;
  try {
    if (envTelemetryOverride(env) !== null) {
      return readTelemetryState(env);
    }
    const existing = readTelemetryState(env);
    if (existing?.asked) {
      return existing;
    }
    const io = opts?.io;
    const isTty = io?.isTty ?? Boolean(stdin.isTTY && stdout.isTTY);
    if (!isTty) {
      return existing;
    }
    const promptIo = io ?? defaultIo();
    promptIo.write(`\n${CONSENT_BLURB}\n`);
    const ans = (
      await promptIo.question("Share anonymous job telemetry? [y/N] ")
    )
      .trim()
      .toLowerCase();
    const state: TelemetryState = {
      opt_in: ans === "y" || ans === "yes",
      asked: true,
      install_id: existing?.install_id || randomUUID(),
    };
    writeTelemetryState(state, env);
    return state;
  } catch {
    return null;
  }
}
