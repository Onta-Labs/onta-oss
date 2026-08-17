/** Interactive Infona shell (``infona`` / ``infona shell``).

Implementation lives in sibling ``shell*.ts`` modules. ``runShell`` stays
the public entry — same connect wizard, same command dispatch.
*/
import * as readline from "node:readline";
import { stdin, stdout } from "node:process";
import { randomUUID } from "node:crypto";
import { Client, InfonaError } from "./client.js";
import { writeConfig } from "./config.js";
import {
  cmdAgent,
  cmdAsk,
  cmdIngest,
  cmdReset,
  cmdStatus,
  cmdType,
  cmdTypes,
  fetchKg,
  selectKg,
} from "./shellCore.js";
import {
  cmdEnrichJobs,
  cmdEnrichReview,
  cmdEnrichRun,
  watchJob,
} from "./shellEnrich.js";
import {
  BOLD,
  CYAN_BOLD,
  DIM,
  GREEN,
  RESET,
  YELLOW,
  ask,
  fmtNum,
  makePrompt,
  printError,
  showBanner,
  showCommands,
  splitArgs,
} from "./shellUi.js";
import { MSG_NEO4J_DOWN, mapFirstHourError } from "./firstHourErrors.js";

export async function runShell(opts: {
  kg?: string;
  local?: boolean;
  noLogin?: boolean;
}): Promise<void> {
  // ONTA-540: first run with no flags/env/config → interactive connect wizard
  // (not silent cloud browser login, not silent force-local). Flags are
  // one-off and never wipe ~/.infona/config.json.
  const { ensureConnected } = await import("./connect.js");
  const connected = await ensureConnected({
    local: opts.local,
    noLogin: opts.noLogin,
  });
  if (!connected) {
    printError(
      "No connection configured. Run `infona init`, pass --local, or set INFONA_API_URL / INFONA_API_KEY.",
    );
    return;
  }

  // These hosts all resolve to the SAME hosted backend (verified: identical
  // openapi.json). api.infona.ai is canonical after the Infona rebrand;
  // the older hosts still work, so any of them counts as "cloud" (not self-hosted).
  const CLOUD_HOSTS = new Set([
    "https://api.infona.ai",
    "https://api.infona.ai",
    "https://api.infona.ai",
    "https://api.infona.cloud",
  ]);
  // Detection precedence: --local > --no-login > INFONA_API_URL → config
  // apiUrl pointing anywhere besides a known cloud host.
  // When self-hosted we never trigger login and tenant defaults to "default"
  // (open-access backend behavior).
  const { readConfig, isLocalhostUrl, LOCAL_API_URL, LOCAL_DEFAULT_TENANT } =
    await import("./config.js");
  const cfg = readConfig();
  const envUrl = process.env.INFONA_API_URL;
  const resolvedUrl = opts.local
    ? LOCAL_API_URL
    : envUrl || cfg.apiUrl || "https://api.infona.ai";
  const envIsSelfHosted = !!envUrl && !CLOUD_HOSTS.has(envUrl);
  const configIsSelfHosted =
    !!cfg.apiUrl && !CLOUD_HOSTS.has(cfg.apiUrl.replace(/\/+$/, ""));
  const selfHostedHint =
    !!opts.local ||
    !!opts.noLogin ||
    envIsSelfHosted ||
    configIsSelfHosted ||
    isLocalhostUrl(resolvedUrl);

  // `let` rather than `const` so /login can swap in a fresh Client after
  // ~/.infona/config.json is rewritten with the new key.
  // --local is one-off (does not write config); config.apiUrl from OSS setup
  // or wizard makes bare `infona` hit localhost without the flag (Client
  // already reads apiUrl/tenant from env + ~/.infona/config.json).
  let client = opts.local
    ? new Client({ baseUrl: LOCAL_API_URL, tenant: LOCAL_DEFAULT_TENANT })
    : selfHostedHint
      ? new Client({
          // Prefer env / saved tenant; else open-access "default" so a leftover
          // cloud demo-tenant in an older code path cannot steal local traffic.
          tenant:
            process.env.INFONA_TENANT ||
            cfg.tenant ||
            LOCAL_DEFAULT_TENANT,
        })
      : new Client();

  // Probe the backend before deciding whether to trigger login. This lets
  // us distinguish "cloud, needs auth" from "self-hosted, open access" and
  // also surfaces an unreachable server with a clear error rather than a
  // confusing browser-login attempt.
  const health = await client.healthCheck();
  if (!health.ok) {
    printError(
      mapFirstHourError({
        message: "ECONNREFUSED",
        baseUrl: client.baseUrl,
        hasApiKey: Boolean(client.apiKey),
      }) ?? `Could not reach ${health.url}.`,
    );
    return;
  }
  if (health.neo4j === false || health.status === "degraded") {
    printError(MSG_NEO4J_DOWN);
    return;
  }

  const selfHosted = selfHostedHint || !health.requiresAuth;
  const mode: "cloud" | "self-hosted" = selfHosted ? "self-hosted" : "cloud";

  // Cloud / auth-required path: if still no key after ensureConnected (e.g.
  // env pointed at cloud URL without a key), offer the wizard — never silent
  // browser auto-open (ONTA-540). Open-access local never reaches here.
  if (!selfHosted && health.requiresAuth && !client.apiKey) {
    stdout.write(
      `\n  ${DIM}Not signed in — choose how to authenticate.${RESET}\n`,
    );
    const { runConnectWizard } = await import("./connect.js");
    const result = await runConnectWizard({ force: false });
    if (result !== "ok") {
      printError("Login did not produce an API key. Aborting.");
      return;
    }
    client = new Client();
    if (!client.apiKey) {
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

  if (selfHosted) {
    stdout.write(
      `${DIM}  Self-hosted mode · ${client.baseUrl} · tenant=${client.tenant}${RESET}\n\n`,
    );
  }

  // One agent session id per shell session — threaded across every /agent turn
  // for multi-turn continuity (the server keys conversation state on it).
  const agentSessionId = randomUUID();

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
      line = (
        await ask(rl, makePrompt(kg, triples, mode, client.baseUrl))
      ).trim();
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
      } else if (line.startsWith("/agent ")) {
        await cmdAgent(
          client,
          kg,
          rl,
          agentSessionId,
          line.slice("/agent ".length),
        );
        await refresh();
      } else if (line === "/agent") {
        await cmdAgent(client, kg, rl, agentSessionId, "");
      } else if (line === "/types" || line.startsWith("/types ")) {
        const query = line === "/types" ? "" : line.slice("/types ".length);
        await cmdTypes(client, kg, query);
      } else if (line.startsWith("/type ") || line === "/type") {
        const arg = line === "/type" ? "" : line.slice("/type ".length);
        await cmdType(client, kg, rl, arg);
      } else if (line === "/enrich" || line.startsWith("/enrich ")) {
        const args = splitArgs(line.slice("/enrich".length).trim());
        if (args.length === 0) {
          stdout.write(
            `  ${YELLOW}Usage:${RESET} /enrich <Type> <attr> ... | /enrich watch <id> | /enrich jobs | /enrich review <id>\n`,
          );
        } else if (args[0] === "jobs") {
          await cmdEnrichJobs(client);
        } else if (args[0] === "watch") {
          const jid = args[1];
          if (!jid) {
            stdout.write(`  ${YELLOW}Usage:${RESET} /enrich watch <job_id>\n`);
          } else {
            await watchJob(client, jid);
          }
        } else if (args[0] === "review") {
          await cmdEnrichReview(client, rl, args[1] ?? "");
        } else {
          await cmdEnrichRun(client, kg, rl, args);
          await refresh();
        }
      } else if (line === "/status") {
        await cmdStatus(client, kg);
        await refresh();
      } else if (line === "/reset") {
        const did = await cmdReset(client, kg, rl);
        if (did) await refresh();
      } else if (line === "/login") {
        const { runLogin } = await import("./login.js");
        await runLogin();
        // Pick up the new key from ~/.infona/config.json for subsequent calls.
        client = new Client();
        await refresh();
      } else if (line === "/tenant" || line.startsWith("/tenant ")) {
        const args = splitArgs(line.slice("/tenant".length).trim());
        const sub = args[0] ?? "list";
        const target = args.slice(1).join(" ");

        if (sub === "use" || sub === "switch") {
          if (!target) {
            stdout.write(`  ${YELLOW}Usage:${RESET} /tenant use <id>\n`);
          } else {
            writeConfig({ tenant: target });
            // Rebuild the client so it picks up the new tenant; preserve the
            // current base URL (self-hosted/local) and key.
            client = new Client({ baseUrl: client.baseUrl });
            stdout.write(
              `  ${GREEN}✓${RESET} Switched to tenant ${BOLD}${target}${RESET}\n`,
            );
            // KGs are per-tenant — the old current KG may not exist here, so
            // pick one from the new tenant.
            const picked = await selectKg(client, rl);
            if (picked) {
              kg = picked;
            } else {
              stdout.write(
                `  ${DIM}No KGs in ${target} yet — /kg create <name>${RESET}\n`,
              );
            }
            await refresh();
          }
        } else if (sub === "current") {
          stdout.write(`  ${BOLD}${client.tenant}${RESET}\n`);
        } else {
          try {
            const tenants = await client.listTenants();
            if (!tenants.length) {
              stdout.write(`  ${DIM}No tenants found for your account.${RESET}\n`);
            } else {
              for (const t of tenants) {
                const marker =
                  t.id === client.tenant ? `${CYAN_BOLD}*${RESET}` : " ";
                stdout.write(
                  `  ${marker} ${BOLD}${t.id}${RESET} ${DIM}${t.label}${RESET}\n`,
                );
              }
              stdout.write(`  ${DIM}/tenant use <id> to switch${RESET}\n`);
            }
          } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            if (err instanceof InfonaError && err.status === 501) {
              printError("Tenant management isn't configured on this backend.");
            } else {
              printError(msg);
            }
          }
        }
      } else if (line === "/kg" || line.startsWith("/kg ")) {
        const args = splitArgs(line.slice("/kg".length).trim());
        const sub = args[0] ?? "list";
        const target = args.slice(1).join(" ");

        if (sub === "list") {
          const list = await client.listKgs();
          if (!list.length) {
            stdout.write(
              `  ${DIM}No context graphs yet. /kg create <name>${RESET}\n`,
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
          `  ${YELLOW}Unknown command.${RESET} Try /ingest, /ask, /agent, /kg, /types, /type, /enrich, /login, /status, /reset, /help, /quit\n`,
        );
      } else {
        // Bare line — auto-route to /ask
        await cmdAsk(client, kg, line);
      }
    } catch (err) {
      if (err instanceof InfonaError) printError(err.message);
      else printError(err instanceof Error ? err.message : String(err));
    }
  }

  rl.close();
}
