import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  ASK_DEBUG_HELP,
  formatAskDebug,
} from "../src/askDebug.js";

const here = dirname(fileURLToPath(import.meta.url));

describe("ask -d user-visible chrome", () => {
  it("help text names Cypher, not SPARQL", () => {
    expect(ASK_DEBUG_HELP).toBe("Show Cypher and latency breakdown");
    expect(ASK_DEBUG_HELP).not.toMatch(/SPARQL/i);
    expect(ASK_DEBUG_HELP).not.toMatch(/Neptune/i);
  });

  it("cliQuery.ts wires ASK_DEBUG_HELP onto ask -d", () => {
    const src = readFileSync(join(here, "../src/cliQuery.ts"), "utf8");
    expect(src).toContain("ASK_DEBUG_HELP");
    expect(src).toContain("formatAskDebug");
    expect(src).not.toMatch(/Show SPARQL and latency breakdown/);
    expect(src).not.toMatch(/\\nSPARQL:/);
  });

  it("debug dump header is Cypher:, not SPARQL:", () => {
    const out = formatAskDebug({
      sparql: "MATCH (e:Entity) RETURN count(*) AS n",
    });
    expect(out).toContain("Cypher:");
    expect(out).toContain("MATCH (e:Entity) RETURN count(*) AS n");
    expect(out).not.toMatch(/SPARQL/);
    expect(out).not.toMatch(/Neptune/i);
  });

  it("prefers result.cypher over the compat sparql field", () => {
    const out = formatAskDebug({
      cypher: "MATCH (n) RETURN n.name",
      sparql: "SELECT * WHERE { ?s ?p ?o }",
    });
    expect(out).toContain("MATCH (n) RETURN n.name");
    expect(out).not.toContain("SELECT * WHERE");
  });

  it("falls back to result.sparql when cypher is absent", () => {
    const out = formatAskDebug({
      sparql: "MATCH (e:Entity) RETURN e",
    });
    expect(out).toContain("MATCH (e:Entity) RETURN e");
  });

  it("title-cases cypher_exec_ms as Cypher Exec, never Neptune Exec", () => {
    const out = formatAskDebug(
      {
        sparql: "MATCH (e:Entity) RETURN 1",
        timing: {
          cypher_exec_ms: 12.3,
          cypher_exec_ms_retry1: 8,
          query_language: "cypher",
        },
      },
      { roundtripMs: 45 },
    );
    expect(out).toContain("Cypher Exec");
    expect(out).toContain("12.3ms");
    expect(out).toContain("Cypher Exec Retry1");
    expect(out).toContain("Query Language");
    expect(out).toContain("cypher");
    expect(out).toContain("Client roundtrip");
    expect(out).not.toMatch(/Neptune/i);
    expect(out).not.toMatch(/SPARQL/);
  });
});
