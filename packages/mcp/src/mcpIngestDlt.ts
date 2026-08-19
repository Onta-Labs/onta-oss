/** DLT ingest MCP tool (ONTA-553).

Thin over the SDK ``ingestDlt`` pass-through — the same method the CLI
and Explorer use (COG-128). No new endpoint.
*/
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Client, InfonaError } from "@infona-ai/cli";
import type { DltIngestRequest, DltResourceMap, DltSourceSpec } from "@infona-ai/cli";
import { z } from "zod";
import { client, errorResult, textResult } from "./mcpShared.js";

const DLT_EXTRA_HINT =
  "dlt is not installed on the Infona backend. Install the optional extra: " +
  "pip install infona-client[dlt]";


function isDltExtraMissing(err: unknown): boolean {
  if (!(err instanceof InfonaError)) return false;
  const blob = `${err.message}\n${err.body ?? ""}`;
  return (
    /infona-client\[dlt\]/.test(blob) ||
    /dlt is not installed/i.test(blob) ||
    (err.status === 503 && /dlt/i.test(blob))
  );
}


/** ONTA-553 / COG-128: thin over SDK ``ingestDlt``. */
export async function ingestDltHandler(
  {
    source,
    map,
    kg,
    kg_name,
  }: {
    source: DltSourceSpec;
    map: Record<string, DltResourceMap>;
    kg?: string;
    kg_name?: string;
  },
  makeClient: () => Client = client,
) {
  const kgName = (kg ?? kg_name ?? "").trim();
  if (!kgName) {
    return errorResult(new Error("ingest_dlt requires `kg` — nothing was ingested."));
  }
  if (!source?.kind || !Array.isArray(source.resources) || !source.resources.length) {
    return errorResult(
      new Error(
        "ingest_dlt requires source.kind and source.resources — nothing was ingested.",
      ),
    );
  }
  if (!map || !Object.keys(map).length) {
    return errorResult(
      new Error(
        "ingest_dlt requires `map` (resource → ontology type) — nothing was ingested.",
      ),
    );
  }
  const body: DltIngestRequest = { source, map, kg: kgName };
  try {
    const result = await makeClient().ingestDlt(body);
    const rows = Number(result.rows_in ?? 0);
    const entities = Number(result.entities_resolved ?? 0);
    const triples = Number(result.triples_inserted ?? 0);
    return textResult(
      `DLT ingest complete: ${rows} rows in, ${entities} entities resolved, ` +
        `${triples} triples inserted into "${kgName}".`,
    );
  } catch (err) {
    if (isDltExtraMissing(err)) {
      return errorResult(new Error(DLT_EXTRA_HINT));
    }
    return errorResult(err);
  }
}


export function registerIngestDltTool(server: McpServer): void {
  server.registerTool(
    "ingest_dlt",
    {
      description:
        "Extract a 3rd-party REST or SQL source via dlt and ingest the rows " +
        "into a context graph. Posts the frozen {source, map, kg} body through " +
        "the same SDK method the CLI and Explorer use (COG-128). The backend " +
        "must have `pip install infona-client[dlt]`. Auth is BYOK.",
      inputSchema: {
        source: z
          .object({
            kind: z.enum(["rest_api", "sql"]),
            base_url: z.string().optional(),
            dsn: z.string().optional(),
            auth: z
              .object({
                type: z.enum(["bearer", "basic", "api_key", "none"]).optional(),
                secret_ref: z.string().optional(),
                token: z.string().optional(),
                username: z.string().optional(),
                api_key_header: z.string().optional(),
              })
              .optional(),
            resources: z.array(z.string()).min(1),
            headers: z.record(z.string()).optional(),
            limit: z.number().int().min(1).max(100_000).optional(),
          })
          .describe("How to extract. Does not write."),
        map: z
          .record(
            z.object({
              type: z.string().min(1),
              id_field: z.string().optional(),
              attributes: z.array(z.string()).optional(),
            }),
          )
          .describe("resource name → ontology type."),
        kg: z.string().optional(),
        kg_name: z.string().optional(),
      },
    },
    async ({ source, map, kg, kg_name }) =>
      ingestDltHandler({ source, map, kg, kg_name }),
  );
}
