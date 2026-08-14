/**
 * User-visible `infona ask -d` chrome.
 *
 * The product query path is Cypher on Neo4j. The HTTP/NLResult field is still
 * named `sparql` (compat — do not rename here); this module only formats the
 * labels a human reads so `-d` does not claim SPARQL / Neptune.
 */

/** Commander help for `ask -d`. */
export const ASK_DEBUG_HELP = "Show Cypher and latency breakdown";

export type AskDebugResult = {
  cypher?: unknown;
  sparql?: unknown;
  timing?: unknown;
};

/** Prefer `cypher` when present; fall back to the compat `sparql` field. */
export function askQueryText(result: AskDebugResult): string {
  if (typeof result.cypher === "string") return result.cypher;
  if (typeof result.sparql === "string") return result.sparql;
  return "";
}

function titleCaseKey(key: string): string {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Drop the `_ms` unit token (trailing or mid-key, e.g. `cypher_exec_ms_retry1`). */
function titleCaseTimingKey(key: string): string {
  return titleCaseKey(key.replace(/_ms(?=_|$)/g, "")).trim();
}

/**
 * Format the `-d` dump: query body + optional latency table.
 * Title-casing `cypher_exec_ms` yields the visible row "Cypher Exec".
 */
export function formatAskDebug(
  result: AskDebugResult,
  opts?: { roundtripMs?: number },
): string {
  const lines: string[] = ["", "Cypher:", askQueryText(result), ""];
  const timing = result.timing;
  if (timing && typeof timing === "object" && !Array.isArray(timing)) {
    const entries = Object.entries(timing as Record<string, unknown>);
    if (entries.length) {
      lines.push("─".repeat(40));
      lines.push(`${"Stage".padEnd(25)} ${"Time".padStart(10)}`);
      lines.push("─".repeat(40));
      for (const [key, val] of entries) {
        if (key === "attempts") {
          lines.push(`${"Attempts".padEnd(25)} ${String(val).padStart(10)}`);
        } else if (typeof val === "string") {
          lines.push(`${titleCaseKey(key).padEnd(25)} ${val.padStart(10)}`);
        } else {
          const label = titleCaseTimingKey(key);
          const num = typeof val === "number" ? val : Number(val);
          lines.push(`${label.padEnd(25)} ${num.toFixed(1).padStart(8)}ms`);
        }
      }
      lines.push("─".repeat(40));
      if (opts?.roundtripMs !== undefined) {
        lines.push(
          `${"Client roundtrip".padEnd(25)} ${opts.roundtripMs.toFixed(1).padStart(8)}ms`,
        );
      }
    }
  }
  return lines.join("\n");
}
