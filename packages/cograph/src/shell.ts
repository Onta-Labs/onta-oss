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

function canRenderBlockArt(): boolean {
  // Apple_Terminal (macOS Terminal.app) treats the block-shade chars (▀█░)
  // we use in the banner as East Asian Ambiguous Width = 2 cells, so each
  // 28-char banner row renders as ~56 cells and wraps mid-letter. iTerm,
  // WezTerm, Kitty, VS Code, Cursor, etc. all treat them as 1 cell and
  // render the art correctly. Skip the banner on Apple_Terminal and show
  // a plain header instead. Force on/off via COGRAPH_BANNER=on|off.
  const force = process.env.COGRAPH_BANNER;
  if (force === "on") return true;
  if (force === "off") return false;
  if (!process.stdout.isTTY) return false;
  if (process.env.TERM_PROGRAM === "Apple_Terminal") return false;
  return true;
}

function showBanner(): void {
  if (canRenderBlockArt()) {
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
  } else {
    stdout.write(`\n  ${CYAN_BOLD}cograph${RESET}\n`);
    stdout.write(`  ${DIM}The object graph for AI agents${RESET}\n\n`);
  }
  showCommands();
}

function showCommands(): void {
  const rows: Array<[string, string]> = [
    ["/ingest <file> ...", "Ingest a CSV/JSON/text file"],
    ["/ask <question>", "Ask in natural language"],
    ["/kg list", "List your knowledge graphs"],
    ["/kg switch <name>", "Switch to a different KG"],
    ["/kg create <name>", "Create a new KG and switch to it"],
    ["/kg delete <name>", "Delete a KG (irreversible)"],
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

/**
 * Tiny live-line spinner. Returns handles to update the trailing text and
 * stop. We use \r + clear-line escape so the line redraws in place.
 */
function startSpinner(initial: string): {
  setText: (text: string) => void;
  stop: () => void;
} {
  const frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
  let frame = 0;
  let text = initial;
  let stopped = false;

  const draw = (): void => {
    if (stopped) return;
    // \x1b[2K = clear entire line; \r = carriage return
    stdout.write(`\r\x1b[2K  ${CYAN}${frames[frame]}${RESET} ${text}`);
    frame = (frame + 1) % frames.length;
  };
  draw();
  const tick = setInterval(draw, 80);

  return {
    setText(t: string) {
      text = t;
    },
    stop() {
      stopped = true;
      clearInterval(tick);
      stdout.write("\r\x1b[2K");
    },
  };
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

function makePrompt(kg: string, triples: number): string {
  const kgPart = `${DIM}(${kg})${RESET}`;
  if (triples > 0) {
    return `  ${CYAN_BOLD}cograph${RESET} ${kgPart} ${DIM}[${fmtNum(triples)}]${RESET} ${CYAN_BOLD}▸${RESET} `;
  }
  return `  ${CYAN_BOLD}cograph${RESET} ${kgPart} ${CYAN_BOLD}▸${RESET} `;
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

  // First-run ergonomics: if no API key is configured (no env var, no
  // ~/.cograph/config.json), trigger the login flow before opening the
  // shell. Saves the friend from having to know to run `cograph login`
  // first — they just run `npx cograph` and the browser pops.
  if (!client.apiKey) {
    stdout.write(
      `\n  ${DIM}Not signed in — opening your browser to log in...${RESET}\n`,
    );
    const { runLogin } = await import("./login.js");
    await runLogin();
    client = new Client();
    if (!client.apiKey) {
      // runLogin already exits the process on hard failures, so reaching
      // here means it returned without writing a key (rare). Bail rather
      // than continue into a broken shell.
      printError("Login did not produce an API key. Aborting.");
      return;
    }
  }
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
      line = (await ask(rl, makePrompt(kg, triples))).trim();
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
      } else if (line === "/kg" || line.startsWith("/kg ")) {
        const args = splitArgs(line.slice("/kg".length).trim());
        const sub = args[0] ?? "list";
        const target = args.slice(1).join(" ");

        if (sub === "list") {
          const list = await client.listKgs();
          if (!list.length) {
            stdout.write(
              `  ${DIM}No knowledge graphs yet. /kg create <name>${RESET}\n`,
            );
          } else {
            for (const k of list) {
              const n = String((k as { name?: string }).name ?? "?");
              const tc = Number((k as { triple_count?: number }).triple_count ?? 0);
              const marker = n === kg ? `${CYAN_BOLD}*${RESET}` : " ";
              stdout.write(
                `  ${marker} ${BOLD}${n}${RESET} ${DIM}(${fmtNum(tc)} triples)${RESET}\n`,
              );
            }
          }
        } else if (sub === "switch") {
          if (!target) {
            stdout.write(`  ${YELLOW}Usage:${RESET} /kg switch <name>\n`);
          } else {
            const list = await client.listKgs();
            const found = list.find(
              (k) => (k as { name?: string }).name === target,
            );
            if (!found) {
              printError(`KG not found: ${target}. Try /kg list.`);
            } else {
              kg = target;
              triples = Number(
                (found as { triple_count?: number }).triple_count ?? 0,
              );
              stdout.write(
                `  ${GREEN}✓${RESET} Switched to ${BOLD}${kg}${RESET}\n`,
              );
            }
          }
        } else if (sub === "create") {
          if (!target) {
            stdout.write(`  ${YELLOW}Usage:${RESET} /kg create <name>\n`);
          } else {
            try {
              await client.createKg(target);
              kg = target;
              triples = 0;
              stdout.write(
                `  ${GREEN}✓${RESET} Created and switched to ${BOLD}${kg}${RESET}\n`,
              );
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              if (/already exists|409/i.test(msg)) {
                kg = target;
                await refresh();
                stdout.write(
                  `  ${DIM}${target} already exists — switched to it.${RESET}\n`,
                );
              } else {
                printError(`Could not create: ${msg}`);
              }
            }
          }
        } else if (sub === "delete") {
          if (!target) {
            stdout.write(`  ${YELLOW}Usage:${RESET} /kg delete <name>\n`);
          } else {
            const isActive = target === kg;
            const tag = isActive ? " (the active KG)" : "";
            const confirm = (
              await ask(
                rl,
                `  ${YELLOW}Delete KG "${target}"${tag}?${RESET} [y/N]: `,
              )
            )
              .trim()
              .toLowerCase();
            if (confirm === "y" || confirm === "yes") {
              try {
                await client.deleteKg(target);
                stdout.write(`  ${GREEN}✓${RESET} Deleted ${BOLD}${target}${RESET}\n`);
                if (isActive) {
                  // Active KG is gone; let the user pick (or create) a new one
                  // before any further commands try to use it.
                  const picked = await selectKg(client, rl);
                  if (!picked) {
                    running = false;
                    break;
                  }
                  kg = picked;
                  await refresh();
                }
              } catch (err) {
                const msg = err instanceof Error ? err.message : String(err);
                printError(`Could not delete: ${msg}`);
              }
            } else {
              stdout.write(`  ${DIM}Cancelled.${RESET}\n`);
            }
          }
        } else {
          stdout.write(
            `  ${YELLOW}Unknown /kg subcommand: ${sub}.${RESET} Try /kg list, /kg switch <name>, /kg create <name>, /kg delete <name>.\n`,
          );
        }
      } else if (line.startsWith("/")) {
        stdout.write(
          `  ${YELLOW}Unknown command.${RESET} Try /ingest, /ask, /kg, /login, /status, /reset, /help, /quit\n`,
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
