import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { Client, CographError } from "cograph";
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
