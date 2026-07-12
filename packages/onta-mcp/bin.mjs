#!/usr/bin/env node
// Onta MCP server — a thin alias of the `cograph-mcp` package (which keeps its
// name for back-compat). Everything (tools, logic) lives in cograph-mcp; this
// just lets `npx -y onta-mcp` launch the same stdio server under the new brand.
//
// cograph-mcp only auto-starts when it is the process entrypoint; imported as a
// library that guard is (correctly) false, so we call its exported main().
import { main } from "cograph-mcp";

main().catch((err) => {
  process.stderr.write(
    `onta-mcp failed to start: ${err instanceof Error ? err.message : String(err)}\n`,
  );
  process.exit(1);
});
