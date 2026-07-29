import {
  mkdirSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  symlinkSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  DEFAULT_LIMIT,
  LOCAL_FILES_ENV_VARS,
  MAX_RESULTS,
  MAX_ROOTS,
  compilePattern,
  describeRootResolution,
  isContained,
  listWorkspaceFiles,
  resolveRoots,
  safeName,
} from "../src/workspace.js";

// ONTA-415: this module is the FIRST directory enumeration in the whole TS tree,
// running as a local child process with the user's full filesystem privileges
// and rendering results into a remote model's context. The containment guards
// are the feature, so they are what these tests assert on.

let root: string;
let outside: string;

function touch(path: string, contents = "a,b\n1,2\n", mtimeSeconds?: number): string {
  writeFileSync(path, contents, "utf-8");
  if (mtimeSeconds !== undefined) utimesSync(path, mtimeSeconds, mtimeSeconds);
  return path;
}

beforeEach(() => {
  // realpath: on macOS the tmpdir is /var/... which is a symlink to /private/var,
  // and every containment comparison in this module is canonical-vs-canonical.
  root = realpathSync(mkdtempSync(join(tmpdir(), "onta-mcp-root-")));
  outside = realpathSync(mkdtempSync(join(tmpdir(), "onta-mcp-outside-")));
});
afterEach(() => {
  rmSync(root, { recursive: true, force: true });
  rmSync(outside, { recursive: true, force: true });
});

describe("resolveRoots: dormant unless explicitly configured", () => {
  it("returns no roots when the env var is unset (the tool must stay unregistered)", () => {
    const res = resolveRoots({});
    expect(res.roots).toEqual([]);
    expect(res.varName).toBeUndefined();
    // The stderr line must name the primary var so a typo is diagnosable.
    expect(describeRootResolution(res)).toContain("ONTA_LOCAL_FILES_DIR");
    expect(describeRootResolution(res)).toContain("disabled");
  });

  it("never falls back to cwd, even implicitly", () => {
    // cwd is chosen by the MCP CLIENT (Claude Desktop spawns servers at `/`), so
    // a cwd default would be both useless and worst-case unbounded.
    const res = resolveRoots({});
    expect(res.roots).toEqual([]);
    expect(res.roots).not.toContain(process.cwd());
  });

  it("honors the ONTA_ then COGRAPH_ then OMNIX_ precedence", () => {
    expect(LOCAL_FILES_ENV_VARS).toEqual([
      "ONTA_LOCAL_FILES_DIR",
      "COGRAPH_LOCAL_FILES_DIR",
      "OMNIX_LOCAL_FILES_DIR",
    ]);
    const both = resolveRoots({
      ONTA_LOCAL_FILES_DIR: root,
      COGRAPH_LOCAL_FILES_DIR: outside,
      OMNIX_LOCAL_FILES_DIR: outside,
    });
    expect(both.roots).toEqual([root]);
    expect(both.varName).toBe("ONTA_LOCAL_FILES_DIR");

    const legacy = resolveRoots({ OMNIX_LOCAL_FILES_DIR: root });
    expect(legacy.roots).toEqual([root]);
    expect(legacy.varName).toBe("OMNIX_LOCAL_FILES_DIR");
  });

  it("rejects a relative path (it would resolve against the client-controlled cwd)", () => {
    const res = resolveRoots({ ONTA_LOCAL_FILES_DIR: "./data" });
    expect(res.roots).toEqual([]);
    expect(res.rejected).toEqual(["./data"]);
  });

  it("rejects a nonexistent directory and a file, keeping the valid entries", () => {
    const file = touch(join(root, "a.csv"));
    const res = resolveRoots({
      ONTA_LOCAL_FILES_DIR: [root, join(root, "nope"), file].join(delimiter),
    });
    expect(res.roots).toEqual([root]);
    expect(res.rejected).toHaveLength(2);
  });

  it(`caps the root list at ${MAX_ROOTS}`, () => {
    const dirs: string[] = [];
    for (let i = 0; i < MAX_ROOTS + 2; i++) {
      const d = join(root, `r${i}`);
      mkdirSync(d);
      dirs.push(d);
    }
    const res = resolveRoots({ ONTA_LOCAL_FILES_DIR: dirs.join(delimiter) });
    expect(res.roots).toHaveLength(MAX_ROOTS);
    expect(res.dropped).toBe(2);
  });
});

describe("listWorkspaceFiles: containment", () => {
  it("does not escape via a directory SYMLINK pointing outside the root", () => {
    touch(join(outside, "secret.csv"));
    touch(join(root, "inside.csv"));
    symlinkSync(outside, join(root, "link-out"));

    const res = listWorkspaceFiles([root], { maxDepth: 3 });
    const paths = res.files.map((f) => f.path);
    expect(paths).toEqual([join(root, "inside.csv")]);
    expect(paths.join("\n")).not.toContain("secret.csv");
    expect(paths.join("\n")).not.toContain(outside);
  });

  it("does not return a FILE symlink whose target resolves outside the root", () => {
    const secret = touch(join(outside, "secret.csv"));
    symlinkSync(secret, join(root, "looks-local.csv"));

    const res = listWorkspaceFiles([root], { maxDepth: 3 });
    expect(res.files).toHaveLength(0);
  });

  it("DOES return a file symlink whose target stays inside the root", () => {
    const real = touch(join(root, "real.csv"));
    mkdirSync(join(root, "sub"));
    symlinkSync(real, join(root, "sub", "alias.csv"));

    const res = listWorkspaceFiles([root], { maxDepth: 2 });
    expect(res.files.map((f) => f.path).sort()).toEqual(
      [real, join(root, "sub", "alias.csv")].sort(),
    );
  });

  it("rejects a `..` escape in subdir", () => {
    touch(join(outside, "secret.csv"));
    expect(() => listWorkspaceFiles([root], { subdir: "../.." })).toThrow(/outside|not found/i);
    const rel = join("..", "..", "etc");
    expect(() => listWorkspaceFiles([root], { subdir: rel })).toThrow();
  });

  it("rejects an ABSOLUTE subdir pointing outside the root", () => {
    touch(join(outside, "secret.csv"));
    expect(() => listWorkspaceFiles([root], { subdir: outside })).toThrow(/outside|not found/i);
  });

  it("rejects a subdir that is a symlink out of the root", () => {
    touch(join(outside, "secret.csv"));
    symlinkSync(outside, join(root, "link-out"));
    expect(() => listWorkspaceFiles([root], { subdir: "link-out" })).toThrow(
      /outside|not found/i,
    );
  });

  it("accepts a legitimate subdir and scopes results to it", () => {
    touch(join(root, "top.csv"));
    mkdirSync(join(root, "sub"));
    const nested = touch(join(root, "sub", "nested.csv"));

    const res = listWorkspaceFiles([root], { subdir: "sub" });
    expect(res.files.map((f) => f.path)).toEqual([nested]);
    // relative_path stays relative to the ROOT, not to the subdir.
    expect(res.files[0]!.relative_path).toBe(join("sub", "nested.csv"));
  });

  it("isContained does not treat a sibling prefix as inside", () => {
    expect(isContained("/a/data", "/a/data-private/x.csv")).toBe(false);
    expect(isContained("/a/data", "/a/data/x.csv")).toBe(true);
    expect(isContained("/a/data", "/a/data")).toBe(true);
  });
});

describe("listWorkspaceFiles: extension allowlist", () => {
  it("returns only .csv / .json / .jsonl", () => {
    touch(join(root, "keep1.csv"));
    touch(join(root, "keep2.json"));
    touch(join(root, "keep3.jsonl"));
    touch(join(root, "drop.tsv"));
    touch(join(root, "drop.txt"));
    touch(join(root, "drop.xlsx"));
    touch(join(root, "drop.pem"));
    touch(join(root, "noext"));

    const names = listWorkspaceFiles([root]).files.map((f) => f.relative_path).sort();
    expect(names).toEqual(["keep1.csv", "keep2.json", "keep3.jsonl"]);
  });

  it("excludes .tsv specifically: it is absent from the SDK EXT_FORMAT map", () => {
    // Advertising .tsv as ingestible would be a lie: it falls through to
    // fmt = "text" in the SDK rather than being parsed as tabular data.
    touch(join(root, "data.tsv"));
    expect(listWorkspaceFiles([root]).files).toHaveLength(0);
  });

  it("is case-insensitive about the extension", () => {
    touch(join(root, "SHOUTY.CSV"));
    expect(listWorkspaceFiles([root]).files).toHaveLength(1);
  });

  it("skips dotfiles, dot-dirs and node_modules", () => {
    touch(join(root, ".hidden.csv"));
    mkdirSync(join(root, ".git"));
    touch(join(root, ".git", "config.json"));
    mkdirSync(join(root, "node_modules"));
    touch(join(root, "node_modules", "pkg.json"));
    const visible = touch(join(root, "visible.csv"));

    const res = listWorkspaceFiles([root], { maxDepth: 3 });
    expect(res.files.map((f) => f.path)).toEqual([visible]);
  });
});

describe("listWorkspaceFiles: depth and result caps", () => {
  function buildDepth(levels: number) {
    let dir = root;
    const made: string[] = [touch(join(root, "d0.csv"))];
    for (let i = 1; i <= levels; i++) {
      dir = join(dir, `lvl${i}`);
      mkdirSync(dir);
      made.push(touch(join(dir, `d${i}.csv`)));
    }
    return made;
  }

  it("maxDepth 0 lists only the root itself", () => {
    buildDepth(3);
    const res = listWorkspaceFiles([root], { maxDepth: 0 });
    expect(res.files.map((f) => f.relative_path)).toEqual(["d0.csv"]);
  });

  it("defaults to depth 2 (root plus two levels below)", () => {
    buildDepth(4);
    const rel = listWorkspaceFiles([root]).files.map((f) => f.relative_path).sort();
    expect(rel).toHaveLength(3);
    expect(rel.some((r) => r.includes("d3.csv"))).toBe(false);
  });

  it("clamps maxDepth to the hard maximum of 3", () => {
    buildDepth(6);
    const rel = listWorkspaceFiles([root], { maxDepth: 99 }).files.map((f) => f.relative_path);
    // depths 0..3 inclusive = 4 files, never the deeper ones.
    expect(rel).toHaveLength(4);
    expect(rel.some((r) => r.includes("d4.csv"))).toBe(false);
  });

  it("caps results at the requested limit and reports the truncation", () => {
    for (let i = 0; i < 12; i++) touch(join(root, `f${i}.csv`));
    const res = listWorkspaceFiles([root], { limit: 5 });
    expect(res.files).toHaveLength(5);
    expect(res.total).toBe(12);
    expect(res.truncated).toBe(true);
  });

  it(`clamps limit to the hard maximum of ${MAX_RESULTS}`, () => {
    for (let i = 0; i < 105; i++) touch(join(root, `f${i}.csv`));
    const res = listWorkspaceFiles([root], { limit: 10_000 });
    expect(res.files).toHaveLength(MAX_RESULTS);
    expect(res.total).toBe(105);
    expect(res.truncated).toBe(true);
  });

  it(`defaults limit to ${DEFAULT_LIMIT}`, () => {
    for (let i = 0; i < 60; i++) touch(join(root, `f${i}.csv`));
    expect(listWorkspaceFiles([root]).files).toHaveLength(DEFAULT_LIMIT);
  });

  it("sorts most recently modified first, so a freshly dropped payload leads", () => {
    touch(join(root, "old.csv"), "a\n1\n", 1_600_000_000);
    touch(join(root, "middle.csv"), "a\n1\n", 1_700_000_000);
    const fresh = touch(join(root, "fresh.csv"), "a\n1\n", 1_800_000_000);
    const res = listWorkspaceFiles([root]);
    expect(res.files[0]!.path).toBe(fresh);
    expect(res.files.map((f) => f.relative_path)).toEqual([
      "fresh.csv",
      "middle.csv",
      "old.csv",
    ]);
  });
});

describe("listWorkspaceFiles: untrusted filename rendering", () => {
  it("safeName drops control characters, bidi overrides and overlong names", () => {
    expect(safeName("normal-file.csv")).toBe("normal-file.csv");
    expect(safeName(`bad${String.fromCharCode(10)}name.csv`)).toBeNull();
    expect(safeName(`bad${String.fromCharCode(0)}name.csv`)).toBeNull();
    expect(safeName(`bad${String.fromCharCode(0x7f)}name.csv`)).toBeNull();
    // U+202E RIGHT-TO-LEFT OVERRIDE: the classic filename-spoofing primitive.
    expect(safeName(`invoice${String.fromCharCode(0x202e)}fdp.csv`)).toBeNull();
    expect(safeName(`${"x".repeat(200)}.csv`)).toBeNull();
  });

  it("drops (rather than renders) a file whose name carries a control character", () => {
    const nasty = `evil${String.fromCharCode(10)}line.csv`;
    try {
      touch(join(root, nasty));
    } catch {
      return; // filesystem refused the name; nothing to assert.
    }
    const good = touch(join(root, "good.csv"));
    const res = listWorkspaceFiles([root]);
    expect(res.files.map((f) => f.path)).toEqual([good]);
    // The drop is surfaced, not silent.
    expect(res.skipped).toBe(1);
  });
});

describe("listWorkspaceFiles: pattern filter", () => {
  beforeEach(() => {
    touch(join(root, "tecentriq-label-demo.csv"));
    touch(join(root, "sales-2026.csv"));
    touch(join(root, "notes.json"));
  });

  it("treats plain text as a case-insensitive substring match", () => {
    const res = listWorkspaceFiles([root], { pattern: "TECENTRIQ" });
    expect(res.files.map((f) => f.relative_path)).toEqual(["tecentriq-label-demo.csv"]);
  });

  it("treats * and ? as an anchored glob", () => {
    expect(
      listWorkspaceFiles([root], { pattern: "*-demo.csv" }).files.map((f) => f.relative_path),
    ).toEqual(["tecentriq-label-demo.csv"]);
    // Anchored: a glob that does not span the whole name matches nothing.
    expect(listWorkspaceFiles([root], { pattern: "demo" }).files).toHaveLength(1);
    expect(listWorkspaceFiles([root], { pattern: "*.json" }).files).toHaveLength(1);
  });

  it("rejects an abusive pattern instead of walking with it", () => {
    expect(() => compilePattern("x".repeat(500))).toThrow(/too long/i);
    expect(() => compilePattern("*a*b*c*d*e*")).toThrow(/wildcards/i);
  });

  it("does not let regex metacharacters in a pattern act as a regex", () => {
    // "." must be literal, otherwise this would match everything.
    expect(listWorkspaceFiles([root], { pattern: "sales.2026" }).files).toHaveLength(0);
  });
});

describe("listWorkspaceFiles: multiple roots", () => {
  it("lists across every configured root and labels each row with its root", () => {
    const a = touch(join(root, "a.csv"), "a\n1\n", 1_700_000_000);
    const b = touch(join(outside, "b.csv"), "a\n1\n", 1_800_000_000);
    const res = listWorkspaceFiles([root, outside]);
    expect(res.files.map((f) => f.path)).toEqual([b, a]);
    expect(res.files.map((f) => f.root)).toEqual([outside, root]);
  });

  it("returns nothing at all when given no roots (the dormant case)", () => {
    touch(join(root, "a.csv"));
    const res = listWorkspaceFiles([]);
    expect(res.files).toEqual([]);
    expect(res.total).toBe(0);
  });

  it("tolerates a root that vanished after startup", () => {
    const gone = join(root, "gone");
    mkdirSync(gone);
    rmSync(gone, { recursive: true, force: true });
    const kept = touch(join(root, "kept.csv"));
    expect(listWorkspaceFiles([gone, root]).files.map((f) => f.path)).toEqual([kept]);
  });
});

describe("listWorkspaceFiles: no file contents are read", () => {
  it("returns only path, relative path, size and mtime", () => {
    // v1 decision: the user's consent act was "here is a directory", not "here
    // is the content of every file in it".
    touch(join(root, "secretish.csv"), "ssn,name\n123-45-6789,Ada\n");
    const [row] = listWorkspaceFiles([root]).files;
    expect(Object.keys(row!).sort()).toEqual(
      ["mtime", "mtime_ms", "path", "relative_path", "root", "size"].sort(),
    );
    expect(JSON.stringify(row)).not.toContain("123-45-6789");
    expect(JSON.stringify(row)).not.toContain("ssn");
  });
});
