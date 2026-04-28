import { homedir } from "node:os";
import { join } from "node:path";
import { existsSync, mkdirSync, readFileSync, writeFileSync, chmodSync } from "node:fs";

export interface CographConfig {
  apiKey?: string;
  apiUrl?: string;
  tenant?: string;
  email?: string;
}

function configDir(): string {
  return join(homedir(), ".cograph");
}

function configPath(): string {
  return join(configDir(), "config.json");
}

/**
 * Read `~/.cograph/config.json`. Returns an empty object if the file is
 * missing or unreadable — callers should treat fields as optional.
 */
export function readConfig(): CographConfig {
  const path = configPath();
  if (!existsSync(path)) return {};
  try {
    const raw = readFileSync(path, "utf-8");
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === "object") {
      return parsed as CographConfig;
    }
  } catch {
    // Corrupt or unreadable; behave as if absent so a fresh login can rewrite.
  }
  return {};
}

/**
 * Write `~/.cograph/config.json` with `chmod 600`. Creates the directory if
 * needed. Merges with the existing config so callers can update one field
 * without clobbering the others.
 */
export function writeConfig(patch: CographConfig): void {
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
