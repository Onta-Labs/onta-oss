/** MCP ingest_dlt handler — thin over SDK ``ingestDlt`` (ONTA-553 / COG-128).

The SDK owns the HTTP path. This module must not hard-code that route.
*/
import { Client, InfonaError } from "@infona-ai/cli";
import type { DltIngestRequest, DltResourceMap, DltSourceSpec } from "@infona-ai/cli";
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
