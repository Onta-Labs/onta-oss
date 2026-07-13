#!/usr/bin/env node
// Onta CLI — a thin alias of the `cograph` package (which keeps its name for
// back-compat). Everything (SDK, shell, subcommands) lives in cograph; this
// just lets `npx onta` / `npm install -g onta` launch the same CLI under the
// new brand. Same pattern as onta-mcp → cograph-mcp.
//
// cograph's cli only auto-parses when it is the process entrypoint; imported
// here that guard is (correctly) false, so we call its exported main().
import { main } from "cograph/cli";

main().catch((err) => {
  process.stderr.write(
    `onta failed to start: ${err instanceof Error ? err.message : String(err)}\n`,
  );
  process.exit(1);
});
