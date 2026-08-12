import { homedir } from "node:os";
import { join } from "node:path";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
  chmodSync,
  unlinkSync,
} from "node:fs";

export interface InfonaConfig {
  apiKey?: string;
  apiUrl?: string;
  tenant?: string;
  email?: string;
  /** Working context graph, set by `infona use <kg>`; commands fall back to it
   *  when `--kg` is not passed. */
  defaultKg?: string;
}

/** Default local open-access API (OSS self-host / `oss_setup.sh`). */
export const LOCAL_API_URL = "http://localhost:8000";
/** Default tenant for open-access local backends (`INFONA_API_KEYS` empty). */
export const LOCAL_DEFAULT_TENANT = "default";

/**
 * Clerk user ids look like `user_2abc…`. They are NOT workspace/tenant ids.
 * A historical `infona login` bug wrote `tenant: userId` into config; the API
 * then 403s with "does not grant access to tenant 'user_…'".
 */
export function isClerkUserId(value: string | undefined | null): boolean {
  return typeof value === "string" && /^user_[A-Za-z0-9]+$/.test(value);
}

/** Canonical config dir. */
function configDir(): string {
  return join(homedir(), ".infona");
}

function configPath(): string {
  return join(configDir(), "config.json");
}

/**
 * Read `~/.infona/config.json`. Returns an empty object if the file is absent
 * or unreadable — callers should treat fields as optional.
 */
export function readConfig(): InfonaConfig {
  const path = configPath();
  if (!existsSync(path)) return {};
  try {
    const raw = readFileSync(path, "utf-8");
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === "object") {
      return parsed as InfonaConfig;
    }
  } catch {
    // Corrupt or unreadable; behave as if absent so a fresh login can rewrite.
  }
  return {};
}

/** True when `~/.infona/config.json` exists on disk (even if empty/`{}`). */
export function configFileExists(): boolean {
  return existsSync(configPath());
}

/**
 * True when the config file carries connection fields that mean "already set
 * up" — an API key and/or a non-empty `apiUrl`. A file that only has
 * `defaultKg` (or is `{}`) still counts as empty for the connect wizard.
 */
export function configHasConnection(cfg: InfonaConfig = readConfig()): boolean {
  return Boolean(
    (typeof cfg.apiKey === "string" && cfg.apiKey.length > 0) ||
      (typeof cfg.apiUrl === "string" && cfg.apiUrl.length > 0),
  );
}

/**
 * Env vars that establish a connection without a config file.
 * Precedence: flags > env > config > wizard (ONTA-540).
 */
export function envHasConnection(
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  return Boolean(
    (env.INFONA_API_KEY && env.INFONA_API_KEY.length > 0) ||
      (env.INFONA_API_URL && env.INFONA_API_URL.length > 0),
  );
}

/**
 * True when we already know how to reach a backend without running the
 * interactive connect wizard. Flags (`--local` / `--no-login`) are handled
 * by the caller — they are one-off and never count as "saved connection".
 */
export function hasResolvedConnection(opts?: {
  env?: NodeJS.ProcessEnv;
  cfg?: InfonaConfig;
}): boolean {
  const env = opts?.env ?? process.env;
  const cfg = opts?.cfg ?? readConfig();
  return envHasConnection(env) || configHasConnection(cfg);
}

/**
 * Write `~/.infona/config.json` with `chmod 600`. Creates the directory (mode
 * 0o700) if needed.
 *
 * Default: merge with the existing config so callers can update one field
 * without clobbering the others. Pass `{ replace: true }` to overwrite the
 * file with exactly `patch` (used by `infona init` after confirm, and by the
 * local open-access path so a stale cloud `apiKey` is not left behind).
 *
 * Fields set to `undefined` / `null` are omitted from the written JSON when
 * replacing; on merge, `undefined` removes the key from the next write.
 */
export function writeConfig(
  patch: InfonaConfig,
  opts?: { replace?: boolean },
): void {
  const dir = configDir();
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true, mode: 0o700 });
  }
  const base: InfonaConfig = opts?.replace ? {} : readConfig();
  const merged: Record<string, unknown> = { ...base };
  for (const [k, v] of Object.entries(patch)) {
    if (v === undefined || v === null) {
      delete merged[k];
    } else {
      merged[k] = v;
    }
  }
  const path = configPath();
  writeFileSync(path, JSON.stringify(merged, null, 2) + "\n", "utf-8");
  try {
    chmodSync(path, 0o600);
  } catch {
    // best-effort; some filesystems (e.g., FAT) don't honor chmod
  }
}

/**
 * Write the canonical local open-access config. Clears any cloud credentials
 * so a prior `infona login` cannot leak X-API-Key onto a no-auth local server.
 *
 * Always preserves `defaultKg` (working graph context) even when `replace` is
 * true — connection mode change must not wipe the user's last `infona use`.
 * `replace` still clobbers connection fields (apiKey / email / apiUrl / tenant)
 * by writing a full open-access shape via writeConfig({ replace: true }).
 */
export function writeLocalOpenAccessConfig(opts?: {
  apiUrl?: string;
  tenant?: string;
  replace?: boolean;
}): InfonaConfig {
  const apiUrl = opts?.apiUrl ?? LOCAL_API_URL;
  const tenant = opts?.tenant ?? LOCAL_DEFAULT_TENANT;
  const prev = readConfig();
  const next: InfonaConfig = {
    apiUrl,
    tenant,
    // Explicitly clear credentials on local open-access.
    apiKey: undefined,
    email: undefined,
  };
  // Keep defaultKg across connection rewrites (replace or merge).
  if (prev.defaultKg) {
    next.defaultKg = prev.defaultKg;
  }
  writeConfig(next, { replace: true });
  return { apiUrl, tenant, ...(next.defaultKg ? { defaultKg: next.defaultKg } : {}) };
}

/** Remove the config file if present (tests / hard reset). */
export function clearConfigFile(): void {
  const path = configPath();
  if (existsSync(path)) {
    try {
      unlinkSync(path);
    } catch {
      // best-effort
    }
  }
}

export function configPathForDisplay(): string {
  return configPath();
}

/** True when a URL targets localhost / 127.0.0.1 (open-access local candidate). */
export function isLocalhostUrl(url: string | undefined | null): boolean {
  if (!url) return false;
  return /^(https?:\/\/)?(localhost|127\.0\.0\.1)(:|\/|$)/i.test(url);
}
