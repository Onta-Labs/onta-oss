/** First-hour CLI failures: one-liners with the next command, not tracebacks. */

import { InfonaError } from "./clientError.js";
import { isLocalhostUrl } from "./config.js";

export const MSG_API_DOWN =
  "API is not running. From the repo: ./scripts/oss_up.sh";

export const MSG_NEO4J_DOWN =
  "Neo4j is not reachable. docker compose up -d neo4j";

export const MSG_LLM_KEY = "set OPENROUTER_API_KEY in .env";

export const MSG_NOT_LOGGED_IN =
  "Not logged in. For local OSS: infona init --local   For cloud: infona login";

export function emptyKgMessage(kg: string): string {
  return `No data in kg '${kg}'. ingest a CSV: infona ingest examples/trials.csv --kg trials`;
}

export type HealthSnapshot = {
  ok: boolean;
  status?: string;
  neo4j?: boolean;
};

export type FirstHourInput = {
  message?: string;
  status?: number;
  body?: string;
  baseUrl?: string;
  hasApiKey?: boolean;
  kg?: string;
  health?: HealthSnapshot | null;
  /** Successful /ask (or agent) answer text. */
  answer?: string;
};

const HOSTED_HOSTS = new Set(["api.infona.ai", "api.infona.cloud"]);

export function isHostedCloudUrl(url: string | undefined | null): boolean {
  if (!url) return false;
  try {
    const raw = url.includes("://") ? url : `https://${url}`;
    return HOSTED_HOSTS.has(new URL(raw).hostname.toLowerCase());
  } catch {
    return /api\.infona\.(ai|cloud)/i.test(url);
  }
}

function blob(input: FirstHourInput): string {
  return [input.message, input.body, input.answer].filter(Boolean).join("\n");
}

function alreadyMapped(text: string): boolean {
  return (
    text.includes("docker compose up -d") ||
    text.includes("./scripts/oss_up.sh") ||
    text.includes("infona init --local") ||
    text.includes("OPENROUTER_API_KEY") ||
    text.includes("infona ingest examples/trials.csv")
  );
}

function extractKg(text: string, fallback?: string): string | undefined {
  if (fallback) return fallback;
  const named = /"kg_name"\s*:\s*"([^"]+)"/.exec(text);
  if (named?.[1]) return named[1];
  const prose =
    /Knowledge graph '([^']+)'/i.exec(text) ||
    /knowledge graph ["']([^"']+)["']/i.exec(text) ||
    /No data in kg '([^']+)'/i.exec(text);
  return prose?.[1];
}

function extractDetail(body?: string): string | undefined {
  if (!body) return undefined;
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    const d = parsed.detail;
    if (typeof d === "string") return d;
    if (d && typeof d === "object") {
      const rec = d as { message?: unknown; error?: unknown };
      if (typeof rec.message === "string") return rec.message;
      if (typeof rec.error === "string") return rec.error;
    }
  } catch {
    // not JSON
  }
  return undefined;
}

function isLocalTarget(input: FirstHourInput): boolean {
  const text = blob(input);
  return (
    isLocalhostUrl(input.baseUrl) ||
    /https?:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/i.test(text)
  );
}

function isApiUnreachable(input: FirstHourInput): boolean {
  if (!isLocalTarget(input)) return false;
  const text = blob(input);
  return /ECONNREFUSED|ENOTFOUND|EHOSTUNREACH|fetch failed|Failed to fetch|Network error|connect ECONNREFUSED/i.test(
    text,
  );
}

function isEmptyOrUnknownKg(input: FirstHourInput): boolean {
  if (input.status === 404) {
    const text = blob(input);
    return /kg_not_found|does not exist in this workspace|knowledge graph/i.test(
      text,
    );
  }
  const text = blob(input);
  return /kg_not_found|exists but contains no data|does not exist in this workspace|contains zero triples|nothing to query\. Ingest/i.test(
    text,
  );
}

function isMissingLlmKey(input: FirstHourInput): boolean {
  const text = blob(input);
  // "no generator produced Cypher" is also the always-LLM miss when a key IS set.
  return /OPENROUTER_API_KEY|no LLM( API)? key|LLM API key|api key is (not set|missing|empty|not configured)|No LLM API key/i.test(
    text,
  );
}

function isNeo4jDown(input: FirstHourInput): boolean {
  const h = input.health;
  if (h && (h.neo4j === false || h.status === "degraded")) return true;
  return /Neo4j is not reachable|Neo4j GraphStore is not configured|Unable to (?:connect|reach)(?: to)? Neo4j|ServiceUnavailable.*[Nn]eo4j|[Nn]eo4j.*(down|unreachable|refused)/.test(
    blob(input),
  );
}

function llmMessage(input: FirstHourInput): string {
  const detail = extractDetail(input.body);
  const api =
    (input.answer && input.answer.trim()) ||
    detail ||
    (input.message && !/^HTTP \d+/.test(input.message) ? input.message : "");
  const cleaned = (api || "No LLM API key configured.").replace(/\s+$/, "");
  if (cleaned.includes(MSG_LLM_KEY)) return cleaned;
  return `${cleaned}\n${MSG_LLM_KEY}`;
}

/**
 * Map a request failure or /ask answer to a first-hour one-liner.
 * Returns null when this is not a known first-hour case.
 */
export function mapFirstHourError(input: FirstHourInput): string | null {
  const text = blob(input);
  if (alreadyMapped(text)) {
    if (text.includes(MSG_API_DOWN)) return MSG_API_DOWN;
    if (text.includes(MSG_NEO4J_DOWN)) return MSG_NEO4J_DOWN;
    if (text.includes(MSG_NOT_LOGGED_IN)) return MSG_NOT_LOGGED_IN;
    if (/No data in kg '/.test(text) && text.includes("infona ingest")) {
      return emptyKgMessage(extractKg(text, input.kg) ?? "unknown");
    }
    if (text.includes(MSG_LLM_KEY)) return llmMessage(input);
  }

  // Hosted default + no key: login / init, even on a raw 401 or network miss.
  if (isHostedCloudUrl(input.baseUrl) && !input.hasApiKey) {
    return MSG_NOT_LOGGED_IN;
  }

  if (isApiUnreachable(input)) return MSG_API_DOWN;

  // /health itself failed and the original call never got an HTTP status.
  if (
    input.health &&
    input.health.ok === false &&
    input.status === undefined &&
    isLocalTarget(input)
  ) {
    return MSG_API_DOWN;
  }

  if (isNeo4jDown(input)) return MSG_NEO4J_DOWN;

  if (isEmptyOrUnknownKg(input)) {
    return emptyKgMessage(extractKg(text, input.kg) ?? "unknown");
  }

  if (isMissingLlmKey(input)) return llmMessage(input);

  return null;
}

/** Rewrite a successful /ask (or agent) answer when it is a first-hour miss. */
export function diagnoseAskResult(answer: string, kg?: string): string {
  return mapFirstHourError({ answer, kg, message: answer }) ?? answer;
}

function errParts(err: unknown): {
  message: string;
  status?: number;
  body?: string;
} {
  if (err instanceof InfonaError) {
    return { message: err.message, status: err.status, body: err.body };
  }
  if (err instanceof Error) return { message: err.message };
  return { message: String(err) };
}

function looksFailed(message: string, status?: number): boolean {
  if (status !== undefined && status >= 500) return true;
  return /ECONNREFUSED|ENOTFOUND|fetch failed|Network error|timed out|Neo4j|GraphStore/i.test(
    message,
  );
}

export async function fetchHealthSnapshot(
  baseUrl: string,
): Promise<HealthSnapshot> {
  try {
    const res = await fetch(`${baseUrl.replace(/\/+$/, "")}/health`, {
      signal: AbortSignal.timeout(3000),
    });
    const data = (await res.json()) as { status?: unknown; neo4j?: unknown };
    const neo4j = typeof data.neo4j === "boolean" ? data.neo4j : undefined;
    const status = typeof data.status === "string" ? data.status : undefined;
    // 503 JSON is a live process with Neo4j down. ok:true so callers
    // surface MSG_NEO4J_DOWN instead of ECONNREFUSED.
    if (!res.ok && neo4j === undefined) return { ok: false };
    return { ok: true, status, neo4j };
  } catch {
    return { ok: false };
  }
}

function shortMessage(msg: string): string {
  const line = (msg.split("\n")[0] ?? msg).trim();
  return line.length > 400 ? `${line.slice(0, 400)}…` : line;
}

/** Async wrapper: probes GET /health on local request failures. */
export async function formatFirstHourFailure(
  err: unknown,
  ctx: Omit<FirstHourInput, "message" | "status" | "body" | "answer"> = {},
): Promise<string> {
  const parts = errParts(err);
  const quick = mapFirstHourError({ ...ctx, ...parts });
  if (quick) return quick;

  if (ctx.baseUrl && isLocalhostUrl(ctx.baseUrl) && looksFailed(parts.message, parts.status)) {
    const health = await fetchHealthSnapshot(ctx.baseUrl);
    const probed = mapFirstHourError({ ...ctx, ...parts, health });
    if (probed) return probed;
  }

  return `Error: ${shortMessage(parts.message)}`;
}
