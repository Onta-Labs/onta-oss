/** ``infona export`` / ``clear`` / ``login`` / ``init`` / ``shell``. */
import {
  client,
  confirm,
  fail,
  program,
  withErrors,
} from "./cliShared.js";

// ---------------------------------------------------------------------------
// export (F10 — get data back out)
// ---------------------------------------------------------------------------

program
  .command("export")
  .description("Export a knowledge graph as JSON or CSV")
  .requiredOption("--kg <name>", "Context graph to export")
  .option(
    "-f, --format <fmt>",
    "Output format: json (default) or csv",
    "json",
  )
  .option("-t, --type <type>", "Export a single type only")
  .option("--limit <n>", "Max rows (default server cap)", (v) => parseInt(v, 10))
  .option("-o, --out <file>", "Write to file instead of stdout")
  .action(
    async (opts: {
      kg: string;
      format?: string;
      type?: string;
      limit?: number;
      out?: string;
    }) => {
      await withErrors(async () => {
        const fmt = (opts.format ?? "json").toLowerCase();
        if (fmt !== "json" && fmt !== "csv") {
          fail(`Unknown format '${fmt}' (use json or csv)`);
        }
        const data = await client().exportKg(opts.kg, {
          format: fmt as "json" | "csv",
          type: opts.type,
          limit: opts.limit,
        });
        const text =
          typeof data === "string" ? data : JSON.stringify(data, null, 2) + "\n";
        if (opts.out) {
          const { writeFileSync } = await import("node:fs");
          writeFileSync(opts.out, text, "utf-8");
          process.stderr.write(`Wrote ${opts.out}\n`);
        } else {
          process.stdout.write(text.endsWith("\n") ? text : text + "\n");
        }
      });
    },
  );

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
        const query = `SELECT ?s ?p ?o FROM <${process.env.INFONA_IRI_BASE || "https://graph.infona.ai"}/graphs/${tenant}> WHERE { ?s ?p ?o . ${filters} } LIMIT 1000`;

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
// init — connect wizard (first-run / re-init)  ONTA-540
// ---------------------------------------------------------------------------

program
  .command("init")
  .description(
    "Connect wizard: local open-access, browser sign-in, or API key (re-run to change)",
  )
  .option(
    "--local",
    "Non-interactive: probe localhost:8000 and write open-access config",
  )
  .option(
    "--force",
    "With --local: overwrite an existing non-local connection without a TTY confirm",
  )
  .action(async (opts: { local?: boolean; force?: boolean }) => {
    await withErrors(async () => {
      const {
        connectLocal,
        runConnectWizard,
      } = await import("./connect.js");
      // Parent --local also counts (infona --local init).
      const parentOpts = program.opts() as { local?: boolean };
      const local = Boolean(opts.local || parentOpts.local);
      if (local) {
        // Non-interactive local path used by scripts and CI; never opens a browser.
        // Write-only IO (no readline) so the process exits after success/error.
        // Existing different connection: TTY confirms; non-TTY needs --force
        // (same local open-access rewrite stays idempotent without --force).
        const result = await connectLocal({
          replace: true,
          force: Boolean(opts.force),
        });
        if (!result.ok) {
          if (result.error === "cancelled") {
            process.exitCode = 0;
            return;
          }
          fail(`Error: ${result.error}`);
        }
        return;
      }
      const result = await runConnectWizard({ force: true });
      if (result === "non-interactive") {
        fail(
          "Error: `infona init` needs a TTY, or pass --local / set INFONA_API_KEY.",
        );
      }
      if (result === "cancelled") {
        // Soft exit — user kept existing config or backed out.
        process.exitCode = 0;
      }
    });
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
      // Parent program also accepts --local/--no-login (so `infona --local`
      // works without a subcommand). When commander parses
      // `infona shell --local`, the parent sees --local first and the
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
