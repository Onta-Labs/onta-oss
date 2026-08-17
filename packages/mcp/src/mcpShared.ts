/** Shared MCP helpers — client factory, result wrappers, job-category lockstep.

Implementation extracted from ``index.ts``. Tools still reach the backend
through the SDK path builders; this module does not invent endpoints.
*/
import { randomUUID } from "node:crypto";
import { Client, InfonaError } from "@infona-ai/cli";
import type { JobCategory } from "@infona-ai/cli";
import {
  describeRootResolution,
  resolveRoots,
} from "./workspace.js";

export const VERSION = "0.1.0";

// ONTA-415: the local directory the user has explicitly granted this server, if
// any. Resolved ONCE at startup from INFONA_LOCAL_FILES_DIR (see workspace.ts for
// the precedence and for why there is no default root). Empty means the
// capability stays DORMANT: `list_local_files` is never registered, so it does
// not appear in tools/list and burns no context in a session that has no root.
export const LOCAL_FILE_ROOTS: string[] = (() => {
  const res = resolveRoots();
  // One stderr line, but ONLY for a user who actually set the var: a bad path,
  // a refused filesystem root, or a successful grant are all worth reporting,
  // whereas the overwhelming majority who never opt in should get no noise at
  // all for a feature they are not using.
  if (res.varName) {
    try {
      process.stderr.write(`${describeRootResolution(res)}\n`);
    } catch {
      // stderr unavailable; the resolution itself still stands.
    }
  }
  return res.roots;
})();

// The job categories the `list_jobs` filter accepts. This MUST stay in lockstep
// with the backend `JobCategory` enum (infona_client/enrichment/models.py) — a
// missing member silently hides that category's jobs from the agent: the enum
// used to omit "discovery", so `list_jobs({category:"discovery"})` was rejected
// AND the natural fallback `category:"enrichment"` filtered the discovery job
// OUT ("No jobs found"), stranding a working web-ingest job the agent had just
// kicked off (persona-eval RCA, ONTA-243). Sourcing the list from the SDK's
// exported `JobCategory` type (via the exhaustiveness check below) makes a future
// backend addition a COMPILE error here rather than a silent runtime gap.
export const JOB_CATEGORIES = [
  "enrichment",
  "dedupe",
  "reconciliation",
  "discovery",
  "ingest",
  "answer",
] as const;

// Compile-time drift guard: `JOB_CATEGORIES` must enumerate EXACTLY the SDK's
// `JobCategory` union — no more, no less. If the backend adds/removes a category
// (and the SDK type is regenerated), these two assignments stop type-checking
// until `JOB_CATEGORIES` is updated to match, so the runtime enum can never drift
// from the backend again. Purely a type check — erased at build time.
type _CategoryUnion = (typeof JOB_CATEGORIES)[number];
const _assertCategoriesCoverSdk: JobCategory = "" as _CategoryUnion;
const _assertSdkCoversCategories: _CategoryUnion = "" as JobCategory;
void _assertCategoriesCoverSdk;
void _assertSdkCoversCategories;

// Stable conversation id for this MCP server process. The `agent` tool threads
// this into every backend `/agent` call when the caller does not supply its own
// `session_id`, so multi-turn context accumulates across tool invocations. The
// OSS planner's clarify-convergence machinery is gated on a session id and
// silently no-ops without one (infona_client/agent/planner.py history load +
// `_effective_instruction`; web_ingest_cap `already_asked`), so a missing id
// means every turn is planned statelessly and a single stated intent gets
// re-clarified indefinitely. Minting once per process keeps the whole session's
// turns on one thread.
export const DEFAULT_SESSION_ID = randomUUID();

/** One-liner when a hosted URL is used without a key. Local OSS needs none. */
export const HOSTED_KEY_REQUIRED =
  "Hosted Infona requires INFONA_API_KEY. Local OSS uses the README MCP JSON (INFONA_API_URL=http://localhost:8000, INFONA_TENANT=default, no key).";

/** Same localhost / 127.0.0.1 check the SDK uses for open-access defaults. */
export function isLocalApiUrl(url: string | undefined | null): boolean {
  if (!url) return false;
  return /^(https?:\/\/)?(localhost|127\.0\.0\.1)(:|\/|$)/i.test(url);
}

/**
 * Local OSS (`INFONA_API_URL=http://localhost:8000`) is open-access — no key.
 * Hosted URLs without a key fail here so tools/list + first-call errors stay
 * one line instead of a later 401.
 */
export function assertClientAccess(
  c: Pick<Client, "apiKey" | "baseUrl">,
): void {
  if (!c.apiKey && !isLocalApiUrl(c.baseUrl)) {
    throw new InfonaError(HOSTED_KEY_REQUIRED);
  }
}

export function client(): Client {
  const c = new Client();
  assertClientAccess(c);
  return c;
}

export function textResult(text: string) {
  return {
    content: [{ type: "text" as const, text }],
  };
}

export function errorResult(err: unknown) {
  const msg =
    err instanceof InfonaError
      ? `Infona error: ${err.message}`
      : err instanceof Error
        ? err.message
        : String(err);
  return {
    content: [{ type: "text" as const, text: msg }],
    isError: true,
  };
}
