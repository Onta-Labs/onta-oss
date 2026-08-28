/** ``infona blueprint export | validate`` — OSS package protocol (INF-565).

Both verbs reach the canonical backend routes through the shared SDK.
They do not reimplement export or validation client-side.
*/
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { client, fail, program, withErrors } from "./cliShared.js";

const blueprint = program
  .command("blueprint")
  .description("Inspect, validate, and export Infona Blueprints");

blueprint
  .command("export")
  .description("Export a live workspace KG as a Blueprint directory")
  .argument("<kg>", "Context graph to export")
  .option(
    "-o, --out <dir>",
    "Directory to write (default: ./<package-name>)",
  )
  .action(async (kg: string, opts: { out?: string }) => {
    await withErrors(async () => {
      const data = await client().exportBlueprint(kg);
      const files = data.files ?? {};
      const yaml = files["blueprint.yaml"];
      if (!yaml) {
        fail("backend export returned no blueprint.yaml");
      }
      const id =
        typeof data.manifest?.id === "string" ? data.manifest.id : kg;
      const defaultDir = id.includes("/") ? id.split("/").pop()! : id;
      const dest = resolve(opts.out ?? defaultDir);
      mkdirSync(dest, { recursive: true });
      for (const [rel, content] of Object.entries(files)) {
        const path = join(dest, rel);
        mkdirSync(dirname(path), { recursive: true });
        writeFileSync(path, content, "utf-8");
      }
      process.stderr.write(`Wrote Blueprint to ${dest}\n`);
    });
  });

blueprint
  .command("validate")
  .description("Validate a Blueprint directory or blueprint.yaml")
  .argument("<path>", "Package directory or blueprint.yaml / .json")
  .action(async (pathArg: string) => {
    await withErrors(async () => {
      const path = resolve(pathArg);
      let files: Record<string, string>;
      try {
        const { statSync } = await import("node:fs");
        const st = statSync(path);
        if (st.isDirectory()) {
          files = {};
          for (const name of [
            "blueprint.yaml",
            "blueprint.yml",
            "blueprint.json",
          ]) {
            try {
              files[name] = readFileSync(join(path, name), "utf-8");
            } catch {
              /* optional */
            }
          }
        } else {
          const base = path.split(/[\\/]/).pop() ?? "blueprint.yaml";
          files = { [base]: readFileSync(path, "utf-8") };
        }
      } catch (err) {
        fail(`cannot read ${path}: ${err instanceof Error ? err.message : err}`);
      }
      const result = await client().validateBlueprint({ files });
      if (result.errors?.length) {
        for (const err of result.errors) {
          process.stderr.write(`${err}\n`);
        }
        fail("Blueprint is not valid");
      }
      process.stdout.write("valid\n");
    });
  });
