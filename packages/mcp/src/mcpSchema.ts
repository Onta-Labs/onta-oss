/** Ontology / schema MCP tools: view, inspect, evolve, apply.

``inspect_graph_schema`` rides the SDK ``kgSchema`` path (KG-scoped
population-aware schema). Evolve/apply ride ``ontologyResolve`` /
``ontologyApply`` / ``ontologyApplyBatch``.
*/
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Client } from "@infona-ai/cli";
import type { ResolvedChange } from "@infona-ai/cli";
import { z } from "zod";
import { client, errorResult, textResult } from "./mcpShared.js";

function renderSlot(s: {
  name: string;
  coverage_pct: number;
  populated: boolean;
  datatype?: string;
  target_type?: string | null;
}): string {
  const kind = s.target_type ? ` -> ${s.target_type}` : s.datatype ? ` (${s.datatype})` : "";
  // EMPTY, not "0%": the agent must not read an unpopulated slot as a weak
  // signal it can still query. It is a declaration with nothing behind it.
  const cov = s.populated ? `${s.coverage_pct}%` : "EMPTY";
  return `${s.name}${kind} ${cov}`;
}

export async function viewOntologyHandler(
  _args: Record<string, never> = {},
  makeClient: () => Client = client,
) {
  try {
    const c = makeClient();
    const types = await c.ontologyTypes();
    if (!types.length) return textResult("No ontology types defined yet.");
    const lines: string[] = [];
    const typeNames: string[] = [];
    for (const t of types) {
      const name = String(t.name ?? "?");
      typeNames.push(name);
      lines.push(`Type: ${name}`);
      const desc = typeof t.description === "string" ? t.description.trim() : "";
      if (desc) lines.push(`  ${desc}`);
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
                `${String(r.predicate ?? r.name ?? "?")} -> ${String(r.target_type ?? "?")}`,
            )
            .join(", ")}`,
        );
      }
    }
    // Canonical skill injection text — do not re-render markdown locally.
    const block = await c.skillsPromptBlock(typeNames);
    if (block.text) {
      lines.push("", block.text);
    }
    return textResult(lines.join("\n"));
  } catch (err) {
    return errorResult(err);
  }
}

export async function inspectGraphSchemaHandler(
  {
    kg_name,
    type,
    min_coverage,
  }: { kg_name: string; type?: string[]; min_coverage?: number },
  makeClient: () => Client = client,
) {
  try {
    const data = await makeClient().kgSchema(kg_name, {
      types: type,
      minCoverage: min_coverage,
    });
    if (!data.types.length) {
      // Name what the graph DOES have, so a filter typo never reads as
      // "that type does not exist" (the failure this tool exists to prevent).
      const available = data.available_type_names ?? [];
      return textResult(
        `Context graph "${kg_name}" has no types matching that request.` +
          (available.length
            ? ` Types it does have: ${available.join(", ")}.`
            : " The graph has no types at all."),
      );
    }
    // Say "matching" when a filter was applied, so a drill-in is never misread
    // as "this graph has exactly one type".
    const scope = type?.length ? "matching type(s)" : "type(s)";
    const lines: string[] = [
      `Schema for context graph "${kg_name}" (${data.total_types} ${scope})` +
        (data.stats_source === "live_scan"
          ? " (scanned live; precomputed stats not materialized yet)"
          : "") +
        ". Percentages are the share of that type's entities carrying the slot; " +
        "EMPTY means declared but with no data in this graph.",
    ];
    for (const t of data.types) {
      const suffix = t.declared_only
        ? " (declared in the ontology, NO instances in this graph)"
        : "";
      lines.push("", `${t.name}: ${t.entity_count} entities${suffix}`);
      if (t.description) lines.push(`  ${t.description}`);
      if (t.attributes.length) {
        lines.push(`  attributes: ${t.attributes.map(renderSlot).join(", ")}`);
      }
      if (t.relationships.length) {
        lines.push(`  relationships: ${t.relationships.map(renderSlot).join(", ")}`);
      }
      const withheld = t.attributes_withheld + t.relationships_withheld;
      if (withheld) {
        lines.push(
          `  (${withheld} more below the min_coverage floor; lower or drop it to see them)`,
        );
      }
    }
    if (data.truncated) {
      lines.push(
        "",
        `Also present, not expanded here (pass type= to drill in): ${data.omitted_type_names.join(", ")}`,
      );
    }
    lines.push("", `Note: ${data.coverage_note}`);
    return textResult(lines.join("\n"));
  } catch (err) {
    return errorResult(err);
  }
}

function describeChange(c: ResolvedChange): string {
  const verb =
    c.kind === "relationship"
      ? `relationship "${c.name}" from ${c.subject_type} -> ${c.datatype_or_target}`
      : `attribute "${c.name}" (${c.datatype_or_target}) on ${c.subject_type}`;
  return `[${c.action}] ${verb} — confidence ${c.confidence.toFixed(2)}: ${c.reason}`;
}

const proposalShape = z.object({
  kind: z.enum(["attribute", "relationship"]),
  subject_type: z.string(),
  name: z.string(),
  datatype_or_target: z.string(),
  action: z.enum(["reuse", "extend", "create"]),
  confidence: z.number(),
  reason: z.string(),
});

export function registerSchemaTools(server: McpServer): void {
  server.registerTool(
    "view_ontology",
    {
      description:
        "View the ontology (types, attributes, relationships) across all context graphs.",
      inputSchema: {},
    },
    () => viewOntologyHandler(),
  );
  server.registerTool(
    "inspect_graph_schema",
    {
      description:
        "Inspect ONE context graph's schema WITH population data: for every type, " +
        "which attributes and relationships actually carry data there, and on what " +
        "share of that type's entities. Use this before asking for specific " +
        "attributes so you never guess between similar names (e.g. " +
        '"fda_indications" vs "indications") or query a slot that is declared but ' +
        "empty. Differs from view_ontology, which is tenant-wide and " +
        "DECLARATION-only (every type in the workspace, no population): this tool " +
        "is scoped to one graph and population-aware. Declared-but-empty types and " +
        "attributes are still listed, marked EMPTY, so an absent slot is never " +
        "confused with a non-existent one. Coverage is relative to each type's own " +
        "entity count, which attributes an entity carrying several types to only " +
        "one of them, so types that share multi-typed entities can legitimately " +
        "read below 100%. Sample VALUES are not returned.",
      inputSchema: {
        kg_name: z
          .string()
          .describe(
            "Name of the context graph to inspect. Use list_knowledge_graphs to see available KGs.",
          ),
        type: z
          .array(z.string())
          .optional()
          .describe(
            "Optional type names to drill into (e.g. [\"Drug\"]). Omit for the whole graph.",
          ),
        min_coverage: z
          .number()
          .optional()
          .describe(
            "Optional coverage floor as a PERCENT (0-100). Withholds slots below it " +
              "to keep a wide schema readable; the response says how many were withheld. " +
              "Omit to see every slot, including empty ones.",
          ),
      },
    },
    async ({ kg_name, type, min_coverage }) =>
      inspectGraphSchemaHandler({ kg_name, type, min_coverage }),
  );
  server.registerTool(
    "evolve_ontology",
    {
      description:
        "Evolve the context-graph ontology from a plain-language description of " +
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
            "Optional name of the context graph to scope the change to. " +
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

  // The raw ResolvedChange proposal shape, shared by the single- and batch-apply
  // tools so they can never drift.
  const proposalShape = z.object({
    kind: z.enum(["attribute", "relationship"]),
    subject_type: z.string(),
    name: z.string(),
    datatype_or_target: z.string(),
    action: z.enum(["reuse", "extend", "create"]),
    confidence: z.number(),
    reason: z.string(),
  });

  server.registerTool(
    "apply_ontology_change",
    {
      description:
        "Confirm and apply a single ontology change proposal returned by " +
        "evolve_ontology. Pass one of the raw proposal objects through unchanged " +
        "as `proposal`. To apply several proposals at once, prefer " +
        "apply_ontology_changes (one call instead of many).",
      inputSchema: {
        proposal: proposalShape.describe(
          "A ResolvedChange proposal object exactly as returned by evolve_ontology.",
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

  server.registerTool(
    "apply_ontology_changes",
    {
      description:
        "Confirm and apply SEVERAL ontology change proposals returned by " +
        "evolve_ontology in a single call — pass the raw proposal objects as " +
        "`proposals`. Prefer this over calling apply_ontology_change once per " +
        "proposal: it is one round-trip instead of N and reports each change's " +
        "outcome. Idempotent; a proposal that fails does not abort the rest.",
      inputSchema: {
        proposals: z
          .array(proposalShape)
          .min(1)
          .describe(
            "The ResolvedChange proposal objects to apply, exactly as returned " +
              "by evolve_ontology (the `Raw proposal objects` array).",
          ),
      },
    },
    async ({ proposals }) => {
      try {
        const result = await client().ontologyApplyBatch(
          proposals as ResolvedChange[],
        );
        const lines = [result.summary, ""];
        for (const r of result.results) {
          const status = r.ok ? "applied" : `FAILED: ${r.error}`;
          lines.push(`  ${describeChange(r.change)} — ${status}`);
        }
        return textResult(lines.join("\n"));
      } catch (err) {
        return errorResult(err);
      }
    },
  );
}
