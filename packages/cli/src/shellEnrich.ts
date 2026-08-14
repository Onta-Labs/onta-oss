/** Interactive-shell enrichment commands: run, watch, jobs, review. */
import * as readline from "node:readline";
import { stdout } from "node:process";
import {
  Client,
  InfonaError,
  type ConflictReview,
  type EnrichJob,
  type EnrichmentTier,
  type JobSummary,
} from "./client.js";
import { resolveTypeName } from "./shellCore.js";
import {
  BOLD,
  CYAN,
  CYAN_BOLD,
  DIM,
  GREEN,
  RED,
  RESET,
  YELLOW,
  ask,
  fmtNum,
  lastUriSegment,
  printError,
  progressBar,
  relativeTime,
  startSpinner,
  statusColor,
} from "./shellUi.js";

export async function cmdEnrichRun(
  client: Client,
  kg: string,
  rl: readline.Interface,
  args: string[],
): Promise<void> {
  if (args.length < 2) {
    stdout.write(
      `  ${YELLOW}Usage:${RESET} /enrich <Type> <attr1> [<attr2> ...]\n`,
    );
    return;
  }
  const typeInput = args[0]!;
  const attrs = args.slice(1).map((a) => a.replace(/^\./, ""));
  const typeName = await resolveTypeName(client, kg, rl, typeInput);
  if (!typeName) return;

  const policy: "stage" = "stage";
  // The backend now picks the source ("auto"): free (Wikidata) vs paid web
  // search, asking us to clarify only when it's genuinely unsure. So we no
  // longer prompt up front or claim a fixed tier here.
  stdout.write(
    `\n  ${BOLD}Plan:${RESET} enrich ${CYAN}${typeName}${RESET}.${attrs
      .map((a) => `${CYAN}${a}${RESET}`)
      .join(`, .`)} in ${BOLD}${kg}${RESET}  ${DIM}·${RESET} ${DIM}auto-routing source…${RESET}\n\n`,
  );

  // Queue at tier "auto" and let the backend route. If it needs us to clarify,
  // re-queue with the explicit tier the user picks.
  let created = await queueEnrich(client, typeName, attrs, kg, policy, "auto");
  if (!created) return;

  if (created.needs_clarification || created.status === "needs_clarification") {
    if (created.routing_note) {
      stdout.write(`  ${DIM}${created.routing_note}${RESET}\n`);
    }
    const candidates = created.candidates ?? ["lite", "core"];
    const offersWeb = candidates.includes("core");
    const offersFree = candidates.includes("lite");
    const prompt = offersWeb && offersFree
      ? `  Source unclear — [w]eb search (paid) or [f]ree (Wikidata)? [w/f/c]: `
      : `  Pick a source — ${candidates.join(" / ")} [c]ancel: `;
    const ans = (await ask(rl, prompt)).trim().toLowerCase();
    let chosen: EnrichmentTier | null = null;
    if (ans === "c" || ans === "cancel") {
      stdout.write(`  ${DIM}Cancelled.${RESET}\n`);
      return;
    } else if (ans === "w" || ans === "web") {
      chosen = "core";
    } else if (ans === "f" || ans === "free") {
      chosen = "lite";
    } else if (candidates.includes(ans)) {
      chosen = ans as EnrichmentTier;
    } else {
      stdout.write(`  ${DIM}Cancelled.${RESET}\n`);
      return;
    }
    created = await queueEnrich(client, typeName, attrs, kg, policy, chosen);
    if (!created) return;
  } else {
    // A job was created — surface the routing decision so the user sees which
    // source ran and why. OSS chains for lite/base/core/pro are all Wikidata
    // unless a paid plugin registered adapters; never claim "live web search"
    // for a tier that only walks free sources (OSS dogfood S7).
    const sourceLabel =
      created.resolved_tier === "lite"
        ? "Wikidata (free)"
        : created.resolved_tier === "base" ||
            created.resolved_tier === "core" ||
            created.resolved_tier === "pro"
          ? `${created.resolved_tier} (registered adapters + Wikidata — no paid web search in OSS)`
          : String(created.resolved_tier ?? "auto");
    stdout.write(
      `  ${DIM}Source:${RESET} ${sourceLabel}${created.routing_note ? ` — ${created.routing_note}` : ""}\n`,
    );
  }

  if (!created.job_id) {
    printError("Backend did not return a job id.");
    return;
  }

  const cost = (created.estimated_cost_usd ?? 0).toFixed(4);
  stdout.write(
    `  ${GREEN}✓${RESET} Job queued: ${CYAN_BOLD}${created.job_id}${RESET} ${DIM}·${RESET} estimated cost ${BOLD}$${cost}${RESET} ${DIM}·${RESET} ${fmtNum(created.total_entities ?? 0)} entities\n`,
  );

  const resolvedTier: EnrichmentTier = created.resolved_tier ?? "core";
  const watch = (await ask(rl, `  Watch progress? [Y/n]: `)).trim().toLowerCase();
  if (watch === "" || watch === "y" || watch === "yes") {
    const finished = await watchJob(client, created.job_id);
    await maybeEscalateToWeb(client, rl, typeName, attrs, kg, policy, resolvedTier, finished);
  } else {
    stdout.write(
      `  ${DIM}Tip: /enrich watch ${created.job_id} to follow it.${RESET}\n`,
    );
  }
}

/**
 * Queue one enrichment job, with a spinner and error rendering. Returns the
 * create-response (which may carry a routing decision or a needs_clarification
 * flag) or null when the call failed.
 */
async function queueEnrich(
  client: Client,
  typeName: string,
  attrs: string[],
  kg: string,
  policy: "stage",
  tier: EnrichmentTier,
): Promise<import("./client.js").EnrichJobCreate | null> {
  const sp = startSpinner(`Queueing enrichment for ${typeName}...`);
  try {
    const created = await client.enrichRun({
      type_name: typeName,
      attributes: attrs,
      tier,
      kg_name: kg,
      conflict_policy: policy,
    });
    sp.stop();
    return created;
  } catch (err) {
    sp.stop();
    if (err instanceof InfonaError) printError(err.message);
    else printError(err instanceof Error ? err.message : String(err));
    return null;
  }
}

// ALL-MISS FALLBACK: if the backend routed to FREE (Wikidata, resolved_tier
// "lite") and that run found nothing — nothing filled/verified/conflicting AND
// at least one miss — offer to re-try at "core". In OSS, core is still the
// Wikidata chain unless a paid plugin registered web adapters, so we label
// honestly and skip the offer when core would re-run the same free source
// (OSS dogfood S7 — fake "paid web search" escalation).
async function maybeEscalateToWeb(
  client: Client,
  rl: readline.Interface,
  typeName: string,
  attrs: string[],
  kg: string,
  policy: "stage",
  resolvedTier: EnrichmentTier,
  finished: EnrichJob | null,
): Promise<void> {
  if (!finished) return;
  if (finished.status !== "applied" && finished.status !== "review") return;
  if (resolvedTier !== "lite") return;
  const p = finished.progress;
  if (p.filled + p.verified + p.conflicts > 0) return;
  if (p.no_match <= 0) return;

  // OSS: core === wikidata. Do not re-queue the same free chain under a
  // "live web search" label — tell the user nothing more is available.
  // (Paid web escalation lives in premium shells that register real adapters.)
  void client;
  void rl;
  void typeName;
  void attrs;
  void kg;
  void policy;
  stdout.write(
    `  ${DIM}Nothing found in Wikidata. In this OSS build higher tiers still use free sources only` +
      ` (no paid web adapters). Tip: re-run /enrich with attributes Wikidata covers` +
      ` (e.g. industry, headquarters, website, founded).${RESET}\n`,
  );
}

export async function watchJob(
  client: Client,
  jobId: string,
): Promise<EnrichJob | null> {
  const startedAt = Date.now();
  let lastJob: EnrichJob | null = null;
  // Render in place
  const draw = (job: EnrichJob): void => {
    const p = job.progress;
    const bar = progressBar(p.processed, p.total);
    const elapsed = Math.max(1, Math.floor((Date.now() - startedAt) / 1000));
    const rate = p.processed / elapsed;
    let etaStr = "—";
    if (rate > 0 && p.total > p.processed) {
      const remaining = Math.ceil((p.total - p.processed) / rate);
      etaStr =
        remaining < 60
          ? `${remaining}s`
          : remaining < 3600
            ? `${Math.floor(remaining / 60)}m`
            : `${Math.floor(remaining / 3600)}h`;
    }
    const sc = statusColor(job.status);
    stdout.write(
      `\r\x1b[2K  ${sc}${job.status}${RESET} ${bar} ${fmtNum(p.processed)}/${fmtNum(p.total)} ` +
        `${DIM}·${RESET} filled ${GREEN}${fmtNum(p.filled)}${RESET} ` +
        `${DIM}·${RESET} verified ${CYAN}${fmtNum(p.verified)}${RESET} ` +
        `${DIM}·${RESET} conflicts ${YELLOW}${fmtNum(p.conflicts)}${RESET} ` +
        `${DIM}·${RESET} not found ${DIM}${fmtNum(p.no_match)}${RESET} ` +
        `${DIM}·${RESET} ETA ${etaStr}`,
    );
  };

  while (true) {
    let job: EnrichJob;
    try {
      job = await client.enrichJob(jobId);
    } catch (err) {
      stdout.write("\r\x1b[2K");
      if (err instanceof InfonaError) printError(err.message);
      else printError(err instanceof Error ? err.message : String(err));
      return null;
    }
    lastJob = job;
    draw(job);
    if (job.status !== "running" && job.status !== "queued") break;
    await new Promise((r) => setTimeout(r, 1500));
  }

  // Final newline after the live line.
  stdout.write("\n");
  if (!lastJob) return null;
  const p = lastJob.progress;
  if (lastJob.status === "review") {
    stdout.write(
      `  ${YELLOW}✦${RESET} ${fmtNum(p.conflicts)} conflict${p.conflicts === 1 ? "" : "s"} need review ` +
        `${DIM}·${RESET} filled ${fmtNum(p.filled)}, verified ${fmtNum(p.verified)}, not found ${fmtNum(p.no_match)}. ` +
        `${DIM}Run${RESET} /enrich review ${lastJob.id}${DIM} to walk through them.${RESET}\n`,
    );
  } else if (lastJob.status === "applied") {
    stdout.write(
      `  ${GREEN}✓${RESET} Applied ${DIM}·${RESET} filled ${fmtNum(p.filled)}, verified ${fmtNum(p.verified)}, not found ${fmtNum(p.no_match)}\n`,
    );
  } else if (lastJob.status === "failed") {
    printError(`Job failed: ${lastJob.error ?? "(no error message)"}`);
  } else if (lastJob.status === "cancelled") {
    stdout.write(`  ${DIM}Job cancelled.${RESET}\n`);
  }
  return lastJob;
}

export async function cmdEnrichJobs(client: Client): Promise<void> {
  const sp = startSpinner("Loading enrichment jobs...");
  let jobs: JobSummary[];
  try {
    jobs = await client.enrichJobs();
  } catch (err) {
    sp.stop();
    if (err instanceof InfonaError) printError(err.message);
    else printError(err instanceof Error ? err.message : String(err));
    return;
  }
  sp.stop();

  if (jobs.length === 0) {
    stdout.write(`  ${DIM}No enrichment jobs yet.${RESET}\n`);
    return;
  }

  const truncAttrs = (attrs: string[]): string => {
    const max = 30;
    const joined = attrs.join(", ");
    if (joined.length <= max) return joined;
    return joined.slice(0, max - 1) + "…";
  };

  const rows = jobs.map((j) => ({
    id: j.id,
    type: j.type_name,
    attrs: truncAttrs(j.attributes ?? []),
    status: j.status,
    progress: `${fmtNum(j.progress?.processed ?? 0)}/${fmtNum(j.progress?.total ?? 0)}`,
    created: relativeTime(j.created_at),
  }));

  const w = {
    id: Math.max("ID".length, ...rows.map((r) => r.id.length)),
    type: Math.max("Type".length, ...rows.map((r) => r.type.length)),
    attrs: Math.max("Attrs".length, ...rows.map((r) => r.attrs.length)),
    status: Math.max("Status".length, ...rows.map((r) => r.status.length)),
    progress: Math.max("Progress".length, ...rows.map((r) => r.progress.length)),
  };

  stdout.write("\n");
  stdout.write(
    `  ${BOLD}${"ID".padEnd(w.id)}  ${"Type".padEnd(w.type)}  ${"Attrs".padEnd(w.attrs)}  ${"Status".padEnd(w.status)}  ${"Progress".padEnd(w.progress)}  Created${RESET}\n`,
  );
  for (const r of rows) {
    const sc = statusColor(r.status);
    stdout.write(
      `  ${CYAN}${r.id.padEnd(w.id)}${RESET}  ${r.type.padEnd(w.type)}  ${DIM}${r.attrs.padEnd(w.attrs)}${RESET}  ${sc}${r.status.padEnd(w.status)}${RESET}  ${r.progress.padEnd(w.progress)}  ${DIM}${r.created}${RESET}\n`,
    );
  }
  stdout.write("\n");
}

export async function cmdEnrichReview(
  client: Client,
  rl: readline.Interface,
  jobId: string,
): Promise<void> {
  if (!jobId) {
    stdout.write(`  ${YELLOW}Usage:${RESET} /enrich review <job_id>\n`);
    return;
  }
  const sp = startSpinner(`Loading conflicts for ${jobId}...`);
  let conflicts: ConflictReview[];
  try {
    conflicts = await client.enrichConflicts(jobId);
  } catch (err) {
    sp.stop();
    if (err instanceof InfonaError) printError(err.message);
    else printError(err instanceof Error ? err.message : String(err));
    return;
  }
  sp.stop();

  if (conflicts.length === 0) {
    stdout.write(`  ${DIM}No conflicts to review.${RESET}\n`);
    return;
  }

  const decisions: ConflictReview[] = [];
  let acceptAll = false;
  let quitEarly = false;

  for (let i = 0; i < conflicts.length; i++) {
    const c = conflicts[i]!;
    const entity = lastUriSegment(c.entity_uri);
    const conf = (c.proposed?.confidence ?? 0).toFixed(2);
    stdout.write("\n");
    stdout.write(
      `  ${DIM}[${i + 1}/${conflicts.length}]${RESET} ${BOLD}${entity}${RESET}.${CYAN}${c.attribute}${RESET}\n`,
    );
    stdout.write(
      `    ${DIM}existing →${RESET} ${c.existing_value}\n` +
        `    ${DIM}proposed →${RESET} ${BOLD}${c.proposed?.value ?? ""}${RESET} ${DIM}(conf ${conf}, src ${c.proposed?.source ?? "?"})${RESET}\n`,
    );
    if (c.proposed?.source_url) {
      stdout.write(`    ${DIM}url      →${RESET} ${c.proposed.source_url}\n`);
    }

    let decision: "accept" | "reject" | "skip";
    if (acceptAll) {
      decision = "accept";
      stdout.write(`    ${GREEN}auto-accepted${RESET}\n`);
    } else {
      const ans = (
        await ask(
          rl,
          `    [a]ccept / [r]eject / [s]kip / [A]ccept all remaining / [q]uit (saves progress) [s]: `,
        )
      ).trim();
      if (ans === "A") {
        acceptAll = true;
        decision = "accept";
      } else if (ans === "a") {
        decision = "accept";
      } else if (ans === "r") {
        decision = "reject";
      } else if (ans === "q") {
        quitEarly = true;
        break;
      } else {
        decision = "skip";
      }
    }
    decisions.push({ ...c, decision });
  }

  if (quitEarly) {
    if (decisions.length === 0) {
      stdout.write(`  ${DIM}No decisions made — nothing to save.${RESET}\n`);
      return;
    }
    const save = (
      await ask(rl, `  Save ${decisions.length} decision(s) so far? [Y/n]: `)
    )
      .trim()
      .toLowerCase();
    if (save !== "" && save !== "y" && save !== "yes") {
      stdout.write(`  ${DIM}Discarded.${RESET}\n`);
      return;
    }
  }

  if (decisions.length === 0) {
    stdout.write(`  ${DIM}No decisions to apply.${RESET}\n`);
    return;
  }

  const sp2 = startSpinner(`Applying ${decisions.length} decision(s)...`);
  try {
    const res = await client.enrichApply(jobId, decisions);
    sp2.stop();
    stdout.write(
      `  ${GREEN}✓${RESET} Applied ${BOLD}${fmtNum(res.applied)}${RESET} change${res.applied === 1 ? "" : "s"}.\n`,
    );
  } catch (err) {
    sp2.stop();
    if (err instanceof InfonaError) printError(err.message);
    else printError(err instanceof Error ? err.message : String(err));
  }
}
