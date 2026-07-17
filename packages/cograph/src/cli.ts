import { createInterface } from "node:readline";
import { readFileSync, realpathSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Command } from "commander";
import { Client, OntaError } from "./client.js";
import { renderAgentResult } from "./agentRender.js";
import { readConfig, writeConfig, configPathForDisplay } from "./config.js";

// Read version from package.json at runtime so we never drift again.
// dist/cli.js sits next to package.json once published; in dev (`npm link`)
// dist/cli.js sits inside packages/cograph/dist/, so the parent dir is the
// package root either way.
function pkgVersion(): string {
  try {
    const here = dirname(fileURLToPath(import.meta.url));
    const pkg = JSON.parse(readFileSync(join(here, "..", "package.json"), "utf-8"));
    return typeof pkg.version === "string" ? pkg.version : "0.0.0";
  } catch {
    return "0.0.0";
  }
}

function client(): Client {
  // Honor the global flags: --tenant overrides the saved default for this
  // command; --local points at a self-hosted backend. Both fall through to
  // env / ~/.onta/config.json when not passed.
  const g = program.opts() as { tenant?: string; local?: boolean };
  return new Client({
    ...(g.tenant ? { tenant: g.tenant } : {}),
    ...(g.local ? { baseUrl: "http://localhost:8000" } : {}),
  });
}

function printJson(data: unknown): void {
  process.stdout.write(JSON.stringify(data, null, 2) + "\n");
}

/** Resolve the working context graph: explicit --kg wins, else `onta use`. */
function resolveKg(explicit?: string): string | undefined {
  return explicit ?? readConfig().defaultKg;
}

function fail(msg: string, code = 1): never {
  process.stderr.write(msg.endsWith("\n") ? msg : msg + "\n");
  process.exit(code);
}

async function withErrors<T>(fn: () => Promise<T>): Promise<T | void> {
  try {
    return await fn();
  } catch (err) {
    if (err instanceof OntaError) {
      fail(`Error: ${err.message}`);
    }
    fail(`Error: ${err instanceof Error ? err.message : String(err)}`);
  }
}

async function confirm(prompt: string): Promise<boolean> {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((resolve) => {
    rl.question(`${prompt} [y/N] `, (ans) => {
      rl.close();
      resolve(ans.trim().toLowerCase() === "y");
    });
  });
}

/** Like confirm() but defaults to yes (used for the primary "apply" action). */
async function confirmYes(prompt: string): Promise<boolean> {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((resolve) => {
    rl.question(`${prompt} [Y/n] `, (ans) => {
      rl.close();
      const a = ans.trim().toLowerCase();
      resolve(a === "" || a === "y" || a === "yes");
    });
  });
}

// ---------------------------------------------------------------------------
// CSV schema review — terminal port of the Explorer's confirm/override gate.
// The backend applies exactly what /ingest/csv/rows is given, so the client is
// responsible for surfacing the inferred mapping and gating held-for-review
// type extensions before any rows are written.
// ---------------------------------------------------------------------------

const useColor = Boolean(process.stdout.isTTY) && !process.env.NO_COLOR;
const sgr = (code: string) => (s: string): string =>
  useColor ? `\x1b[${code}m${s}\x1b[0m` : s;
const bold = sgr("1");
const dim = sgr("2");

type Mapping = Record<string, any>;

interface EntityView {
  name: string;
  type_name: string;
  id_column?: string | null;
  id_from?: string[] | null;
  key_strategy?: string | null;
  confidence?: number | null;
  why?: string | null;
}

function entityViews(m: Mapping): EntityView[] {
  if (Array.isArray(m.entities) && m.entities.length > 0) {
    return m.entities.map((e: any) => ({
      name: e.name,
      type_name: e.type_name,
      id_column: e.id_column,
      id_from: e.id_from,
      key_strategy: e.key_strategy ?? null,
      confidence: e.confidence,
      why: e.why,
    }));
  }
  return [
    {
      name: m.entity_type,
      type_name: m.entity_type,
      key_strategy: m.key_strategy ?? null,
      confidence: m.confidence,
      why: m.why,
    },
  ];
}

function heldTypes(m: Mapping): any[] {
  const types = m.ontology_extensions?.types;
  return Array.isArray(types) ? types.filter((t: any) => t.held_for_review) : [];
}

/** Strip response-only audit fields (violations, inference_audit, profile) and
 *  keep only what /ingest/csv/rows applies. Held type extensions are dropped
 *  unless explicitly approved — same gate the Explorer applies on confirm. */
function buildMappingForIngest(m: Mapping, approved: Set<string>): Mapping {
  const out: Mapping = { entity_type: m.entity_type, columns: m.columns };
  if (m.entities) out.entities = m.entities;
  if (m.relationships) out.relationships = m.relationships;
  const types = m.ontology_extensions?.types;
  if (Array.isArray(types)) {
    out.ontology_extensions = {
      types: types.filter(
        (t: any) => !t.held_for_review || approved.has(t.type_name),
      ),
    };
  }
  return out;
}

function fmtConf(v: any): string {
  if (v == null) return "";
  const n = Number(v);
  if (Number.isNaN(n)) return "";
  return dim(` (${n.toFixed(2)}${n < 0.7 ? " !" : ""})`);
}

function renderMapping(
  m: Mapping,
  info: { totalRows: number; rowsProfiled: number },
): void {
  const w = (s: string) => process.stdout.write(s);
  w(
    "\n" +
      bold("Proposed schema") +
      dim(
        `  (profiled ${info.rowsProfiled.toLocaleString()} of ${info.totalRows.toLocaleString()} rows)`,
      ) +
      "\n",
  );
  w(dim("Review how the data maps to the graph before any rows are written.") + "\n\n");

  const ents = entityViews(m);
  const multi = Array.isArray(m.entities) && m.entities.length > 0;
  w(bold("Entities & keys") + "\n");
  for (const e of ents) {
    const key = e.id_column
      ? `key: ${e.id_column}`
      : e.id_from && e.id_from.length
        ? `key: ${e.id_from.join(" + ")}`
        : e.key_strategy === "synthetic"
          ? "key: (synthetic)"
          : "key: —";
    w(`  • ${bold(e.type_name)}  ${dim(key)}${fmtConf(e.confidence)}\n`);
    if (e.why) w(`      ${dim(e.why)}\n`);
    const cols = (m.columns ?? []).filter((col: any) =>
      multi ? col.entity === e.name : true,
    );
    for (const col of cols) {
      const role =
        col.role === "type_id"
          ? "key "
          : col.role === "relationship"
            ? "edge"
            : "attr";
      let detail = "";
      if (col.role === "relationship" && col.target_type)
        detail = ` → ${col.target_type}`;
      else if (
        col.role === "attribute" &&
        col.attribute_name &&
        col.attribute_name !== col.column_name
      )
        detail = ` as ${col.attribute_name}`;
      const dt =
        col.datatype && col.datatype !== "string"
          ? " " + dim(`[${col.datatype}]`)
          : "";
      w(
        `      ${dim("[" + role + "]")} ${col.column_name}${detail}${dt}${fmtConf(col.confidence)}\n`,
      );
    }
  }

  const rels = m.relationships ?? [];
  if (rels.length) {
    w("\n" + bold("Edges") + "\n");
    for (const r of rels)
      w(`  • ${r.subject} ${dim(r.predicate)} ${r.object}${fmtConf(r.confidence)}\n`);
  }

  const vio = m.violations ?? [];
  if (vio.length) {
    w(
      "\n" +
        dim(
          `Refute pass corrected ${vio.length} issue${vio.length === 1 ? "" : "s"}: ${vio
            .map((v: any) => v.template)
            .join(", ")}`,
        ) +
        "\n",
    );
  }
}

/** Interactive confirm/override gate, passed to client.ingest as
 *  onSchemaInferred. Returns the mapping to ingest, or null to cancel. */
async function reviewMapping(
  m: Mapping,
  info: { totalRows: number; rowsProfiled: number },
): Promise<Mapping | null> {
  renderMapping(m, info);
  const approved = new Set<string>();
  const held = heldTypes(m);
  if (held.length) {
    process.stdout.write(
      "\n" +
        bold(`${held.length} new type${held.length === 1 ? "" : "s"} held for review`) +
        dim(" — approve to create, or skip to leave for later") +
        "\n",
    );
    for (const t of held) {
      const from = t.promoted_from_attribute
        ? dim(` (from "${t.promoted_from_attribute}")`)
        : "";
      process.stdout.write(`  • ${t.type_name}${from}${fmtConf(t.confidence)}\n`);
      if (await confirm(`    Approve "${t.type_name}"?`)) approved.add(t.type_name);
    }
  }
  process.stdout.write("\n");
  const ok = await confirmYes(
    `Apply this mapping and ingest ${info.totalRows.toLocaleString()} rows?`,
  );
  if (!ok) return null;
  return buildMappingForIngest(m, approved);
}

const program = new Command();
program
  .name("onta")
  .description("Onta Context Graph CLI")
  .version(pkgVersion())
  // Default action when no subcommand is given: drop into the interactive
  // shell. So `onta` (or `npx onta`) Just Works for the common case;
  // subcommands like `onta ingest <file>` still route to their own
  // actions because commander dispatches subcommands first.
  .option("--local", "Use http://localhost:8000 and skip login (self-hosted)")
  .option("--no-login", "Skip browser login (assume open-access backend)")
  .option(
    "--tenant <id>",
    "Target a specific tenant for this command (overrides the saved default)",
  )
  .action(async (opts: { local?: boolean; login?: boolean }) => {
    const { runShell } = await import("./shell.js");
    await runShell({
      local: opts.local,
      // commander's --no-login inverts: opts.login === false when flag passed.
      noLogin: opts.login === false,
    });
  });

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
          "No context graphs. Create one with: onta kg create <name>\n",
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
        process.stdout.write(cur ? `context graph: ${cur}\n` : "no context graph set — onta use <kg>\n");
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
        : dim(`  (built-in default — set one with: onta tenant use <id>)\n`),
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
        if (err instanceof OntaError && err.status === 501) {
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
      process.stdout.write(dim(`\nSwitch with: onta tenant use <id>\n`));
    });
  });

tenantCmd
  .command("use <id>")
  .description("Set the active tenant (saved to ~/.onta/config.json)")
  .action((id: string) => {
    writeConfig({ tenant: id });
    process.stdout.write(`${bold("✓")} Active tenant set to ${bold(id)}\n`);
    process.stdout.write(dim(`Saved to ${configPathForDisplay()}\n`));
  });

// ---------------------------------------------------------------------------
// ingest
// ---------------------------------------------------------------------------

program
  .command("ingest [file]")
  .description("Ingest data from a file or --text")
  .option("-t, --text <text>", "Inline text to ingest")
  .option("--kg <name>", "Target context graph name")
  .option(
    "-f, --format <fmt>",
    "Override format detection (text|csv|json)",
  )
  .option(
    "-y, --yes",
    "Skip the CSV schema review and apply the inferred mapping non-interactively",
  )
  .option(
    "--type <Type>",
    "CSV only. Deterministic ingest: skip schema inference; columns become attributes verbatim (first column = entity name)",
  )
  .option(
    "--join-on <attr>",
    "CSV only. Merge rows onto existing entities matching this key attribute instead of minting new ones",
  )
  .action(
    async (
      file: string | undefined,
      opts: {
        text?: string;
        kg?: string;
        format?: string;
        yes?: boolean;
        type?: string;
        joinOn?: string;
      },
    ) => {
      await withErrors(async () => {
        const c = client();
        const kg = resolveKg(opts.kg);
        if (opts.text) {
          process.stdout.write(
            `Ingesting text (${opts.text.length.toLocaleString()} chars)...\n`,
          );
          const result = await c.ingest(opts.text, {
            kg,
            contentType: opts.format ?? "text",
          });
          printIngestResult(result);
          return;
        }
        if (!file) {
          fail("Provide a file or --text");
        }
        // For CSV, interpose the same schema review/confirm gate the Explorer
        // shows. Interactive on a TTY unless --yes; otherwise apply the
        // inferred mapping as-is (held type extensions auto-approved, matching
        // the prior non-interactive behavior). Hook is ignored for text/json.
        // --type is the deterministic path: no inference, so nothing to review.
        const interactive =
          Boolean(process.stdin.isTTY) &&
          Boolean(process.stdout.isTTY) &&
          !opts.yes &&
          !opts.type;
        const onSchemaInferred = interactive
          ? reviewMapping
          : (m: Mapping) =>
              Promise.resolve(
                buildMappingForIngest(
                  m,
                  new Set(heldTypes(m).map((t: any) => t.type_name)),
                ),
              );
        // ingest() handles file reading + format detection + CSV two-step flow.
        process.stdout.write(
          opts.type ? `Ingesting ${file} as ${opts.type}...\n` : `Ingesting ${file}...\n`,
        );
        const result = await c.ingest(file, {
          kg,
          contentType: opts.format,
          ...(opts.type ? { typeName: opts.type } : { onSchemaInferred }),
          ...(opts.joinOn ? { keyJoin: { keyAttribute: opts.joinOn } } : {}),
        });
        if ((result as Record<string, unknown>).cancelled) {
          process.stdout.write("Cancelled — nothing was written.\n");
          return;
        }
        printIngestResult(result);
      });
    },
  );

function printIngestResult(result: Record<string, unknown>): void {
  const num = (k: string) => Number(result[k] ?? 0);
  // Only report the counters this ingest path actually produced (CSV row
  // ingest has no extraction phase, text ingest has no row mapping).
  if (result.entities_extracted !== undefined) {
    process.stdout.write(`  Entities extracted: ${num("entities_extracted")}\n`);
  }
  process.stdout.write(`  Entities resolved:  ${num("entities_resolved")}\n`);
  process.stdout.write(`  Triples inserted:   ${num("triples_inserted")}\n`);
  const types = result.types_created;
  if (Array.isArray(types) && types.length) {
    process.stdout.write(`  Types created:      ${types.join(", ")}\n`);
  }
  const rejections = result.rejections;
  if (Array.isArray(rejections) && rejections.length) {
    process.stdout.write(`  Rejections:         ${rejections.length}\n`);
  }
}

// ---------------------------------------------------------------------------
// ask
// ---------------------------------------------------------------------------

program
  .command("ask <question>")
  .description("Ask a natural language question")
  .option("--kg <name>", "Context graph to query")
  .option("-d, --debug", "Show SPARQL and latency breakdown")
  .option("-m, --model <model>", "Override query model")
  .action(
    async (
      question: string,
      opts: { kg?: string; debug?: boolean; model?: string },
    ) => {
      await withErrors(async () => {
        if (opts.model) process.stdout.write(`Model: ${opts.model}\n`);
        process.stdout.write(`Q: ${question}\n`);
        process.stdout.write("Generating answer...\n");
        const t0 = Date.now();
        const result = await client().ask(question, {
          kg: resolveKg(opts.kg),
          model: opts.model,
        });
        const roundtripMs = Date.now() - t0;
        process.stdout.write(`\nA: ${result.answer ?? "No answer"}\n`);
        if (opts.debug) {
          process.stdout.write(`\nSPARQL:\n${result.sparql ?? ""}\n`);
          const timing = (result.timing ?? {}) as Record<string, unknown>;
          if (Object.keys(timing).length) {
            process.stdout.write(`\n${"─".repeat(40)}\n`);
            process.stdout.write(
              `${"Stage".padEnd(25)} ${"Time".padStart(10)}\n`,
            );
            process.stdout.write(`${"─".repeat(40)}\n`);
            for (const [key, val] of Object.entries(timing)) {
              if (key === "attempts") {
                process.stdout.write(
                  `${"Attempts".padEnd(25)} ${String(val).padStart(10)}\n`,
                );
              } else if (typeof val === "string") {
                const label = key
                  .replace(/_/g, " ")
                  .replace(/\b\w/g, (c) => c.toUpperCase());
                process.stdout.write(
                  `${label.padEnd(25)} ${val.padStart(10)}\n`,
                );
              } else {
                const label = key
                  .replace(/_ms$/, "")
                  .replace(/_/g, " ")
                  .replace(/\b\w/g, (c) => c.toUpperCase());
                const num = typeof val === "number" ? val : Number(val);
                process.stdout.write(
                  `${label.padEnd(25)} ${num.toFixed(1).padStart(8)}ms\n`,
                );
              }
            }
            process.stdout.write(`${"─".repeat(40)}\n`);
            process.stdout.write(
              `${"Client roundtrip".padEnd(25)} ${roundtripMs.toFixed(1).padStart(8)}ms\n`,
            );
          }
        }
      });
    },
  );

// ---------------------------------------------------------------------------
// agent — unified Ask-AI agent (POST /graphs/{tenant}/agent)
// ---------------------------------------------------------------------------
//
// The ONE command that reaches the unified agent the web app + MCP already use:
// it classifies intent server-side (question | enrich | clean | dedup |
// ontology) and either answers, asks a clarifying question, or proposes a plan
// to confirm. The discrete commands (ask/enrich/er/ontology) stay as
// convenient shortcuts; migrating them onto the agent is a deliberate non-goal.
//
// Confirm flow (non-interactive): a returned plan is NOT executed automatically.
// Either re-run with --confirm <plan_id> (the only mutating path), or pass --yes
// to confirm-and-execute in the same invocation.

/**
 * Core of the `agent` command — extracted so it's unit-testable with a mocked
 * {@link Client} (the commander action below just builds a real client and
 * delegates). Drives the three non-interactive paths:
 *  - `--confirm <id>` → execute that plan directly, render the result.
 *  - default          → one agent turn, render it; if it's a plan, either
 *                       confirm-and-execute (`--yes`) or print a confirm hint.
 *
 * Exported for tests; not part of the published SDK surface (cli.ts is the bin
 * entry, not in `package.json#exports`).
 */
export async function runAgentCommand(
  c: Client,
  message: string,
  opts: { kg?: string; type?: string; yes?: boolean; confirm?: string },
): Promise<void> {
  // KG resolution mirrors `ask`: an explicit --kg wins, else the SDK's
  // saved/default kg (passing undefined lets the backend use its default).
  const context = { kgName: opts.kg, typeName: opts.type };

  // --confirm path: execute the named plan directly and render the result.
  if (opts.confirm) {
    const result = await c.agent({ confirmPlanId: opts.confirm, ...context });
    renderAgentResult(result);
    return;
  }

  const result = await c.agent({ message, ...context });
  renderAgentResult(result);

  // A plan is the only kind that awaits a follow-up. With --yes we confirm
  // immediately; otherwise we print how to confirm it later.
  if (result.kind === "plan") {
    const planId =
      typeof result.plan_id === "string" ? result.plan_id : undefined;
    if (!planId) return;
    if (opts.yes) {
      const executed = await c.agent({ confirmPlanId: planId, ...context });
      renderAgentResult(executed);
    } else {
      const flags = [
        opts.kg ? `--kg ${opts.kg}` : "",
        opts.type ? `--type ${opts.type}` : "",
      ]
        .filter(Boolean)
        .join(" ");
      const hint = `onta agent --confirm ${planId}${flags ? " " + flags : ""} ${JSON.stringify(message)}`;
      process.stdout.write(
        `${dim("Confirm & run:")} ${hint}\n` +
          `${dim("  or re-run with --yes to execute now.")}\n`,
      );
    }
  }
}

program
  .command("agent <message>")
  .description("Talk to the unified Ask-AI agent (answers, plans, and runs actions)")
  .option("--kg <name>", "Context graph to operate within")
  .option("--type <Type>", "Active type scope (for enrich/clean/dedup planning)")
  .option(
    "-y, --yes",
    "Auto-confirm and execute a returned plan in the same run",
  )
  .option(
    "--confirm <planId>",
    "Execute a specific previously-proposed plan id (skips planning)",
  )
  .action(
    async (
      message: string,
      opts: { kg?: string; type?: string; yes?: boolean; confirm?: string },
    ) => {
      await withErrors(() => runAgentCommand(client(), message, opts));
    },
  );

// ---------------------------------------------------------------------------
// ontology
// ---------------------------------------------------------------------------

const onto = program.command("ontology").description("View ontology");

onto
  .command("types")
  .description("List ontology types")
  .action(async () => {
    await withErrors(async () => {
      const types = await client().ontologyTypes();
      if (!types.length) {
        process.stdout.write("No ontology types defined.\n");
        return;
      }
      for (const t of types) {
        const parent = t.parent_type
          ? ` (subClassOf ${t.parent_type})`
          : "";
        const desc = t.description ? ` — ${t.description}` : "";
        process.stdout.write(`  ${t.name}${parent}${desc}\n`);
        const attrs = (t.attributes ?? []) as Array<Record<string, unknown>>;
        for (const a of attrs) {
          process.stdout.write(
            `    .${a.name} (${a.datatype ?? "string"})\n`,
          );
        }
      }
    });
  });

// ---------------------------------------------------------------------------
// er — entity resolution
// ---------------------------------------------------------------------------

const er = program.command("er").description("Entity resolution");

er.command("rebuild")
  .description(
    "Second pass: collapse intra-batch entity fragments in an ingested KG",
  )
  .requiredOption("--kg <name>", "Context graph to rebuild")
  .action(async (opts: { kg: string }) => {
    await withErrors(async () => {
      process.stdout.write(`Rebuilding entity resolution for ${opts.kg}…\n`);
      const report = await client().erRebuild(opts.kg);
      const types = (report.types ?? []) as Array<Record<string, unknown>>;
      for (const t of types) {
        const name = String(t.type ?? "?").padEnd(16, " ");
        process.stdout.write(
          `  ${name} ${t.entities_before} → ${t.entities_after}` +
            `  (−${t.fragments_absorbed} fragments across ${t.clusters_merged} clusters)\n`,
        );
      }
      process.stdout.write(
        `Done. ${report.fragments_absorbed_total ?? 0} fragments absorbed.\n`,
      );
    });
  });

// ---------------------------------------------------------------------------
// enrich
// ---------------------------------------------------------------------------

program
  .command("enrich [target]")
  .description(
    "Agentic enrichment — fill an attribute from web sources, with citations. " +
      "Target is Type.attribute (e.g. `onta enrich Product.price --kg my-kg`).",
  )
  .option("--kg <name>", "Context graph (or set one once with `onta use <kg>`)")
  .option("--type <Type>", "Entity type to enrich (alternative to the Type.attribute argument)")
  .option("--attribute <attr>", "Attribute to fill (alternative to the Type.attribute argument)")
  .option("--tier <tier>", "auto | lite | base | core | pro (auto lets the backend pick free vs paid web search)", "auto")
  .option("--limit <n>", "Max entities to enrich (default: every matched entity; 3 with --wait)")
  .option("--apply", "Write results to the graph (with provenance), not just stage")
  .option("--wait", "Block until the job settles and print the results (default: queue and return)")
  .action(
    async (
      target: string | undefined,
      opts: {
        kg?: string;
        type?: string;
        attribute?: string;
        tier: string;
        limit?: string;
        apply?: boolean;
        wait?: boolean;
      },
    ) => {
      await withErrors(async () => {
        const c = client();
        const kg = resolveKg(opts.kg);
        if (!kg) fail("Error: no context graph — pass --kg or set one with `onta use <kg>`.");
        // `Type.attribute` argument and --type/--attribute flags are equivalent;
        // explicit flags win when both are given.
        let typeName = opts.type;
        let attribute = opts.attribute;
        if (target) {
          const dot = target.indexOf(".");
          if (dot > 0) {
            typeName ??= target.slice(0, dot);
            attribute ??= target.slice(dot + 1);
          } else {
            typeName ??= target;
          }
        }
        if (!typeName || !attribute) {
          fail(
            "Error: tell me what to fill — `onta enrich Type.attribute --kg <kg>` (or --type/--attribute).",
          );
        }
        // Queued (default) runs cover every matched entity unless capped;
        // --wait keeps the small interactive default.
        const limit =
          opts.limit !== undefined ? Number(opts.limit) : opts.wait ? 3 : undefined;
        process.stdout.write(
          `Enriching ${typeName}.${attribute} in ${kg} (tier ${opts.tier})…\n`,
        );
        const runEnrich = (tier: "auto" | "lite" | "base" | "core" | "pro") =>
          c.enrichRun({
            kg_name: kg,
            type_name: typeName,
            attributes: [attribute],
            tier,
            ...(limit !== undefined ? { limit } : {}),
            conflict_policy: opts.apply ? "overwrite" : "stage",
            confidence_min: 0.1,
          });
        let created = await runEnrich(
          opts.tier as "auto" | "lite" | "base" | "core" | "pro",
        );
        // Non-interactive: we can't ask the user, so on an ambiguous "auto"
        // route we default to web search (core) per the product decision.
        if (created.needs_clarification || created.status === "needs_clarification") {
          process.stdout.write(
            "Source ambiguous — defaulting to web search (core).\n",
          );
          created = await runEnrich("core");
        } else if (created.resolved_tier) {
          // The tier picks the CHAIN (registered data APIs lead every concrete
          // tier, then web search); the winning source is reported per value.
          const sourceLabel =
            created.resolved_tier === "lite"
              ? "Wikidata (free)"
              : "registered data APIs + web search";
          process.stdout.write(
            `Sources: ${sourceLabel}${created.routing_note ? ` — ${created.routing_note}` : ""}\n`,
          );
        }
        if (!created.job_id) {
          fail("Error: backend did not return a job id.");
        }
        const jobId = created.job_id;
        if (!opts.wait) {
          process.stdout.write(
            `\nqueued · job ${jobId.slice(0, 8)} · ${typeName}.${attribute} in ${kg}\n`,
          );
          process.stdout.write(
            `it runs in the background — check on it any time:\n` +
              `  onta jobs ${jobId.slice(0, 8)}    (or: onta jobs last)\n`,
          );
          return;
        }
        const terminal = ["applied", "review", "failed", "cancelled"];
        let job = await c.enrichJob(jobId);
        for (let i = 0; i < 40 && !terminal.includes(job.status); i++) {
          await new Promise((r) => setTimeout(r, 2000));
          job = await c.enrichJob(jobId);
        }
        const p = job.progress;
        const filled = (job.results ?? []).filter((r) => r.verdict);
        for (const r of filled) {
          const v = r.verdict!;
          process.stdout.write(`\n  ${r.entity_uri.split("/").pop()}\n`);
          process.stdout.write(`    ${r.attribute}: ${v.value}\n`);
          process.stdout.write(
            `    source: ${v.source}${v.source_url ? "  " + v.source_url : ""}\n`,
          );
          if (v.reasoning) process.stdout.write(`    ${v.reasoning}\n`);
        }
        process.stdout.write(
          `\nChecked ${p.processed} · filled ${p.filled} · verified ${p.verified} · conflicts ${p.conflicts} · not found ${p.no_match}\n`,
        );
        process.stdout.write(
          `${opts.apply ? "Applied to the graph (value + provenance triples)." : "Staged for review — re-run with --apply to write."}\n`,
        );
      });
    },
  );

// ---------------------------------------------------------------------------
// jobs
// ---------------------------------------------------------------------------

const JOB_TERMINAL = ["applied", "review", "failed", "cancelled"];

program
  .command("jobs [id]")
  .description(
    "Background jobs — list recent ones, or inspect one by id, id prefix, or `last`",
  )
  .option("--kg <name>", "Only jobs for this context graph")
  .option("--wait", "Block until the job settles instead of returning its current state")
  .option("--urls", "Show full citation URLs instead of the shortened form")
  .action(
    async (
      id: string | undefined,
      opts: { kg?: string; wait?: boolean; urls?: boolean },
    ) => {
    await withErrors(async () => {
      const c = client();
      const all = await c.jobs();
      const kg = resolveKg(opts.kg);
      const scoped = kg ? all.filter((j) => j.kg_name === kg) : all;

      if (!id) {
        if (!scoped.length) {
          process.stdout.write("No jobs yet.\n");
          return;
        }
        process.stdout.write(`Recent jobs${kg ? ` in ${kg}` : ""}:\n\n`);
        for (const j of scoped.slice(0, 10)) {
          const what =
            j.type_name && j.attributes?.length
              ? `${j.type_name}.${j.attributes[0]}`
              : String(j.category ?? "job");
          process.stdout.write(
            `  ${j.id.slice(0, 8)}  ${String(j.status).padEnd(10)} ${what} · ${j.kg_name}\n`,
          );
        }
        process.stdout.write("\nInspect one:  onta jobs <id>   (or: onta jobs last)\n");
        return;
      }

      // Resolve `last` / an id prefix to a full job id.
      const enrichment = scoped.filter(
        (j) => (j.category ?? "enrichment") === "enrichment",
      );
      const full =
        id === "last"
          ? enrichment[0]?.id
          : (scoped.find((j) => j.id === id || j.id.startsWith(id))?.id ?? id);
      if (!full) fail("No enrichment jobs found yet.");

      let job = opts.wait ? await c.waitForJob(full) : await c.enrichJob(full);
      if (opts.wait) {
        // One waitForJob call is a single server long-poll window (~120s);
        // a long job can outlast it, so loop to terminal (bounded, same cap
        // as `enrich --wait`).
        for (let i = 0; i < 40 && !JOB_TERMINAL.includes(String(job.status)); i++) {
          job = await c.waitForJob(full);
        }
      }
      const p = job.progress;
      const done = JOB_TERMINAL.includes(String(job.status));
      const stillRunning = opts.wait
        ? "  (wait window elapsed — still running; re-run to keep waiting)"
        : "  (still running — re-run to refresh, or pass --wait)";
      process.stdout.write(
        `job ${job.id.slice(0, 8)} · ${job.type_name}.${job.attributes?.[0] ?? "?"} in ${job.kg_name}\n`,
      );
      process.stdout.write(
        `status: ${job.status}${done ? "" : stillRunning}\n`,
      );
      process.stdout.write(
        `progress: checked ${p.processed}/${p.total} · filled ${p.filled} · conflicts ${p.conflicts} · not found ${p.no_match}\n`,
      );
      const cited = (job.results ?? []).filter((r) => r.verdict);
      if (done && cited.length) {
        // Aligned sample table; citations shortened to their meaningful tail
        // (the full URL is one --urls away) so the receipt reads at a glance.
        const shortUrl = (u: string): string => {
          if (opts.urls || u.length <= 56) return u;
          // Trim to the tail, snapped to a query-param boundary so the visible
          // part is whole params (usually the id that matters).
          const tail = u.slice(-44);
          const amp = tail.indexOf("&");
          return `…${amp > 0 ? tail.slice(amp + 1) : tail}`;
        };
        const sample = cited.slice(0, 3).map((r) => ({
          name: (r.entity_uri.split("/").pop() ?? "").replace(/_/g, " "),
          value: String(r.verdict!.value),
          source: r.verdict!.source ?? "",
          url: r.verdict!.source_url ?? "",
        }));
        const wName = Math.max(...sample.map((s) => s.name.length));
        const wVal = Math.max(...sample.map((s) => s.value.length));
        process.stdout.write("\n");
        for (const s of sample) {
          const cite = s.url ? `  \x1b[2m${s.source} · ${shortUrl(s.url)}\x1b[0m` : `  \x1b[2m${s.source}\x1b[0m`;
          process.stdout.write(
            `  ${s.name.padEnd(wName)}  ${s.value.padStart(wVal)}${cite}\n`,
          );
          if (opts.urls && s.url) process.stdout.write(`    \x1b[2m${s.url}\x1b[0m\n`);
        }
        if (cited.length > 3) {
          process.stdout.write(
            `  … ${cited.length - 3} more — every value cited (add --urls for full links)\n`,
          );
        }
      }
      });
    },
  );

// ---------------------------------------------------------------------------
// schedule
// ---------------------------------------------------------------------------

program
  .command("schedule [target]")
  .description(
    "Recurring enrichment — `onta schedule Type.attribute --kg <kg> --weekly`, or `onta schedule list`",
  )
  .option("--kg <name>", "Context graph")
  .option("--weekly", "Re-run once a week")
  .option("--daily", "Re-run once a day")
  .option("--hourly", "Re-run once an hour")
  .option("--tier <tier>", "auto | lite | base | core | pro", "auto")
  .action(
    async (
      target: string | undefined,
      opts: {
        kg?: string;
        weekly?: boolean;
        daily?: boolean;
        hourly?: boolean;
        tier: string;
      },
    ) => {
      await withErrors(async () => {
        const c = client();

        if (!target || target === "list") {
          const res = await c.raw.schedules();
          if (!res.ok) fail(`Error: could not list schedules (${res.status}).`);
          const rows = (await res.json()) as Array<{
            id: string;
            kg_name: string;
            action: string;
            interval_seconds?: number | null;
            cron?: string | null;
            enabled: boolean;
            next_run?: string | null;
            params?: { type_name?: string; attributes?: string[] };
          }>;
          const kg = resolveKg(opts.kg);
          const scoped = kg ? rows.filter((s) => s.kg_name === kg) : rows;
          if (!scoped.length) {
            process.stdout.write("No schedules yet.\n");
            return;
          }
          for (const s of scoped) {
            const cadence =
              s.interval_seconds === 604800
                ? "weekly"
                : s.interval_seconds === 86400
                  ? "daily"
                  : s.interval_seconds === 3600
                    ? "hourly"
                    : (s.cron ?? `${s.interval_seconds}s`);
            const what =
              s.params?.type_name && s.params?.attributes?.length
                ? ` · ${s.params.type_name}.${s.params.attributes[0]}`
                : "";
            process.stdout.write(
              `  ${cadence} · ${s.action}${what} · ${s.kg_name} · next run ${String(s.next_run ?? "—").slice(0, 10)}${s.enabled ? "" : " (disabled)"}\n`,
            );
          }
          return;
        }

        const dot = target.indexOf(".");
        if (dot <= 0) {
          fail(
            "Error: tell me what to keep fresh — `onta schedule Type.attribute --kg <kg> --weekly`.",
          );
        }
        const kg = resolveKg(opts.kg);
        if (!kg) fail("Error: no context graph — pass --kg or set one with `onta use <kg>`.");
        const interval = opts.weekly
          ? 604800
          : opts.daily
            ? 86400
            : opts.hourly
              ? 3600
              : undefined;
        if (!interval) fail("Error: pick a cadence — --weekly, --daily, or --hourly.");
        const res = await c.raw.createSchedule({
          kg_name: kg,
          category: "enrichment",
          action: "enrich",
          interval_seconds: interval,
          enabled: true,
          params: {
            type_name: target.slice(0, dot),
            attributes: [target.slice(dot + 1)],
            tier: opts.tier,
            conflict_policy: "verify",
          },
        });
        if (!res.ok) {
          fail(`Error: schedule create failed (${res.status}): ${(await res.text()).slice(0, 300)}`);
        }
        const s = (await res.json()) as { next_run?: string | null };
        const label = interval === 604800 ? "weekly" : interval === 86400 ? "daily" : "hourly";
        process.stdout.write(
          `scheduled ${label} · ${target} in ${kg} · next run ${String(s.next_run ?? "").slice(0, 10)}\n`,
        );
      });
    },
  );

// ---------------------------------------------------------------------------
// vis
// ---------------------------------------------------------------------------

program
  .command("vis [type]")
  .description("Visualise a type — instance count, attribute coverage, top relations")
  .option("--kg <name>", "Context graph to inspect")
  .option("--all", "List every type, not just the top 10")
  .action(async (typeName: string | undefined, opts: { kg?: string; all?: boolean }) => {
    await withErrors(async () => {
      const c = client();

      // Resolve KG: use --kg flag, or pick first available KG.
      let kg = resolveKg(opts.kg);
      if (!kg) {
        const kgs = await c.listKgs();
        if (!kgs.length) {
          fail("No context graphs found. Run 'onta ingest' first.");
        }
        kg = String(kgs[0].name ?? "");
      }

      // No type given: KG overview — entity types by instance count.
      if (!typeName) {
        const counts = (await c.typeCounts(kg))
          .slice()
          .sort((a, b) => (b.entity_count ?? 0) - (a.entity_count ?? 0));
        if (!counts.length) {
          process.stdout.write(`No entities in '${kg}' yet.\n`);
          return;
        }
        const shown = opts.all ? counts : counts.slice(0, 10);
        const max = Math.max(...shown.map((x) => x.entity_count ?? 0), 1);
        const wName = Math.max(...shown.map((x) => x.name.length));
        const header = `${kg} — ${counts.length.toLocaleString()} entity type${counts.length === 1 ? "" : "s"}`;
        process.stdout.write(`\n${header}\n${"─".repeat(header.length)}\n`);
        for (const x of shown) {
          const n = x.entity_count ?? 0;
          const bar = "█".repeat(Math.max(1, Math.round((n / max) * 24)));
          process.stdout.write(
            `  ${x.name.padEnd(wName)}  ${bar}  ${n.toLocaleString()}\n`,
          );
        }
        if (!opts.all && counts.length > shown.length) {
          process.stdout.write(
            `  … ${counts.length - shown.length} more — onta vis --all\n`,
          );
        }
        process.stdout.write(`\nDrill in:  onta vis <Type>\n`);
        return;
      }

      let summary: import("./client.js").TypeSummary;
      try {
        summary = await c.typeSummary(kg, typeName);
      } catch {
        fail(`Type '${typeName}' not found in KG '${kg}'.`);
      }

      const { entity_count, attributes, relationships, description, parent_type } = summary;
      const header = `${typeName}${parent_type ? ` (subClassOf ${parent_type})` : ""} — ${entity_count.toLocaleString()} instances`;
      process.stdout.write(`\n${header}\n${"─".repeat(header.length)}\n`);
      if (description) process.stdout.write(`${description}\n`);

      // Attributes table
      if (attributes.length) {
        process.stdout.write(`\nAttributes (${attributes.length}):\n`);
        const sorted = [...attributes].sort((a, b) => b.coverage_pct - a.coverage_pct);
        for (const a of sorted.slice(0, 10)) {
          const bar = "█".repeat(Math.round(a.coverage_pct / 10));
          const pct = `${a.coverage_pct}%`.padStart(6);
          process.stdout.write(`  ${a.name.padEnd(24)} ${pct}  ${bar}\n`);
        }
        if (attributes.length > 10) {
          process.stdout.write(`  … and ${attributes.length - 10} more\n`);
        }
      }

      // Relations table
      if (relationships.length) {
        process.stdout.write(`\nRelationships (${relationships.length}):\n`);
        for (const r of relationships.slice(0, 8)) {
          const target = r.target_type ? ` → ${r.target_type}` : "";
          const pct = `${r.coverage_pct}%`.padStart(6);
          const avg = r.avg_degree ? ` (avg ${r.avg_degree})` : "";
          process.stdout.write(`  ${(r.name + target).padEnd(36)} ${pct}${avg}\n`);
        }
      }

      const explorerUrl = `https://getonta.com/dashboard/explore/${encodeURIComponent(typeName)}?kg=${encodeURIComponent(kg)}`;
      process.stdout.write(`\n→ Open visually at ${explorerUrl}\n`);
      process.stdout.write("  (Sign in for interactive viz, search, and click-to-enrich.)\n\n");
    });
  });

// ---------------------------------------------------------------------------
// clear
// ---------------------------------------------------------------------------

program
  .command("clear")
  .description("Clear data")
  .option("--kg <name>", "Clear a specific context graph")
  .option(
    "--include-ontology",
    "Also clear the ontology (only meaningful when --kg is omitted)",
    false,
  )
  .option("-y, --yes", "Skip confirmation", false)
  .action(
    async (opts: { kg?: string; includeOntology?: boolean; yes?: boolean }) => {
      await withErrors(async () => {
        let msg: string;
        if (opts.kg) {
          msg = `Clear KG '${opts.kg}'?`;
        } else if (opts.includeOntology) {
          msg = "Clear EVERYTHING including ontology?";
        } else {
          msg = "Clear all instance data (ontology preserved)?";
        }

        if (!opts.yes) {
          const ok = await confirm(msg);
          if (!ok) {
            process.stdout.write("Cancelled.\n");
            return;
          }
        }

        const c = client();
        if (opts.kg) {
          await c.deleteKg(opts.kg);
          process.stdout.write(`Cleared KG: ${opts.kg}\n`);
          return;
        }

        // Bulk-clear via /query + DELETE /triples — same loop the Python CLI uses.
        const tenant = c.tenant;
        const baseUrl = `${c.baseUrl}/graphs/${tenant}`;
        const headers: Record<string, string> = {
          "Content-Type": "application/json",
        };
        if (c.apiKey) headers["X-API-Key"] = c.apiKey;

        const filters = opts.includeOntology
          ? ""
          : `FILTER(CONTAINS(STR(?s), '/entities/') || CONTAINS(STR(?s), '/onto/') || CONTAINS(STR(?s), '/kgs/'))`;
        const query = `SELECT ?s ?p ?o FROM <https://cograph.tech/graphs/${tenant}> WHERE { ?s ?p ?o . ${filters} } LIMIT 1000`;

        process.stdout.write("Clearing...\n");
        let deleted = 0;
        for (let i = 0; i < 50; i++) {
          const fetchRes = await fetch(`${baseUrl}/query`, {
            method: "POST",
            headers,
            body: JSON.stringify({ query }),
          });
          if (!fetchRes.ok) break;
          const data = (await fetchRes.json()) as {
            bindings?: Array<Record<string, unknown>>;
          };
          const bindings = data.bindings ?? [];
          if (!bindings.length) break;
          const triples = bindings
            .filter((b) => b.s)
            .map((b) => ({
              subject: b.s,
              predicate: b.p,
              object: b.o,
            }));
          for (let j = 0; j < triples.length; j += 100) {
            await fetch(`${baseUrl}/triples`, {
              method: "DELETE",
              headers,
              body: JSON.stringify({ triples: triples.slice(j, j + 100) }),
            });
          }
          deleted += triples.length;
        }
        process.stdout.write(`Deleted ${deleted} triples\n`);
      });
    },
  );

// ---------------------------------------------------------------------------
// login
// ---------------------------------------------------------------------------

program
  .command("login")
  .description("Sign in via your browser and save an API key")
  .action(async () => {
    const { runLogin } = await import("./login.js");
    await runLogin();
  });

// ---------------------------------------------------------------------------
// shell
// ---------------------------------------------------------------------------

program
  .command("shell")
  .description("Start an interactive REPL")
  .option("--kg <name>", "Context graph to use")
  .option("--local", "Use http://localhost:8000 and skip login (self-hosted)")
  .option("--no-login", "Skip browser login (assume open-access backend)")
  .action(
    async (opts: { kg?: string; local?: boolean; login?: boolean }) => {
      // Parent program also accepts --local/--no-login (so `onta --local`
      // works without a subcommand). When commander parses
      // `onta shell --local`, the parent sees --local first and the
      // subcommand never gets it — so merge from program.opts() too.
      const parentOpts = program.opts() as {
        local?: boolean;
        login?: boolean;
      };
      const { runShell } = await import("./shell.js");
      await runShell({
        kg: opts.kg,
        local: opts.local || parentOpts.local,
        noLogin: opts.login === false || parentOpts.login === false,
      });
    },
  );

// ---------------------------------------------------------------------------

/** True when this module is the process entry point (run as `onta …`), not
 *  when it's imported (e.g. by the unit tests that exercise `runAgentCommand`).
 *  Guards the auto-parse so importing the module has no side effects.
 *
 *  npm installs the `bin` as a SYMLINK (node_modules/.bin/onta →
 *  dist/cli.js). Node sets import.meta.url to the *realpath* of the entry file
 *  while process.argv[1] keeps the *symlink* path, so a naive href comparison
 *  never matches and the CLI silently does nothing. Resolve the symlink first:
 *  compare fileURLToPath(import.meta.url) against realpathSync(process.argv[1]).
 */
function isMainModule(): boolean {
  const argv1 = process.argv[1];
  if (!argv1) return false;
  try {
    return fileURLToPath(import.meta.url) === realpathSync(argv1);
  } catch {
    return false;
  }
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
  void main();
}

// silence unused import warning if ever needed
void printJson;
