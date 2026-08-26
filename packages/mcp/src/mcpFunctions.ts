/** Type-attached function MCP tools.

Rides typed Client methods on `/graphs/{tenant}/functions`. Invoke writes
materialized attributes back onto the entity.
*/
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Client } from "@infona-ai/cli";
import type { FunctionRef } from "@infona-ai/cli";
import { z } from "zod";
import { client, errorResult, textResult } from "./mcpShared.js";

function renderFn(f: FunctionRef): string {
  const ep = f.endpoint_url ? ` ${f.endpoint_url}` : "";
  const desc = f.description ? ` — ${f.description}` : "";
  return `${f.entity_type}/${f.name}${desc}${ep}`;
}

export async function listFunctionsHandler(
  { entity_type }: { entity_type?: string },
  makeClient: () => Client = client,
) {
  try {
    const fns = await makeClient().listFunctions(entity_type);
    if (!fns.length) {
      return textResult(
        entity_type
          ? `No functions attached to "${entity_type}".`
          : "No functions registered in this workspace.",
      );
    }
    return textResult(fns.map(renderFn).join("\n"));
  } catch (err) {
    return errorResult(err);
  }
}

export async function registerFunctionHandler(
  {
    name,
    entity_type,
    endpoint_url,
    description,
  }: {
    name: string;
    entity_type: string;
    endpoint_url: string;
    description?: string;
  },
  makeClient: () => Client = client,
) {
  try {
    const got = await makeClient().registerFunction({
      name,
      entity_type,
      endpoint_url,
      description,
    });
    return textResult(
      `Registered ${got.registered} on ${got.entity_type} (${got.layer}).`,
    );
  } catch (err) {
    return errorResult(err);
  }
}

export async function invokeFunctionHandler(
  {
    name,
    entity_uri,
    kg_name,
  }: { name: string; entity_uri: string; kg_name: string },
  makeClient: () => Client = client,
) {
  try {
    const got = await makeClient().invokeFunction(name, { entity_uri, kg_name });
    const lines = [
      `Invoked ${got.function} on ${got.entity_uri} (${got.duration_ms}ms).`,
      JSON.stringify(got.output ?? {}, null, 2),
    ];
    return textResult(lines.join("\n"));
  } catch (err) {
    return errorResult(err);
  }
}

export async function deleteFunctionHandler(
  { name, entity_type }: { name: string; entity_type?: string },
  makeClient: () => Client = client,
) {
  try {
    await makeClient().deleteFunction(name, { entityType: entity_type });
    return textResult(`Deleted function ${name}.`);
  } catch (err) {
    return errorResult(err);
  }
}

export function registerFunctionsTools(server: McpServer): void {
  server.registerTool(
    "list_functions",
    {
      description: "List type-attached function endpoints in this workspace.",
      inputSchema: {
        entity_type: z.string().optional().describe("Restrict to one entity type."),
      },
    },
    (args) => listFunctionsHandler(args),
  );
  server.registerTool(
    "register_function",
    {
      description:
        "Attach an HTTPS URL or Lambda ARN to a type (writes the function " +
        "registry). The endpoint is invoked later via invoke_function.",
      inputSchema: {
        name: z.string().describe("Function name (slug)."),
        entity_type: z.string().describe("Type to attach to."),
        endpoint_url: z
          .string()
          .describe("HTTPS URL or Lambda ARN (arn:aws:lambda:…)."),
        description: z.string().optional(),
      },
    },
    (args) => registerFunctionHandler(args),
  );
  server.registerTool(
    "invoke_function",
    {
      description:
        "Invoke a registered function for one entity and materialize the result " +
        "as triples (writes the graph).",
      inputSchema: {
        name: z.string().describe("Registered function name."),
        entity_uri: z.string().describe("Entity URI to invoke against."),
        kg_name: z.string().describe("Context graph that holds the entity."),
      },
    },
    (args) => invokeFunctionHandler(args),
  );
  server.registerTool(
    "delete_function",
    {
      description:
        "Delete a function attachment (writes the function registry).",
      inputSchema: {
        name: z.string(),
        entity_type: z.string().optional().describe("Disambiguate when names collide."),
      },
    },
    (args) => deleteFunctionHandler(args),
  );
}
