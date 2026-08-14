/** Agent / schedule / job MCP tools.

``agent`` rides ``POST /graphs/{tenant}/agent`` via the SDK. Schedule
tools wrap the canonical ``raw.schedules`` / ``raw.createSchedule``
path builders. Job tools use ``jobs`` / ``enrichJob`` / ``waitForJob``.
*/
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { InfonaError, isTerminalJobStatus } from "@infona-ai/cli";
import type { AgentResult, JobCategory, JobStatus, Schedule } from "@infona-ai/cli";
import { z } from "zod";
import { formatAskQueryDump } from "./askQueryDump.js";
import {
  client,
  DEFAULT_SESSION_ID,
  errorResult,
  JOB_CATEGORIES,
  textResult,
} from "./mcpShared.js";

/**
 * Render a kind-tagged agent result (the shape returned by `/agent`) as readable
 * text plus the raw JSON, so an MCP client can both read a summary and act on the
 * machine-readable fields (e.g. carry a `plan_id` back into a confirm call).
 */
function describeAgentResult(r: AgentResult): string {
  const lines: string[] = [];
  switch (r.kind) {
    case "answer": {
      const answer = (r.answer as string | undefined) ?? "(no answer)";
      if (r.narrative) lines.push(String(r.narrative));
      lines.push(`Answer: ${answer}`);
      const runId =
        (typeof r.run_id === "string" && r.run_id) ||
        (typeof r.job_id === "string" && r.job_id) ||
        "";
      if (runId) lines.push(`run_id: ${runId}`);
      const queryDump = formatAskQueryDump(r);
      if (queryDump) lines.push(queryDump);
      const citations = Array.isArray(r.citations) ? r.citations : [];
      if (citations.length) {
        lines.push("\nCitations:");
        for (const c of citations) {
          if (typeof c === "string") lines.push(`  - ${c}`);
          else if (c && typeof c === "object") {
            const o = c as Record<string, unknown>;
            const bit = [o.title ?? o.label ?? o.source, o.url ?? o.source_url]
              .filter(Boolean)
              .join(" — ");
            lines.push(`  - ${bit || JSON.stringify(c)}`);
          } else lines.push(`  - ${String(c)}`);
        }
      }
      break;
    }
    case "clarify":
      lines.push(
        `Clarification needed: ${String(r.question ?? "Could you clarify?")}`,
      );
      break;
    case "plan": {
      const steps = Array.isArray(r.steps) ? r.steps : [];
      lines.push(
        `Proposed plan (${steps.length} step${steps.length === 1 ? "" : "s"}) — ` +
          `NOT yet executed. Review, then confirm by calling agent again with ` +
          `confirm_plan_id="${String(r.plan_id ?? "")}".`,
      );
      for (const s of steps as Array<Record<string, unknown>>) {
        const cap = String(s.capability ?? "?");
        const action = String(s.action ?? "?");
        const rationale = s.rationale ? ` — ${String(s.rationale)}` : "";
        lines.push(`  • [${cap}] ${action}${rationale}`);
        const cost = s.cost as Record<string, unknown> | undefined;
        if (cost?.note) lines.push(`      cost: ${String(cost.note)}`);
      }
      break;
    }
    case "result": {
      const steps = Array.isArray(r.steps) ? r.steps : [];
      lines.push(`Executed plan ${String(r.plan_id ?? "")}:`);
      for (const s of steps as Array<Record<string, unknown>>) {
        const status = String(s.status ?? "?");
        const msg = s.message ? ` — ${String(s.message)}` : "";
        lines.push(`  • [${String(s.capability ?? "?")}] ${status}${msg}`);
      }
      break;
    }
    case "error":
      lines.push(`Agent error: ${String(r.error ?? "unknown error")}`);
      break;
    default:
      lines.push(`Agent returned: ${String(r.kind)}`);
  }
  // Always append the raw JSON so the caller can read structured fields
  // (plan_id, steps, rows, …) it needs to drive the next turn.
  lines.push("", "Raw result:", JSON.stringify(r, null, 2));
  return lines.join("\n");
}

async function readScheduleResponse<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let detail = "";
    try {
      detail = await resp.text();
    } catch {
      /* body already consumed / empty */
    }
    throw new InfonaError(
      `schedules request failed (HTTP ${resp.status})${detail ? `: ${detail}` : ""}`,
    );
  }
  return (await resp.json()) as T;
}

/**
 * Render a job record (the shape returned by `enrichJob` / `waitForJob`) as a
 * readable status summary plus the raw JSON. Shared by `get_job` and
 * `wait_for_job` so both surface identical status/progress lines.
 */
function renderJob(job: Record<string, unknown>, fallbackId: string): string[] {
  const status = String(job.status ?? "?");
  const lines = [`Job ${String(job.id ?? fallbackId)} — ${status}`];
  // Top-level scalars worth surfacing as named lines.
  for (const k of ["type_name", "resolved_tier", "result_count"]) {
    if (job[k] !== undefined && job[k] !== null)
      lines.push(`  ${k}: ${String(job[k])}`);
  }
  // Live progress lives under `progress.*` (processed / filled / verified /
  // total / phase) for EVERY category — read the nested shape so a discovery
  // job's streaming progress is legible at a glance (ONTA-243).
  const progress = job.progress as Record<string, unknown> | undefined;
  if (progress && typeof progress === "object") {
    for (const k of ["phase", "processed", "filled", "verified", "total"]) {
      if (progress[k] !== undefined && progress[k] !== null)
        lines.push(`  ${k}: ${String(progress[k])}`);
    }
  }
  return lines;
}

export function registerAgentTools(server: McpServer): void {
  server.registerTool(
    "agent",
    {
      description:
        "Talk to the Infona Ask-AI agent — the single conversational front door " +
        "to a context graph. Send a natural-language message and the agent " +
        "classifies your intent and either ANSWERS a question directly, asks a " +
        "CLARIFYing question, or proposes a PLAN of actions (enrich attributes, " +
        "clean/normalize values, merge duplicates, inspect/extend the ontology). " +
        "A plan is NOT executed until you confirm it: call this tool again with " +
        "the returned plan_id as `confirm_plan_id`. Planning is free; any paid " +
        "step a plan contains (e.g. web enrichment) is authorized server-side at " +
        "execute time, so confirming honors your tenant's entitlements. Prefer " +
        "this over the lower-level tools for conversational, multi-step work.",
      inputSchema: {
        message: z
          .string()
          .optional()
          .describe(
            "Your natural-language message to the agent (e.g. 'how many mentors " +
              "speak Persian?' or 'enrich the company for managers'). Optional " +
              "when confirm_plan_id is set (a confirm turn carries no new message).",
          ),
        kg_name: z
          .string()
          .optional()
          .describe(
            "Context graph to operate within. Use list_knowledge_graphs to see " +
              "available KGs.",
          ),
        type_name: z
          .string()
          .optional()
          .describe(
            "Optional active type to scope the turn to (needed for enrich / clean " +
              "/ dedup planning, e.g. 'Mentor').",
          ),
        urls: z
          .array(z.string())
          .optional()
          .describe(
            "Optional explicit web page links to parse for this turn. When the " +
              "message asks to fill in attributes on existing records, the agent " +
              "extracts those values from these pages; otherwise it pulls a new " +
              "set of records from them. Plain http(s) URLs.",
          ),
        session_id: z
          .string()
          .optional()
          .describe(
            "Optional conversation id to keep multi-turn context across calls. " +
              "When omitted, the server threads a stable per-process id so " +
              "multi-turn context still accumulates and clarify-convergence " +
              "activates; pass an explicit id only to segment separate " +
              "conversations.",
          ),
        confirm_plan_id: z
          .string()
          .optional()
          .describe(
            "When set, CONFIRM and EXECUTE the previously-proposed plan with this " +
              "id (the only mutating path) instead of sending a new message. Use " +
              "the plan_id from a prior 'plan' result.",
          ),
        spend_ceiling_usd: z
          .number()
          .min(0)
          .optional()
          .describe(
            "Optional HARD per-run spend ceiling in USD for any paid " +
              "enrichment/discovery job this turn kicks off (ONTA-282/ONTA-378). " +
              "The job HALTS CLEANLY once its cumulative spend reaches this figure. " +
              "Omit for the deployment default; 0 means unlimited. Lets a caller " +
              "bound a single run without changing the global ceiling.",
          ),
      },
    },
    async ({
      message,
      kg_name,
      type_name,
      urls,
      session_id,
      confirm_plan_id,
      spend_ceiling_usd,
    }) => {
      try {
        const result = await client().agent({
          message,
          kgName: kg_name,
          typeName: type_name,
          urls,
          // Fall back to the per-process session id so turns stay on one thread
          // and the planner's clarify-convergence machinery activates even when
          // the caller does not supply its own conversation id.
          sessionId: session_id ?? DEFAULT_SESSION_ID,
          confirmPlanId: confirm_plan_id,
          spendCeilingUsd: spend_ceiling_usd,
        });
        return textResult(describeAgentResult(result));
      } catch (err) {
        return errorResult(err);
      }
    },
  );
  server.registerTool(
    "schedule",
    {
      description:
        "Set up a RECURRING standing alert / scheduled refresh, or list existing " +
        "ones. Use this when the user wants something to run ON A CADENCE (weekly, " +
        "daily, …) and be NOTIFIED automatically when watched values CHANGE — set " +
        "up ONCE, not re-run by hand ('a standing weekly alert that notifies my " +
        "orchestrator when a model changes price', 'a weekly refresh delivered to " +
        "me automatically'). It creates a recurring `notify` schedule that, each " +
        "run, snapshots the watched values and delivers a change payload to your " +
        "webhook ONLY when they changed since last run. Pass `action:\"list\"` to " +
        "see the tenant's schedules instead. This is a thin wrapper over the " +
        "canonical /graphs/{tenant}/schedules route (the same one the web app and " +
        "CLI use) — no bespoke endpoint.",
      inputSchema: {
        action: z
          .enum(["create", "list"])
          .default("create")
          .describe("`create` a new recurring alert (default) or `list` existing ones."),
        kg_name: z
          .string()
          .optional()
          .describe(
            "Context graph the alert watches. Required for `create`. Use " +
              "list_knowledge_graphs to see available KGs.",
          ),
        cadence: z
          .enum(["hourly", "daily", "weekly", "monthly"])
          .default("weekly")
          .describe("How often the alert runs (default weekly)."),
        condition: z
          .string()
          .optional()
          .describe(
            "Plain-language description of WHAT to watch for a change on (e.g. " +
              "'price or deprecation date on models I route to'). Recorded on the " +
              "schedule so the watch can be resolved to concrete values.",
          ),
        deliver_to: z
          .string()
          .optional()
          .describe(
            "An http(s) webhook URL to deliver change notifications to. When " +
              "omitted the schedule is still created but delivery is inactive until " +
              "a URL is added. The outbound POST is SSRF-guarded server-side.",
          ),
      },
    },
    async ({ action, kg_name, cadence, condition, deliver_to }) => {
      try {
        const c = client();
        if (action === "list") {
          const schedules = await readScheduleResponse<Schedule[]>(
            await c.raw.schedules(),
          );
          if (!schedules.length) return textResult("No schedules found.");
          const lines = schedules.map((s) => {
            const every = s.interval_seconds
              ? `every ${s.interval_seconds}s`
              : s.cron
                ? `cron ${s.cron}`
                : "?";
            const state = s.enabled ? "enabled" : "disabled";
            return `- ${s.id} [${s.action}] ${every} — ${state} (next: ${s.next_run ?? "?"})`;
          });
          return textResult(lines.join("\n"));
        }

        if (!kg_name) {
          return errorResult(
            new InfonaError("kg_name is required to create a schedule."),
          );
        }
        const intervalByCadence = {
          hourly: 3600,
          daily: 86_400,
          weekly: 604_800,
          monthly: 2_592_000,
        } as const;
        const params: Record<string, unknown> = {
          watch: { condition: condition ?? "" },
          condition: condition ?? "",
        };
        if (deliver_to) params.sink = { url: deliver_to };
        // Body matches the canonical POST /schedules contract. `category` is carried
        // for the model only (a notify fires no enrich-style job); enrichment is a
        // neutral default, mirroring the agent subscribe capability.
        const body = {
          kg_name,
          category: "enrichment" as JobCategory,
          action: "notify" as const,
          params,
          interval_seconds: intervalByCadence[cadence],
          enabled: true,
        };
        const created = await readScheduleResponse<Schedule>(
          await c.raw.createSchedule(body),
        );
        const lines = [
          `Created a standing ${cadence} alert on "${kg_name}" (schedule ${created.id}).`,
          `  next run: ${created.next_run ?? "?"}`,
          deliver_to
            ? `  delivers change notifications to: ${deliver_to}`
            : "  no delivery URL yet — add one to activate automatic delivery.",
          "",
          "It runs on its own and notifies only when the watched values change.",
        ];
        return textResult(lines.join("\n"));
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  server.registerTool(
    "list_jobs",
    {
      description:
        "List background jobs (enrichment, dedupe/merge, reconciliation, " +
        "web-discovery) for the tenant, newest first. Use this to check on async " +
        "work the `agent` tool kicked off (e.g. after confirming an enrich, " +
        "find-duplicates, or discover-from-the-web plan): a plan's steps run as " +
        "background jobs, and this is how you see their status. Pass no category " +
        "to see ALL jobs across every category.",
      inputSchema: {
        category: z
          .enum(JOB_CATEGORIES)
          .optional()
          .describe(
            "Optional filter to a single job category (enrichment, dedupe, " +
              "reconciliation, discovery). A web-ingest job kicked off via the " +
              "`agent` tool is category 'discovery'.",
          ),
      },
    },
    async ({ category }) => {
      try {
        const jobs = await client().jobs(category ? { category } : {});
        if (!jobs.length) return textResult("No jobs found.");
        const lines = jobs.map((j) => {
          const rec = j as unknown as Record<string, unknown>;
          const id = String(rec.id ?? "?");
          const cat = String(rec.category ?? "?");
          const status = String(rec.status ?? "?");
          const label = rec.label ?? rec.type_name ?? "";
          return `- ${id} [${cat}] ${status}${label ? ` — ${String(label)}` : ""}`;
        });
        return textResult(lines.join("\n"));
      } catch (err) {
        return errorResult(err);
      }
    },
  );
  server.registerTool(
    "get_job",
    {
      description:
        "Get the full record + progress of a single background job by id (as " +
        "listed by list_jobs). Works for ANY category — enrichment, dedupe, " +
        "reconciliation, and web-discovery (there is no separate discovery-job " +
        "endpoint). Returns status, tier, live per-record progress (processed / " +
        "filled / total + phase), and, when finished, the result count. This " +
        "returns INSTANTLY with the current status — to WAIT for a long-running " +
        "job to finish, use `wait_for_job` instead of polling this in a loop.",
      inputSchema: {
        job_id: z.string().describe("The job id (from list_jobs)."),
      },
    },
    async ({ job_id }) => {
      try {
        const job = (await client().enrichJob(job_id)) as unknown as Record<
          string,
          unknown
        >;
        const lines = renderJob(job, job_id);
        lines.push("", "Raw job:", JSON.stringify(job, null, 2));
        return textResult(lines.join("\n"));
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  server.registerTool(
    "wait_for_job",
    {
      description:
        "WAIT for a background job to finish, efficiently. Web-discovery and " +
        "enrichment jobs (kicked off via the `agent` tool) take MINUTES to " +
        "settle — do NOT poll get_job in a tight loop, which returns 'running' " +
        "instantly and wastes your turns. This tool blocks SERVER-SIDE until the " +
        "job reaches a terminal state (done / failed / cancelled / awaiting " +
        "review) or a bounded timeout, then returns its status + progress — so " +
        "ONE call covers a whole wait window. If it returns and the job is still " +
        "'running', just call wait_for_job AGAIN with the same job_id to keep " +
        "waiting; a few calls cover a multi-minute job. The graph is populated " +
        "INCREMENTALLY as the job runs, so you can also `ask` it for entities " +
        "landed so far before it fully finishes.",
      inputSchema: {
        job_id: z.string().describe("The job id (from list_jobs)."),
        timeout_s: z
          .number()
          .optional()
          .describe(
            "How long the SERVER should block, in seconds (default 60, capped " +
              "at 120 server-side). Omit for the default.",
          ),
      },
    },
    async ({ job_id, timeout_s }) => {
      try {
        const job = (await client().waitForJob(
          job_id,
          timeout_s,
        )) as unknown as Record<string, unknown>;
        const status = String(job.status ?? "?") as JobStatus;
        const lines = renderJob(job, job_id);
        lines.push("");
        if (isTerminalJobStatus(status)) {
          lines.push(
            `This job has settled (status: ${status}) — it is done and will not ` +
              `advance further.`,
          );
        } else {
          lines.push(
            `Still running (status: ${status}) after the wait window — the job ` +
              `has not finished yet. Call wait_for_job again with the same ` +
              `job_id to keep waiting, or ask the graph now for the entities ` +
              `landed so far.`,
          );
        }
        lines.push("", "Raw job:", JSON.stringify(job, null, 2));
        return textResult(lines.join("\n"));
      } catch (err) {
        return errorResult(err);
      }
    },
  );
}
