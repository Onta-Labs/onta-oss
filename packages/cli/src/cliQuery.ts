/** ``infona ask`` / ``infona agent``.

``agent`` is the ONE command that reaches unified ``POST /graphs/{tenant}/agent``.
``runAgentCommand`` is exported for tests (not part of the published SDK).
*/
import { ASK_DEBUG_HELP, formatAskDebug } from "./askDebug.js";
import { renderAgentResult } from "./agentRender.js";
import type { Client } from "./client.js";
import {
  client,
  dim,
  program,
  resolveKg,
  withErrors,
} from "./cliShared.js";
import { diagnoseAskResult } from "./firstHourErrors.js";

// ---------------------------------------------------------------------------
// ask
// ---------------------------------------------------------------------------

program
  .command("ask <question>")
  .description("Ask a natural language question")
  .option("--kg <name>", "Context graph to query")
  .option("-d, --debug", ASK_DEBUG_HELP)
  .option("-m, --model <model>", "Override query model")
  .action(
    async (
      question: string,
      opts: { kg?: string; debug?: boolean; model?: string },
    ) => {
      await withErrors(async () => {
        if (opts.model) process.stdout.write(`Model: ${opts.model}\n`);
        process.stdout.write(`Q: ${question}\n`);
        process.stdout.write("Generating answer...\n");
        const t0 = Date.now();
        const kg = resolveKg(opts.kg);
        const result = await client().ask(question, {
          kg,
          model: opts.model,
        });
        const roundtripMs = Date.now() - t0;
        const raw = typeof result.answer === "string" ? result.answer : "No answer";
        process.stdout.write(`\nA: ${diagnoseAskResult(raw, kg)}\n`);
        if (opts.debug) {
          process.stdout.write(
            formatAskDebug(result, { roundtripMs }) + "\n",
          );
        }
      });
    },
  );

// ---------------------------------------------------------------------------
// agent — unified Ask-AI agent (POST /graphs/{tenant}/agent)
// ---------------------------------------------------------------------------
//
// The ONE command that reaches the unified agent the web app + MCP already use:
// it classifies intent server-side (question | enrich | clean | dedup |
// ontology) and either answers, asks a clarifying question, or proposes a plan
// to confirm. The discrete commands (ask/enrich/er/ontology) stay as
// convenient shortcuts; migrating them onto the agent is a deliberate non-goal.
//
// Confirm flow (non-interactive): a returned plan is NOT executed automatically.
// Either re-run with --confirm <plan_id> (the only mutating path), or pass --yes
// to confirm-and-execute in the same invocation.

/**
 * Core of the `agent` command — extracted so it's unit-testable with a mocked
 * {@link Client} (the commander action below just builds a real client and
 * delegates). Drives the three non-interactive paths:
 *  - `--confirm <id>` → execute that plan directly, render the result.
 *  - default          → one agent turn, render it; if it's a plan, either
 *                       confirm-and-execute (`--yes`) or print a confirm hint.
 *
 * Exported for tests; not part of the published SDK surface (cli.ts is the bin
 * entry, not in `package.json#exports`).
 */
export async function runAgentCommand(
  c: Client,
  message: string,
  opts: { kg?: string; type?: string; yes?: boolean; confirm?: string },
): Promise<void> {
  // KG resolution mirrors `ask`: an explicit --kg wins, else the SDK's
  // saved/default kg (passing undefined lets the backend use its default).
  const context = { kgName: opts.kg, typeName: opts.type };

  // --confirm path: execute the named plan directly and render the result.
  if (opts.confirm) {
    const result = await c.agent({ confirmPlanId: opts.confirm, ...context });
    renderAgentResult(result);
    return;
  }

  const result = await c.agent({ message, ...context });
  renderAgentResult(result);

  // A plan is the only kind that awaits a follow-up. With --yes we confirm
  // immediately; otherwise we print how to confirm it later.
  if (result.kind === "plan") {
    const planId =
      typeof result.plan_id === "string" ? result.plan_id : undefined;
    if (!planId) return;
    if (opts.yes) {
      const executed = await c.agent({ confirmPlanId: planId, ...context });
      renderAgentResult(executed);
    } else {
      const flags = [
        opts.kg ? `--kg ${opts.kg}` : "",
        opts.type ? `--type ${opts.type}` : "",
      ]
        .filter(Boolean)
        .join(" ");
      const hint = `infona agent --confirm ${planId}${flags ? " " + flags : ""} ${JSON.stringify(message)}`;
      process.stdout.write(
        `${dim("Confirm & run:")} ${hint}\n` +
          `${dim("  or re-run with --yes to execute now.")}\n`,
      );
    }
  }
}

program
  .command("agent <message>")
  .description("Talk to the unified Ask-AI agent (answers, plans, and runs actions)")
  .option("--kg <name>", "Context graph to operate within")
  .option("--type <Type>", "Active type scope (for enrich/clean/dedup planning)")
  .option(
    "-y, --yes",
    "Auto-confirm and execute a returned plan in the same run",
  )
  .option(
    "--confirm <planId>",
    "Execute a specific previously-proposed plan id (skips planning)",
  )
  .action(
    async (
      message: string,
      opts: { kg?: string; type?: string; yes?: boolean; confirm?: string },
    ) => {
      await withErrors(() => runAgentCommand(client(), message, opts));
    },
  );
