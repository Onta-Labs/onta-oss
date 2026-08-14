/** Infona CLI entry — commander program + public re-exports.

Implementation lives in sibling ``cli*.ts`` modules. Every previously
importable name (``main``, ``runAgentCommand``) is re-exported here.
*/
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { fail, program } from "./cliShared.js";
import "./cliKg.js";
import "./cliIngest.js";
import "./cliQuery.js";
import "./cliOnto.js";
import "./cliJobs.js";
import "./cliMisc.js";

export { runAgentCommand } from "./cliQuery.js";

/** True when this module is the process entry point (run as `infona …`), not
 *  when it's imported (e.g. by the unit tests that exercise `runAgentCommand`).
 *  Guards the auto-parse so importing the module has no side effects.
 *
 *  npm installs the `bin` as a SYMLINK (node_modules/.bin/infona →
 *  dist/cli.js). Node sets import.meta.url to the *realpath* of the entry file
 *  while process.argv[1] keeps the *symlink* path, so a naive href comparison
 *  never matches and the CLI silently does nothing. Resolve the symlink first:
 *  compare fileURLToPath(import.meta.url) against realpathSync(process.argv[1]).
 */
function isMainModule(): boolean {
  const argv1 = process.argv[1];
  if (!argv1) return false;
  try {
    // realpath BOTH sides so npm `.bin` symlinks match (COG-129) and so
    // CI runners that resolve import.meta.url via a different mount still hit.
    const self = realpathSync(fileURLToPath(import.meta.url));
    const entry = realpathSync(argv1);
    if (self === entry) return true;
  } catch {
    // fall through to basename heuristics
  }
  // Fallback for exotic layouts (Windows junctions, partial realpath failure):
  // treat as main when the process was clearly launched as our bin / cli.js.
  const norm = argv1.replace(/\\/g, "/");
  return (
    norm.endsWith("/cli.js") ||
    norm.endsWith("/infona") ||
    norm.endsWith("/onta")
  );
}

/** Run the CLI. Exported (and reachable via the `"./cli"` subpath export in
 *  package.json) so a caller can launch the same program programmatically — the
 *  isMainModule() guard stays false in that case because the process entry point
 *  is the caller, not this file. */
export async function main(argv: string[] = process.argv): Promise<void> {
  await program.parseAsync(argv).catch((err) => {
    fail(`Error: ${err instanceof Error ? err.message : String(err)}`);
  });
}

if (isMainModule()) {
  void main().catch((err) => {
    // Surface unexpected async failures (otherwise spawnSync can see exit 1
    // with empty stdout — which broke packages/cli symlink --version on CI).
    const msg = err instanceof Error ? err.stack ?? err.message : String(err);
    process.stderr.write(msg.endsWith("\n") ? msg : `${msg}\n`);
    process.exitCode = 1;
  });
}
