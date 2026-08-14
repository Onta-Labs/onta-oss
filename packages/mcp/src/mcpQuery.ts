/** Query-side MCP tools: list KGs, ask, search, grep.

Handlers ride the SDK (`listKgs` / `ask` / `search` / `grep`) — the ONE
search and grep endpoints every interface uses.
*/
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Client } from "@infona-ai/cli";
import { z } from "zod";
import { formatAskQueryDump } from "./askQueryDump.js";
import { client, errorResult, textResult } from "./mcpShared.js";

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

export function registerQueryTools(server: McpServer): void {
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
        const runId =
          (typeof data.run_id === "string" && data.run_id) ||
          (typeof data.job_id === "string" && data.job_id) ||
          "";
        const citations = Array.isArray(data.citations) ? data.citations : [];

        // Surface provenance fields the UI (and the model) need for expandable
        // citations / Cypher / query id. Previously only answer+explanation were
        // returned, so agents silently dropped the query body and run_id.
        const lines: string[] = [];
        if (narrative) lines.push(narrative.trim());
        lines.push(`Answer: ${answer}`);
        if (explanation) lines.push(`Explanation: ${explanation}`);
        if (runId) lines.push(`run_id: ${runId}`);
        const queryDump = formatAskQueryDump(data);
        if (queryDump) lines.push(queryDump);
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
}
