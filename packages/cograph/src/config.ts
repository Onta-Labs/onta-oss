import { homedir } from "node:os";
import { join } from "node:path";
import { existsSync, mkdirSync, readFileSync, writeFileSync, chmodSync } from "node:fs";

export interface OntaConfig {
  apiKey?: string;
  apiUrl?: string;
  tenant?: string;
  email?: string;
  /** Working context graph, set by `onta use <kg>`; commands fall back to it
   *  when `--kg` is not passed. */
  defaultKg?: string;
}

/** Canonical config dir — the write target. */
function configDir(): string {
  return join(homedir(), ".onta");
}

function configPath(): string {
  return join(configDir(), "config.json");
}

/**
 * Legacy config path (pre-rename `~/.cograph/config.json`). Read-only fallback:
 * a login created under the old brand keeps working until the next
 * {@link writeConfig}, which migrates it forward to `~/.onta`.
 */
function legacyConfigPath(): string {
  return join(homedir(), ".cograph", "config.json");
}

/**
 * Read `~/.onta/config.json`. If that file is absent but the legacy
 * `~/.cograph/config.json` exists, the legacy file is read transparently so
 * existing logins keep working. Returns an empty object if neither file is
 * present or the chosen file is unreadable — callers should treat fields as
 * optional.
 */
export function readConfig(): OntaConfig {
  let path = configPath();
  if (!existsSync(path)) {
    const legacy = legacyConfigPath();
    if (!existsSync(legacy)) return {};
    path = legacy;
  }
  try {
    const raw = readFileSync(path, "utf-8");
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === "object") {
      return parsed as OntaConfig;
    }
  } catch {
    // Corrupt or unreadable; behave as if absent so a fresh login can rewrite.
  }
  return {};
}

/**
 * Write `~/.onta/config.json` with `chmod 600`. Creates the directory (mode
 * 0o700) if needed. Merges with the existing config so callers can update one
 * field without clobbering the others — and because the merge reads through
 * {@link readConfig}, a legacy `~/.cograph/config.json` is picked up and
 * migrated forward to `~/.onta` on the first write after the rename.
 */
export function writeConfig(patch: OntaConfig): void {
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
