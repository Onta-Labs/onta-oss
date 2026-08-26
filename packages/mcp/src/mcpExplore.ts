/** Explore / workspace MCP tools.

Rides typed Client methods (`exploreRecords`, `getEntity`, `exploreSummary`,
`listTenants`, `createTenant`, `recomputeStats`). No handmade URLs.
Active tenant is `INFONA_TENANT` (process env); Client has no session setter.
*/
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Client } from "@infona-ai/cli";
import type { EntityDetail, TypeRecordsPage, TypeSummary } from "@infona-ai/cli";
import { z } from "zod";
import { client, errorResult, textResult } from "./mcpShared.js";

const CELL_CAP = 48;

function columnLabel(col: unknown): string {
  if (typeof col === "string") return col;
  if (col && typeof col === "object") {
    const o = col as { name?: unknown; kind?: unknown };
    const name = typeof o.name === "string" ? o.name : String(o.name ?? "?");
    const kind = typeof o.kind === "string" && o.kind ? o.kind : "";
    return kind ? `${name} (${kind})` : name;
  }
  return String(col);
}

function columnKey(col: unknown): string {
  if (typeof col === "string") return col;
  if (col && typeof col === "object" && typeof (col as { name?: unknown }).name === "string") {
    return (col as { name: string }).name;
  }
  return String(col);
}

function cell(value: unknown): string {
  if (value == null) return "";
  const s = typeof value === "string" ? value : JSON.stringify(value);
  return s.length > CELL_CAP ? `${s.slice(0, CELL_CAP - 1)}…` : s;
}

function renderRecords(kg: string, typeName: string, page: TypeRecordsPage): string {
  const cols = Array.isArray(page.columns) ? page.columns : [];
  const rows = Array.isArray(page.rows) ? page.rows : [];
  const lines: string[] = [
    `${typeName} in "${kg}": ${page.total ?? rows.length} entit${(page.total ?? rows.length) === 1 ? "y" : "ies"}`,
  ];
  if (cols.length) {
    lines.push(`Columns: ${cols.map(columnLabel).join(", ")}`);
  }
  if (!rows.length) {
    lines.push("No rows on this page.");
  } else {
    const keys = cols.length ? cols.map(columnKey) : Object.keys(rows[0] ?? {});
    const header = ["name", ...keys.filter((k) => k !== "name" && k !== "id")];
    lines.push(header.join(" | "));
    for (const row of rows) {
      const rec = row as Record<string, unknown>;
      lines.push(header.map((k) => cell(rec[k])).join(" | "));
    }
  }
  if (page.next_cursor) {
    lines.push(`next_cursor: ${page.next_cursor}`);
  }
  return lines.join("\n");
}

function slotLine(
  s: { name: string; coverage_pct?: number; datatype?: string; target_type?: string | null; count?: number },
): string {
  const kind = s.target_type ? ` -> ${s.target_type}` : s.datatype ? ` (${s.datatype})` : "";
  const cov =
    typeof s.coverage_pct === "number" ? ` ${s.coverage_pct}%` : "";
  const n = typeof s.count === "number" ? ` n=${s.count}` : "";
  return `${s.name}${kind}${cov}${n}`;
}

function extraSamples(summary: TypeSummary): string[] {
  const raw = (summary as unknown as { samples?: unknown }).samples;
  if (!Array.isArray(raw) || !raw.length) return [];
  const out: string[] = ["  samples:"];
  for (const s of raw) {
    if (s && typeof s === "object") {
      const o = s as { uri?: unknown; label?: unknown };
      out.push(`    ${String(o.label ?? o.uri ?? JSON.stringify(s))}`);
    } else {
      out.push(`    ${String(s)}`);
    }
  }
  return out;
}

function renderSummary(kg: string, summary: TypeSummary): string {
  const lines: string[] = [
    `${summary.name} in "${kg}": ${summary.entity_count} entities`,
  ];
  if (summary.description) lines.push(summary.description);
  if (summary.parent_type) lines.push(`parent: ${summary.parent_type}`);
  if (summary.attributes?.length) {
    lines.push(`  attributes: ${summary.attributes.map(slotLine).join(", ")}`);
  }
  if (summary.relationships?.length) {
    lines.push(`  relationships: ${summary.relationships.map(slotLine).join(", ")}`);
  }
  lines.push(...extraSamples(summary));
  if (summary.spatially_indexed) lines.push("  spatially indexed");
  if (summary.temporally_indexed) lines.push("  temporally indexed");
  return lines.join("\n");
}

function renderEntity(entity: EntityDetail): string {
  const lines: string[] = [
    entity.name ? `${entity.name} (${entity.id})` : String(entity.id),
  ];
  if (entity.primary_type) lines.push(`type: ${entity.primary_type}`);
  if (entity.source) lines.push(`source: ${entity.source}`);
  const props = entity.properties ?? {};
  const keys = Object.keys(props);
  if (keys.length) {
    lines.push("properties:");
    for (const k of keys) lines.push(`  ${k}: ${cell(props[k])}`);
  }
  const out = entity.outgoing ?? [];
  if (out.length) {
    lines.push("outgoing:");
    for (const r of out) {
      lines.push(
        `  ${r.attr || r.rel_type} -> ${r.other_name || r.other_id}` +
          (r.other_type ? ` (${r.other_type})` : ""),
      );
    }
  }
  const inn = entity.incoming ?? [];
  if (inn.length) {
    lines.push("incoming:");
    for (const r of inn) {
      lines.push(
        `  ${r.other_name || r.other_id} -> ${r.attr || r.rel_type}` +
          (r.other_type ? ` (${r.other_type})` : ""),
      );
    }
  }
  return lines.join("\n");
}

export async function listRecordsHandler(
  {
    kg_name,
    type_name,
    limit,
    cursor,
  }: { kg_name: string; type_name: string; limit?: number; cursor?: string },
  makeClient: () => Client = client,
) {
  try {
    const page = await makeClient().exploreRecords(kg_name, type_name, {
      limit,
      cursor,
    });
    return textResult(renderRecords(kg_name, type_name, page));
  } catch (err) {
    return errorResult(err);
  }
}

export async function getEntityHandler(
  { kg_name, entity_id }: { kg_name: string; entity_id: string },
  makeClient: () => Client = client,
) {
  try {
    const entity = await makeClient().getEntity(kg_name, entity_id);
    return textResult(renderEntity(entity));
  } catch (err) {
    return errorResult(err);
  }
}

export async function typeSummaryHandler(
  { kg_name, type_name }: { kg_name: string; type_name: string },
  makeClient: () => Client = client,
) {
  try {
    const summary = await makeClient().exploreSummary(kg_name, type_name);
    return textResult(renderSummary(kg_name, summary));
  } catch (err) {
    return errorResult(err);
  }
}

export async function listTenantsHandler(
  _args: Record<string, never> = {},
  makeClient: () => Client = client,
) {
  try {
    const tenants = await makeClient().listTenants();
    if (!tenants.length) return textResult("No workspaces on this key.");
    const lines = tenants.map((t) => {
      const label = t.label && t.label !== t.id ? ` (${t.label})` : "";
      return `- ${t.id}${label}`;
    });
    lines.push(
      "",
      "Active tenant is INFONA_TENANT for this MCP process; it cannot be switched mid-session.",
    );
    return textResult(lines.join("\n"));
  } catch (err) {
    return errorResult(err);
  }
}

export async function createTenantHandler(
  { label }: { label?: string },
  makeClient: () => Client = client,
) {
  try {
    const body = label ? { label } : {};
    const t = await makeClient().createTenant(body);
    return textResult(`Created workspace ${t.id}${t.label ? ` (${t.label})` : ""}.`);
  } catch (err) {
    return errorResult(err);
  }
}

export async function recomputeStatsHandler(
  { kg_name }: { kg_name: string },
  makeClient: () => Client = client,
) {
  try {
    const got = await makeClient().recomputeStats(kg_name);
    return textResult(
      `Stats recompute ${got.status ?? "scheduled"} for "${got.kg ?? kg_name}".`,
    );
  } catch (err) {
    return errorResult(err);
  }
}

export function registerExploreTools(server: McpServer): void {
  server.registerTool(
    "list_records",
    {
      description:
        "List entity records of one type in one context graph (paged). " +
        "Renders columns and rows compactly; relationship vs literal is noted " +
        "only when the payload includes a column kind.",
      inputSchema: {
        kg_name: z.string().describe("Context graph name."),
        type_name: z.string().describe("Entity type (e.g. SynthWidget)."),
        limit: z.number().int().min(1).max(200).optional(),
        cursor: z.string().optional().describe("Keyset cursor from the previous page."),
      },
    },
    (args) => listRecordsHandler(args),
  );
  server.registerTool(
    "get_entity",
    {
      description:
        "Fetch one entity's properties and incident relationships by id.",
      inputSchema: {
        kg_name: z.string().describe("Context graph name."),
        entity_id: z
          .string()
          .describe("Entity id or URI (the path after /entities/… is accepted)."),
      },
    },
    (args) => getEntityHandler(args),
  );
  server.registerTool(
    "type_summary",
    {
      description:
        "Type inventory for one type in one graph: entity count, attribute/" +
        "relationship coverage, and any samples the API already returns. " +
        "Does not invent coverage or sample values.",
      inputSchema: {
        kg_name: z.string().describe("Context graph name."),
        type_name: z.string().describe("Entity type."),
      },
    },
    (args) => typeSummaryHandler(args),
  );
  server.registerTool(
    "list_tenants",
    {
      description:
        "List workspaces this API key can access. The MCP process tenant is " +
        "INFONA_TENANT (set in the server env); there is no mid-session switch.",
      inputSchema: {},
    },
    () => listTenantsHandler(),
  );
  server.registerTool(
    "create_tenant",
    {
      description:
        "Create a new workspace for the authenticated user (writes the caller's " +
        "tenant list). Empty body mints 'Untitled workspace N'. The new id is " +
        "NOT adopted as INFONA_TENANT — restart the server with that env to use it.",
      inputSchema: {
        label: z.string().optional().describe("Human-readable workspace label."),
      },
    },
    (args) => createTenantHandler(args),
  );
  server.registerTool(
    "recompute_stats",
    {
      description:
        "Schedule a recompute of precomputed type-stats for a context graph " +
        "(writes the stats graph, not instance data). Returns immediately.",
      inputSchema: {
        kg_name: z.string().describe("Context graph whose stats to refresh."),
      },
    },
    (args) => recomputeStatsHandler(args),
  );
}
