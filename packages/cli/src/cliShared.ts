/** Shared CLI helpers + the commander ``program`` singleton.

``client()`` honors global ``--tenant`` / ``--local`` flags. Mapping
review is the terminal port of the Explorer confirm/override gate —
ingest still writes through the SDK ``ingest`` path.
*/
import { createInterface } from "node:readline";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Command } from "commander";
import { Client } from "./client.js";
import { readConfig } from "./config.js";
import { formatFirstHourFailure } from "./firstHourErrors.js";

function pkgVersion(): string {
  try {
    const here = dirname(fileURLToPath(import.meta.url));
    const pkg = JSON.parse(readFileSync(join(here, "..", "package.json"), "utf-8"));
    return typeof pkg.version === "string" ? pkg.version : "0.0.0";
  } catch {
    return "0.0.0";
  }
}

export function client(): Client {
  // Honor the global flags: --tenant overrides the saved default for this
  // command; --local points at a self-hosted backend. Both fall through to
  // env / ~/.infona/config.json when not passed.
  //
  // When --local and no explicit --tenant, force tenant "default" so a
  // leftover ~/.infona config (demo-tenant from cloud login) cannot steal
  // open-access local traffic (matches shell mode).
  const g = program.opts() as { tenant?: string; local?: boolean };
  return new Client({
    ...(g.tenant
      ? { tenant: g.tenant }
      : g.local
        ? { tenant: "default" }
        : {}),
    ...(g.local ? { baseUrl: "http://localhost:8000" } : {}),
  });
}

export function printJson(data: unknown): void {
  process.stdout.write(JSON.stringify(data, null, 2) + "\n");
}

/** Resolve the working context graph: explicit --kg wins, else `infona use`. */
export function resolveKg(explicit?: string): string | undefined {
  return explicit ?? readConfig().defaultKg;
}

export function fail(msg: string, code = 1): never {
  process.stderr.write(msg.endsWith("\n") ? msg : msg + "\n");
  process.exit(code);
}

export async function withErrors<T>(fn: () => Promise<T>): Promise<T | void> {
  try {
    return await fn();
  } catch (err) {
    const c = client();
    fail(
      await formatFirstHourFailure(err, {
        baseUrl: c.baseUrl,
        hasApiKey: Boolean(c.apiKey),
        kg: readConfig().defaultKg,
      }),
    );
  }
}

export async function confirm(prompt: string): Promise<boolean> {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((resolve) => {
    rl.question(`${prompt} [y/N] `, (ans) => {
      rl.close();
      resolve(ans.trim().toLowerCase() === "y");
    });
  });
}

/** Like confirm() but defaults to yes (used for the primary "apply" action). */
export async function confirmYes(prompt: string): Promise<boolean> {
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
export const bold = sgr("1");
export const dim = sgr("2");

export type Mapping = Record<string, any>;

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

export function heldTypes(m: Mapping): any[] {
  const types = m.ontology_extensions?.types;
  return Array.isArray(types) ? types.filter((t: any) => t.held_for_review) : [];
}

/** Strip response-only audit fields (violations, inference_audit, profile) and
 *  keep only what /ingest/csv/rows applies. Held type extensions are dropped
 *  unless explicitly approved — same gate the Explorer applies on confirm. */
export function buildMappingForIngest(m: Mapping, approved: Set<string>): Mapping {
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
export async function reviewMapping(
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

export const program = new Command();
program
  .name("infona")
  .description("Infona Context Graph CLI")
  .version(pkgVersion())
  // Default action when no subcommand is given: drop into the interactive
  // shell. So `infona` / `infona` (compat alias) Just Works for the common case;
  // subcommands like `infona ingest <file>` still route to their own
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
