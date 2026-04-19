import { defineConfig } from "tsup";

export default defineConfig([
  {
    entry: { index: "src/index.ts" },
    format: ["esm"],
    dts: true,
    clean: true,
    target: "node20",
    sourcemap: true,
  },
  {
    entry: { cli: "src/cli.ts" },
    format: ["esm"],
    dts: false,
    clean: false,
    target: "node20",
    sourcemap: true,
    banner: { js: "#!/usr/bin/env node" },
  },
]);
