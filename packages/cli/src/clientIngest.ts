/** File / CSV / text ingest methods for the SDK client. */
import { existsSync, readFileSync, statSync } from "node:fs";
import { extname } from "node:path";
import { SCHEMA_SAMPLE_CAP, parseCsv, stridedSample } from "./clientCsv.js";
import { InfonaError, EXT_FORMAT } from "./clientError.js";
import { ClientHttp } from "./clientHttp.js";
import type { IngestOptions } from "./clientTypes.js";

export class ClientIngest extends ClientHttp {
  /**
   * Ingest a file path or raw text into a context graph.
   *
   * If `pathOrText` points to an existing file, its contents are read and the
   * format is inferred from the extension (.csv, .json, .txt) unless
   * `contentType` is given. CSV files use the two-step schema-inference + row
   * mapping flow.
   */
  async ingest(
    pathOrText: string,
    opts: IngestOptions = {},
  ): Promise<Record<string, unknown>> {
    let content: string;
    let fmt: string;

    let isFile = false;
    if (!opts.asText || opts.asFile) {
      try {
        isFile = existsSync(pathOrText) && statSync(pathOrText).isFile();
      } catch {
        isFile = false;
      }
    }

    // ONTA-253: a file-intent caller (asFile) must NEVER degrade to text. If the
    // path does not resolve to a readable file, hard-error here instead of
    // POSTing the path string itself as content — otherwise the backend
    // LLM-extracts phantom entities out of the filename and reports a fabricated
    // success. The dual-mode default (asFile unset) still degrades to text for
    // the CLI's intentional `ingest <raw text>` path. Text-intent callers pass
    // asText so an existing path string is still POSTed as content, not re-read.
    if (opts.asFile && !isFile) {
      throw new InfonaError(
        `File not found or not a readable file: ${pathOrText}. ` +
          `Pass raw text without asFile to ingest it as text.`,
      );
    }

    if (isFile) {
      const ext = extname(pathOrText).toLowerCase();
      if (ext === ".pdf") {
        throw new InfonaError(
          // Do NOT point at another surface: neither the Python CLI nor the raw
          // API accepts a PDF either. There is no PDF ingest path anywhere in
          // the product, so say so rather than sending the user hunting.
          "PDF ingest is not supported. Extract the text or tables first (CSV is " +
            "the deterministic, best-supported path), then ingest that.",
        );
      }
      content = readFileSync(pathOrText, "utf-8");
      fmt = opts.contentType ?? EXT_FORMAT[ext] ?? "text";
      if (fmt === "csv") {
        return this.ingestCsv(content, opts);
      }
    } else {
      content = pathOrText;
      fmt = opts.contentType ?? "text";
    }

    const body: Record<string, unknown> = {
      content,
      content_type: fmt,
      source: "client",
    };
    if (opts.kg) body.kg_name = opts.kg;
    return this.request("POST", `${this.base()}/ingest`, body, 120_000);
  }

  protected async ingestCsv(
    content: string,
    opts: IngestOptions,
  ): Promise<Record<string, unknown>> {
    const kgName = opts.kg;
    const batchSize = opts.batchSize ?? 200;
    const concurrency = opts.concurrency ?? 4;

    const rows = parseCsv(content);
    if (rows.length === 0) throw new InfonaError("CSV is empty");
    const headers = Object.keys(rows[0]!);

    let mappingToPost: Record<string, unknown>;
    if (opts.typeName) {
      // Deterministic --type mode: no inference round-trip, no review gate.
      // Columns map verbatim so downstream consumers that bind on exact
      // attribute names (enrichment sources, joins) are never broken by a
      // model renaming a column.
      const T = opts.typeName;
      // First column is type_id / entity key. Use the CSV header as the
      // attribute leaf — never force attribute_name "name": on Neo4j that
      // collides with reserved Entity.name (model B2) and 500s at ontology
      // commit. Entity display name still comes from the type_id value.
      mappingToPost = {
        entity_type: T,
        columns: headers.map((h, i) => ({
          column_name: h,
          role: i === 0 ? "type_id" : "attribute",
          attribute_name: h,
          entity: T,
        })),
        entities: [
          { name: T, type_name: T, id_column: headers[0], key_strategy: "column" },
        ],
        relationships: null,
        violations: [],
      };
    } else {
      // Send the whole file to the profiler, evenly strided across it (never the
      // head — head-of-file bias, e.g. a key column that goes sparse later, is
      // exactly what evidence-grounded inference fixes). Profile fidelity =
      // decision quality. Mirrors the Explorer's upload flow.
      const sampleRows = stridedSample(rows);

      const schemaBody = {
        headers,
        sample_rows: sampleRows,
        total_rows: rows.length,
      };
      const mapping = await this.request<Record<string, unknown>>(
        "POST",
        `${this.base()}/ingest/csv/schema`,
        schemaBody,
        300_000,
      );

      // Confirm/override gate (same contract as the Explorer's review step): the
      // caller inspects the inferred mapping and returns what to ingest, or null
      // to cancel before any rows are written. /ingest/csv/rows applies exactly
      // what we post back. When no hook is given, apply the inference as-is.
      mappingToPost = mapping;
      if (opts.onSchemaInferred) {
        const reviewed = await opts.onSchemaInferred(mapping, {
          totalRows: rows.length,
          rowsProfiled: sampleRows.length,
        });
        if (reviewed == null) {
          return { cancelled: true, message: "Ingest cancelled before any rows were written." };
        }
        mappingToPost = reviewed;
      }
    }

    // Slice rows into batches up front so we can fire them off in a
    // bounded worker pool. Sequential 50-row batches over 891 rows took
    // ~60s end-to-end (18 round-trips); 200-row batches × 4 in flight
    // brings that to ~5s on the same backend.
    const batches: Array<Record<string, string>[]> = [];
    for (let i = 0; i < rows.length; i += batchSize) {
      batches.push(rows.slice(i, i + batchSize));
    }

    let totalEntities = 0;
    let totalTriples = 0;
    let rowsProcessed = 0;
    let nextBatch = 0;

    const postBatch = async (batch: Record<string, string>[]) => {
      const body: Record<string, unknown> = {
        mapping: mappingToPost,
        rows: batch,
        source: "client",
      };
      if (kgName) body.kg_name = kgName;
      // ONTA-250: forward join-by-exact-key mode to the canonical route (thin
      // pass-through — the server matches + merges). snake_case per the route
      // contract (KeyJoin.key_attribute / mint_unmatched).
      if (opts.keyJoin) {
        body.key_join = {
          key_attribute: opts.keyJoin.keyAttribute,
          ...(opts.keyJoin.mintUnmatched !== undefined
            ? { mint_unmatched: opts.keyJoin.mintUnmatched }
            : {}),
        };
      }
      const result = await this.request<{
        entities_resolved?: number;
        triples_inserted?: number;
      }>("POST", `${this.base()}/ingest/csv/rows`, body, 300_000);
      return {
        entities: result.entities_resolved ?? 0,
        triples: result.triples_inserted ?? 0,
        size: batch.length,
      };
    };

    const worker = async (): Promise<void> => {
      while (true) {
        const idx = nextBatch++;
        if (idx >= batches.length) return;
        const r = await postBatch(batches[idx]!);
        totalEntities += r.entities;
        totalTriples += r.triples;
        rowsProcessed += r.size;
        opts.onProgress?.({
          rowsProcessed,
          totalRows: rows.length,
          entitiesResolved: totalEntities,
          triplesInserted: totalTriples,
        });
      }
    };

    const workers: Array<Promise<void>> = [];
    for (let i = 0; i < Math.min(concurrency, batches.length); i++) {
      workers.push(worker());
    }
    await Promise.all(workers);

    // All batches are in — kick off a background recompute of the Explorer
    // type-stats for this KG so type-detail views load instantly. The endpoint
    // returns immediately (the scan runs server-side in the background); this
    // is best-effort and never fails the ingest.
    if (kgName) {
      try {
        await this.request(
          "POST",
          `${this.base()}/explore/kgs/${encodeURIComponent(kgName)}/recompute-stats`,
          {},
          15_000,
        );
      } catch {
        // non-fatal — stats fall back to a live scan until the next recompute
      }
    }

    return {
      entities_resolved: totalEntities,
      triples_inserted: totalTriples,
      mapping: mappingToPost,
    };
  }
}
