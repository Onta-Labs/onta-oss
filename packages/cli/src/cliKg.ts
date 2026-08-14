/** ``infona kg`` / ``use`` / ``tenant`` commands. */
import { InfonaError } from "./client.js";
import { readConfig, writeConfig, configPathForDisplay } from "./config.js";
import {
  bold,
  client,
  dim,
  fail,
  program,
  withErrors,
} from "./cliShared.js";

// ---------------------------------------------------------------------------
// kg
// ---------------------------------------------------------------------------

const kg = program.command("kg").description("Manage context graphs");

kg.command("list")
  .description("List context graphs")
  .action(async () => {
    await withErrors(async () => {
      const kgs = await client().listKgs();
      if (!kgs.length) {
        process.stdout.write(
          "No context graphs. Create one with: infona kg create <name>\n",
        );
        return;
      }
      for (const k of kgs) {
        const name = String(k.name ?? "?");
        const triples = Number(k.triple_count ?? 0);
        const desc = k.description ? ` — ${k.description}` : "";
        const padName = name.padEnd(20, " ");
        const padTriples = String(triples).padStart(6, " ");
        process.stdout.write(`  ${padName} ${padTriples} triples${desc}\n`);
      }
    });
  });

kg.command("create <name>")
  .description("Create a context graph")
  .option("-d, --description <text>", "Description")
  .action(async (name: string, opts: { description?: string }) => {
    await withErrors(async () => {
      const created = await client().createKg(name, opts.description);
      process.stdout.write(`Created context graph: ${created.name ?? name}\n`);
    });
  });

kg.command("delete <name>")
  .description("Delete a context graph")
  .action(async (name: string) => {
    await withErrors(async () => {
      await client().deleteKg(name);
      process.stdout.write(`Deleted context graph: ${name}\n`);
    });
  });

// ---------------------------------------------------------------------------
// tenant
// ---------------------------------------------------------------------------

program
  .command("use [kg]")
  .description("Set the working context graph — later commands can drop --kg")
  .action(async (kg: string | undefined) => {
    await withErrors(async () => {
      if (!kg) {
        const cur = readConfig().defaultKg;
        process.stdout.write(cur ? `context graph: ${cur}\n` : "no context graph set — infona use <kg>\n");
        return;
      }
      writeConfig({ defaultKg: kg });
      process.stdout.write(`context graph: ${kg}\n`);
    });
  });

const tenantCmd = program
  .command("tenant")
  .description("Show or switch the active tenant");

tenantCmd
  .command("current", { isDefault: true })
  .description("Show the active tenant")
  .action(() => {
    const active = client().tenant;
    const saved = readConfig().tenant;
    process.stdout.write(`Active tenant: ${bold(active)}\n`);
    process.stdout.write(
      saved
        ? dim(`  saved default in ${configPathForDisplay()}\n`)
        : dim(`  (built-in default — set one with: infona tenant use <id>)\n`),
    );
  });

tenantCmd
  .command("list")
  .description("List the tenants you can access")
  .action(async () => {
    await withErrors(async () => {
      const c = client();
      let tenants: Array<{ id: string; label: string }>;
      try {
        tenants = await c.listTenants();
      } catch (err) {
        if (err instanceof InfonaError && err.status === 501) {
          fail(
            "This backend doesn't support tenant management (no tenant provider configured).",
          );
        }
        throw err;
      }
      if (!tenants.length) {
        process.stdout.write("No tenants found for your account.\n");
        return;
      }
      const active = c.tenant;
      for (const t of tenants) {
        const marker = t.id === active ? "*" : " ";
        process.stdout.write(`  ${marker} ${t.id.padEnd(24)} ${dim(t.label)}\n`);
      }
      process.stdout.write(dim(`\nSwitch with: infona tenant use <id>\n`));
    });
  });

tenantCmd
  .command("use <id>")
  .description("Set the active tenant (saved to ~/.infona/config.json)")
  .action((id: string) => {
    writeConfig({ tenant: id });
    process.stdout.write(`${bold("✓")} Active tenant set to ${bold(id)}\n`);
    process.stdout.write(dim(`Saved to ${configPathForDisplay()}\n`));
  });
