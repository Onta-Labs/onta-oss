/** Second-pass entity-resolution MCP tool.

``er_rebuild`` rides the SDK ``erRebuild`` path (canonical
``POST /graphs/{tenant}/explore/kgs/{kg}/er-rebuild``). Same method the
CLI's ``infona er rebuild`` uses, including the 300s timeout. No new
endpoint.
*/
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Client } from "@infona-ai/cli";
import { z } from "zod";
import { client, errorResult, textResult } from "./mcpShared.js";

function asList<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function flatten<T>(
  report: Record<string, unknown>,
  key: "merges" | "conflicts" | "unresolved",
): T[] {
  const top = asList<T>(report[key]);
  if (top.length) return top;
  const out: T[] = [];
  for (const t of asList<Record<string, unknown>>(report.types)) {
    out.push(...asList<T>(t[key]));
  }
  return out;
}

/** Compact before/after + leftover-conflict dump for the agent. */
export function renderErRebuild(
  report: Record<string, unknown>,
  kg: string,
): string {
  const lines: string[] = [`Rebuilding entity resolution for ${kg}…`];
  const types = asList<Record<string, unknown>>(report.types);
  for (const t of types) {
    lines.push(
      `  ${String(t.type ?? "?")}  ${t.entities_before} → ${t.entities_after}` +
        `  (−${t.fragments_absorbed} fragments across ${t.clusters_merged} clusters)`,
    );
  }

  for (const merge of flatten<Record<string, unknown>>(report, "merges")) {
    const losers = asList<string>(merge.losers);
    lines.push("", `  merge  ${merge.winner ?? "?"}`);
    if (losers.length) lines.push(`         losers:     ${losers.join(", ")}`);
    if (merge.reason != null && merge.reason !== "") {
      lines.push(`         reason:     ${merge.reason}`);
    }
  }

  for (const conflict of flatten<Record<string, unknown>>(report, "conflicts")) {
    lines.push("", `  conflict  ${conflict.field ?? "?"}`);
    lines.push(`         entity:     ${conflict.entity ?? ""}`);
    if (conflict.reason != null && conflict.reason !== "") {
      lines.push(`         reason:     ${conflict.reason}`);
    }
  }

  for (const item of flatten<Record<string, unknown>>(report, "unresolved")) {
    lines.push("", `  unresolved  ${item.field ?? "?"}`);
    lines.push(`         entity:     ${item.entity ?? ""}`);
    const flagged =
      item.flagged || "equal-trust sources — not silently guessed";
    lines.push(`         flagged: ${flagged}`);
  }

  let total = report.fragments_absorbed_total;
  if (total === undefined || total === null) {
    total = types.reduce((sum, t) => sum + Number(t.fragments_absorbed ?? 0), 0);
  }
  lines.push("", `Done. ${total} fragments absorbed.`);
  return lines.join("\n");
}

export async function erRebuildHandler(
  { kg_name }: { kg_name: string },
  makeClient: () => Client = client,
) {
  if (!kg_name?.trim()) {
    return errorResult(
      new Error("er_rebuild requires `kg_name` — nothing was rebuilt."),
    );
  }
  try {
    const report = await makeClient().erRebuild(kg_name);
    return textResult(renderErRebuild(report, kg_name));
  } catch (err) {
    return errorResult(err);
  }
}

export function registerErRebuildTools(server: McpServer): void {
  server.registerTool(
    "er_rebuild",
    {
      description:
        "Second-pass entity resolution: re-run ER over an already-ingested " +
        "context graph to collapse intra-batch duplicate fragments. Same path " +
        "as the CLI's `infona er rebuild` (`Client.erRebuild` → " +
        "`POST …/explore/kgs/{kg}/er-rebuild`). Synchronous and can take " +
        "minutes on a large graph (300s timeout). Use after messy ingest, " +
        "or when `ask` still sees duplicate entities of one type.",
      inputSchema: {
        kg_name: z
          .string()
          .describe(
            "Name of the context graph to rebuild. Use list_knowledge_graphs to see available KGs.",
          ),
      },
    },
    // Wrapped, not passed directly: the MCP SDK calls the callback with a
    // second `extra` argument, which would otherwise land in `makeClient`.
    (args) => erRebuildHandler(args),
  );
}
