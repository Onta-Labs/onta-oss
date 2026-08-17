/** Interactive-shell core commands: ingest, ask, agent, types, kg helpers. */
import * as readline from "node:readline";
import { stdout } from "node:process";
import { Client, type TypeCount } from "./client.js";
import { renderAgentResult } from "./agentRender.js";
import {
  diagnoseAskResult,
  formatFirstHourFailure,
} from "./firstHourErrors.js";
import {
  BOLD,
  CYAN,
  DIM,
  GREEN,
  RESET,
  YELLOW,
  ask,
  fmtNum,
  printError,
  splitArgs,
  startSpinner,
} from "./shellUi.js";

interface KgInfo {
  name: string;
  triple_count: number;
}

async function printMapped(
  client: Client,
  err: unknown,
  kg?: string,
): Promise<void> {
  printError(
    await formatFirstHourFailure(err, {
      baseUrl: client.baseUrl,
      hasApiKey: Boolean(client.apiKey),
      kg,
    }),
  );
}

export async function fetchKg(client: Client, name: string): Promise<KgInfo | null> {
  try {
    const kgs = await client.listKgs();
    const found = kgs.find((k) => (k as { name?: string }).name === name);
    if (!found) return null;
    const tc = (found as { triple_count?: number }).triple_count ?? 0;
    return { name, triple_count: typeof tc === "number" ? tc : 0 };
  } catch {
    return null;
  }
}

export async function selectKg(
  client: Client,
  rl: readline.Interface,
): Promise<string | null> {
  let kgs: Array<Record<string, unknown>> = [];
  try {
    kgs = await client.listKgs();
  } catch (err) {
    await printMapped(client, err);
    return null;
  }

  if (kgs.length === 0) {
    stdout.write(
      `  ${DIM}No context graphs found. Enter a name to create your first KG.${RESET}\n`,
    );
    const name = (await ask(rl, "  KG name: ")).trim();
    if (!name) return null;
    // Persist immediately. Without this, the name only existed as a local
    // string until the user ran /ingest, so quitting before ingesting lost
    // the KG entirely — and the next shell session showed "No KGs found"
    // again.
    try {
      await client.createKg(name);
      stdout.write(`  ${GREEN}✓${RESET} Created ${BOLD}${name}${RESET}\n`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      // 409 / "already exists" is fine — someone created it between listKgs
      // and now, or the user retried. Anything else is a real failure.
      if (!/already exists|409/i.test(msg)) {
        printError(`Could not create context graph: ${msg}`);
        return null;
      }
    }
    return name;
  }

  if (kgs.length === 1) {
    const only = (kgs[0] as { name?: string }).name;
    if (only) {
      stdout.write(`  ${DIM}Using only available KG: ${BOLD}${only}${RESET}\n`);
      return only;
    }
  }

  stdout.write(`  ${BOLD}Available context graphs:${RESET}\n`);
  kgs.forEach((kg, i) => {
    const n = (kg as { name?: string }).name ?? "?";
    const tc = (kg as { triple_count?: number }).triple_count ?? 0;
    stdout.write(`    ${CYAN}${i + 1}${RESET}. ${n} ${DIM}(${fmtNum(tc)} triples)${RESET}\n`);
  });
  const pick = (await ask(rl, "  Select KG [1]: ")).trim() || "1";
  const idx = Number.parseInt(pick, 10);
  if (Number.isFinite(idx) && idx >= 1 && idx <= kgs.length) {
    const name = (kgs[idx - 1] as { name?: string }).name;
    if (name) return name;
  }
  // Allow typing a name directly
  if (pick && !/^\d+$/.test(pick)) return pick;
  printError("Invalid selection.");
  return null;
}

export async function cmdIngest(
  client: Client,
  kg: string,
  args: string[],
): Promise<void> {
  if (args.length === 0) {
    stdout.write(`  ${YELLOW}Usage:${RESET} /ingest <file> [<file>...]\n`);
    return;
  }
  for (const file of args) {
    const sp = startSpinner(`Inferring schema from ${file}...`);
    try {
      const result = await client.ingest(file, {
        kg,
        onProgress: ({
          rowsProcessed,
          totalRows,
          entitiesResolved,
          triplesInserted,
        }) => {
          const pct = Math.round((rowsProcessed / totalRows) * 100);
          sp.setText(
            `Ingesting ${file} ${DIM}·${RESET} ${BOLD}${pct}%${RESET} ` +
              `${DIM}(${fmtNum(rowsProcessed)}/${fmtNum(totalRows)} rows · ` +
              `${fmtNum(entitiesResolved)} entities · ${fmtNum(triplesInserted)} triples)${RESET}`,
          );
        },
      });
      sp.stop();
      const ents =
        (result as { entities_resolved?: number }).entities_resolved ?? 0;
      const trip =
        (result as { triples_inserted?: number }).triples_inserted ?? 0;
      stdout.write(
        `  ${GREEN}✓${RESET} ${file} ${DIM}·${RESET} ${fmtNum(ents)} entities · ${fmtNum(trip)} triples\n`,
      );
    } catch (err) {
      sp.stop();
      await printMapped(client, err, kg);
    }
  }
}

export async function cmdAsk(
  client: Client,
  kg: string,
  question: string,
): Promise<void> {
  const q = question.trim();
  if (!q) {
    stdout.write(`  ${YELLOW}Usage:${RESET} /ask <your question>\n`);
    return;
  }
  try {
    const result = await client.ask(q, { kg });
    const answer =
      (result as { narrative_answer?: string }).narrative_answer ||
      (result as { answer?: string }).answer ||
      "No answer generated.";
    stdout.write("\n");
    stdout.write(`  ${diagnoseAskResult(answer, kg)}\n`);
    stdout.write("\n");
  } catch (err) {
    await printMapped(client, err, kg);
  }
}

/**
 * `/agent <message>` — one turn of the unified Ask-AI agent inside the REPL.
 *
 * Sends the message (threading the per-session `sessionId` for multi-turn
 * continuity), renders the kind-tagged response with the shared renderer, and —
 * because the shell IS interactive — when the response is a `plan`, prompts
 * `Confirm & run? [y/N]`. On `y` it confirms the plan (the only mutating path)
 * and renders the `result`. Mirrors the cli.ts agent command, but the confirm
 * is an inline prompt rather than --yes/--confirm.
 */
export async function cmdAgent(
  client: Client,
  kg: string,
  rl: readline.Interface,
  sessionId: string,
  message: string,
): Promise<void> {
  const msg = message.trim();
  if (!msg) {
    stdout.write(`  ${YELLOW}Usage:${RESET} /agent <your message>\n`);
    return;
  }
  const context = { kgName: kg, sessionId };
  const sp = startSpinner("Thinking...");
  let result;
  try {
    result = await client.agent({ message: msg, ...context });
  } catch (err) {
    sp.stop();
    await printMapped(client, err);
    return;
  }
  sp.stop();
  renderAgentResult(result);

  // Only a plan awaits confirmation. Prompt inline; on "y", confirm + execute.
  if (result.kind === "plan") {
    const planId =
      typeof result.plan_id === "string" ? result.plan_id : undefined;
    if (!planId) return;
    const ans = (await ask(rl, `  ${YELLOW}Confirm & run?${RESET} [y/N]: `))
      .trim()
      .toLowerCase();
    if (ans !== "y" && ans !== "yes") {
      stdout.write(`  ${DIM}Not run. Plan ${planId} kept.${RESET}\n`);
      return;
    }
    const sp2 = startSpinner("Running plan...");
    let executed;
    try {
      executed = await client.agent({ confirmPlanId: planId, ...context });
    } catch (err) {
      sp2.stop();
      await printMapped(client, err, kg);
      return;
    }
    sp2.stop();
    renderAgentResult(executed);
  }
}

export async function cmdStatus(client: Client, kg: string): Promise<void> {
  try {
    const info = await fetchKg(client, kg);
    stdout.write("\n");
    stdout.write(`  ${BOLD}KG${RESET}       ${kg}\n`);
    if (info) {
      stdout.write(`  ${BOLD}Triples${RESET}  ${fmtNum(info.triple_count)}\n`);
    } else {
      stdout.write(`  ${BOLD}Triples${RESET}  ${DIM}(empty)${RESET}\n`);
    }
    try {
      const types = await client.ontologyTypes();
      const names = types
        .map((t) => (t as { name?: string }).name)
        .filter((n): n is string => Boolean(n));
      if (names.length > 0) {
        stdout.write(`  ${BOLD}Types${RESET}    ${names.join(", ")}\n`);
      } else {
        stdout.write(`  ${BOLD}Types${RESET}    ${DIM}(none)${RESET}\n`);
      }
    } catch (err) {
      printError(
        `Could not list ontology types: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
    stdout.write("\n");
  } catch (err) {
    await printMapped(client, err);
  }
}

export async function cmdReset(
  client: Client,
  kg: string,
  rl: readline.Interface,
): Promise<boolean> {
  const confirm = (
    await ask(rl, `  ${YELLOW}Delete KG "${kg}"?${RESET} [y/N]: `)
  )
    .trim()
    .toLowerCase();
  if (confirm !== "y" && confirm !== "yes") {
    stdout.write(`  ${DIM}Cancelled.${RESET}\n`);
    return false;
  }
  try {
    await client.deleteKg(kg);
    stdout.write(`  ${GREEN}✓${RESET} Graph cleared.\n`);
    return true;
  } catch (err) {
    await printMapped(client, err);
    return false;
  }
}

export async function cmdTypes(
  client: Client,
  kg: string,
  query: string,
): Promise<void> {
  const sp = startSpinner(
    query ? `Searching types matching "${query}"...` : "Loading types...",
  );
  let types: TypeCount[];
  try {
    types = await client.typeCounts(kg);
  } catch (err) {
    sp.stop();
    await printMapped(client, err);
    return;
  }
  sp.stop();

  const q = query.trim().toLowerCase();
  const filtered = q
    ? types.filter((t) => t.name.toLowerCase().includes(q))
    : types;

  if (filtered.length === 0) {
    if (types.length === 0) {
      stdout.write(
        `  ${DIM}No types yet in ${BOLD}${kg}${RESET}${DIM}. Try ${RESET}/ingest <file>${DIM} first.${RESET}\n`,
      );
    } else {
      stdout.write(
        `  ${DIM}No types match "${query}". Try ${RESET}/types${DIM} for the full list.${RESET}\n`,
      );
    }
    return;
  }

  // Right-align counts; leave room for the longest name we'll print.
  const nameWidth = Math.max(
    "Type".length,
    ...filtered.map((t) => t.name.length),
  );
  const countWidth = Math.max(
    "Entities".length,
    ...filtered.map((t) => fmtNum(t.entity_count).length),
  );
  stdout.write("\n");
  stdout.write(
    `  ${BOLD}${"Type".padEnd(nameWidth)}   ${"Entities".padStart(countWidth)}${RESET}\n`,
  );
  let total = 0;
  for (const t of filtered) {
    total += t.entity_count;
    stdout.write(
      `  ${CYAN}${t.name.padEnd(nameWidth)}${RESET}   ${fmtNum(t.entity_count).padStart(countWidth)}\n`,
    );
  }
  stdout.write("\n");
  const summary = q
    ? `${filtered.length} match${filtered.length === 1 ? "" : "es"}.`
    : `${filtered.length} type${filtered.length === 1 ? "" : "s"}, ${fmtNum(total)} entities total.`;
  stdout.write(`  ${DIM}${summary}${RESET}\n`);
  stdout.write(
    `  ${DIM}Drill in:  ${RESET}/type <name>${DIM}   Filter:  ${RESET}/types <query>${DIM}${RESET}\n\n`,
  );
}

/**
 * Resolve a user-supplied type name to a canonical type. Case-insensitive
 * exact match wins; otherwise we fall back to prefix match. If multiple
 * types share a prefix, prompt the user to pick from a numbered list.
 */
export async function resolveTypeName(
  client: Client,
  kg: string,
  rl: readline.Interface,
  input: string,
): Promise<string | null> {
  const types = await client.typeCounts(kg);
  if (types.length === 0) {
    printError(`No types in ${kg} yet. Try /ingest <file> first.`);
    return null;
  }
  const q = input.trim().toLowerCase();
  const exact = types.find((t) => t.name.toLowerCase() === q);
  if (exact) return exact.name;
  const prefix = types.filter((t) => t.name.toLowerCase().startsWith(q));
  const matches = prefix.length > 0
    ? prefix
    : types.filter((t) => t.name.toLowerCase().includes(q));
  if (matches.length === 0) {
    printError(
      `No type matches "${input}". Try /types to see what's available.`,
    );
    return null;
  }
  if (matches.length === 1) return matches[0]!.name;
  stdout.write(`  ${DIM}Multiple types match "${input}":${RESET}\n`);
  matches.forEach((t, i) => {
    stdout.write(
      `    ${CYAN}${i + 1}${RESET}. ${BOLD}${t.name}${RESET} ${DIM}(${fmtNum(t.entity_count)} entities)${RESET}\n`,
    );
  });
  const pick = (await ask(rl, `  Pick [1]: `)).trim() || "1";
  const idx = Number.parseInt(pick, 10);
  if (Number.isFinite(idx) && idx >= 1 && idx <= matches.length) {
    return matches[idx - 1]!.name;
  }
  printError("Invalid selection.");
  return null;
}

export async function cmdType(
  client: Client,
  kg: string,
  rl: readline.Interface,
  input: string,
): Promise<void> {
  // Pull off any --system flag so the rest can be treated as the type name.
  // Conservative parse: only the literal flag, anywhere in the input.
  const tokens = splitArgs(input.trim());
  const includeSystem = tokens.includes("--system");
  const nameTokens = tokens.filter((t) => t !== "--system");
  const nameInput = nameTokens.join(" ").trim();
  if (!nameInput) {
    stdout.write(`  ${YELLOW}Usage:${RESET} /type <name> [--system]\n`);
    return;
  }
  const name = await resolveTypeName(client, kg, rl, nameInput);
  if (!name) return;

  const sp = startSpinner(`Loading ${name}...`);
  let usage;
  try {
    usage = await client.typeUsage(kg, name, { includeSystem });
  } catch (err) {
    sp.stop();
    await printMapped(client, err);
    return;
  }
  sp.stop();

  const total = usage.entity_count;
  const pct = (n: number): string =>
    total > 0 ? `${Math.round((n / total) * 100).toString().padStart(3)}%` : "  —";

  // Dedup: when the resolver produces both a literal attribute and a typed
  // relationship for the same column (e.g. .title literal + .title→JobTitle),
  // we collapse to a single relationship row and surface the literal count
  // as a "(+775 string)" annotation. The relationship row "wins" because
  // its count is the union upper bound (every entity with a typed link)
  // and it's the richer fact. Pure literals and pure relationships are
  // unaffected.
  const relNames = new Set(usage.relationships.map((r) => r.name));
  const attrLitByName = new Map(usage.attributes.map((a) => [a.name, a]));
  const litOnlyAttrs = usage.attributes.filter((a) => !relNames.has(a.name));

  stdout.write("\n");
  stdout.write(
    `  ${BOLD}${usage.name}${RESET}  ${DIM}${fmtNum(total)} entities${RESET}\n`,
  );
  if (usage.description) {
    stdout.write(`  ${DIM}${usage.description}${RESET}\n`);
  }
  if (usage.parent_type) {
    stdout.write(`  ${DIM}subClassOf  ${usage.parent_type}${RESET}\n`);
  }

  if (litOnlyAttrs.length > 0) {
    stdout.write(
      `\n  ${BOLD}Attributes (${litOnlyAttrs.length})${RESET}\n`,
    );
    const nameW = Math.max(
      ...litOnlyAttrs.map((a) => a.name.length + 1),
      8,
    );
    const typeW = Math.max(
      ...litOnlyAttrs.map((a) => a.datatype.length),
      8,
    );
    const cntW = Math.max(
      ...litOnlyAttrs.map((a) => fmtNum(a.count).length),
      4,
    );
    for (const a of litOnlyAttrs) {
      const dotName = `.${a.name}`;
      stdout.write(
        `    ${CYAN}${dotName.padEnd(nameW)}${RESET}  ${DIM}${a.datatype.padEnd(typeW)}${RESET}  ${fmtNum(a.count).padStart(cntW)}  ${DIM}(${pct(a.count)})${RESET}\n`,
      );
    }
  }

  if (usage.relationships.length > 0) {
    stdout.write(
      `\n  ${BOLD}Relationships (${usage.relationships.length})${RESET}\n`,
    );
    const nameW = Math.max(
      ...usage.relationships.map((r) => r.name.length + 1),
      8,
    );
    const tgtW = Math.max(
      ...usage.relationships.map((r) => (r.target_type ?? "?").length),
      6,
    );
    for (const r of usage.relationships) {
      const dotName = `.${r.name}`;
      const tgt = r.target_type ?? "?";
      const lit = attrLitByName.get(r.name);
      const litNote = lit
        ? ` ${DIM}(+${fmtNum(lit.count)} ${lit.datatype})${RESET}`
        : "";
      stdout.write(
        `    ${CYAN}${dotName.padEnd(nameW)}${RESET}  ${DIM}→${RESET} ${BOLD}${tgt.padEnd(tgtW)}${RESET}  ${fmtNum(r.count).padStart(6)}  ${DIM}(${pct(r.count)})${RESET}${litNote}\n`,
      );
    }
  }

  if (usage.samples.length > 0) {
    stdout.write(`\n  ${BOLD}Sample entities${RESET}\n`);
    usage.samples.forEach((s, i) => {
      const label = s.label || s.uri.split("/").pop() || s.uri;
      stdout.write(`    ${DIM}${i + 1}.${RESET} ${label}\n`);
    });
  }

  if (
    usage.attributes.length === 0 &&
    usage.relationships.length === 0 &&
    total === 0
  ) {
    stdout.write(
      `\n  ${DIM}Type defined in the ontology but no instances yet in ${kg}.${RESET}\n`,
    );
  }
  stdout.write("\n");
}
