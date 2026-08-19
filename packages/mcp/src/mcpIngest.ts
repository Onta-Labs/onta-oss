/** Ingest / export / local-files MCP tools.

CSV and text ingest go through the SDK ``ingest`` path (canonical
``POST /graphs/{tenant}/ingest``). Export rides ``exportKg``. No new
endpoints.
*/
import { existsSync, statSync } from "node:fs";
import { basename } from "node:path";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Client } from "@infona-ai/cli";
import { z } from "zod";
import {
  ALLOWED_EXTENSIONS,
  DEFAULT_DEPTH,
  DEFAULT_LIMIT,
  MAX_DEPTH,
  MAX_RESULTS,
  listWorkspaceFiles,
  renderListResult,
} from "./workspace.js";
import { registerIngestDltTool } from "./mcpIngestDlt.js";
import { client, errorResult, LOCAL_FILE_ROOTS, textResult } from "./mcpShared.js";

export { ingestDltHandler } from "./mcpIngestDlt.js";

// ONTA-415: the "did you mean" suffix on a not-found path. This is the SECOND
// consumer of the one containment-guarded primitive, not a second enumeration
// path, so there is a single guard and a single test surface.
//
// The suffix is CONDITIONAL on a root actually being configured. Naming a tool
// that is absent from tools/list is the ONTA-243 failure class documented on
// JOB_CATEGORIES in mcpShared.ts: the model is sent after a capability it
// cannot call and strands the task. With no root there is no lister and no hint.
function notFoundHint(filePath: string, roots: string[]): string {
  if (!roots.length) return "";
  let rows: ReturnType<typeof listWorkspaceFiles>["files"] = [];
  try {
    rows = listWorkspaceFiles(roots, { limit: MAX_RESULTS }).files;
  } catch {
    rows = [];
  }
  if (!rows.length) {
    return (
      ` No ${ALLOWED_EXTENSIONS.join(" / ")} files were found under the ` +
      `configured local root either. Call list_local_files to check.`
    );
  }
  // Prefer an exact basename match (the classic "right file, wrong directory"),
  // otherwise show the most recently modified candidates.
  const want = basename(filePath).toLowerCase();
  const exact = rows.filter((r) => basename(r.path).toLowerCase() === want);
  const shown = (exact.length ? exact : rows).slice(0, 5);
  const lead = exact.length
    ? ` A file with that name does exist under the configured local root:`
    : ` Local files available under the configured root (most recent first):`;
  return (
    `${lead}\n` +
    // Backtick-fenced for the same reason as in `renderListResult`: these are
    // attacker-controllable filenames being rendered into model context.
    shown.map((r) => `  \`${r.path}\``).join("\n") +
    `\nCall list_local_files for the full list.`
  );
}

// ONTA-253: this tool's contract is "ingest a CSV FILE" — so a path that does
// not resolve to a readable file must be a CLEAR error, never a silent
// text-ingest of the filename. We stat the path up front (returning a specific
// error that names the missing file) BEFORE touching the SDK, and additionally
// pass `asFile:true` so the SDK hard-errors rather than degrading to text even
// if the file vanishes between the stat and the read (TOCTOU). Previously a
// missing path fell through the SDK's `ingest()` text fallback, the backend
// LLM-extracted phantom entities out of the path string, and this tool reported
// a fabricated "N entities resolved" success (persona-eval RCA).
export async function ingestCsvHandler(
  {
    file_path,
    kg_name,
    join_on,
  }: { file_path: string; kg_name: string; join_on?: string },
  makeClient: () => Client = client,
  roots: string[] = LOCAL_FILE_ROOTS,
) {
  let ok = false;
  try {
    ok = existsSync(file_path) && statSync(file_path).isFile();
  } catch {
    ok = false;
  }
  if (!ok) {
    return errorResult(
      new Error(
        `CSV file not found or not a readable file: ${file_path}. ` +
          `ingest_csv requires an absolute path to an existing CSV file — ` +
          `nothing was ingested.` +
          notFoundHint(file_path, roots),
      ),
    );
  }
  try {
    const result = await makeClient().ingest(file_path, {
      kg: kg_name,
      asFile: true,
      // ONTA-250: when join_on is given, merge each row onto the EXISTING entity
      // whose key attribute matches, instead of minting a duplicate (thin
      // pass-through to the SDK's keyJoin → the canonical route's key_join).
      ...(join_on ? { keyJoin: { keyAttribute: join_on } } : {}),
    });
    const entities = Number(result.entities_resolved ?? 0);
    const triples = Number(result.triples_inserted ?? 0);
    return textResult(
      `Ingestion complete: ${entities} entities resolved, ${triples} triples inserted into "${kg_name}".`,
    );
  } catch (err) {
    return errorResult(err);
  }
}

export async function ingestTextHandler(
  {
    text,
    kg_name,
    format,
  }: { text: string; kg_name: string; format?: string },
  makeClient: () => Client = client,
) {
  const content = text ?? "";
  if (!content.trim()) {
    return errorResult(
      new Error(
        "ingest_text requires non-empty `text` — nothing was ingested.",
      ),
    );
  }
  if (!kg_name?.trim()) {
    return errorResult(
      new Error(
        "ingest_text requires `kg_name` — nothing was ingested.",
      ),
    );
  }
  try {
    // asText:true forces the SDK's text path even if `text` happens to look
    // like an existing file path — the caller's intent is raw content.
    const result = await makeClient().ingest(content, {
      kg: kg_name,
      contentType: format ?? "text",
      asText: true,
    });
    const extracted = Number(result.entities_extracted ?? 0);
    const entities = Number(result.entities_resolved ?? 0);
    const triples = Number(result.triples_inserted ?? 0);
    // Text ingest has an extraction phase; surface that counter when present
    // (mirrors the CLI's printIngestResult) so agents can see extraction vs
    // resolution, not only the write counts.
    const parts = [
      result.entities_extracted !== undefined
        ? `${extracted} entities extracted`
        : null,
      `${entities} entities resolved`,
      `${triples} triples inserted into "${kg_name}"`,
    ].filter(Boolean);
    return textResult(`Ingestion complete: ${parts.join(", ")}.`);
  } catch (err) {
    return errorResult(err);
  }
}

// F10: get data back out. Thin over the canonical
// `GET /graphs/{tenant}/kgs/{kg}/export` route via the SDK's `exportKg` — the
// same path the CLI's `infona export` uses (interface convergence). Exported
// so the render/truncation contract can be unit-tested with a stubbed client.
// Cap body size so a multi-MB graph dump never floods the agent context window;
// agents can re-call with `type`/`limit` or use the CLI for a full dump.
const EXPORT_MAX_CHARS = 100_000;

export async function exportKgHandler(
  {
    kg_name,
    format,
    type,
    limit,
  }: {
    kg_name: string;
    format?: "json" | "csv";
    type?: string;
    limit?: number;
  },
  makeClient: () => Client = client,
) {
  if (!kg_name?.trim()) {
    return errorResult(
      new Error("export_kg requires `kg_name` — nothing was exported."),
    );
  }
  try {
    const fmt = format ?? "json";
    const data = await makeClient().exportKg(kg_name, {
      format: fmt,
      type,
      limit,
    });
    // JSON → pretty object; CSV → raw string body (mirrors the CLI).
    let body =
      typeof data === "string" ? data : JSON.stringify(data, null, 2);
    if (body.length > EXPORT_MAX_CHARS) {
      // Keep the whole response (body + note) under the cap so the agent
      // context window still has headroom; the note must never push us over.
      const notePrefix =
        "\n\nNote: export truncated for MCP context size (original " +
        `${body.length} characters). Narrow with \`type\` and/or \`limit\`, ` +
        "or use the CLI `infona export` for the full dump.";
      const keep = Math.max(0, EXPORT_MAX_CHARS - notePrefix.length);
      body = body.slice(0, keep) + notePrefix;
    }
    return textResult(body);
  } catch (err) {
    return errorResult(err);
  }
}

export async function listLocalFilesHandler(
  {
    subdir,
    pattern,
    max_depth,
    limit,
  }: { subdir?: string; pattern?: string; max_depth?: number; limit?: number },
  roots: string[] = LOCAL_FILE_ROOTS,
) {
  const depth = max_depth ?? DEFAULT_DEPTH;
  try {
    const res = listWorkspaceFiles(roots, {
      subdir,
      pattern,
      maxDepth: depth,
      limit: limit ?? DEFAULT_LIMIT,
    });
    return textResult(renderListResult(res, roots, Math.min(depth, MAX_DEPTH)));
  } catch (err) {
    return errorResult(err);
  }
}

export function registerIngestTools(server: McpServer): void {
  server.registerTool(
    "ingest_csv",
    {
      description:
        "Ingest a CSV file into a context graph. The schema is automatically " +
        "inferred. To JOIN an internal CSV onto an EXISTING graph — merging each " +
        "row onto the entity that already carries the same exact key value instead " +
        "of creating duplicates — set join_on to the key attribute (e.g. an id " +
        "column). For free-form notes / unstructured text (no file on disk), use " +
        "`ingest_text` instead.",
      inputSchema: {
        file_path: z
          .string()
          .describe("Absolute path to the CSV file to ingest."),
        kg_name: z
          .string()
          .describe(
            'Name for the context graph (e.g., "sales-data", "customer-records").',
          ),
        join_on: z
          .string()
          .optional()
          .describe(
            "Optional. The snake_case attribute name to JOIN on (the attribute the " +
              "key column maps to, e.g. an id column). When set, each row is merged " +
              "ONTO the existing entity whose key attribute equals the row's key " +
              "value — no duplicate is minted; a row matching nothing mints a new " +
              "node. Omit for ordinary ingest.",
          ),
      },
    },
    async ({ file_path, kg_name, join_on }) =>
      ingestCsvHandler({ file_path, kg_name, join_on }),
  );
  registerIngestDltTool(server);
  server.registerTool(
    "ingest_text",
    {
      description:
        "Ingest free-form text (or JSON) into a context graph WITHOUT writing a " +
        "file first. The backend extracts entities via LLM, resolves them " +
        "against the ontology, and inserts triples — the same path as the CLI's " +
        "`infona ingest --text`. Use this to remember notes, meeting summaries, " +
        "or any unstructured knowledge in-context. For tabular CSV files on " +
        "disk, use `ingest_csv` instead.",
      inputSchema: {
        text: z
          .string()
          .describe(
            "Raw text (or JSON string) to extract entities from and write into the graph.",
          ),
        kg_name: z
          .string()
          .describe(
            'Name of the context graph to write into (e.g. "notes", "crm"). ' +
              "Created if it does not exist.",
          ),
        format: z
          .enum(["text", "json"])
          .optional()
          .describe(
            'Content format. Default "text". Pass "json" when `text` is a JSON ' +
              "document/array so extraction skips free-text parsing.",
          ),
      },
    },
    async ({ text, kg_name, format }) =>
      ingestTextHandler({ text, kg_name, format }),
  );
  server.registerTool(
    "export_kg",
    {
      description:
        "Export a context graph's instance data as JSON or CSV. Rides the same " +
        "canonical `GET /graphs/{tenant}/kgs/{kg}/export` route as the CLI's " +
        "`infona export`. Use this to pull data out for inspection, download, or " +
        "handoff to another tool. Large dumps are truncated in the response with " +
        "a note — narrow with `type`/`limit` or use the CLI for the full file.",
      inputSchema: {
        kg_name: z
          .string()
          .describe(
            "Name of the context graph to export. Use list_knowledge_graphs to see available KGs.",
          ),
        format: z
          .enum(["json", "csv"])
          .optional()
          .describe('Output format. Default "json". Pass "csv" for a tabular dump.'),
        type: z
          .string()
          .optional()
          .describe(
            'Optional entity type filter (e.g. "Book") — export only that type\'s rows.',
          ),
        limit: z
          .number()
          .int()
          .min(1)
          .optional()
          .describe("Max rows to export (server-side cap still applies)."),
      },
    },
    // Wrapped, not passed directly: the MCP SDK calls the callback with a second
    // `extra` argument, which would otherwise land in `makeClient`.
    (args) => exportKgHandler(args),
  );
  // Registered ONLY when a root resolved. A registered-but-always-erroring tool
  // would still advertise the capability in every tools/list and burn context in
  // every session, so the dormant state is "absent", not "present and broken".
  if (LOCAL_FILE_ROOTS.length) {
    server.registerTool(
      "list_local_files",
      {
        description:
          "List data files (" +
          ALLOWED_EXTENSIONS.join(", ") +
          ") that exist on the user's LOCAL machine, so you can pass a real " +
          "absolute path to ingest_csv instead of guessing one. IMPORTANT: only a " +
          "directory the user explicitly configured is visible to this server. It " +
          "cannot see the rest of the filesystem, so if a file is not listed here " +
          "it is not reachable: do NOT retry with '/' or some other path, ask the " +
          "user instead. Results are the most recently modified files first, and " +
          "no file contents are read.",
        inputSchema: {
          subdir: z
            .string()
            .optional()
            .describe(
              "Optional subdirectory, relative to the configured root, to list " +
                "instead of the whole root. Paths outside the root are rejected.",
            ),
          pattern: z
            .string()
            .optional()
            .describe(
              "Optional filename filter. Plain text is a case-insensitive " +
                'substring match (e.g. "tecentriq"); `*` and `?` make it an ' +
                'anchored glob (e.g. "*-demo.csv").',
            ),
          max_depth: z
            .number()
            .int()
            .min(0)
            .max(MAX_DEPTH)
            .optional()
            .describe(
              `How many directory levels below the root to descend (0 = the root ` +
                `only). Default ${DEFAULT_DEPTH}, max ${MAX_DEPTH}.`,
            ),
          limit: z
            .number()
            .int()
            .min(1)
            .max(MAX_RESULTS)
            .optional()
            .describe(`Max files to return. Default ${DEFAULT_LIMIT}, max ${MAX_RESULTS}.`),
        },
      },
      async ({ subdir, pattern, max_depth, limit }) =>
        listLocalFilesHandler({ subdir, pattern, max_depth, limit }),
    );
  }

  server.registerTool(
    "create_knowledge_graph",
    {
      description:
        "Create a new, empty context graph in the current tenant. Use this " +
        "before ingesting data into a fresh graph (ingest_csv / ingest_text also " +
        "auto-create a graph, so this is for setting one up explicitly / with a description).",
      inputSchema: {
        name: z
          .string()
          .describe('Name for the new context graph (e.g. "sales-2026").'),
        description: z
          .string()
          .optional()
          .describe("Optional human-readable description of the graph."),
      },
    },
    async ({ name, description }) => {
      try {
        const kg = await client().createKg(name, description);
        return textResult(
          `Created context graph "${String(kg.name ?? name)}".`,
        );
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  server.registerTool(
    "delete_knowledge_graph",
    {
      description:
        "Delete a context graph and ALL of its data. This is irreversible — " +
        "confirm with the user before calling it.",
      inputSchema: {
        name: z.string().describe("Name of the context graph to delete."),
      },
    },
    async ({ name }) => {
      try {
        await client().deleteKg(name);
        return textResult(`Deleted context graph "${name}".`);
      } catch (err) {
        return errorResult(err);
      }
    },
  );
}
