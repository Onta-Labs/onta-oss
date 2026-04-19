#!/usr/bin/env node
// Make the published CLI binaries executable. Invoked from each package's
// `prepack` script. Argument is the path (relative to the monorepo root) of
// the file to chmod +x.
import { chmodSync, existsSync } from "node:fs";
import { resolve } from "node:path";

const target = process.argv[2];
if (!target) {
  process.stderr.write("chmod-bin: missing target path\n");
  process.exit(1);
}

// Resolve against the monorepo root (this script lives at scripts/chmod-bin.js).
const root = resolve(new URL("..", import.meta.url).pathname);
const full = resolve(root, target);

if (!existsSync(full)) {
  process.stderr.write(`chmod-bin: ${full} does not exist\n`);
  process.exit(1);
}

chmodSync(full, 0o755);
process.stdout.write(`chmod +x ${target}\n`);
