import { randomUUID } from "node:crypto";
import { existsSync, realpathSync, statSync } from "node:fs";
import { basename } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { Client, InfonaError, isTerminalJobStatus } from "@infona-ai/cli";
import type {
  AgentResult,
  JobCategory,
  JobStatus,
  ResolvedChange,
  Schedule,
} from "@infona-ai/cli";
import { z } from "zod";
import {
  ALLOWED_EXTENSIONS,
  DEFAULT_DEPTH,
  DEFAULT_LIMIT,
  MAX_DEPTH,
  MAX_RESULTS,
  describeRootResolution,
  listWorkspaceFiles,
  renderListResult,
  resolveRoots,
} from "./workspace.js";

const VERSION = "0.1.0";

// ONTA-415: the local directory the user has explicitly granted this server, if
// any. Resolved ONCE at startup from INFONA_LOCAL_FILES_DIR (see workspace.ts for
// the precedence and for why there is no default root). Empty means the
// capability stays DORMANT: `list_local_files` is never registered, so it does
// not appear in tools/list and burns no context in a session that has no root.
const LOCAL_FILE_ROOTS: string[] = (() => {
  const res = resolveRoots();
  // One stderr line, but ONLY for a user who actually set the var: a bad path,
  // a refused filesystem root, or a successful grant are all worth reporting,
  // whereas the overwhelming majority who never opt in should get no noise at
  // all for a feature they are not using.
  if (res.varName) {
    try {
      process.stderr.write(`${describeRootResolution(res)}\n`);
    } catch {
      // stderr unavailable; the resolution itself still stands.
    }
  }
  return res.roots;
})();

// The job categories the `list_jobs` filter accepts. This MUST stay in lockstep
// with the backend `JobCategory` enum (infona_client/enrichment/models.py) — a
// missing member silently hides that category's jobs from the agent: the enum
// used to omit "discovery", so `list_jobs({category:"discovery"})` was rejected
// AND the natural fallback `category:"enrichment"` filtered the discovery job
// OUT ("No jobs found"), stranding a working web-ingest job the agent had just
// kicked off (persona-eval RCA, ONTA-243). Sourcing the list from the SDK's
// exported `JobCategory` type (via the exhaustiveness check below) makes a future
// backend addition a COMPILE error here rather than a silent runtime gap.
const JOB_CATEGORIES = [
  "enrichment",
  "dedupe",
  "reconciliation",
  "discovery",
  "ingest",
  "answer",
] as const;

// Compile-time drift guard: `JOB_CATEGORIES` must enumerate EXACTLY the SDK's
// `JobCategory` union — no more, no less. If the backend adds/removes a category
// (and the SDK type is regenerated), these two assignments stop type-checking
// until `JOB_CATEGORIES` is updated to match, so the runtime enum can never drift
// from the backend again. Purely a type check — erased at build time.
type _CategoryUnion = (typeof JOB_CATEGORIES)[number];
const _assertCategoriesCoverSdk: JobCategory = "" as _CategoryUnion;
const _assertSdkCoversCategories: _CategoryUnion = "" as JobCategory;
void _assertCategoriesCoverSdk;
void _assertSdkCoversCategories;

// Stable conversation id for this MCP server process. The `agent` tool threads
// this into every backend `/agent` call when the caller does not supply its own
// `session_id`, so multi-turn context accumulates across tool invocations. The
// OSS planner's clarify-convergence machinery is gated on a session id and
// silently no-ops without one (infona_client/agent/planner.py history load +
// `_effective_instruction`; web_ingest_cap `already_asked`), so a missing id
// means every turn is planned statelessly and a single stated intent gets
// re-clarified indefinitely. Minting once per process keeps the whole session's
// turns on one thread.
const DEFAULT_SESSION_ID = randomUUID();

const server = new McpServer(
  {
    name: "infona",
    version: VERSION,
  },
  {
    instructions:
      "Infona is a context graph platform. Use these tools to " +
      "query structured data across multiple context graphs using natural language.",
  },
);

function client(): Client {
  return new Client();
}

function textResult(text: string) {
  return {
    content: [{ type: "text" as const, text }],
  };
}

function errorResult(err: unknown) {
  const msg =
    err instanceof InfonaError
      ? `Infona error: ${err.message}`
      : err instanceof Error
        ? err.message
        : String(err);
  return {
    content: [{ type: "text" as const, text: msg }],
    isError: true,
  };
}

server.registerTool(
  "list_knowledge_graphs",
  {
    description:
      "List all available context graphs and their descriptions.",
    inputSchema: {},
  },
  async () => {
    try {
      const kgs = await client().listKgs();
      if (!kgs.length) return textResult("No context graphs found.");
      const lines = kgs.map((kg) => {
        const name = String(kg.name ?? "?");
        const desc = kg.description ? `: ${kg.description}` : "";
        return `- ${name}${desc}`;
      });
      return textResult(lines.join("\n"));
    } catch (err) {
      return errorResult(err);
    }
  },
);

server.registerTool(
  "ask",
  {
    description:
      "Ask a natural language question against a context graph. " +
      'Use list_knowledge_graphs to see available KGs first.',
    inputSchema: {
      question: z
        .string()
        .describe(
          'The natural language question to ask (e.g., "How many events are in San Francisco?")',
        ),
      kg_name: z
        .string()
        .optional()
        .describe(
          "Name of the context graph to query. Use list_knowledge_graphs to see available KGs.",
        ),
    },
  },
  async ({ question, kg_name }) => {
    try {
      const data = await client().ask(question, { kg: kg_name });
      // Prefer the human narrative when present; keep the raw binding dump as
      // Answer so agents can still quote the precise value.
      const narrative =
        (typeof data.narrative_answer === "string" && data.narrative_answer) ||
        (typeof data.narrative === "string" && data.narrative) ||
        "";
      const answer =
        (typeof data.answer === "string" && data.answer) ||
        (data.answer != null ? String(data.answer) : "No answer");
      const explanation =
        typeof data.explanation === "string" ? data.explanation : "";
      const sparql = typeof data.sparql === "string" ? data.sparql : "";
      const runId =
        (typeof data.run_id === "string" && data.run_id) ||
        (typeof data.job_id === "string" && data.job_id) ||
        "";
      const citations = Array.isArray(data.citations) ? data.citations : [];

      // Surface provenance fields the UI (and the model) need for expandable
      // citations / SPARQL / query id. Previously only answer+explanation were
      // returned, so agents silently dropped SPARQL and run_id.
      const lines: string[] = [];
      if (narrative) lines.push(narrative.trim());
      lines.push(`Answer: ${answer}`);
      if (explanation) lines.push(`Explanation: ${explanation}`);
      if (runId) lines.push(`run_id: ${runId}`);
      if (sparql) lines.push(`\nSPARQL:\n${sparql}`);
      if (citations.length) {
        lines.push("\nCitations:");
        for (const c of citations) {
          if (typeof c === "string") {
            lines.push(`  - ${c}`);
          } else if (c && typeof c === "object") {
            const o = c as Record<string, unknown>;
            const url = o.url ?? o.source_url ?? o.href;
            const title = o.title ?? o.label ?? o.source;
            const bit = [title, url].filter(Boolean).join(" — ");
            lines.push(`  - ${bit || JSON.stringify(c)}`);
          } else {
            lines.push(`  - ${String(c)}`);
          }
        }
      }
      // Machine-readable block for clients that parse structured fields.
      lines.push(
        "",
        "Raw result:",
        JSON.stringify(
          {
            answer: data.answer,
            narrative_answer: data.narrative_answer ?? data.narrative,
            sparql: data.sparql,
            run_id: runId || undefined,
            citations: data.citations,
            explanation: data.explanation,
            timing: data.timing,
          },
          null,
          2,
        ),
      );
      return textResult(lines.join("\n"));
    } catch (err) {
      return errorResult(err);
    }
  },
);

// ONTA-178: thin over the canonical `POST /graphs/{tenant}/search` route (via
// the SDK's `search`) — the ONE search endpoint every interface rides. The
// server embeds the query and ranks; this tool only renders hits. Exported so
// the degraded-empty rendering contract can be unit-tested with a stub client.
export async function searchHandler(
  {
    query,
    kg_name,
    type,
    entity_uris,
    top_k,
  }: {
    query: string;
    kg_name?: string;
    type?: string;
    entity_uris?: string[];
    top_k?: number;
  },
  makeClient: () => Client = client,
) {
  try {
    const res = await makeClient().search(query, {
      kg: kg_name,
      type,
      entityUris: entity_uris,
      topK: top_k,
    });
    // Honesty first: when the index is off / embedding unavailable the route
    // still returns 200 with degraded:true (keyword-only). An empty page under
    // that mode must NOT read as "nothing exists" — it may simply be
    // unindexed. Always surface the degraded note, even with zero hits, and
    // point agents at `grep` for an index-free literal scan of one graph.
    const degradedNote =
      "Note: results are keyword-only (reduced recall) — the embedding " +
      "service was unavailable or the semantic index is off for this query. " +
      "Use `grep` for an index-free literal substring scan of one graph.";
    if (!res.hits.length) {
      return textResult(
        res.degraded
          ? `No matching entities found.\n\n${degradedNote}`
          : "No matching entities found.",
      );
    }
    const lines = res.hits.map((h, i) => {
      const label =
        typeof h.attrs?.label === "string" && h.attrs.label
          ? h.attrs.label
          : h.entity_uri;
      const kind =
        typeof h.attrs?.type === "string" && h.attrs.type
          ? ` (${h.attrs.type})`
          : "";
      return `${i + 1}. ${label}${kind} — ${h.entity_uri}\n   [${h.attr}] ${h.snippet}`;
    });
    if (res.degraded) {
      lines.push("", degradedNote);
    }
    return textResult(lines.join("\n"));
  } catch (err) {
    return errorResult(err);
  }
}

server.registerTool(
  "search",
  {
    description:
      "Search entities by NAME or by what their free-text attributes say " +
      "(descriptions, bios, notes, speeches, …). Hybrid keyword + meaning " +
      "search over the derived index, returning a matching snippet as the " +
      "citation. Use it for \"which entity is called X\" and \"which entities " +
      "mention/discuss X\"; use `ask` for aggregate or structured questions, " +
      "and `grep` when you need a literal SUBSTRING match or when search " +
      "reports reduced recall (keyword-only / index off) and returns nothing. " +
      "For filter-then-semantic, run a structured filter first and pass the " +
      "resulting entity URIs via `entity_uris` so ranking only considers that set.",
    inputSchema: {
      query: z.string().describe("Free-text search query (topic, phrase, or quote)."),
      kg_name: z
        .string()
        .optional()
        .describe(
          "Optional context graph to search within. Omit to search every KG " +
            "in the tenant. Use list_knowledge_graphs to see available KGs.",
        ),
      type: z
        .string()
        .optional()
        .describe('Optional entity type filter (e.g. "Speech").'),
      entity_uris: z
        .array(z.string())
        .optional()
        .describe(
          "Optional entity-URI allowlist (filter-then-semantic): pass URIs " +
            "from a structured filter / SPARQL so hybrid ranking only " +
            "considers that set. Omit = unrestricted; [] = zero hits; " +
            "server blanks-strip + dedupes and 400s above 500 unique URIs.",
        ),
      top_k: z
        .number()
        .int()
        .min(1)
        .max(50)
        .optional()
        .describe("Max entities to return (server clamps to 1..50; default 10)."),
    },
  },
  (args) => searchHandler(args),
);

// ONTA-415: the "did you mean" suffix on a not-found path. This is the SECOND
// consumer of the one containment-guarded primitive, not a second enumeration
// path, so there is a single guard and a single test surface.
//
// The suffix is CONDITIONAL on a root actually being configured. Naming a tool
// that is absent from tools/list is the ONTA-243 failure class documented at the
// top of this file: the model is sent after a capability it cannot call and
// strands the task. With no root there is no lister and no hint.
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

// ONTA-416: thin over the canonical `POST /graphs/{tenant}/grep` route (via the
// SDK's `grep`) — the ONE literal-scan endpoint every interface rides. The
// handler is EXPORTED so its rendering contract can be tested with a stubbed
// client (same pattern as `ingestCsvHandler`), since the rendering is what keeps
// an unbounded literal out of the agent's context window.
export async function grepHandler(
  {
    q,
    kg_name,
    type,
    predicate,
    case_sensitive,
    limit,
  }: {
    q: string;
    kg_name: string;
    type?: string;
    predicate?: string;
    case_sensitive?: boolean;
    limit?: number;
  },
  makeClient: () => Client = client,
) {
  try {
    const res = await makeClient().grep(q, kg_name, {
      type,
      predicate,
      caseSensitive: case_sensitive,
      limit,
    });
    if (!res.matches.length) {
      return textResult(
        `No literal matches for ${JSON.stringify(q)} in "${kg_name}".`,
      );
    }
    const lines = res.matches.map((m, i) => {
      const who = m.label || m.entity_uri;
      const kind = m.type ? ` (${m.type})` : "";
      return `${i + 1}. ${who}${kind} — ${m.entity_uri}\n   [${m.attr}] ${m.snippet}`;
    });
    if (res.truncated) {
      // Never let a capped page read as an exhaustive answer: an agent that
      // concludes "only N exist" from a truncated grep draws a false negative.
      lines.push(
        "",
        `Note: stopped at the limit of ${res.limit} matches — MORE EXIST. ` +
          "Narrow with `type`/`predicate`, or raise `limit` (max 200).",
      );
    }
    return textResult(lines.join("\n"));
  } catch (err) {
    return errorResult(err);
  }
}

server.registerTool(
  "grep",
  {
    description:
      "Literal substring search across every literal value in ONE context " +
      "graph, by scanning its triples directly (no index). Use it for exact " +
      'string debugging — "is this value anywhere in the graph?", "which ' +
      'entities contain this id/typo/URL?" — and for data that `search` ' +
      "cannot see because it was never indexed. Plain substring matching, not " +
      "regex. Prefer `search` for meaning/topic questions and `ask` for " +
      "aggregate or structured ones: this scan is unranked and can be slow on " +
      "a large graph.",
    inputSchema: {
      q: z
        .string()
        .min(2)
        .describe(
          "Exact substring to look for (at least 2 non-whitespace characters).",
        ),
      kg_name: z
        .string()
        .describe(
          "REQUIRED context graph to scan — the scan is index-free, so it must " +
            "be bounded to one graph. Use list_knowledge_graphs to see them.",
        ),
      type: z
        .string()
        .optional()
        .describe('Only match entities of this type (e.g. "Person").'),
      predicate: z
        .string()
        .optional()
        .describe(
          'Only match this attribute — a leaf name ("title") or a full predicate URI.',
        ),
      case_sensitive: z
        .boolean()
        .optional()
        .describe("Match case-sensitively (default false)."),
      limit: z
        .number()
        .int()
        .min(1)
        .max(200)
        .optional()
        .describe("Max matches to return (server clamps to 1..200; default 50)."),
    },
  },
  // Wrapped, not passed directly: the MCP SDK calls the callback with a second
  // `extra` argument, which would otherwise land in `makeClient`.
  (args) => grepHandler(args),
);

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

// Dogfood S1: agents previously could not write in-context knowledge without
// first writing a CSV to disk and calling `ingest_csv`. This tool posts raw
// text (or JSON) through the SAME canonical `POST /graphs/{tenant}/ingest`
// route the CLI's `infona ingest --text` uses — no reimplementation, no local
// write path. Exported for unit tests with a stubbed client.
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

// ONTA-415: attacks the ROOT CAUSE of ONTA-253. That fix made a guessed path
// fail LOUDLY (local stat + `asFile:true`) but did nothing to stop the guessing:
// because `ingest_csv` demands an absolute path, an agent with no way to see the
// filesystem had to invent one. This tool removes the guess.
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

server.registerTool(
  "view_ontology",
  {
    description:
      "View the ontology (types, attributes, relationships) across all context graphs.",
    inputSchema: {},
  },
  async () => {
    try {
      const types = await client().ontologyTypes();
      if (!types.length) return textResult("No ontology types defined yet.");
      const lines: string[] = [];
      for (const t of types) {
        const name = String(t.name ?? "?");
        lines.push(`Type: ${name}`);
        const attrs = (t.attributes ?? []) as Array<Record<string, unknown>>;
        if (attrs.length) {
          lines.push(
            `  Attributes: ${attrs.map((a) => String(a.name ?? "?")).join(", ")}`,
          );
        }
        const rels = (t.relationships ?? []) as Array<Record<string, unknown>>;
        if (rels.length) {
          lines.push(
            `  Relationships: ${rels
              .map(
                (r) =>
                  `${String(r.predicate ?? "?")} -> ${String(r.target_type ?? "?")}`,
              )
              .join(", ")}`,
          );
        }
      }
      return textResult(lines.join("\n"));
    } catch (err) {
      return errorResult(err);
    }
  },
);

// ONTA-418: `view_ontology` is tenant-wide and DECLARATION-only, so an agent
// could see that a type has an attribute but never whether that attribute
// carries data in the graph it is about to query. So it guessed between similar
// names (`fda_indications` vs `indications`). This tool rides the backend's
// KG-scoped schema route (via the SDK's `kgSchema`), which assembles the whole
// KG's population data server-side from stats that are already materialized:
// ONE request, not a per-type fan-out from here.
function renderSlot(s: {
  name: string;
  coverage_pct: number;
  populated: boolean;
  datatype?: string;
  target_type?: string | null;
}): string {
  const kind = s.target_type ? ` -> ${s.target_type}` : s.datatype ? ` (${s.datatype})` : "";
  // EMPTY, not "0%": the agent must not read an unpopulated slot as a weak
  // signal it can still query. It is a declaration with nothing behind it.
  const cov = s.populated ? `${s.coverage_pct}%` : "EMPTY";
  return `${s.name}${kind} ${cov}`;
}

export async function inspectGraphSchemaHandler(
  {
    kg_name,
    type,
    min_coverage,
  }: { kg_name: string; type?: string[]; min_coverage?: number },
  makeClient: () => Client = client,
) {
  try {
    const data = await makeClient().kgSchema(kg_name, {
      types: type,
      minCoverage: min_coverage,
    });
    if (!data.types.length) {
      // Name what the graph DOES have, so a filter typo never reads as
      // "that type does not exist" (the failure this tool exists to prevent).
      const available = data.available_type_names ?? [];
      return textResult(
        `Context graph "${kg_name}" has no types matching that request.` +
          (available.length
            ? ` Types it does have: ${available.join(", ")}.`
            : " The graph has no types at all."),
      );
    }
    // Say "matching" when a filter was applied, so a drill-in is never misread
    // as "this graph has exactly one type".
    const scope = type?.length ? "matching type(s)" : "type(s)";
    const lines: string[] = [
      `Schema for context graph "${kg_name}" (${data.total_types} ${scope})` +
        (data.stats_source === "live_scan"
          ? " (scanned live; precomputed stats not materialized yet)"
          : "") +
        ". Percentages are the share of that type's entities carrying the slot; " +
        "EMPTY means declared but with no data in this graph.",
    ];
    for (const t of data.types) {
      const suffix = t.declared_only
        ? " (declared in the ontology, NO instances in this graph)"
        : "";
      lines.push("", `${t.name}: ${t.entity_count} entities${suffix}`);
      if (t.description) lines.push(`  ${t.description}`);
      if (t.attributes.length) {
        lines.push(`  attributes: ${t.attributes.map(renderSlot).join(", ")}`);
      }
      if (t.relationships.length) {
        lines.push(`  relationships: ${t.relationships.map(renderSlot).join(", ")}`);
      }
      const withheld = t.attributes_withheld + t.relationships_withheld;
      if (withheld) {
        lines.push(
          `  (${withheld} more below the min_coverage floor; lower or drop it to see them)`,
        );
      }
    }
    if (data.truncated) {
      lines.push(
        "",
        `Also present, not expanded here (pass type= to drill in): ${data.omitted_type_names.join(", ")}`,
      );
    }
    lines.push("", `Note: ${data.coverage_note}`);
    return textResult(lines.join("\n"));
  } catch (err) {
    return errorResult(err);
  }
}

server.registerTool(
  "inspect_graph_schema",
  {
    description:
      "Inspect ONE context graph's schema WITH population data: for every type, " +
      "which attributes and relationships actually carry data there, and on what " +
      "share of that type's entities. Use this before asking for specific " +
      "attributes so you never guess between similar names (e.g. " +
      '"fda_indications" vs "indications") or query a slot that is declared but ' +
      "empty. Differs from view_ontology, which is tenant-wide and " +
      "DECLARATION-only (every type in the workspace, no population): this tool " +
      "is scoped to one graph and population-aware. Declared-but-empty types and " +
      "attributes are still listed, marked EMPTY, so an absent slot is never " +
      "confused with a non-existent one. Coverage is relative to each type's own " +
      "entity count, which attributes an entity carrying several types to only " +
      "one of them, so types that share multi-typed entities can legitimately " +
      "read below 100%. Sample VALUES are not returned.",
    inputSchema: {
      kg_name: z
        .string()
        .describe(
          "Name of the context graph to inspect. Use list_knowledge_graphs to see available KGs.",
        ),
      type: z
        .array(z.string())
        .optional()
        .describe(
          "Optional type names to drill into (e.g. [\"Drug\"]). Omit for the whole graph.",
        ),
      min_coverage: z
        .number()
        .optional()
        .describe(
          "Optional coverage floor as a PERCENT (0-100). Withholds slots below it " +
            "to keep a wide schema readable; the response says how many were withheld. " +
            "Omit to see every slot, including empty ones.",
        ),
    },
  },
  async ({ kg_name, type, min_coverage }) =>
    inspectGraphSchemaHandler({ kg_name, type, min_coverage }),
);

function describeChange(c: ResolvedChange): string {
  const verb =
    c.kind === "relationship"
      ? `relationship "${c.name}" from ${c.subject_type} -> ${c.datatype_or_target}`
      : `attribute "${c.name}" (${c.datatype_or_target}) on ${c.subject_type}`;
  return `[${c.action}] ${verb} — confidence ${c.confidence.toFixed(2)}: ${c.reason}`;
}

server.registerTool(
  "evolve_ontology",
  {
    description:
      "Evolve the context-graph ontology from a plain-language description of " +
      "the change you want. You do NOT need to know exact type, attribute, or " +
      'relationship names — just describe the change in natural language (e.g. ' +
      '"track which company a person works for" or "people should have a birth ' +
      'date") and the server resolves it against the existing ontology. ' +
      "High-confidence changes are applied automatically; lower-confidence ones " +
      "are returned as proposals for you to confirm by passing them to " +
      "apply_ontology_change.",
    inputSchema: {
      ask: z
        .string()
        .describe(
          "A plain-language description of the ontology change to make " +
            '(e.g. "track which company a person works for"). No exact schema ' +
            "names required.",
        ),
      knowledge_graph: z
        .string()
        .optional()
        .describe(
          "Optional name of the context graph to scope the change to. " +
            "Use list_knowledge_graphs to see available KGs.",
        ),
    },
  },
  async ({ ask, knowledge_graph }) => {
    try {
      const result = await client().ontologyResolve(ask, { knowledge_graph });
      const lines: string[] = [result.summary];

      if (result.applied.length) {
        lines.push("", "Auto-applied:");
        for (const c of result.applied) lines.push(`  ${describeChange(c)}`);
      } else {
        lines.push("", "Auto-applied: none");
      }

      if (result.proposals.length) {
        lines.push(
          "",
          "Proposals needing confirmation (pass one straight to apply_ontology_change):",
        );
        for (const c of result.proposals) lines.push(`  ${describeChange(c)}`);
        lines.push(
          "",
          "Raw proposal objects:",
          JSON.stringify(result.proposals, null, 2),
        );
      } else {
        lines.push("", "Proposals needing confirmation: none");
      }

      return textResult(lines.join("\n"));
    } catch (err) {
      return errorResult(err);
    }
  },
);

// The raw ResolvedChange proposal shape, shared by the single- and batch-apply
// tools so they can never drift.
const proposalShape = z.object({
  kind: z.enum(["attribute", "relationship"]),
  subject_type: z.string(),
  name: z.string(),
  datatype_or_target: z.string(),
  action: z.enum(["reuse", "extend", "create"]),
  confidence: z.number(),
  reason: z.string(),
});

server.registerTool(
  "apply_ontology_change",
  {
    description:
      "Confirm and apply a single ontology change proposal returned by " +
      "evolve_ontology. Pass one of the raw proposal objects through unchanged " +
      "as `proposal`. To apply several proposals at once, prefer " +
      "apply_ontology_changes (one call instead of many).",
    inputSchema: {
      proposal: proposalShape.describe(
        "A ResolvedChange proposal object exactly as returned by evolve_ontology.",
      ),
    },
  },
  async ({ proposal }) => {
    try {
      const result = await client().ontologyApply(proposal as ResolvedChange);
      const lines = [result.summary];
      lines.push("", `Operations applied: ${result.operations}`);
      lines.push(describeChange(result.applied));
      return textResult(lines.join("\n"));
    } catch (err) {
      return errorResult(err);
    }
  },
);

server.registerTool(
  "apply_ontology_changes",
  {
    description:
      "Confirm and apply SEVERAL ontology change proposals returned by " +
      "evolve_ontology in a single call — pass the raw proposal objects as " +
      "`proposals`. Prefer this over calling apply_ontology_change once per " +
      "proposal: it is one round-trip instead of N and reports each change's " +
      "outcome. Idempotent; a proposal that fails does not abort the rest.",
    inputSchema: {
      proposals: z
        .array(proposalShape)
        .min(1)
        .describe(
          "The ResolvedChange proposal objects to apply, exactly as returned " +
            "by evolve_ontology (the `Raw proposal objects` array).",
        ),
    },
  },
  async ({ proposals }) => {
    try {
      const result = await client().ontologyApplyBatch(
        proposals as ResolvedChange[],
      );
      const lines = [result.summary, ""];
      for (const r of result.results) {
        const status = r.ok ? "applied" : `FAILED: ${r.error}`;
        lines.push(`  ${describeChange(r.change)} — ${status}`);
      }
      return textResult(lines.join("\n"));
    } catch (err) {
      return errorResult(err);
    }
  },
);

/**
 * Render a kind-tagged agent result (the shape returned by `/agent`) as readable
 * text plus the raw JSON, so an MCP client can both read a summary and act on the
 * machine-readable fields (e.g. carry a `plan_id` back into a confirm call).
 */
function describeAgentResult(r: AgentResult): string {
  const lines: string[] = [];
  switch (r.kind) {
    case "answer": {
      const answer = (r.answer as string | undefined) ?? "(no answer)";
      if (r.narrative) lines.push(String(r.narrative));
      lines.push(`Answer: ${answer}`);
      const runId =
        (typeof r.run_id === "string" && r.run_id) ||
        (typeof r.job_id === "string" && r.job_id) ||
        "";
      if (runId) lines.push(`run_id: ${runId}`);
      if (r.sparql) lines.push(`\nSPARQL:\n${String(r.sparql)}`);
      const citations = Array.isArray(r.citations) ? r.citations : [];
      if (citations.length) {
        lines.push("\nCitations:");
        for (const c of citations) {
          if (typeof c === "string") lines.push(`  - ${c}`);
          else if (c && typeof c === "object") {
            const o = c as Record<string, unknown>;
            const bit = [o.title ?? o.label ?? o.source, o.url ?? o.source_url]
              .filter(Boolean)
              .join(" — ");
            lines.push(`  - ${bit || JSON.stringify(c)}`);
          } else lines.push(`  - ${String(c)}`);
        }
      }
      break;
    }
    case "clarify":
      lines.push(
        `Clarification needed: ${String(r.question ?? "Could you clarify?")}`,
      );
      break;
    case "plan": {
      const steps = Array.isArray(r.steps) ? r.steps : [];
      lines.push(
        `Proposed plan (${steps.length} step${steps.length === 1 ? "" : "s"}) — ` +
          `NOT yet executed. Review, then confirm by calling agent again with ` +
          `confirm_plan_id="${String(r.plan_id ?? "")}".`,
      );
      for (const s of steps as Array<Record<string, unknown>>) {
        const cap = String(s.capability ?? "?");
        const action = String(s.action ?? "?");
        const rationale = s.rationale ? ` — ${String(s.rationale)}` : "";
        lines.push(`  • [${cap}] ${action}${rationale}`);
        const cost = s.cost as Record<string, unknown> | undefined;
        if (cost?.note) lines.push(`      cost: ${String(cost.note)}`);
      }
      break;
    }
    case "result": {
      const steps = Array.isArray(r.steps) ? r.steps : [];
      lines.push(`Executed plan ${String(r.plan_id ?? "")}:`);
      for (const s of steps as Array<Record<string, unknown>>) {
        const status = String(s.status ?? "?");
        const msg = s.message ? ` — ${String(s.message)}` : "";
        lines.push(`  • [${String(s.capability ?? "?")}] ${status}${msg}`);
      }
      break;
    }
    case "error":
      lines.push(`Agent error: ${String(r.error ?? "unknown error")}`);
      break;
    default:
      lines.push(`Agent returned: ${String(r.kind)}`);
  }
  // Always append the raw JSON so the caller can read structured fields
  // (plan_id, steps, rows, …) it needs to drive the next turn.
  lines.push("", "Raw result:", JSON.stringify(r, null, 2));
  return lines.join("\n");
}

server.registerTool(
  "agent",
  {
    description:
      "Talk to the Infona Ask-AI agent — the single conversational front door " +
      "to a context graph. Send a natural-language message and the agent " +
      "classifies your intent and either ANSWERS a question directly, asks a " +
      "CLARIFYing question, or proposes a PLAN of actions (enrich attributes, " +
      "clean/normalize values, merge duplicates, inspect/extend the ontology). " +
      "A plan is NOT executed until you confirm it: call this tool again with " +
      "the returned plan_id as `confirm_plan_id`. Planning is free; any paid " +
      "step a plan contains (e.g. web enrichment) is authorized server-side at " +
      "execute time, so confirming honors your tenant's entitlements. Prefer " +
      "this over the lower-level tools for conversational, multi-step work.",
    inputSchema: {
      message: z
        .string()
        .optional()
        .describe(
          "Your natural-language message to the agent (e.g. 'how many mentors " +
            "speak Persian?' or 'enrich the company for managers'). Optional " +
            "when confirm_plan_id is set (a confirm turn carries no new message).",
        ),
      kg_name: z
        .string()
        .optional()
        .describe(
          "Context graph to operate within. Use list_knowledge_graphs to see " +
            "available KGs.",
        ),
      type_name: z
        .string()
        .optional()
        .describe(
          "Optional active type to scope the turn to (needed for enrich / clean " +
            "/ dedup planning, e.g. 'Mentor').",
        ),
      urls: z
        .array(z.string())
        .optional()
        .describe(
          "Optional explicit web page links to parse for this turn. When the " +
            "message asks to fill in attributes on existing records, the agent " +
            "extracts those values from these pages; otherwise it pulls a new " +
            "set of records from them. Plain http(s) URLs.",
        ),
      session_id: z
        .string()
        .optional()
        .describe(
          "Optional conversation id to keep multi-turn context across calls. " +
            "When omitted, the server threads a stable per-process id so " +
            "multi-turn context still accumulates and clarify-convergence " +
            "activates; pass an explicit id only to segment separate " +
            "conversations.",
        ),
      confirm_plan_id: z
        .string()
        .optional()
        .describe(
          "When set, CONFIRM and EXECUTE the previously-proposed plan with this " +
            "id (the only mutating path) instead of sending a new message. Use " +
            "the plan_id from a prior 'plan' result.",
        ),
      spend_ceiling_usd: z
        .number()
        .min(0)
        .optional()
        .describe(
          "Optional HARD per-run spend ceiling in USD for any paid " +
            "enrichment/discovery job this turn kicks off (ONTA-282/ONTA-378). " +
            "The job HALTS CLEANLY once its cumulative spend reaches this figure. " +
            "Omit for the deployment default; 0 means unlimited. Lets a caller " +
            "bound a single run without changing the global ceiling.",
        ),
    },
  },
  async ({
    message,
    kg_name,
    type_name,
    urls,
    session_id,
    confirm_plan_id,
    spend_ceiling_usd,
  }) => {
    try {
      const result = await client().agent({
        message,
        kgName: kg_name,
        typeName: type_name,
        urls,
        // Fall back to the per-process session id so turns stay on one thread
        // and the planner's clarify-convergence machinery activates even when
        // the caller does not supply its own conversation id.
        sessionId: session_id ?? DEFAULT_SESSION_ID,
        confirmPlanId: confirm_plan_id,
        spendCeilingUsd: spend_ceiling_usd,
      });
      return textResult(describeAgentResult(result));
    } catch (err) {
      return errorResult(err);
    }
  },
);

// Read a Response from the SDK's `raw` schedule methods, mapping a non-2xx into
// a clear thrown error (requestRaw resolves non-2xx as a Response, only throwing
// on network/timeout) so the tool's catch renders it uniformly.
async function readScheduleResponse<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let detail = "";
    try {
      detail = await resp.text();
    } catch {
      /* body already consumed / empty */
    }
    throw new InfonaError(
      `schedules request failed (HTTP ${resp.status})${detail ? `: ${detail}` : ""}`,
    );
  }
  return (await resp.json()) as T;
}

server.registerTool(
  "schedule",
  {
    description:
      "Set up a RECURRING standing alert / scheduled refresh, or list existing " +
      "ones. Use this when the user wants something to run ON A CADENCE (weekly, " +
      "daily, …) and be NOTIFIED automatically when watched values CHANGE — set " +
      "up ONCE, not re-run by hand ('a standing weekly alert that notifies my " +
      "orchestrator when a model changes price', 'a weekly refresh delivered to " +
      "me automatically'). It creates a recurring `notify` schedule that, each " +
      "run, snapshots the watched values and delivers a change payload to your " +
      "webhook ONLY when they changed since last run. Pass `action:\"list\"` to " +
      "see the tenant's schedules instead. This is a thin wrapper over the " +
      "canonical /graphs/{tenant}/schedules route (the same one the web app and " +
      "CLI use) — no bespoke endpoint.",
    inputSchema: {
      action: z
        .enum(["create", "list"])
        .default("create")
        .describe("`create` a new recurring alert (default) or `list` existing ones."),
      kg_name: z
        .string()
        .optional()
        .describe(
          "Context graph the alert watches. Required for `create`. Use " +
            "list_knowledge_graphs to see available KGs.",
        ),
      cadence: z
        .enum(["hourly", "daily", "weekly", "monthly"])
        .default("weekly")
        .describe("How often the alert runs (default weekly)."),
      condition: z
        .string()
        .optional()
        .describe(
          "Plain-language description of WHAT to watch for a change on (e.g. " +
            "'price or deprecation date on models I route to'). Recorded on the " +
            "schedule so the watch can be resolved to concrete values.",
        ),
      deliver_to: z
        .string()
        .optional()
        .describe(
          "An http(s) webhook URL to deliver change notifications to. When " +
            "omitted the schedule is still created but delivery is inactive until " +
            "a URL is added. The outbound POST is SSRF-guarded server-side.",
        ),
    },
  },
  async ({ action, kg_name, cadence, condition, deliver_to }) => {
    try {
      const c = client();
      if (action === "list") {
        const schedules = await readScheduleResponse<Schedule[]>(
          await c.raw.schedules(),
        );
        if (!schedules.length) return textResult("No schedules found.");
        const lines = schedules.map((s) => {
          const every = s.interval_seconds
            ? `every ${s.interval_seconds}s`
            : s.cron
              ? `cron ${s.cron}`
              : "?";
          const state = s.enabled ? "enabled" : "disabled";
          return `- ${s.id} [${s.action}] ${every} — ${state} (next: ${s.next_run ?? "?"})`;
        });
        return textResult(lines.join("\n"));
      }

      if (!kg_name) {
        return errorResult(
          new InfonaError("kg_name is required to create a schedule."),
        );
      }
      const intervalByCadence = {
        hourly: 3600,
        daily: 86_400,
        weekly: 604_800,
        monthly: 2_592_000,
      } as const;
      const params: Record<string, unknown> = {
        watch: { condition: condition ?? "" },
        condition: condition ?? "",
      };
      if (deliver_to) params.sink = { url: deliver_to };
      // Body matches the canonical POST /schedules contract. `category` is carried
      // for the model only (a notify fires no enrich-style job); enrichment is a
      // neutral default, mirroring the agent subscribe capability.
      const body = {
        kg_name,
        category: "enrichment" as JobCategory,
        action: "notify" as const,
        params,
        interval_seconds: intervalByCadence[cadence],
        enabled: true,
      };
      const created = await readScheduleResponse<Schedule>(
        await c.raw.createSchedule(body),
      );
      const lines = [
        `Created a standing ${cadence} alert on "${kg_name}" (schedule ${created.id}).`,
        `  next run: ${created.next_run ?? "?"}`,
        deliver_to
          ? `  delivers change notifications to: ${deliver_to}`
          : "  no delivery URL yet — add one to activate automatic delivery.",
        "",
        "It runs on its own and notifies only when the watched values change.",
      ];
      return textResult(lines.join("\n"));
    } catch (err) {
      return errorResult(err);
    }
  },
);

server.registerTool(
  "list_jobs",
  {
    description:
      "List background jobs (enrichment, dedupe/merge, reconciliation, " +
      "web-discovery) for the tenant, newest first. Use this to check on async " +
      "work the `agent` tool kicked off (e.g. after confirming an enrich, " +
      "find-duplicates, or discover-from-the-web plan): a plan's steps run as " +
      "background jobs, and this is how you see their status. Pass no category " +
      "to see ALL jobs across every category.",
    inputSchema: {
      category: z
        .enum(JOB_CATEGORIES)
        .optional()
        .describe(
          "Optional filter to a single job category (enrichment, dedupe, " +
            "reconciliation, discovery). A web-ingest job kicked off via the " +
            "`agent` tool is category 'discovery'.",
        ),
    },
  },
  async ({ category }) => {
    try {
      const jobs = await client().jobs(category ? { category } : {});
      if (!jobs.length) return textResult("No jobs found.");
      const lines = jobs.map((j) => {
        const rec = j as unknown as Record<string, unknown>;
        const id = String(rec.id ?? "?");
        const cat = String(rec.category ?? "?");
        const status = String(rec.status ?? "?");
        const label = rec.label ?? rec.type_name ?? "";
        return `- ${id} [${cat}] ${status}${label ? ` — ${String(label)}` : ""}`;
      });
      return textResult(lines.join("\n"));
    } catch (err) {
      return errorResult(err);
    }
  },
);

/**
 * Render a job record (the shape returned by `enrichJob` / `waitForJob`) as a
 * readable status summary plus the raw JSON. Shared by `get_job` and
 * `wait_for_job` so both surface identical status/progress lines.
 */
function renderJob(job: Record<string, unknown>, fallbackId: string): string[] {
  const status = String(job.status ?? "?");
  const lines = [`Job ${String(job.id ?? fallbackId)} — ${status}`];
  // Top-level scalars worth surfacing as named lines.
  for (const k of ["type_name", "resolved_tier", "result_count"]) {
    if (job[k] !== undefined && job[k] !== null)
      lines.push(`  ${k}: ${String(job[k])}`);
  }
  // Live progress lives under `progress.*` (processed / filled / verified /
  // total / phase) for EVERY category — read the nested shape so a discovery
  // job's streaming progress is legible at a glance (ONTA-243).
  const progress = job.progress as Record<string, unknown> | undefined;
  if (progress && typeof progress === "object") {
    for (const k of ["phase", "processed", "filled", "verified", "total"]) {
      if (progress[k] !== undefined && progress[k] !== null)
        lines.push(`  ${k}: ${String(progress[k])}`);
    }
  }
  return lines;
}

server.registerTool(
  "get_job",
  {
    description:
      "Get the full record + progress of a single background job by id (as " +
      "listed by list_jobs). Works for ANY category — enrichment, dedupe, " +
      "reconciliation, and web-discovery (there is no separate discovery-job " +
      "endpoint). Returns status, tier, live per-record progress (processed / " +
      "filled / total + phase), and, when finished, the result count. This " +
      "returns INSTANTLY with the current status — to WAIT for a long-running " +
      "job to finish, use `wait_for_job` instead of polling this in a loop.",
    inputSchema: {
      job_id: z.string().describe("The job id (from list_jobs)."),
    },
  },
  async ({ job_id }) => {
    try {
      const job = (await client().enrichJob(job_id)) as unknown as Record<
        string,
        unknown
      >;
      const lines = renderJob(job, job_id);
      lines.push("", "Raw job:", JSON.stringify(job, null, 2));
      return textResult(lines.join("\n"));
    } catch (err) {
      return errorResult(err);
    }
  },
);

server.registerTool(
  "wait_for_job",
  {
    description:
      "WAIT for a background job to finish, efficiently. Web-discovery and " +
      "enrichment jobs (kicked off via the `agent` tool) take MINUTES to " +
      "settle — do NOT poll get_job in a tight loop, which returns 'running' " +
      "instantly and wastes your turns. This tool blocks SERVER-SIDE until the " +
      "job reaches a terminal state (done / failed / cancelled / awaiting " +
      "review) or a bounded timeout, then returns its status + progress — so " +
      "ONE call covers a whole wait window. If it returns and the job is still " +
      "'running', just call wait_for_job AGAIN with the same job_id to keep " +
      "waiting; a few calls cover a multi-minute job. The graph is populated " +
      "INCREMENTALLY as the job runs, so you can also `ask` it for entities " +
      "landed so far before it fully finishes.",
    inputSchema: {
      job_id: z.string().describe("The job id (from list_jobs)."),
      timeout_s: z
        .number()
        .optional()
        .describe(
          "How long the SERVER should block, in seconds (default 60, capped " +
            "at 120 server-side). Omit for the default.",
        ),
    },
  },
  async ({ job_id, timeout_s }) => {
    try {
      const job = (await client().waitForJob(
        job_id,
        timeout_s,
      )) as unknown as Record<string, unknown>;
      const status = String(job.status ?? "?") as JobStatus;
      const lines = renderJob(job, job_id);
      lines.push("");
      if (isTerminalJobStatus(status)) {
        lines.push(
          `This job has settled (status: ${status}) — it is done and will not ` +
            `advance further.`,
        );
      } else {
        lines.push(
          `Still running (status: ${status}) after the wait window — the job ` +
            `has not finished yet. Call wait_for_job again with the same ` +
            `job_id to keep waiting, or ask the graph now for the entities ` +
            `landed so far.`,
        );
      }
      lines.push("", "Raw job:", JSON.stringify(job, null, 2));
      return textResult(lines.join("\n"));
    } catch (err) {
      return errorResult(err);
    }
  },
);

// Exported so a caller can start the SAME server without re-implementing it
// (e.g. a test that imports this package as a library): the `isEntrypoint` guard
// below is (correctly) false there, so it calls main() explicitly. Direct
// `npx -y @infona-ai/mcp` still auto-starts via the guard.
export async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

// Only start the stdio server when run as the CLI entrypoint. Guarding this lets
// a test import the module (e.g. to unit-test `ingestCsvHandler`) without opening
// a stdio transport / hanging the test process.
//
// The comparison MUST resolve symlinks on both sides. `npx -y @infona-ai/mcp` and a
// global `npm i -g @infona-ai/mcp` install the package's `bin` as a SYMLINK (e.g.
// /usr/local/bin/infona-mcp -> …/@infona-ai/mcp/dist/index.js). When node runs the file
// through that symlink, `process.argv[1]` is the symlink path while
// `import.meta.url` is this module's realpath, so a raw href compare NEVER
// matches — the guard stays false and the server silently never starts (spawns,
// connects to nothing, and exits without ever handling a request). Realpath'ing
// both sides makes the two agree for
// direct, symlinked, and npx invocations alike, while still staying false when a
// different file (a test runner) is the entrypoint.
const isEntrypoint = (() => {
  try {
    if (
      typeof process === "undefined" ||
      !Array.isArray(process.argv) ||
      process.argv[1] === undefined
    ) {
      return false;
    }
    const invoked = pathToFileURL(realpathSync(process.argv[1])).href;
    const self = pathToFileURL(realpathSync(fileURLToPath(import.meta.url))).href;
    return invoked === self;
  } catch {
    return false;
  }
})();

if (isEntrypoint) {
  main().catch((err) => {
    process.stderr.write(
      `infona-mcp failed to start: ${err instanceof Error ? err.message : String(err)}\n`,
    );
    process.exit(1);
  });
}
