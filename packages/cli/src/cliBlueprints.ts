/** ``infona blueprint`` install / inspect / uninstall / fork / extend / update.

INF-575 / INF-579 / INF-578. Shares the ``blueprint`` command group with
``cliBlueprint.ts`` (export / validate, INF-565). All verbs POST to
``/graphs/{tenant}/blueprints``.
*/
import { existsSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { client, fail, printJson, program, resolveKg, withErrors } from "./cliShared.js";

function readPackageDocument(path: string): { manifest_yaml?: string; manifest?: Record<string, unknown> } {
  let file = path;
  if (statSync(path).isDirectory()) {
    const yaml = join(path, "blueprint.yaml");
    const yml = join(path, "blueprint.yml");
    const json = join(path, "blueprint.json");
    if (existsSync(yaml)) file = yaml;
    else if (existsSync(yml)) file = yml;
    else if (existsSync(json)) file = json;
    else fail(`${path} is not a Blueprint package (missing blueprint.yaml)`);
  }
  const text = readFileSync(file, "utf-8");
  if (file.endsWith(".json")) {
    return { manifest: JSON.parse(text) as Record<string, unknown> };
  }
  return { manifest_yaml: text };
}

const bp =
  program.commands.find((c) => c.name() === "blueprint") ??
  program.command("blueprint").description(
    "Install, inspect, export, or uninstall a Blueprint package",
  );

bp.command("install")
  .description("Install a Blueprint into this workspace (idempotent)")
  .argument("<path>", "package directory or blueprint.yaml")
  .option("--kg <name>", "knowledge graph that receives the optional sample")
  .option("--no-sample", "skip the bounded sample (empty graph only)")
  .action(async (path: string, opts: { kg?: string; sample?: boolean }) => {
    await withErrors(async () => {
      const kg = resolveKg(opts.kg);
      if (!kg) fail("pass --kg or set a default with `infona use`");
      const result = await client().installBlueprint({
        kg,
        include_sample: opts.sample !== false,
        ...readPackageDocument(path),
      });
      printJson(result);
    });
  });

bp.command("inspect")
  .description("Show the installed pin for a Blueprint")
  .argument("<id>", "blueprint id (namespace/name)")
  .action(async (id: string) => {
    await withErrors(async () => {
      printJson(await client().inspectBlueprint(id));
    });
  });

bp.command("list")
  .description("List Blueprints installed in this workspace")
  .action(async () => {
    await withErrors(async () => {
      printJson(await client().listBlueprints());
    });
  });

bp.command("uninstall")
  .description("Remove what install wrote; leave the rest of the workspace")
  .argument("<id>", "blueprint id (namespace/name)")
  .action(async (id: string) => {
    await withErrors(async () => {
      printJson(await client().uninstallBlueprint(id));
    });
  });

bp.command("fork")
  .description("Copy a Blueprint package into a new identity with lineage")
  .argument("<id>", "parent blueprint id (namespace/name)")
  .option("--as <id>", "new package id (namespace/name)")
  .action(async (id: string, opts: { as?: string }) => {
    await withErrors(async () => {
      printJson(await client().forkBlueprint(id, { as: opts.as }));
    });
  });

bp.command("extend")
  .description("Add a private overlay on the installed pin (same identity)")
  .argument("<id>", "installed blueprint id (namespace/name)")
  .option("--overlay <path>", "overlay YAML or JSON file")
  .action(async (id: string, opts: { overlay?: string }) => {
    await withErrors(async () => {
      if (!opts.overlay) fail("pass --overlay");
      const doc = readPackageDocument(opts.overlay);
      printJson(
        await client().extendBlueprint(id, {
          ...(doc.manifest ? { overlay: doc.manifest } : {}),
          ...(doc.manifest_yaml ? { overlay_yaml: doc.manifest_yaml } : {}),
        }),
      );
    });
  });

bp.command("update")
  .description("Apply a new public base without clobbering the private overlay")
  .argument("<id>", "installed blueprint id (namespace/name)")
  .argument("<path>", "new package directory or blueprint.yaml")
  .option("--no-sample", "skip the bounded sample")
  .action(async (id: string, path: string, opts: { sample?: boolean }) => {
    await withErrors(async () => {
      printJson(
        await client().updateBlueprint(id, {
          include_sample: opts.sample !== false,
          ...readPackageDocument(path),
        }),
      );
    });
  });
