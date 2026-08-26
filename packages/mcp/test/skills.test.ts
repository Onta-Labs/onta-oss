import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  listSkillsHandler,
  getSkillHandler,
  validateSkillHandler,
  putSkillHandler,
  deleteSkillHandler,
  skillsPromptBlockHandler,
  viewOntologyHandler,
} from "../src/index.js";

const here = dirname(fileURLToPath(import.meta.url));

afterEach(() => vi.restoreAllMocks());

function stub(methods: Record<string, ReturnType<typeof vi.fn>>) {
  return methods as unknown as import("@infona-ai/cli").Client;
}

const SKILL = {
  slug: "how-to-query",
  type_name: "SynthWidget",
  title: "Ada Example",
  summary: "How to query a widget",
  layer: "tenant",
  enabled: true,
  version: 1,
  body_chars: 12,
  editable: true,
  body: "# Ada Example\nUse color.",
  metadata: {},
};

describe("skill tools — SDK forwarding", () => {
  it("list_skills / get_skill forward type_name and slug", async () => {
    const listSkills = vi.fn(async () => [SKILL]);
    const getSkill = vi.fn(async () => SKILL);
    await listSkillsHandler({ type_name: "SynthWidget" }, () => stub({ listSkills }));
    expect(listSkills).toHaveBeenCalledWith("SynthWidget");
    const got = await getSkillHandler(
      { type_name: "SynthWidget", slug: "how-to-query" },
      () => stub({ getSkill }),
    );
    expect(getSkill).toHaveBeenCalledWith("SynthWidget", "how-to-query");
    expect(got.content.map((c) => c.text).join("\n")).toContain("# Ada Example");
  });

  it("validate_skill posts through validateSkill (no write)", async () => {
    const validateSkill = vi.fn(async () => ({
      valid: false,
      errors: [{ message: "body too short" }],
    }));
    const res = await validateSkillHandler(
      { type_name: "SynthWidget", slug: "s", body: "x" },
      () => stub({ validateSkill }),
    );
    expect(validateSkill).toHaveBeenCalledWith({
      type_name: "SynthWidget",
      slug: "s",
      body: "x",
    });
    expect(res.content.map((c) => c.text).join("\n")).toContain("body too short");
  });

  it("put_skill with body → createSkill; without body → updateSkill", async () => {
    const createSkill = vi.fn(async () => SKILL);
    const updateSkill = vi.fn(async () => SKILL);
    await putSkillHandler(
      { type_name: "SynthWidget", slug: "how-to-query", body: "# Ada Example" },
      () => stub({ createSkill, updateSkill }),
    );
    expect(createSkill).toHaveBeenCalledTimes(1);
    expect(updateSkill).not.toHaveBeenCalled();

    await putSkillHandler(
      { type_name: "SynthWidget", slug: "how-to-query", title: "Ada Example" },
      () => stub({ createSkill, updateSkill }),
    );
    expect(updateSkill).toHaveBeenCalledWith("SynthWidget", "how-to-query", {
      title: "Ada Example",
      summary: undefined,
      enabled: undefined,
    });
  });

  it("delete_skill calls deleteSkill", async () => {
    const deleteSkill = vi.fn(async () => ({ ok: true }));
    await deleteSkillHandler(
      { type_name: "SynthWidget", slug: "how-to-query" },
      () => stub({ deleteSkill }),
    );
    expect(deleteSkill).toHaveBeenCalledWith("SynthWidget", "how-to-query");
  });

  it("skills_prompt_block returns backend text as-is (no local render)", async () => {
    const skillsPromptBlock = vi.fn(async () => ({
      text: "## SynthWidget\nAda Example skill text",
      skill_count: 1,
      chars: 40,
    }));
    const res = await skillsPromptBlockHandler(
      { type_names: ["SynthWidget"] },
      () => stub({ skillsPromptBlock }),
    );
    expect(skillsPromptBlock).toHaveBeenCalledWith(["SynthWidget"]);
    expect(res.content.map((c) => c.text).join("\n")).toBe(
      "## SynthWidget\nAda Example skill text",
    );
  });
});

describe("view_ontology includes type descriptions + prompt-block", () => {
  it("renders description and appends skillsPromptBlock text", async () => {
    const ontologyTypes = vi.fn(async () => [
      {
        name: "SynthWidget",
        description: "A synthetic widget",
        attributes: [{ name: "color" }],
        relationships: [],
      },
    ]);
    const skillsPromptBlock = vi.fn(async () => ({
      text: "SKILL BLOCK",
      skill_count: 1,
      chars: 11,
    }));
    const res = await viewOntologyHandler({}, () =>
      stub({ ontologyTypes, skillsPromptBlock }),
    );
    expect(skillsPromptBlock).toHaveBeenCalledWith(["SynthWidget"]);
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("A synthetic widget");
    expect(text).toContain("SKILL BLOCK");
  });
});

describe("skill tool sources do not hardcode backend paths", () => {
  it("mcpSkills.ts has no fetch( and no path-string /graphs/", () => {
    const src = readFileSync(join(here, "../src/mcpSkills.ts"), "utf8");
    const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
    expect(code).not.toMatch(/["'`][^"'`]*\/graphs\//);
    expect(code).not.toMatch(/\bfetch\s*\(/);
  });
});
