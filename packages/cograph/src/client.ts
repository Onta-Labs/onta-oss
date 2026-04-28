import { existsSync, readFileSync, statSync } from "node:fs";
import { extname } from "node:path";
import { readConfig } from "./config.js";

export class CographError extends Error {
  status?: number;
  body?: string;

  constructor(message: string, opts?: { status?: number; body?: string }) {
    super(message);
    this.name = "CographError";
    this.status = opts?.status;
    this.body = opts?.body;
  }
}

export interface ClientOptions {
  apiKey?: string;
  baseUrl?: string;
  tenant?: string;
}

export interface IngestOptions {
  kg?: string;
  contentType?: "text" | "csv" | "json" | string;
}

export interface AskOptions {
  kg?: string;
  model?: string;
}

function envVar(name: string, fallback?: string): string | undefined {
  // Prefer COGRAPH_, fall back to OMNIX_ so old configs keep working.
  return (
    process.env[`COGRAPH_${name}`] ||
    process.env[`OMNIX_${name}`] ||
    fallback
  );
}

const EXT_FORMAT: Record<string, string> = {
  ".csv": "csv",
  ".json": "json",
  ".jsonl": "json",
  ".txt": "text",
};

/**
 * Parse a CSV string into an array of row objects.
 *
 * Minimal RFC-4180-ish parser: handles quoted fields with commas, escaped
 * quotes (`""`), CRLF/LF line endings. Does not handle BOM stripping or
 * encoding detection — we assume UTF-8 text in.
 */
export function parseCsv(content: string): Record<string, string>[] {
  const rows: string[][] = [];
  let cur: string[] = [];
  let field = "";
  let inQuotes = false;

  for (let i = 0; i < content.length; i++) {
    const ch = content[i];
    if (inQuotes) {
      if (ch === '"') {
        if (content[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        field += ch;
      }
    } else {
      if (ch === '"') {
        inQuotes = true;
      } else if (ch === ",") {
        cur.push(field);
        field = "";
      } else if (ch === "\n") {
        cur.push(field);
        rows.push(cur);
        cur = [];
        field = "";
      } else if (ch === "\r") {
        // swallow; handled by the following \n in CRLF, or treat lone \r as line end
        if (content[i + 1] !== "\n") {
          cur.push(field);
          rows.push(cur);
          cur = [];
          field = "";
        }
      } else {
        field += ch;
      }
    }
  }
  // flush trailing field/row
  if (field.length > 0 || cur.length > 0) {
    cur.push(field);
    rows.push(cur);
  }

  if (rows.length === 0) return [];
  const headers = rows[0]!.map((h) => h.trim());
  const out: Record<string, string>[] = [];
  for (let r = 1; r < rows.length; r++) {
    const row = rows[r]!;
    // skip blank trailing lines
    if (row.length === 1 && row[0] === "") continue;
    const obj: Record<string, string> = {};
    for (let c = 0; c < headers.length; c++) {
      obj[headers[c]!] = row[c] ?? "";
    }
    out.push(obj);
  }
  return out;
}

export class Client {
  apiKey: string | undefined;
  baseUrl: string;
  tenant: string;

  constructor(opts: ClientOptions = {}) {
    // Resolution order for each field: explicit opts → env var → ~/.cograph/config.json
    // (written by `cograph login`) → built-in default. Reading the config eagerly
    // is cheap (small JSON file) and lets users skip env vars entirely after login.
    const cfg = readConfig();
    this.apiKey = opts.apiKey ?? envVar("API_KEY") ?? cfg.apiKey;
    const url =
      opts.baseUrl ?? envVar("API_URL") ?? cfg.apiUrl ?? "https://api.cograph.cloud";
    this.baseUrl = url.replace(/\/+$/, "");
    this.tenant = opts.tenant ?? envVar("TENANT") ?? cfg.tenant ?? "demo-tenant";
  }

  private headers(): Record<string, string> {
    const h: Record<string, string> = { "Content-Type": "application/json" };
    if (this.apiKey) h["X-API-Key"] = this.apiKey;
    return h;
  }

  private base(): string {
    return `${this.baseUrl}/graphs/${this.tenant}`;
  }

  private async request<T = unknown>(
    method: string,
    url: string,
    body?: unknown,
    timeoutMs: number = 120_000,
  ): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let res: Response;
    try {
      res = await fetch(url, {
        method,
        headers: this.headers(),
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (err) {
      clearTimeout(timer);
      if (err instanceof Error && err.name === "AbortError") {
        throw new CographError(`Request to ${url} timed out after ${timeoutMs}ms`);
      }
      throw new CographError(
        `Network error contacting ${url}: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
    clearTimeout(timer);

    if (!res.ok) {
      let text = "";
      try {
        text = await res.text();
      } catch {
        // ignore
      }
      throw new CographError(`HTTP ${res.status}: ${text}`, {
        status: res.status,
        body: text,
      });
    }

    // 204 No Content
    if (res.status === 204) return undefined as T;

    const ct = res.headers.get("content-type") ?? "";
    if (ct.includes("application/json")) {
      return (await res.json()) as T;
    }
    // fall back to text
    const text = await res.text();
    try {
      return JSON.parse(text) as T;
    } catch {
      return text as unknown as T;
    }
  }

  /**
   * Ingest a file path or raw text into a knowledge graph.
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
    try {
      isFile = existsSync(pathOrText) && statSync(pathOrText).isFile();
    } catch {
      isFile = false;
    }

    if (isFile) {
      const ext = extname(pathOrText).toLowerCase();
      if (ext === ".pdf") {
        throw new CographError(
          "PDF ingest not yet supported in the Node CLI; use the Python CLI or POST raw bytes to the API.",
        );
      }
      content = readFileSync(pathOrText, "utf-8");
      fmt = opts.contentType ?? EXT_FORMAT[ext] ?? "text";
      if (fmt === "csv") {
        return this.ingestCsv(content, opts.kg);
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

  private async ingestCsv(
    content: string,
    kgName: string | undefined,
  ): Promise<Record<string, unknown>> {
    const rows = parseCsv(content);
    if (rows.length === 0) throw new CographError("CSV is empty");
    const headers = Object.keys(rows[0]!);

    const schemaBody = {
      headers,
      sample_rows: rows.slice(0, 5),
      total_rows: rows.length,
    };
    const mapping = await this.request<Record<string, unknown>>(
      "POST",
      `${this.base()}/ingest/csv/schema`,
      schemaBody,
      300_000,
    );

    const batchSize = 50;
    let totalEntities = 0;
    let totalTriples = 0;
    for (let i = 0; i < rows.length; i += batchSize) {
      const batch = rows.slice(i, i + batchSize);
      const body: Record<string, unknown> = {
        mapping,
        rows: batch,
        source: "client",
      };
      if (kgName) body.kg_name = kgName;
      const result = await this.request<{
        entities_resolved?: number;
        triples_inserted?: number;
      }>("POST", `${this.base()}/ingest/csv/rows`, body, 300_000);
      totalEntities += result.entities_resolved ?? 0;
      totalTriples += result.triples_inserted ?? 0;
    }

    return {
      entities_resolved: totalEntities,
      triples_inserted: totalTriples,
      mapping,
    };
  }

  /** Ask a natural language question and return the parsed response. */
  async ask(
    question: string,
    opts: AskOptions = {},
  ): Promise<Record<string, unknown>> {
    const body: Record<string, unknown> = { question };
    if (opts.kg) body.kg_name = opts.kg;
    if (opts.model) body.model = opts.model;
    return this.request("POST", `${this.base()}/ask`, body, 60_000);
  }

  /** List all knowledge graphs for the current tenant. */
  async listKgs(): Promise<Array<Record<string, unknown>>> {
    const data = await this.request<unknown>(
      "GET",
      `${this.base()}/kgs`,
      undefined,
      15_000,
    );
    if (Array.isArray(data)) return data as Array<Record<string, unknown>>;
    if (data && typeof data === "object" && "kgs" in data) {
      const kgs = (data as { kgs?: unknown }).kgs;
      if (Array.isArray(kgs)) return kgs as Array<Record<string, unknown>>;
    }
    return [];
  }

  /** Create a knowledge graph. */
  async createKg(
    name: string,
    description?: string,
  ): Promise<Record<string, unknown>> {
    const body: Record<string, unknown> = { name };
    if (description) body.description = description;
    return this.request("POST", `${this.base()}/kgs`, body, 15_000);
  }

  /** Delete a knowledge graph by name. */
  async deleteKg(name: string): Promise<Record<string, unknown>> {
    return this.request(
      "DELETE",
      `${this.base()}/kgs/${encodeURIComponent(name)}`,
      undefined,
      30_000,
    );
  }

  /** List ontology types. */
  async ontologyTypes(): Promise<Array<Record<string, unknown>>> {
    const data = await this.request<unknown>(
      "GET",
      `${this.base()}/ontology/types`,
      undefined,
      15_000,
    );
    return Array.isArray(data) ? (data as Array<Record<string, unknown>>) : [];
  }
}
