// ONTA-415: containment-guarded enumeration of a user-configured local
// directory. This is the ONE primitive behind both the `list_local_files` tool
// and the "did you mean" suffix on `ingest_csv`'s not-found error, so there is
// exactly one guard to audit and no back-door enumeration path.
//
// WHY THIS MODULE IS WRITTEN THIS DEFENSIVELY
// The MCP server runs as a LOCAL CHILD PROCESS with the invoking user's full
// filesystem privileges (StdioServerTransport, spawned by `npx` from a client
// config), and everything it returns is rendered into a REMOTE model's context.
// There is no sandbox anywhere in that path. Before this module the entire TS
// tree had never enumerated a directory: filesystem access was exactly stat +
// read of a caller-supplied path, plus ~/.onta/config.json. Directory listing is
// therefore a NEW CAPABILITY CLASS for this server, and every guard below is
// load-bearing rather than defensive decoration.
//
// WHY THERE IS NO DEFAULT ROOT
// The obvious default, `process.cwd()`, is a trap: cwd is chosen by the MCP
// CLIENT, not by the user. Claude Desktop on macOS spawns servers with cwd `/`
// or the app bundle, so a cwd default is simultaneously useless (it will not
// contain the user's data) and worst-case unbounded (`/`). The root must be
// stated explicitly by the user via an env var, and when none resolves the tool
// is not registered at all.
//
// NAMING
// The module-internal vocabulary says "workspace" (the user's local working
// directory), but the ENV VAR deliberately does not. "Workspace" is the
// user-facing word for a TENANT in Infona's copy, so `INFONA_WORKSPACE_DIR`
// would read as "the tenant's directory on disk", which is not what this is.
// The var is `INFONA_LOCAL_FILES_DIR`: it names the tool it enables
// (`list_local_files`), it says "local" (as opposed to anything server-side),
// and it cannot be misread as a tenant.

import { lstatSync, readdirSync, realpathSync, statSync } from "node:fs";
import {
  delimiter,
  extname,
  isAbsolute,
  join,
  parse,
  relative,
  resolve,
  sep,
} from "node:path";

// Env vars consulted for the root, in precedence order. This MIRRORS the SDK's
// private `envVar()` helper (packages/cograph/src/client.ts): INFONA_ (current
// brand) then ONTA_ then COGRAPH_ then OMNIX_ (legacy). That helper is
// module-private and is NOT exported from `@infona-ai/cli`, so it cannot
// literally be reused here; the precedence is duplicated deliberately and
// must stay in lockstep with it.
export const LOCAL_FILES_ENV_VARS = [
  "INFONA_LOCAL_FILES_DIR",
  "ONTA_LOCAL_FILES_DIR",
  "COGRAPH_LOCAL_FILES_DIR",
  "OMNIX_LOCAL_FILES_DIR",
] as const;

// Extension allowlist. Deliberately EXACTLY the ingestible structured formats in
// the SDK's `EXT_FORMAT` map (packages/cograph/src/client.ts):
//   - `.tsv` is NOT included. It is absent from `EXT_FORMAT`, so it would fall
//     through to `fmt = "text"`; listing it here as an ingestible data file
//     would be a lie to the model.
//   - `.txt` is NOT included either. It maps to the TEXT ingest path, which is a
//     different tool contract than "here is a tabular payload".
export const ALLOWED_EXTENSIONS = [".csv", ".json", ".jsonl"] as const;

export const MAX_ROOTS = 4;
export const DEFAULT_DEPTH = 2;
export const MAX_DEPTH = 3;
export const DEFAULT_LIMIT = 50;
export const MAX_RESULTS = 100;
// Independent of the results cap: a huge tree must not be able to stall the
// call even when almost nothing in it matches the extension allowlist.
export const MAX_DIRENTS = 20_000;
export const MAX_NAME_LEN = 120;
const MAX_PATTERN_LEN = 100;
const MAX_PATTERN_WILDCARDS = 4;

// Control characters (C0 + DEL), the Unicode bidi overrides / isolates that are
// the classic filename-spoofing primitive, and the zero-width characters (ZWSP /
// ZWNJ / ZWJ / BOM) that let two visually identical names differ. Filenames in a
// Downloads or shared directory are ATTACKER-CONTROLLABLE, and this module is the
// first place in this server that renders untrusted local strings into model
// context.
const UNSAFE_NAME_RE = /[\u0000-\u001F\u007F\u200B-\u200D\u202A-\u202E\u2066-\u2069\uFEFF]/;

export interface LocalFile {
  /** Absolute path, directly passable to `ingest_csv`. */
  path: string;
  /** Path relative to the configured root, for compact display. */
  relative_path: string;
  /** The configured root this file was found under. */
  root: string;
  size: number;
  /** ISO-8601 mtime. */
  mtime: string;
  /** Sort key; the raw epoch-ms mtime behind `mtime`. */
  mtime_ms: number;
}

export interface ListOptions {
  subdir?: string;
  pattern?: string;
  maxDepth?: number;
  limit?: number;
}

export interface ListResult {
  /** The `limit` most recently modified matches. */
  files: LocalFile[];
  /** Total matches found before the limit was applied. */
  total: number;
  truncated: boolean;
  /** Directory entries visited (the MAX_DIRENTS budget). */
  scanned: number;
  /** True when the walk stopped early because the dirent budget ran out. */
  budgetExhausted: boolean;
  /** Entries dropped because their name was unsafe or overlong. */
  skipped: number;
}

export interface RootResolution {
  /** Realpath'd, existing, absolute directories. Empty means "stay dormant". */
  roots: string[];
  /** Which env var supplied the value, if any. */
  varName?: string;
  /** Entries that did not resolve to an existing absolute directory. */
  rejected: string[];
  /** Entries rejected for naming a filesystem root such as "/". */
  fsRoots: string[];
  /** Entries dropped for exceeding MAX_ROOTS. */
  dropped: number;
}

function clamp(n: number, lo: number, hi: number): number {
  if (!Number.isFinite(n)) return lo;
  return Math.min(hi, Math.max(lo, Math.trunc(n)));
}

/** True when `p` is the root itself or lies underneath it. */
export function isContained(root: string, p: string): boolean {
  return p === root || p.startsWith(root + sep);
}

/**
 * True for a filesystem root ("/" on POSIX, "C:\\" on Windows).
 *
 * Such a root is REFUSED rather than supported. Two independent reasons:
 * granting the whole filesystem defeats the entire point of a scoped opt-in;
 * and `isContained` would be wrong for it anyway, since `"/" + sep` is `"//"`
 * and nothing would ever compare as contained. Refusing explicitly turns a
 * confusing "enabled, but every listing is empty" state into a clear message.
 */
export function isFilesystemRoot(p: string): boolean {
  return parse(p).root === p;
}

/**
 * Drop any root that lies inside another configured root.
 *
 * Without this, configuring both `/a` and `/a/b` returns every file under
 * `/a/b` TWICE (once per root), which reads to the model as two distinct files.
 * The outer root already covers the inner one, so keeping the outer is lossless.
 */
export function dedupeNestedRoots(roots: string[]): string[] {
  const unique = [...new Set(roots)];
  return unique.filter(
    (candidate) => !unique.some((other) => other !== candidate && isContained(other, candidate)),
  );
}

/**
 * Validate one path segment for rendering.
 *
 * Returns the name unchanged when it is safe to render, or `null` when the
 * entry must be DROPPED.
 *
 * Why drop rather than sanitize in place: the whole contract of a returned row
 * is that `path` can be handed straight to `ingest_csv`. A stripped or
 * truncated name produces a path that no longer exists on disk, so sanitizing
 * would leave us choosing between leaking the raw bytes into model context and
 * handing back a broken path. Dropping keeps both invariants, and the count of
 * dropped entries is surfaced in the output so it is diagnosable rather than
 * silent.
 */
export function safeName(name: string): string | null {
  if (!name || name.length > MAX_NAME_LEN) return null;
  if (UNSAFE_NAME_RE.test(name)) return null;
  // Leading / trailing whitespace (including NBSP, which `trim` also strips) is
  // invisible once rendered, so " report.csv" and "report.csv" would look like
  // the same file to the model while being two different paths.
  if (name !== name.trim()) return null;
  return name;
}

/**
 * Compile a caller-supplied filename filter.
 *
 * With `*` or `?` it is an anchored glob; otherwise it is a case-insensitive
 * substring match (what a user typing "tecentriq" means). Length and wildcard
 * count are capped so a caller cannot hand us a pattern whose backtracking cost
 * dominates the walk.
 */
export function compilePattern(pattern: string): RegExp | null {
  const p = pattern.trim();
  if (!p) return null;
  if (p.length > MAX_PATTERN_LEN) {
    throw new Error(`pattern is too long (max ${MAX_PATTERN_LEN} characters).`);
  }
  const wildcards = (p.match(/[*?]/g) ?? []).length;
  if (wildcards > MAX_PATTERN_WILDCARDS) {
    throw new Error(`pattern has too many wildcards (max ${MAX_PATTERN_WILDCARDS}).`);
  }
  const escaped = p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (wildcards === 0) return new RegExp(escaped, "i");
  const body = escaped.replace(/\\\*/g, ".*").replace(/\\\?/g, ".");
  return new RegExp(`^${body}$`, "i");
}

/**
 * Resolve the configured root(s) from the environment. Called ONCE at startup.
 *
 * Rules: split on the platform path delimiter, cap at MAX_ROOTS, require each
 * entry to be ABSOLUTE (a relative entry would resolve against the
 * client-controlled cwd, which is exactly the trap this feature avoids),
 * realpath it, and keep only existing directories. Realpath'ing here means every
 * later containment comparison is canonical-vs-canonical, which matters on macOS
 * where /var is a symlink to /private/var.
 */
export function resolveRoots(env: NodeJS.ProcessEnv = process.env): RootResolution {
  let varName: string | undefined;
  let raw: string | undefined;
  for (const name of LOCAL_FILES_ENV_VARS) {
    const value = env[name];
    if (value && value.trim()) {
      varName = name;
      raw = value;
      break;
    }
  }
  if (!raw) return { roots: [], rejected: [], fsRoots: [], dropped: 0 };

  const parts = raw
    .split(delimiter)
    .map((s) => s.trim())
    .filter(Boolean);
  const dropped = Math.max(0, parts.length - MAX_ROOTS);
  const roots: string[] = [];
  const rejected: string[] = [];
  const fsRoots: string[] = [];
  for (const part of parts.slice(0, MAX_ROOTS)) {
    if (!isAbsolute(part)) {
      rejected.push(part);
      continue;
    }
    try {
      const real = realpathSync(part);
      if (!statSync(real).isDirectory()) {
        rejected.push(part);
        continue;
      }
      if (isFilesystemRoot(real)) {
        fsRoots.push(part);
        continue;
      }
      if (!roots.includes(real)) roots.push(real);
    } catch {
      rejected.push(part);
    }
  }
  return { roots: dedupeNestedRoots(roots), varName, rejected, fsRoots, dropped };
}

/** One stderr line at startup, so a typo in the env var is diagnosable. */
export function describeRootResolution(res: RootResolution): string {
  const primary = LOCAL_FILES_ENV_VARS[0];
  if (!res.varName) {
    return (
      `onta-mcp: list_local_files is disabled (not registered). Set ${primary} ` +
      `to an absolute directory path to let the agent list local data files.`
    );
  }
  const notes: string[] = [];
  if (res.rejected.length) {
    notes.push(
      `ignored ${res.rejected.length} entry/entries that are not existing ` +
        `absolute directories: ${res.rejected.join(", ")}`,
    );
  }
  if (res.fsRoots.length) {
    notes.push(
      `refused ${res.fsRoots.join(", ")}: the whole filesystem cannot be a ` +
        `root, point ${primary} at the specific directory holding your data`,
    );
  }
  if (res.dropped) notes.push(`dropped ${res.dropped} entry/entries over the ${MAX_ROOTS}-root cap`);
  const suffix = notes.length ? ` (${notes.join("; ")})` : "";
  if (!res.roots.length) {
    return (
      `onta-mcp: list_local_files is disabled (not registered). ${res.varName} ` +
      `is set but no entry was usable as a root${suffix}.`
    );
  }
  return (
    `onta-mcp: list_local_files enabled from ${res.varName}; visible root(s): ` +
    `${res.roots.join(", ")}${suffix}.`
  );
}

/**
 * List the ingestible data files under `roots`.
 *
 * Containment rules, all mandatory:
 *  - Directory symlinks are NEVER followed. We classify with lstat semantics and
 *    simply do not descend into a link. This is simpler and strictly safer than
 *    realpath-then-contain during recursion, where a race between the check and
 *    the descent can still walk you out of the root.
 *  - Every RETURNED file is realpath'd and required to resolve inside its root.
 *    `..` in `subdir`, and a file symlink aimed outside the root, are both
 *    handled by that one check.
 *  - Dotfiles, dot-directories and `node_modules` are skipped.
 *  - Extension allowlist, depth cap, results cap, and an independent dirent
 *    budget all apply.
 *
 * NO FILE CONTENTS ARE READ. Not even a header preview, deliberately: the user's
 * consent act was "here is a directory", not "here is the content of every file
 * in it", and `ingest_csv` already surfaces the inferred schema mapping
 * server-side once a file is actually chosen. Revisit only with a reason that
 * survives that argument.
 */
export function listWorkspaceFiles(roots: string[], opts: ListOptions = {}): ListResult {
  const maxDepth = clamp(opts.maxDepth ?? DEFAULT_DEPTH, 0, MAX_DEPTH);
  const limit = clamp(opts.limit ?? DEFAULT_LIMIT, 1, MAX_RESULTS);
  const re = opts.pattern ? compilePattern(opts.pattern) : null;

  // Canonicalize the roots here too: `resolveRoots` already does it, but this
  // function is the guard, so it must not depend on its caller having done so.
  const resolved: string[] = [];
  for (const root of roots) {
    try {
      const real = realpathSync(root);
      // A filesystem root is refused here too, not just in `resolveRoots`: this
      // function is THE guard and must not depend on its caller having filtered.
      if (isFilesystemRoot(real)) continue;
      if (statSync(real).isDirectory() && !resolved.includes(real)) resolved.push(real);
    } catch {
      // A root that vanished since startup simply contributes nothing.
    }
  }
  const canonical = dedupeNestedRoots(resolved);

  const matches: LocalFile[] = [];
  let scanned = 0;
  let skipped = 0;
  let budgetExhausted = false;
  let startsFound = 0;

  for (const root of canonical) {
    let start = root;
    if (opts.subdir) {
      const requested = resolve(root, opts.subdir);
      let real: string;
      try {
        real = realpathSync(requested);
        if (!statSync(real).isDirectory()) continue;
      } catch {
        continue;
      }
      // Containment on the REALPATH: this is what turns `..`, an absolute
      // `subdir`, and a symlinked subdir into a rejection rather than an escape.
      if (!isContained(root, real)) continue;
      start = real;
    }
    startsFound++;

    const stack: Array<{ dir: string; depth: number }> = [{ dir: start, depth: 0 }];
    while (stack.length) {
      const { dir, depth } = stack.pop()!;
      let entries;
      try {
        entries = readdirSync(dir, { withFileTypes: true });
      } catch {
        // Unreadable directory (permissions, race). Skip it, do not abort.
        continue;
      }
      for (const entry of entries) {
        scanned++;
        if (scanned > MAX_DIRENTS) {
          budgetExhausted = true;
          break;
        }
        const name = entry.name;
        if (name.startsWith(".") || name === "node_modules") continue;
        if (safeName(name) === null) {
          skipped++;
          continue;
        }
        const full = join(dir, name);

        let isDir = entry.isDirectory();
        let isLink = entry.isSymbolicLink();
        let isReg = entry.isFile();
        if (!isDir && !isLink && !isReg) {
          // Some filesystems report DT_UNKNOWN for every dirent. Fall back to an
          // explicit LSTAT (which, unlike stat, does not follow the link) so an
          // unknown entry is classified by the same non-following rule.
          try {
            const ls = lstatSync(full);
            isDir = ls.isDirectory();
            isLink = ls.isSymbolicLink();
            isReg = ls.isFile();
          } catch {
            continue;
          }
        }

        if (isLink) {
          // Never descend a symlink, whatever it points at. It may still be
          // RETURNED if it resolves to a regular file inside the root, which the
          // realpath containment check below decides.
          consider(full, name, root, re, matches);
          continue;
        }
        if (isDir) {
          if (depth < maxDepth) stack.push({ dir: full, depth: depth + 1 });
          continue;
        }
        if (isReg) consider(full, name, root, re, matches);
      }
      if (budgetExhausted) break;
    }
    if (budgetExhausted) break;
  }

  if (opts.subdir && canonical.length && startsFound === 0) {
    throw new Error(
      `subdir "${opts.subdir}" was not found inside the configured root(s), or ` +
        `it resolves outside them. Only paths under the configured root are visible.`,
    );
  }

  // Most recently modified first: a freshly dropped payload is what the agent is
  // almost always looking for.
  matches.sort((a, b) => b.mtime_ms - a.mtime_ms);
  return {
    files: matches.slice(0, limit),
    total: matches.length,
    truncated: matches.length > limit,
    scanned,
    budgetExhausted,
    skipped,
  };
}

function consider(
  full: string,
  name: string,
  root: string,
  re: RegExp | null,
  out: LocalFile[],
): void {
  const ext = extname(name).toLowerCase();
  if (!(ALLOWED_EXTENSIONS as readonly string[]).includes(ext)) return;
  if (re && !re.test(name)) return;
  let real: string;
  let st;
  try {
    real = realpathSync(full);
    st = statSync(real);
  } catch {
    // Broken symlink, or the file vanished mid-walk.
    return;
  }
  // A symlink aimed at a DIRECTORY reaches here as a non-file; drop it.
  if (!st.isFile()) return;
  // THE containment check for returned rows.
  if (!isContained(root, real)) return;
  out.push({
    path: full,
    relative_path: relative(root, full) || name,
    root,
    size: st.size,
    mtime: st.mtime.toISOString(),
    mtime_ms: st.mtimeMs,
  });
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

/** Render a `ListResult` for the model. */
export function renderListResult(res: ListResult, roots: string[], maxDepth: number): string {
  const where = roots.join(", ");
  const lines: string[] = [];
  if (!res.files.length) {
    lines.push(
      `No ${ALLOWED_EXTENSIONS.join(" / ")} files matched under ${where} ` +
        `(depth <= ${maxDepth}). Only this root is visible to the server.`,
    );
  } else {
    lines.push(`${res.total} file(s) matched under ${where} (depth <= ${maxDepth}), most recently modified first:`);
    lines.push("");
    // Filenames are attacker-controllable text being rendered into model
    // context, so they are backtick-fenced as data rather than left bare. This
    // is a weak signal, not a security boundary (a name may itself contain a
    // backtick); the real containment is that only the granted root is visible.
    res.files.forEach((f, i) => {
      lines.push(`${i + 1}. \`${f.relative_path}\` | ${formatBytes(f.size)} | modified ${f.mtime}`);
      lines.push(`   \`${f.path}\``);
    });
  }
  const notes: string[] = [];
  if (res.truncated) {
    notes.push(
      `${res.total - res.files.length} more not shown. Narrow with \`subdir\` or ` +
        `\`pattern\`, or raise \`limit\` (max ${MAX_RESULTS}).`,
    );
  }
  if (res.budgetExhausted) {
    notes.push(
      `The scan stopped after ${MAX_DIRENTS} directory entries, so this listing ` +
        `may be incomplete. Narrow with \`subdir\` or a smaller \`max_depth\`.`,
    );
  }
  if (res.skipped) {
    notes.push(`${res.skipped} entry/entries were skipped because their filename was unsafe to render or overlong.`);
  }
  if (notes.length) {
    lines.push("");
    lines.push(...notes.map((n) => `Note: ${n}`));
  }
  return lines.join("\n");
}
