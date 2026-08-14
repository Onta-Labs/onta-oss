/** Interactive-shell UI primitives: colors, banner, spinner, prompts. */
import * as readline from "node:readline";
import { stdout } from "node:process";

export const CYAN = "\x1b[36m";
export const CYAN_BOLD = "\x1b[1;36m";
export const DIM = "\x1b[2m";
export const RED = "\x1b[31m";
export const GREEN = "\x1b[32m";
export const YELLOW = "\x1b[33m";
export const BOLD = "\x1b[1m";
export const RESET = "\x1b[0m";

export function fmtNum(n: number): string {
  return n.toLocaleString("en-US");
}

function canRenderBlockArt(): boolean {
  // Apple_Terminal (macOS Terminal.app) treats the block-shade chars (█░)
  // we use in the banner as East Asian Ambiguous Width = 2 cells, so each
  // banner row renders at double width and wraps mid-letter. iTerm,
  // WezTerm, Kitty, VS Code, Cursor, etc. all treat them as 1 cell and
  // render the art correctly. Skip the banner on Apple_Terminal and show
  // a plain header instead. Force on/off via INFONA_BANNER=on|off.
  const force = process.env.INFONA_BANNER;
  if (force === "on") return true;
  if (force === "off") return false;
  if (!process.stdout.isTTY) return false;
  if (process.env.TERM_PROGRAM === "Apple_Terminal") return false;
  return true;
}

export function showBanner(): void {
  if (canRenderBlockArt()) {
    const lines = [
      "",
      `${CYAN}       ███████    ██████   █████ ███████████   █████████${RESET}`,
      `${CYAN}     ███░░░░░███ ░░██████ ░░███ ░█░░░███░░░█  ███░░░░░███${RESET}`,
      `${CYAN}    ███     ░░███ ░███░███ ░███ ░   ░███  ░  ░███    ░███${RESET}`,
      `${CYAN}    ░███      ░███ ░███░░███░███     ░███     ░███████████${RESET}`,
      `${CYAN}    ░███      ░███ ░███ ░░██████     ░███     ░███░░░░░███${RESET}`,
      `${CYAN}    ░░███     ███  ░███  ░░█████     ░███     ░███    ░███${RESET}`,
      `${CYAN}     ░░░███████░   █████  ░░█████    █████    █████   █████${RESET}`,
      `${CYAN}       ░░░░░░░    ░░░░░    ░░░░░    ░░░░░    ░░░░░   ░░░░░${RESET}`,
      "",
      `${DIM}    The object graph for AI agents${RESET}`,
      "",
    ];
    for (const l of lines) stdout.write(l + "\n");
  } else {
    stdout.write(`\n  ${CYAN_BOLD}INFONA${RESET}\n`);
    stdout.write(`  ${DIM}The object graph for AI agents${RESET}\n\n`);
  }
  showCommands();
}

export function showCommands(): void {
  const rows: Array<[string, string]> = [
    ["/ingest <file> ...", "Ingest a CSV/JSON/text file"],
    ["/ask <question>", "Ask in natural language"],
    ["/agent <message>", "Unified Ask-AI agent — answers, plans, runs actions"],
    ["/kg list", "List your context graphs"],
    ["/kg switch <name>", "Switch to a different KG"],
    ["/kg create <name>", "Create a new KG and switch to it"],
    ["/kg delete <name>", "Delete a KG (irreversible)"],
    ["/tenant list", "List tenants you can access"],
    ["/tenant use <id>", "Switch tenant (then pick a KG)"],
    ["/types [query]", "List types in the current KG (with entity counts)"],
    ["/type <name>", "Drill into one type — attributes, relationships, samples"],
    ["/type <name> --system", "…also include auto-attached system attributes"],
    ["/enrich <Type> <attr> ...", "Plan + run an enrichment job (interactive)"],
    ["/enrich watch <job_id>", "Live progress for a running job"],
    ["/enrich jobs", "List recent enrichment jobs"],
    ["/enrich review <job_id>", "Walk through conflicts and accept/reject"],
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

export function printError(msg: string): void {
  stdout.write(`  ${RED}✗${RESET} ${msg}\n`);
}

export function ask(rl: readline.Interface, prompt: string): Promise<string> {
  return new Promise((resolve) => {
    rl.question(prompt, (answer) => resolve(answer));
  });
}

/**
 * Tiny live-line spinner. Returns handles to update the trailing text and
 * stop. We use \r + clear-line escape so the line redraws in place.
 */
export function startSpinner(initial: string): {
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

export function lastUriSegment(uri: string): string {
  if (!uri) return uri;
  const hash = uri.lastIndexOf("#");
  if (hash >= 0 && hash < uri.length - 1) return uri.slice(hash + 1);
  const slash = uri.lastIndexOf("/");
  if (slash >= 0 && slash < uri.length - 1) return uri.slice(slash + 1);
  return uri;
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "—";
  const diffMs = Date.now() - t;
  const s = Math.max(0, Math.floor(diffMs / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

export function progressBar(processed: number, total: number, width = 20): string {
  if (!total || total <= 0) return "[" + " ".repeat(width) + "]";
  const ratio = Math.max(0, Math.min(1, processed / total));
  const filled = Math.round(ratio * width);
  return "[" + "█".repeat(filled) + "░".repeat(width - filled) + "]";
}

export function statusColor(status: string): string {
  switch (status) {
    case "applied":
      return GREEN;
    case "failed":
      return RED;
    case "review":
      return YELLOW;
    case "cancelled":
      return DIM;
    default:
      return CYAN;
  }
}

function urlHost(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url.replace(/^https?:\/\//, "").replace(/\/+$/, "");
  }
}

export function makePrompt(
  kg: string,
  triples: number,
  mode: "cloud" | "self-hosted" = "cloud",
  baseUrl?: string,
): string {
  const kgPart = `${DIM}(${kg})${RESET}`;
  const triplePart = triples > 0 ? `${DIM}[${fmtNum(triples)}]${RESET} ` : "";
  if (mode === "self-hosted" && baseUrl) {
    const host = urlHost(baseUrl);
    return `  ${CYAN_BOLD}infona${RESET}${DIM}@${host}${RESET} ${kgPart} ${triplePart}${CYAN_BOLD}▸${RESET} `;
  }
  return `  ${CYAN_BOLD}infona${RESET} ${kgPart} ${triplePart}${CYAN_BOLD}▸${RESET} `;
}

/**
 * Split a command-line style argument string. Supports double-quoted args.
 */
export function splitArgs(s: string): string[] {
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
