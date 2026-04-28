import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { randomBytes } from "node:crypto";
import { hostname } from "node:os";
import { spawn } from "node:child_process";
import { stdout } from "node:process";
import { writeConfig, configPathForDisplay } from "./config.js";

const WEB_URL = process.env.COGRAPH_WEB_URL || "https://cograph.cloud";

interface CallbackPayload {
  state?: string;
  apiKey?: string;
  name?: string;
  userId?: string;
  email?: string | null;
}

/**
 * Browser-redirect login flow. Starts a one-shot local HTTP server, opens
 * the user's browser to /cli-login on the web app, and waits for the page
 * to POST back the API key. Saves the result to ~/.cograph/config.json.
 */
export async function runLogin(): Promise<void> {
  const state = randomBytes(24).toString("hex");
  const host = hostname();

  const result = await new Promise<CallbackPayload | { error: string }>((resolve) => {
    const server = createServer((req, res) => handleRequest(req, res, state, resolve));
    server.on("error", (err) => resolve({ error: err.message }));
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      if (!addr || typeof addr === "string") {
        resolve({ error: "Failed to bind local callback server" });
        return;
      }
      const port = addr.port;
      const callback = `http://127.0.0.1:${port}`;
      const url = `${WEB_URL}/cli-login?callback=${encodeURIComponent(
        callback,
      )}&state=${state}&hostname=${encodeURIComponent(host)}`;

      stdout.write("\n");
      stdout.write(`  Opening ${url}\n`);
      stdout.write(`  If the browser doesn't open, visit that URL manually.\n`);
      stdout.write(`  Waiting for sign-in callback...\n\n`);
      openBrowser(url);

      // Stop waiting after 5 minutes so we don't hang forever.
      setTimeout(
        () => resolve({ error: "Timed out waiting for browser callback (5 min)" }),
        5 * 60_000,
      ).unref();
    });

    // Close the server once we have a result.
    const origResolve = resolve;
    resolve = ((value) => {
      server.close();
      origResolve(value);
    }) as typeof resolve;
  });

  if ("error" in result) {
    stdout.write(`  Login failed: ${result.error}\n`);
    process.exit(1);
  }

  if (!result.apiKey) {
    stdout.write(`  Login failed: callback did not include an apiKey\n`);
    process.exit(1);
  }

  writeConfig({
    apiKey: result.apiKey,
    tenant: result.userId,
    email: result.email ?? undefined,
  });

  stdout.write(
    `  ✓ Logged in${result.email ? ` as ${result.email}` : ""}. Key saved to ${configPathForDisplay()}\n\n`,
  );
}

function handleRequest(
  req: IncomingMessage,
  res: ServerResponse,
  expectedState: string,
  resolve: (value: CallbackPayload | { error: string }) => void,
): void {
  // CORS — the page is on cograph.cloud, posting to localhost.
  res.setHeader("Access-Control-Allow-Origin", WEB_URL);
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");

  if (req.method === "OPTIONS") {
    res.writeHead(204);
    res.end();
    return;
  }

  if (req.method !== "POST") {
    res.writeHead(405);
    res.end("Method not allowed");
    return;
  }

  let body = "";
  req.on("data", (chunk) => {
    body += chunk;
    if (body.length > 64 * 1024) {
      // 64 KB cap — anything larger is suspicious.
      req.destroy();
    }
  });
  req.on("end", () => {
    let payload: CallbackPayload;
    try {
      payload = JSON.parse(body) as CallbackPayload;
    } catch {
      res.writeHead(400);
      res.end("Invalid JSON");
      return;
    }

    if (payload.state !== expectedState) {
      res.writeHead(400);
      res.end("State mismatch");
      resolve({ error: "State mismatch — possible CSRF, login aborted" });
      return;
    }

    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
    resolve(payload);
  });
}

function openBrowser(url: string): void {
  const platform = process.platform;
  const cmd = platform === "darwin" ? "open" : platform === "win32" ? "cmd" : "xdg-open";
  const args = platform === "win32" ? ["/c", "start", "", url] : [url];
  try {
    const child = spawn(cmd, args, { stdio: "ignore", detached: true });
    child.unref();
  } catch {
    // best-effort; the URL is already printed for manual paste
  }
}
