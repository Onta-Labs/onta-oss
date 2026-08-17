/** ``infona ontology`` / ``infona er`` — same canonical routes as Explorer/MCP. */
import { formatErRebuild } from "./erRebuildRender.js";
import { client, fail, program, withErrors } from "./cliShared.js";

// ---------------------------------------------------------------------------
// ontology
// ---------------------------------------------------------------------------

const onto = program.command("ontology").description("View and evolve ontology");

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

// Dogfood S6: evolve/history/diff were HTTP/MCP-only; CLI parity over the same
// canonical routes (interface convergence).
onto
  .command("resolve <ask>")
  .description(
    "Evolve the ontology from a plain-English request (POST /ontology/resolve)",
  )
  .option("--kg <name>", "Knowledge graph context for the resolve call")
  .option(
    "--apply-proposals",
    "Also apply any returned proposals that need confirmation",
  )
  .action(async (ask: string, opts: { kg?: string; applyProposals?: boolean }) => {
    await withErrors(async () => {
      const c = client();
      const result = await c.ontologyResolve(ask, {
        knowledge_graph: opts.kg,
      });
      const applied = result.applied ?? [];
      const proposals = result.proposals ?? [];
      process.stdout.write(
        `applied ${applied.length} · proposals ${proposals.length}\n`,
      );
      for (const ch of applied) {
        process.stdout.write(
          `  ✓ ${(ch as { action?: string }).action ?? "change"} ${JSON.stringify(ch).slice(0, 160)}\n`,
        );
      }
      for (const ch of proposals) {
        process.stdout.write(
          `  ? proposal ${JSON.stringify(ch).slice(0, 160)}\n`,
        );
      }
      if (opts.applyProposals && proposals.length) {
        const batch = await c.ontologyApplyBatch(
          proposals as import("./client.js").ResolvedChange[],
        );
        const rows = batch.results ?? [];
        const ok = rows.filter((r) => r.ok).length;
        process.stdout.write(
          `apply-batch: ${ok}/${rows.length} ok\n`,
        );
      }
    });
  });

onto
  .command("history")
  .description("Ontology changelog (GET /ontology/history)")
  .option("--grouped", "Group mid-ingest commit bursts", true)
  .action(async (opts: { grouped?: boolean }) => {
    await withErrors(async () => {
      // Always send the flag: empty query falls back to server default
      // grouped=true, so --no-grouped would be a silent no-op otherwise.
      const q = `?grouped=${opts.grouped === false ? "false" : "true"}`;
      // Raw pass-through — same canonical route as Explorer/MCP.
      const res = await client().raw.ontologyHistory(q);
      if (!res.ok) {
        fail(`Error: ontology history failed (${res.status})`);
      }
      const body = await res.json();
      process.stdout.write(`${JSON.stringify(body, null, 2)}\n`);
    });
  });

onto
  .command("diff")
  .description("Ontology structural diff (GET /ontology/diff)")
  .option("--from <rev>", "From revision (e.g. revision:4)", "revision:0")
  .option("--to <rev>", "To revision", "current")
  .action(async (opts: { from?: string; to?: string }) => {
    await withErrors(async () => {
      const q = `?from=${encodeURIComponent(opts.from ?? "revision:0")}&to=${encodeURIComponent(opts.to ?? "current")}`;
      const res = await client().raw.ontologyDiff(q);
      if (!res.ok) {
        fail(`Error: ontology diff failed (${res.status})`);
      }
      const body = await res.json();
      process.stdout.write(`${JSON.stringify(body, null, 2)}\n`);
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
      const report = await client().erRebuild(opts.kg);
      process.stdout.write(formatErRebuild(report, opts.kg));
    });
  });
