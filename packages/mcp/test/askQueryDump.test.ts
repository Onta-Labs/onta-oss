import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { formatAskQueryDump } from "../src/askQueryDump.js";

const here = dirname(fileURLToPath(import.meta.url));

describe("MCP ask query dump chrome", () => {
  it("labels the body Cypher:, not SPARQL:", () => {
    const out = formatAskQueryDump({
      sparql: "MATCH (e:Entity) RETURN count(*) AS n",
    });
    expect(out).toContain("Cypher:");
    expect(out).toContain("MATCH (e:Entity) RETURN count(*) AS n");
    expect(out).not.toMatch(/SPARQL/);
    expect(out).not.toMatch(/Neptune/i);
  });

  it("prefers cypher over the compat sparql field", () => {
    const out = formatAskQueryDump({
      cypher: "MATCH (n) RETURN n.name",
      sparql: "SELECT * WHERE { ?s ?p ?o }",
    });
    expect(out).toContain("MATCH (n) RETURN n.name");
    expect(out).not.toContain("SELECT * WHERE");
  });

  it("omits the dump when neither field is a non-empty string", () => {
    expect(formatAskQueryDump({})).toBe("");
    expect(formatAskQueryDump({ sparql: "" })).toBe("");
    expect(formatAskQueryDump({ cypher: 1, sparql: null })).toBe("");
    expect(formatAskQueryDump(undefined)).toBe("");
  });

  it("ask/agent tools use the helper and have no SPARQL: dump header", () => {
    const query = readFileSync(join(here, "../src/mcpQuery.ts"), "utf8");
    const agent = readFileSync(join(here, "../src/mcpAgent.ts"), "utf8");
    expect(query).toContain("formatAskQueryDump");
    expect(agent).toContain("formatAskQueryDump");
    expect(query).not.toMatch(/\\nSPARQL:/);
    expect(agent).not.toMatch(/\\nSPARQL:/);
  });
});
