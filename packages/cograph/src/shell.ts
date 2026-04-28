import * as readline from "node:readline";
import { stdin, stdout } from "node:process";
import { Client, CographError } from "./client.js";

const CYAN = "\x1b[36m";
const CYAN_BOLD = "\x1b[1;36m";
const DIM = "\x1b[2m";
const RED = "\x1b[31m";
const GREEN = "\x1b[32m";
const YELLOW = "\x1b[33m";
const BOLD = "\x1b[1m";
const RESET = "\x1b[0m";

function fmtNum(n: number): string {
  return n.toLocaleString("en-US");
}

function showBanner(): void {
  const lines = [
    "",
    `${CYAN}    ░█▀▀░█▀█░█▀▀░█▀▄░█▀█░█▀█░█░█${RESET}`,
    `${CYAN}    ░█░░░█░█░█░█░█▀▄░█▀█░█▀▀░█▀█${RESET}`,
    `${CYAN}    ░▀▀▀░▀▀▀░▀▀▀░▀░▀░▀░▀░▀░░░▀░▀${RESET}`,
    "",
    `${DIM}    The object graph for AI agents${RESET}`,
    "",
  ];
  for (const l of lines) stdout.write(l + "\n");
  showCommands();
}

function showCommands(): void {
  const rows: Array<[string, string]> = [
    ["/ingest <file> ...", "Ingest a CSV/JSON/text file"],
    ["/ask <question>", "Ask in natural language"],
    ["/login", "Re-authenticate (browser)"],
    ["/status", "Show graph stats"],
    ["/reset", "Clear the current KG"],
    ["/help", "Show this command list"],
    ["/quit", "Exit"],
  ];
  const colWidth = Math.max(...rows.map((r) => r[0].length));
  for (const [cmd, desc] of rows) {
    const pad = " ".repeat(colWidth - cmd.length);
    stdout.write(`    ${CYAN_BOLD}${cmd}${RESET}${pad}   ${DIM}${desc}${RESET}\n`);
  }
  stdout.write("\n");
}

function printError(msg: string): void {
  stdout.write(`  ${RED}✗${RESET} ${msg}\n`);
}

interface KgInfo {
  name: string;
  triple_count: number;
}

async function fetchKg(client: Client, name: string): Promise<KgInfo | null> {
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

function ask(rl: readline.Interface, prompt: string): Promise<string> {
  return new Promise((resolve) => {
    rl.question(prompt, (answer) => resolve(answer));
  });
}

async function selectKg(
  client: Client,
  rl: readline.Interface,
): Promise<string | null> {
  let kgs: Array<Record<string, unknown>> = [];
  try {
    kgs = await client.listKgs();
  } catch (err) {
    printError(
      `Could not list knowledge graphs: ${err instanceof Error ? err.message : String(err)}`,
    );
    return null;
  }

  if (kgs.length === 0) {
    stdout.write(
      `  ${DIM}No knowledge graphs found. Enter a name to create your first KG.${RESET}\n`,
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
        printError(`Could not create knowledge graph: ${msg}`);
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

  stdout.write(`  ${BOLD}Available knowledge graphs:${RESET}\n`);
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

async function cmdIngest(
  client: Client,
  kg: string,
  args: string[],
): Promise<void> {
  if (args.length === 0) {
    stdout.write(`  ${YELLOW}Usage:${RESET} /ingest <file> [<file>...]\n`);
    return;
  }
  for (const file of args) {
    stdout.write(`  ${DIM}${file}${RESET}\n`);
    try {
      const result = await client.ingest(file, { kg });
      const ents =
        (result as { entities_resolved?: number }).entities_resolved ?? 0;
      const trip =
        (result as { triples_inserted?: number }).triples_inserted ?? 0;
      stdout.write(
        `  ${GREEN}✓${RESET} ${fmtNum(ents)} entities · ${fmtNum(trip)} triples\n`,
      );
    } catch (err) {
      if (err instanceof CographError) printError(err.message);
      else printError(err instanceof Error ? err.message : String(err));
    }
  }
}

async function cmdAsk(
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
    stdout.write(`  ${answer}\n`);
    stdout.write("\n");
  } catch (err) {
    if (err instanceof CographError) printError(err.message);
    else printError(err instanceof Error ? err.message : String(err));
  }
}

async function cmdStatus(client: Client, kg: string): Promise<void> {
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
    if (err instanceof CographError) printError(err.message);
    else printError(err instanceof Error ? err.message : String(err));
  }
}

async function cmdReset(
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
    if (err instanceof CographError) printError(err.message);
    else printError(err instanceof Error ? err.message : String(err));
    return false;
  }
}

function makePrompt(triples: number): string {
  if (triples > 0) {
    return `  ${CYAN_BOLD}cograph${RESET} ${DIM}[${fmtNum(triples)}]${RESET} ${CYAN_BOLD}▸${RESET} `;
  }
  return `  ${CYAN_BOLD}cograph ▸${RESET} `;
}

/**
 * Split a command-line style argument string. Supports double-quoted args.
 */
function splitArgs(s: string): string[] {
  const out: string[] = [];
  let cur = "";
  let inQ = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (inQ) {
      if (c === '"') inQ = false;
      else cur += c;
    } else {
      if (c === '"') inQ = true;
      else if (c === " " || c === "\t") {
        if (cur) {
          out.push(cur);
          cur = "";
        }
      } else cur += c;
    }
  }
  if (cur) out.push(cur);
  return out;
}

export async function runShell(opts: { kg?: string }): Promise<void> {
  // `let` rather than `const` so /login can swap in a fresh Client after
  // ~/.cograph/config.json is rewritten with the new key.
  let client = new Client();
  const rl = readline.createInterface({
    input: stdin,
    output: stdout,
    terminal: true,
  });

  showBanner();

  let kg = opts.kg;
  if (!kg) {
    const picked = await selectKg(client, rl);
    if (!picked) {
      rl.close();
      return;
    }
    kg = picked;
  }

  let triples = 0;
  const info = await fetchKg(client, kg);
  if (info && info.triple_count > 0) {
    triples = info.triple_count;
    stdout.write(
      `  ${DIM}Connected to${RESET} ${BOLD}${kg}${RESET}${DIM}: ${fmtNum(triples)} triples${RESET}\n\n`,
    );
  } else {
    stdout.write(
      `  ${DIM}Connected — ${kg} is empty (use /ingest to add data)${RESET}\n\n`,
    );
  }

  const refresh = async (): Promise<void> => {
    const fresh = await fetchKg(client, kg!);
    triples = fresh?.triple_count ?? 0;
  };

  let running = true;
  rl.on("close", () => {
    running = false;
  });

  while (running) {
    let line: string;
    try {
      line = (await ask(rl, makePrompt(triples))).trim();
    } catch {
      break;
    }
    if (!running) break;
    if (!line) continue;

    if (line === "/quit" || line === "/exit" || line === "/q") {
      stdout.write(`  ${DIM}Bye.${RESET}\n`);
      break;
    }

    if (line === "/help") {
      showCommands();
      continue;
    }

    try {
      if (line.startsWith("/ingest")) {
        const args = splitArgs(line.slice("/ingest".length).trim());
        await cmdIngest(client, kg, args);
        await refresh();
      } else if (line.startsWith("/ask ")) {
        await cmdAsk(client, kg, line.slice("/ask ".length));
      } else if (line === "/ask") {
        await cmdAsk(client, kg, "");
      } else if (line === "/status") {
        await cmdStatus(client, kg);
        await refresh();
      } else if (line === "/reset") {
        const did = await cmdReset(client, kg, rl);
        if (did) await refresh();
      } else if (line === "/login") {
        const { runLogin } = await import("./login.js");
        await runLogin();
        // Pick up the new key from ~/.cograph/config.json for subsequent calls.
        client = new Client();
        await refresh();
      } else if (line.startsWith("/")) {
        stdout.write(
          `  ${YELLOW}Unknown command.${RESET} Try /ingest, /ask, /login, /status, /reset, /help, /quit\n`,
        );
      } else {
        // Bare line — auto-route to /ask
        await cmdAsk(client, kg, line);
      }
    } catch (err) {
      if (err instanceof CographError) printError(err.message);
      else printError(err instanceof Error ? err.message : String(err));
    }
  }

  rl.close();
}
