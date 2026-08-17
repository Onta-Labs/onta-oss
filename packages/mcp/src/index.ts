/** Infona MCP server — stdio entry + public handler re-exports.

Implementation lives in sibling ``mcp*.ts`` modules. Every previously
importable name is re-exported here. Tools reach the backend through
the SDK path builders (interface/endpoint convergence).
*/
import { realpathSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { registerAgentTools } from "./mcpAgent.js";
import { registerIngestTools } from "./mcpIngest.js";
import { registerQueryTools } from "./mcpQuery.js";
import { registerSchemaTools } from "./mcpSchema.js";
import { client, VERSION } from "./mcpShared.js";

export { searchHandler, grepHandler } from "./mcpQuery.js";
export {
  ingestCsvHandler,
  ingestTextHandler,
  exportKgHandler,
  listLocalFilesHandler,
} from "./mcpIngest.js";
export { inspectGraphSchemaHandler } from "./mcpSchema.js";

const server = new McpServer(
  {
    name: "infona",
    version: VERSION,
  },
  {
    instructions:
      "Infona is a context graph platform. Use these tools to " +
      "query structured data across multiple context graphs using natural language.",
  },
);

registerQueryTools(server);
registerIngestTools(server);
registerSchemaTools(server);
registerAgentTools(server);
// Exported so a caller can start the SAME server without re-implementing it
// (e.g. a test that imports this package as a library): the `isEntrypoint` guard
// below is (correctly) false there, so it calls main() explicitly. Direct
// `npx -y @infona-ai/mcp` still auto-starts via the guard.
export async function main(): Promise<void> {
  // Resolve env/config now so hosted-without-key dies on stderr before stdio
  // handshake. Localhost OSS constructs with no INFONA_API_KEY (README JSON).
  client();
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

// Only start the stdio server when run as the CLI entrypoint. Guarding this lets
// a test import the module (e.g. to unit-test `ingestCsvHandler`) without opening
// a stdio transport / hanging the test process.
//
// The comparison MUST resolve symlinks on both sides. `npx -y @infona-ai/mcp` and a
// global `npm i -g @infona-ai/mcp` install the package's `bin` as a SYMLINK (e.g.
// /usr/local/bin/infona-mcp -> …/@infona-ai/mcp/dist/index.js). When node runs the file
// through that symlink, `process.argv[1]` is the symlink path while
// `import.meta.url` is this module's realpath, so a raw href compare NEVER
// matches — the guard stays false and the server silently never starts (spawns,
// connects to nothing, and exits without ever handling a request). Realpath'ing
// both sides makes the two agree for
// direct, symlinked, and npx invocations alike, while still staying false when a
// different file (a test runner) is the entrypoint.
const isEntrypoint = (() => {
  try {
    if (
      typeof process === "undefined" ||
      !Array.isArray(process.argv) ||
      process.argv[1] === undefined
    ) {
      return false;
    }
    const invoked = pathToFileURL(realpathSync(process.argv[1])).href;
    const self = pathToFileURL(realpathSync(fileURLToPath(import.meta.url))).href;
    return invoked === self;
  } catch {
    return false;
  }
})();

if (isEntrypoint) {
  main().catch((err) => {
    process.stderr.write(
      `infona-mcp failed to start: ${err instanceof Error ? err.message : String(err)}\n`,
    );
    process.exit(1);
  });
}
