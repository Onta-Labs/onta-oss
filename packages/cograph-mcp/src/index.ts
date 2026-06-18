import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { Client, CographError } from "cograph";
import type { ResolvedChange } from "cograph";
import { z } from "zod";

const VERSION = "0.1.0";

const server = new McpServer(
  {
    name: "cograph",
    version: VERSION,
  },
  {
    instructions:
      "Cograph is a knowledge graph platform. Use these tools to query " +
      "structured data across multiple knowledge graphs using natural language.",
  },
);

function client(): Client {
  return new Client();
}

function textResult(text: string) {
  return {
    content: [{ type: "text" as const, text }],
  };
}

function errorResult(err: unknown) {
  const msg =
    err instanceof CographError
      ? `Cograph error: ${err.message}`
      : err instanceof Error
        ? err.message
        : String(err);
  return {
    content: [{ type: "text" as const, text: msg }],
    isError: true,
  };
}

server.registerTool(
  "list_knowledge_graphs",
  {
    description:
      "List all available knowledge graphs and their descriptions.",
    inputSchema: {},
  },
  async () => {
    try {
      const kgs = await client().listKgs();
      if (!kgs.length) return textResult("No knowledge graphs found.");
      const lines = kgs.map((kg) => {
        const name = String(kg.name ?? "?");
        const desc = kg.description ? `: ${kg.description}` : "";
        return `- ${name}${desc}`;
      });
      return textResult(lines.join("\n"));
    } catch (err) {
      return errorResult(err);
    }
  },
);

server.registerTool(
  "ask",
  {
    description:
      "Ask a natural language question against a knowledge graph. " +
      'Use list_knowledge_graphs to see available KGs first.',
    inputSchema: {
      question: z
        .string()
        .describe(
          'The natural language question to ask (e.g., "How many events are in San Francisco?")',
        ),
      kg_name: z
        .string()
        .optional()
        .describe(
          "Name of the knowledge graph to query. Use list_knowledge_graphs to see available KGs.",
        ),
    },
  },
  async ({ question, kg_name }) => {
    try {
      const data = await client().ask(question, { kg: kg_name });
      const answer = data.answer ?? "No answer";
      const explanation = data.explanation;
      let out = `Answer: ${answer}`;
      if (explanation) out += `\nExplanation: ${explanation}`;
      return textResult(out);
    } catch (err) {
      return errorResult(err);
    }
  },
);

server.registerTool(
  "ingest_csv",
  {
    description:
      "Ingest a CSV file into a knowledge graph. The schema is automatically inferred.",
    inputSchema: {
      file_path: z
        .string()
        .describe("Absolute path to the CSV file to ingest."),
      kg_name: z
        .string()
        .describe(
          'Name for the knowledge graph (e.g., "sales-data", "customer-records").',
        ),
    },
  },
  async ({ file_path, kg_name }) => {
    try {
      const result = await client().ingest(file_path, { kg: kg_name });
      const entities = Number(result.entities_resolved ?? 0);
      const triples = Number(result.triples_inserted ?? 0);
      return textResult(
        `Ingestion complete: ${entities} entities resolved, ${triples} triples inserted into "${kg_name}".`,
      );
    } catch (err) {
      return errorResult(err);
    }
  },
);

server.registerTool(
  "view_ontology",
  {
    description:
      "View the ontology (types, attributes, relationships) across all knowledge graphs.",
    inputSchema: {},
  },
  async () => {
    try {
      const types = await client().ontologyTypes();
      if (!types.length) return textResult("No ontology types defined yet.");
      const lines: string[] = [];
      for (const t of types) {
        const name = String(t.name ?? "?");
        lines.push(`Type: ${name}`);
        const attrs = (t.attributes ?? []) as Array<Record<string, unknown>>;
        if (attrs.length) {
          lines.push(
            `  Attributes: ${attrs.map((a) => String(a.name ?? "?")).join(", ")}`,
          );
        }
        const rels = (t.relationships ?? []) as Array<Record<string, unknown>>;
        if (rels.length) {
          lines.push(
            `  Relationships: ${rels
              .map(
                (r) =>
                  `${String(r.predicate ?? "?")} -> ${String(r.target_type ?? "?")}`,
              )
              .join(", ")}`,
          );
        }
      }
      return textResult(lines.join("\n"));
    } catch (err) {
      return errorResult(err);
    }
  },
);

function describeChange(c: ResolvedChange): string {
  const verb =
    c.kind === "relationship"
      ? `relationship "${c.name}" from ${c.subject_type} -> ${c.datatype_or_target}`
      : `attribute "${c.name}" (${c.datatype_or_target}) on ${c.subject_type}`;
  return `[${c.action}] ${verb} — confidence ${c.confidence.toFixed(2)}: ${c.reason}`;
}

server.registerTool(
  "evolve_ontology",
  {
    description:
      "Evolve the knowledge-graph ontology from a plain-language description of " +
      "the change you want. You do NOT need to know exact type, attribute, or " +
      'relationship names — just describe the change in natural language (e.g. ' +
      '"track which company a person works for" or "people should have a birth ' +
      'date") and the server resolves it against the existing ontology. ' +
      "High-confidence changes are applied automatically; lower-confidence ones " +
      "are returned as proposals for you to confirm by passing them to " +
      "apply_ontology_change.",
    inputSchema: {
      ask: z
        .string()
        .describe(
          "A plain-language description of the ontology change to make " +
            '(e.g. "track which company a person works for"). No exact schema ' +
            "names required.",
        ),
      knowledge_graph: z
        .string()
        .optional()
        .describe(
          "Optional name of the knowledge graph to scope the change to. " +
            "Use list_knowledge_graphs to see available KGs.",
        ),
    },
  },
  async ({ ask, knowledge_graph }) => {
    try {
      const result = await client().ontologyResolve(ask, { knowledge_graph });
      const lines: string[] = [result.summary];

      if (result.applied.length) {
        lines.push("", "Auto-applied:");
        for (const c of result.applied) lines.push(`  ${describeChange(c)}`);
      } else {
        lines.push("", "Auto-applied: none");
      }

      if (result.proposals.length) {
        lines.push(
          "",
          "Proposals needing confirmation (pass one straight to apply_ontology_change):",
        );
        for (const c of result.proposals) lines.push(`  ${describeChange(c)}`);
        lines.push(
          "",
          "Raw proposal objects:",
          JSON.stringify(result.proposals, null, 2),
        );
      } else {
        lines.push("", "Proposals needing confirmation: none");
      }

      return textResult(lines.join("\n"));
    } catch (err) {
      return errorResult(err);
    }
  },
);

server.registerTool(
  "apply_ontology_change",
  {
    description:
      "Confirm and apply a single ontology change proposal returned by " +
      "evolve_ontology. Pass one of the raw proposal objects through unchanged " +
      "as `proposal`.",
    inputSchema: {
      proposal: z
        .object({
          kind: z.enum(["attribute", "relationship"]),
          subject_type: z.string(),
          name: z.string(),
          datatype_or_target: z.string(),
          action: z.enum(["reuse", "extend", "create"]),
          confidence: z.number(),
          reason: z.string(),
        })
        .describe(
          "A ResolvedChange proposal object exactly as returned by " +
            "evolve_ontology.",
        ),
    },
  },
  async ({ proposal }) => {
    try {
      const result = await client().ontologyApply(proposal as ResolvedChange);
      const lines = [result.summary];
      lines.push("", `Operations applied: ${result.operations}`);
      lines.push(describeChange(result.applied));
      return textResult(lines.join("\n"));
    } catch (err) {
      return errorResult(err);
    }
  },
);

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  process.stderr.write(
    `cograph-mcp failed to start: ${err instanceof Error ? err.message : String(err)}\n`,
  );
  process.exit(1);
});
