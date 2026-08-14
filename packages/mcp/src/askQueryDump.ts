/**
 * User-visible dump of the executed query body (ask + agent answer).
 *
 * The HTTP/NLResult field is still named `sparql` (compat); the product query
 * path is Cypher on Neo4j. Prefer `cypher` when present, then fall back.
 */

export function formatAskQueryDump(payload: {
  cypher?: unknown;
  sparql?: unknown;
}): string {
  const text =
    typeof payload.cypher === "string" && payload.cypher
      ? payload.cypher
      : typeof payload.sparql === "string"
        ? payload.sparql
        : "";
  if (!text) return "";
  return `\nCypher:\n${text}`;
}
