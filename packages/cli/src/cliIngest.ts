/** ``infona ingest`` — file / --text through the SDK ingest path. */
import {
  buildMappingForIngest,
  client,
  fail,
  heldTypes,
  program,
  resolveKg,
  reviewMapping,
  withErrors,
  type Mapping,
} from "./cliShared.js";

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
