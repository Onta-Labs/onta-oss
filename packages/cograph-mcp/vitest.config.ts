import { defineConfig } from "vitest/config";

// Tests live in `test/` and run in a Node environment (the MCP server targets
// Node 20+). `dist/` is excluded so vitest never picks up built output. Not
// published — `package.json#files` is `["dist", "README.md"]`, so this config +
// the test/ dir stay out of the npm tarball.
export default defineConfig({
  test: {
    environment: "node",
    include: ["test/**/*.test.ts"],
    exclude: ["dist/**", "node_modules/**"],
  },
});
