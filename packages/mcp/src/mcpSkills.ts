/** Type-attached skill MCP tools.

Rides typed Client methods on `/graphs/{tenant}/skills`. `skills_prompt_block`
returns the backend text as-is — never re-rendered locally.
*/
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Client } from "@infona-ai/cli";
import type { SkillDetail, SkillSummary, SkillWrite } from "@infona-ai/cli";
import { z } from "zod";
import { client, errorResult, textResult } from "./mcpShared.js";

function renderSummary(s: SkillSummary): string {
  const title = s.title || s.slug;
  const flag = s.editable ? "editable" : "read-only";
  const on = s.enabled === false ? "disabled" : "enabled";
  return `${s.type_name}/${s.slug}  ${title}  [${s.layer}, ${on}, ${flag}]`;
}

function renderDetail(s: SkillDetail): string {
  const lines = [renderSummary(s)];
  if (s.summary) lines.push(s.summary);
  if (s.body) lines.push("", s.body);
  return lines.join("\n");
}

export async function listSkillsHandler(
  { type_name }: { type_name?: string },
  makeClient: () => Client = client,
) {
  try {
    const skills = await makeClient().listSkills(type_name);
    if (!skills.length) {
      return textResult(
        type_name
          ? `No skills on type "${type_name}".`
          : "No skills in this workspace.",
      );
    }
    return textResult(skills.map(renderSummary).join("\n"));
  } catch (err) {
    return errorResult(err);
  }
}

export async function getSkillHandler(
  { type_name, slug }: { type_name: string; slug: string },
  makeClient: () => Client = client,
) {
  try {
    const skill = await makeClient().getSkill(type_name, slug);
    return textResult(renderDetail(skill));
  } catch (err) {
    return errorResult(err);
  }
}

export async function validateSkillHandler(
  body: { type_name: string; slug?: string; body?: string; title?: string; summary?: string },
  makeClient: () => Client = client,
) {
  try {
    const write: SkillWrite = { type_name: body.type_name };
    if (body.slug) write.slug = body.slug;
    if (body.body != null) write.body = body.body;
    if (body.title) write.title = body.title;
    if (body.summary) write.summary = body.summary;
    const got = await makeClient().validateSkill(write);
    if (got.valid) return textResult("Skill is valid.");
    const msgs = (got.errors ?? []).map((e) => `- ${e.message}`).join("\n");
    return textResult(`Skill is invalid:\n${msgs || "(no details)"}`);
  } catch (err) {
    return errorResult(err);
  }
}

export async function putSkillHandler(
  {
    type_name,
    slug,
    body,
    title,
    summary,
    enabled,
  }: {
    type_name: string;
    slug: string;
    body?: string;
    title?: string;
    summary?: string;
    enabled?: boolean;
  },
  makeClient: () => Client = client,
) {
  try {
    const c = makeClient();
    const skill =
      body != null
        ? await c.createSkill({ type_name, slug, body, title, summary, enabled })
        : await c.updateSkill(type_name, slug, { title, summary, enabled });
    return textResult(`Saved ${renderSummary(skill)}`);
  } catch (err) {
    return errorResult(err);
  }
}

export async function deleteSkillHandler(
  { type_name, slug }: { type_name: string; slug: string },
  makeClient: () => Client = client,
) {
  try {
    await makeClient().deleteSkill(type_name, slug);
    return textResult(`Deleted skill ${type_name}/${slug}.`);
  } catch (err) {
    return errorResult(err);
  }
}

export async function skillsPromptBlockHandler(
  { type_names }: { type_names?: string[] },
  makeClient: () => Client = client,
) {
  try {
    const block = await makeClient().skillsPromptBlock(type_names);
    // Canonical backend text — do not re-render markdown locally.
    if (!block.text) {
      return textResult(
        block.skill_count
          ? `(${block.skill_count} skill(s) resolved; prompt-block text is empty)`
          : "No skill prompt-block for those types.",
      );
    }
    return textResult(block.text);
  } catch (err) {
    return errorResult(err);
  }
}

export function registerSkillsTools(server: McpServer): void {
  server.registerTool(
    "list_skills",
    {
      description:
        "List type-attached skills visible in this workspace (tenant + curated layers).",
      inputSchema: {
        type_name: z.string().optional().describe("Restrict to one entity type."),
      },
    },
    (args) => listSkillsHandler(args),
  );
  server.registerTool(
    "get_skill",
    {
      description: "Read one skill including its full markdown body.",
      inputSchema: {
        type_name: z.string(),
        slug: z.string(),
      },
    },
    (args) => getSkillHandler(args),
  );
  server.registerTool(
    "validate_skill",
    {
      description: "Validate a skill body without writing it.",
      inputSchema: {
        type_name: z.string(),
        slug: z.string().optional(),
        body: z.string().optional().describe("Markdown skill body."),
        title: z.string().optional(),
        summary: z.string().optional(),
      },
    },
    (args) => validateSkillHandler(args),
  );
  server.registerTool(
    "put_skill",
    {
      description:
        "Create or update a TENANT skill (writes the ontology overlay). " +
        "A markdown `body` create-or-replaces via POST; omit body to PATCH title/" +
        "summary/enabled. Curated global skills are read-only (403).",
      inputSchema: {
        type_name: z.string(),
        slug: z.string(),
        body: z.string().optional().describe("Markdown body; when set, create-or-replace."),
        title: z.string().optional(),
        summary: z.string().optional(),
        enabled: z.boolean().optional(),
      },
    },
    (args) => putSkillHandler(args),
  );
  server.registerTool(
    "delete_skill",
    {
      description:
        "Delete a TENANT skill (writes the ontology overlay). Curated global " +
        "skills cannot be deleted (403).",
      inputSchema: {
        type_name: z.string(),
        slug: z.string(),
      },
    },
    (args) => deleteSkillHandler(args),
  );
  server.registerTool(
    "skills_prompt_block",
    {
      description:
        "Return the EXACT skill text an agent is handed for the given types. " +
        "Do not locally render skills; this is the canonical prompt-block.",
      inputSchema: {
        type_names: z
          .array(z.string())
          .optional()
          .describe("Entity types to resolve. Empty → empty block."),
      },
    },
    (args) => skillsPromptBlockHandler(args),
  );
}
