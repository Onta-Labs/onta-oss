/** Blueprint MCP tools — same SDK methods as the CLI (INF-575). */
import { existsSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Client } from "@infona-ai/cli";
import { z } from "zod";
import { client, errorResult, textResult } from "./mcpShared.js";

function readPackageDocument(
  path: string,
): { manifest_yaml?: string; manifest?: Record<string, unknown> } {
  let file = path;
  if (statSync(path).isDirectory()) {
    const yaml = join(path, "blueprint.yaml");
    const yml = join(path, "blueprint.yml");
    const json = join(path, "blueprint.json");
    if (existsSync(yaml)) file = yaml;
    else if (existsSync(yml)) file = yml;
    else if (existsSync(json)) file = json;
    else throw new Error(`${path} is not a Blueprint package (missing blueprint.yaml)`);
  }
  const text = readFileSync(file, "utf-8");
  if (file.endsWith(".json")) {
    return { manifest: JSON.parse(text) as Record<string, unknown> };
  }
  return { manifest_yaml: text };
}

export async function validateBlueprintHandler(
  { path, manifest_yaml }: { path?: string; manifest_yaml?: string },
  makeClient: () => Client = client,
) {
  try {
    const body = {
      ...(path ? readPackageDocument(path) : {}),
      ...(manifest_yaml ? { manifest_yaml } : {}),
    };
    const got = await makeClient().validateBlueprint(body);
    if (got.valid) return textResult("valid");
    return textResult(`invalid\n${(got.errors ?? []).join("\n")}`);
  } catch (err) {
    return errorResult(err);
  }
}

export async function installBlueprintHandler(
  {
    path,
    manifest_yaml,
    kg,
    include_sample,
  }: {
    path?: string;
    manifest_yaml?: string;
    kg: string;
    include_sample?: boolean;
  },
  makeClient: () => Client = client,
) {
  try {
    const got = await makeClient().installBlueprint({
      kg,
      include_sample: include_sample !== false,
      ...(path ? readPackageDocument(path) : {}),
      ...(manifest_yaml ? { manifest_yaml } : {}),
    });
    return textResult(JSON.stringify(got, null, 2));
  } catch (err) {
    return errorResult(err);
  }
}

export async function inspectBlueprintHandler(
  { id }: { id: string },
  makeClient: () => Client = client,
) {
  try {
    return textResult(JSON.stringify(await makeClient().inspectBlueprint(id), null, 2));
  } catch (err) {
    return errorResult(err);
  }
}

export async function uninstallBlueprintHandler(
  { id }: { id: string },
  makeClient: () => Client = client,
) {
  try {
    return textResult(
      JSON.stringify(await makeClient().uninstallBlueprint(id), null, 2),
    );
  } catch (err) {
    return errorResult(err);
  }
}

export async function forkBlueprintHandler(
  { id, as: asId }: { id: string; as?: string },
  makeClient: () => Client = client,
) {
  try {
    return textResult(
      JSON.stringify(await makeClient().forkBlueprint(id, { as: asId }), null, 2),
    );
  } catch (err) {
    return errorResult(err);
  }
}

export async function extendBlueprintHandler(
  {
    id,
    overlay,
    overlay_yaml,
    path,
  }: {
    id: string;
    overlay?: Record<string, unknown>;
    overlay_yaml?: string;
    path?: string;
  },
  makeClient: () => Client = client,
) {
  try {
    const fromPath = path ? readPackageDocument(path) : {};
    return textResult(
      JSON.stringify(
        await makeClient().extendBlueprint(id, {
          overlay,
          overlay_yaml,
          ...(fromPath.manifest ? { overlay: fromPath.manifest } : {}),
          ...(fromPath.manifest_yaml ? { overlay_yaml: fromPath.manifest_yaml } : {}),
        }),
        null,
        2,
      ),
    );
  } catch (err) {
    return errorResult(err);
  }
}

export async function updateBlueprintHandler(
  {
    id,
    path,
    manifest_yaml,
    include_sample,
  }: {
    id: string;
    path?: string;
    manifest_yaml?: string;
    include_sample?: boolean;
  },
  makeClient: () => Client = client,
) {
  try {
    return textResult(
      JSON.stringify(
        await makeClient().updateBlueprint(id, {
          include_sample,
          ...(path ? readPackageDocument(path) : {}),
          ...(manifest_yaml ? { manifest_yaml } : {}),
        }),
        null,
        2,
      ),
    );
  } catch (err) {
    return errorResult(err);
  }
}

export async function firstRunBlueprintHandler(
  {
    id,
    credentials,
    question,
    max_rows,
  }: {
    id: string;
    credentials?: Record<string, string>;
    question?: string;
    max_rows?: number;
  },
  makeClient: () => Client = client,
) {
  try {
    return textResult(
      JSON.stringify(
        await makeClient().firstRunBlueprint(id, {
          credentials,
          question,
          max_rows,
        }),
        null,
        2,
      ),
    );
  } catch (err) {
    return errorResult(err);
  }
}

export function registerBlueprintTools(server: McpServer): void {
  server.registerTool(
    "validate_blueprint",
    {
      description:
        "Validate a Blueprint v1 document. Writes nothing. Same route as `infona blueprint validate`.",
      inputSchema: {
        path: z.string().optional().describe("Local package directory; MCP reads blueprint.yaml and POSTs the document."),
        manifest_yaml: z.string().optional().describe("Inline blueprint.yaml text."),
      },
    },
    (args) => validateBlueprintHandler(args),
  );
  server.registerTool(
    "install_blueprint",
    {
      description:
        "Install a Blueprint into this workspace (idempotent). Optional bounded sample is never current. Same route as `infona blueprint install`.",
      inputSchema: {
        kg: z.string().describe("Knowledge graph that receives the optional sample."),
        path: z.string().optional().describe("Local package directory; MCP reads blueprint.yaml and POSTs the document."),
        manifest_yaml: z.string().optional(),
        include_sample: z.boolean().optional(),
      },
    },
    (args) => installBlueprintHandler(args),
  );
  server.registerTool(
    "inspect_blueprint",
    {
      description: "Show the installed Blueprint pin in this workspace.",
      inputSchema: {
        id: z.string().describe("Blueprint id (namespace/name)."),
      },
    },
    (args) => inspectBlueprintHandler(args),
  );
  server.registerTool(
    "uninstall_blueprint",
    {
      description:
        "Remove what install wrote. Refuses if another KG still holds typed data.",
      inputSchema: {
        id: z.string().describe("Blueprint id (namespace/name)."),
      },
    },
    (args) => uninstallBlueprintHandler(args),
  );
  server.registerTool(
    "fork_blueprint",
    {
      description:
        "Fork a Blueprint package into a new identity with lineage. Does not copy instance data. Same route as `infona blueprint fork`.",
      inputSchema: {
        id: z.string().describe("Parent blueprint id (namespace/name)."),
        as: z.string().optional().describe("New package id (namespace/name). Default: {tenant}/{parent-name}."),
      },
    },
    (args) => forkBlueprintHandler(args),
  );
  server.registerTool(
    "extend_blueprint",
    {
      description:
        "Add a private overlay on an installed Blueprint pin. Same identity. Same route as `infona blueprint extend`.",
      inputSchema: {
        id: z.string().describe("Installed blueprint id (namespace/name)."),
        path: z.string().optional().describe("Local overlay YAML/JSON file."),
        overlay_yaml: z.string().optional(),
      },
    },
    (args) => extendBlueprintHandler(args),
  );
  server.registerTool(
    "update_blueprint",
    {
      description:
        "Apply a new public base without clobbering the private overlay. Same route as `infona blueprint update`.",
      inputSchema: {
        id: z.string().describe("Installed blueprint id (namespace/name)."),
        path: z.string().optional().describe("New package directory."),
        manifest_yaml: z.string().optional(),
        include_sample: z.boolean().optional(),
      },
    },
    (args) => updateBlueprintHandler(args),
  );
  server.registerTool(
    "first_run_blueprint",
    {
      description:
        "After install: walk source credentials, run acquire_condition_set, answer one supported question. Sample is never current. Same route as `infona blueprint first-run`.",
      inputSchema: {
        id: z.string().describe("Installed blueprint id (namespace/name)."),
        credentials: z.record(z.string()).optional().describe("Workspace-side KEY_ENV values. Missing byok keys fail closed."),
        question: z.string().optional().describe("Supported question to answer."),
        max_rows: z.number().optional(),
      },
    },
    (args) => firstRunBlueprintHandler(args),
  );
}
