/**
 * User-visible dump of the executed query body (ask + agent answer).
 *
 * The HTTP/NLResult field is still named `sparql` (compat); the product query
 * path is Cypher on Neo4j. Prefer `cypher` when present, then fall back.
 */

export function formatAskQueryDump(payload: unknown): string {
  const rec =
    payload && typeof payload === "object"
      ? (payload as Record<string, unknown>)
      : {};
  const text =
    typeof rec.cypher === "string" && rec.cypher
      ? rec.cypher
      : typeof rec.sparql === "string"
        ? rec.sparql
        : "";
  if (!text) return "";
  return `\nCypher:\n${text}`;
}
