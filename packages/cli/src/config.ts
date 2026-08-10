import { homedir } from "node:os";
import { join } from "node:path";
import { existsSync, mkdirSync, readFileSync, writeFileSync, chmodSync } from "node:fs";

export interface InfonaConfig {
  apiKey?: string;
  apiUrl?: string;
  tenant?: string;
  email?: string;
  /** Working context graph, set by `infona use <kg>`; commands fall back to it
   *  when `--kg` is not passed. */
  defaultKg?: string;
}

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

/**
 * Write `~/.infona/config.json` with `chmod 600`. Creates the directory (mode
 * 0o700) if needed. Merges with the existing config so callers can update one
 * field without clobbering the others.
 */
export function writeConfig(patch: InfonaConfig): void {
  const dir = configDir();
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true, mode: 0o700 });
  }
  const merged = { ...readConfig(), ...patch };
  const path = configPath();
  writeFileSync(path, JSON.stringify(merged, null, 2) + "\n", "utf-8");
  try {
    chmodSync(path, 0o600);
  } catch {
    // best-effort; some filesystems (e.g., FAT) don't honor chmod
  }
}

export function configPathForDisplay(): string {
  return configPath();
}
