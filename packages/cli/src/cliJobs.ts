/** ``infona enrich`` / ``jobs`` / ``schedule`` / ``vis``.

Enrichment and jobs go through the SDK. Schedule create/list use
``raw.createSchedule`` / ``raw.schedules`` — no bespoke endpoints.
*/
import {
  client,
  fail,
  program,
  resolveKg,
  withErrors,
} from "./cliShared.js";

// ---------------------------------------------------------------------------
// enrich
// ---------------------------------------------------------------------------

program
  .command("enrich [target]")
  .description(
    "Agentic enrichment — fill an attribute from web sources, with citations. " +
      "Target is Type.attribute (e.g. `infona enrich Product.price --kg my-kg`).",
  )
  .option("--kg <name>", "Context graph (or set one once with `infona use <kg>`)")
  .option("--type <Type>", "Entity type to enrich (alternative to the Type.attribute argument)")
  .option("--attribute <attr>", "Attribute to fill (alternative to the Type.attribute argument)")
  .option("--tier <tier>", "auto | lite | base | core | pro (auto routes free Wikidata vs richer chains; OSS has no paid web search)", "auto")
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
        if (!kg) fail("Error: no context graph — pass --kg or set one with `infona use <kg>`.");
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
            "Error: tell me what to fill — `infona enrich Type.attribute --kg <kg>` (or --type/--attribute).",
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
        // Non-interactive: on ambiguous "auto" default to core. In OSS core is
        // still the free Wikidata chain unless a paid plugin registered adapters
        // (OSS dogfood S7 — never claim "web search" here).
        if (created.needs_clarification || created.status === "needs_clarification") {
          process.stdout.write(
            "Source ambiguous — defaulting to core (registered adapters + Wikidata in OSS).\n",
          );
          created = await runEnrich("core");
        } else if (created.resolved_tier) {
          const sourceLabel =
            created.resolved_tier === "lite"
              ? "Wikidata (free)"
              : `${created.resolved_tier} (registered adapters + Wikidata — no paid web search in OSS)`;
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
              `  infona jobs ${jobId.slice(0, 8)}    (or: infona jobs last)\n`,
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
        // Honor actual job status (dogfood S7): conflict_policy=stage still
        // auto-writes clean fills → status "applied". Never claim "staged for
        // review — re-run with --apply" when the backend already applied.
        if (job.status === "applied") {
          process.stdout.write(
            "Applied to the graph (value + provenance triples).\n",
          );
        } else if (job.status === "review") {
          // Review walkthrough is shell-only (`/enrich review`); there is no
          // non-interactive `infona enrich review` subcommand.
          process.stdout.write(
            `Needs review (${p.conflicts} conflict${p.conflicts === 1 ? "" : "s"}) — open the shell and run: /enrich review ${jobId.slice(0, 8)}\n`,
          );
        } else if (
          job.status === "failed" ||
          job.status === "cancelled"
        ) {
          process.stdout.write(
            `Job ended as ${job.status}${job.error ? ": " + job.error : ""}.\n`,
          );
        } else {
          process.stdout.write(
            `Job status: ${job.status} (still running or unknown — check: infona jobs ${jobId.slice(0, 8)}).\n`,
          );
        }
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
        process.stdout.write("\nInspect one:  infona jobs <id>   (or: infona jobs last)\n");
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
    "Recurring enrichment — `infona schedule Type.attribute --kg <kg> --weekly`, or `infona schedule list`",
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
            "Error: tell me what to keep fresh — `infona schedule Type.attribute --kg <kg> --weekly`.",
          );
        }
        const kg = resolveKg(opts.kg);
        if (!kg) fail("Error: no context graph — pass --kg or set one with `infona use <kg>`.");
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
          fail("No context graphs found. Run 'infona ingest' first.");
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
            `  … ${counts.length - shown.length} more — infona vis --all\n`,
          );
        }
        process.stdout.write(`\nDrill in:  infona vis <Type>\n`);
        return;
      }

      let summary: import("./client.js").TypeSummary;
      try {
        summary = await c.typeSummary(kg, typeName);
      } catch (err) {
        // Backend is source of truth (P-A1a): 404 when type has neither
        // instances in this KG nor a tenant-ontology declaration. Other
        // failures (network, 5xx) keep a distinct message so overview vs
        // drill-in divergence is not misread as "type missing".
        const status = (err as { status?: number })?.status;
        if (status === 404) {
          fail(
            `Type '${typeName}' not found in KG '${kg}' ` +
              `(no instances here and not declared in the workspace ontology).`,
          );
        }
        const msg = err instanceof Error ? err.message : String(err);
        fail(`Could not load type summary for '${typeName}' in KG '${kg}': ${msg}`);
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

      const explorerUrl = `https://infona.ai/dashboard/explore/${encodeURIComponent(typeName)}?kg=${encodeURIComponent(kg)}`;
      process.stdout.write(`\n→ Open visually at ${explorerUrl}\n`);
      process.stdout.write("  (Sign in for interactive viz, search, and click-to-enrich.)\n\n");
    });
  });
