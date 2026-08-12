#!/usr/bin/env bash
# OSS one-shot local setup (ONTA-540 / P-K0).
#
# After Neo4j + the API are up, this script:
#   1. Probes GET $INFONA_API_URL/health (default http://localhost:8000)
#   2. Verifies open-access (GET /graphs/default/kgs is not 401)
#   3. Writes ~/.infona/config.json  { "apiUrl": "...", "tenant": "default" }
#      so bare `infona` works WITHOUT --local
#   4. Best-effort: npm ci + build + npm link the CLI when Node is present
#
# Never opens a browser. Never forces local mode for pure npm cloud users —
# only this repo path (or `infona init --local` / the wizard's local choice)
# writes the local open-access config.
#
# Usage (from the infona-oss repo root):
#   ./scripts/oss_setup.sh
#   INFONA_API_URL=http://127.0.0.1:8000 ./scripts/oss_setup.sh
#
# Documented as the canonical OSS setup entry in README.md.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_URL="${INFONA_API_URL:-http://localhost:8000}"
API_URL="${API_URL%/}"
TENANT="${INFONA_TENANT:-default}"
CONFIG_DIR="${HOME}/.infona"
CONFIG_FILE="${CONFIG_DIR}/config.json"

log()  { printf '  %s\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
die()  { printf '  ✗ %s\n' "$*" >&2; exit 1; }

log "Infona OSS setup"
log "  API:    ${API_URL}"
log "  tenant: ${TENANT}"
log "  config: ${CONFIG_FILE}"
log ""

# --- 1. Health probe -------------------------------------------------------- #
if ! command -v curl >/dev/null 2>&1; then
  die "curl is required for the health probe"
fi

HTTP_HEALTH="$(
  curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 \
    "${API_URL}/health" 2>/dev/null || echo "000"
)"
if [[ "${HTTP_HEALTH}" != "200" ]]; then
  die "Health probe failed (${API_URL}/health → HTTP ${HTTP_HEALTH}). Start the API first:
    set -a && source .env && set +a
    uvicorn infona_client.api.app:create_app --factory --port 8000"
fi
ok "Health OK (${API_URL}/health)"

# --- 2. Open-access probe (must NOT require auth for local OSS) ------------ #
HTTP_KGS="$(
  curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 \
    -H 'Content-Type: application/json' \
    "${API_URL}/graphs/${TENANT}/kgs" 2>/dev/null || echo "000"
)"
if [[ "${HTTP_KGS}" == "401" ]]; then
  die "Local server requires authentication (HTTP 401 on /graphs/${TENANT}/kgs).
    For open-access OSS, leave INFONA_API_KEYS empty, then re-run.
    Or connect with:  infona init   # pick API key"
fi
if [[ "${HTTP_KGS}" != "200" && "${HTTP_KGS}" != "404" ]]; then
  # 200 = list ok; some builds may 404 empty route variants — still not auth.
  # Anything else (000/5xx) is a hard fail.
  if [[ "${HTTP_KGS}" == "000" || "${HTTP_KGS}" =~ ^5 ]]; then
    die "Could not probe ${API_URL}/graphs/${TENANT}/kgs (HTTP ${HTTP_KGS})"
  fi
fi
ok "Open-access OK (no auth required for tenant=${TENANT})"

# --- 3. Write ~/.infona/config.json ---------------------------------------- #
mkdir -p "${CONFIG_DIR}"
chmod 700 "${CONFIG_DIR}" 2>/dev/null || true

# Prefer Node (canonical write via the CLI package) when available so the
# shape always matches packages/cli/src/config.ts. Fall back to a pure-shell
# JSON write so setup still works without Node.
write_config_node() {
  local cli_src="${ROOT}/packages/cli/src/config.ts"
  local cli_dist="${ROOT}/packages/cli/dist/cli.js"
  # Use a tiny inline node script that mirrors writeLocalOpenAccessConfig
  # without requiring a build — pure JSON write with the same fields.
  node -e '
    const fs = require("fs");
    const path = require("path");
    const os = require("os");
    const dir = path.join(os.homedir(), ".infona");
    const file = path.join(dir, "config.json");
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    const cfg = {
      apiUrl: process.env.INFONA_SETUP_URL,
      tenant: process.env.INFONA_SETUP_TENANT,
    };
    // Preserve defaultKg if present; drop apiKey/email for open-access local.
    try {
      const prev = JSON.parse(fs.readFileSync(file, "utf8"));
      if (prev && typeof prev.defaultKg === "string") cfg.defaultKg = prev.defaultKg;
    } catch (_) {}
    fs.writeFileSync(file, JSON.stringify(cfg, null, 2) + "\n", { mode: 0o600 });
    try { fs.chmodSync(file, 0o600); } catch (_) {}
    process.stdout.write(file + "\n");
  '
}

write_config_shell() {
  local tmp
  tmp="$(mktemp)"
  # Preserve defaultKg from an existing file when possible (grep, not jq — jq may be absent).
  local default_kg=""
  if [[ -f "${CONFIG_FILE}" ]]; then
    default_kg="$(
      # shellcheck disable=SC2002
      cat "${CONFIG_FILE}" 2>/dev/null \
        | sed -n 's/.*"defaultKg"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        | head -1
    )" || true
  fi
  if [[ -n "${default_kg}" ]]; then
    printf '{\n  "apiUrl": "%s",\n  "tenant": "%s",\n  "defaultKg": "%s"\n}\n' \
      "${API_URL}" "${TENANT}" "${default_kg}" >"${tmp}"
  else
    printf '{\n  "apiUrl": "%s",\n  "tenant": "%s"\n}\n' \
      "${API_URL}" "${TENANT}" >"${tmp}"
  fi
  mv "${tmp}" "${CONFIG_FILE}"
  chmod 600 "${CONFIG_FILE}" 2>/dev/null || true
}

if command -v node >/dev/null 2>&1; then
  INFONA_SETUP_URL="${API_URL}" INFONA_SETUP_TENANT="${TENANT}" write_config_node >/dev/null
else
  write_config_shell
fi
ok "Wrote ${CONFIG_FILE}"
log "    $(tr -d '\n' < "${CONFIG_FILE}" | head -c 200)"
log ""

# --- 4. Best-effort CLI build + link --------------------------------------- #
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  if [[ -f "${ROOT}/package.json" && -d "${ROOT}/packages/cli" ]]; then
    log "Building CLI (best-effort)…"
    (
      cd "${ROOT}"
      if [[ ! -d node_modules ]]; then
        npm ci --ignore-scripts 2>/dev/null || npm install --ignore-scripts 2>/dev/null || true
      fi
      npm run build -w packages/cli 2>/dev/null \
        || npm run build --workspace=packages/cli 2>/dev/null \
        || true
      if [[ -f packages/cli/dist/cli.js ]]; then
        chmod +x packages/cli/dist/cli.js 2>/dev/null || true
        # Global link so `infona` is on PATH for this machine (best-effort).
        (cd packages/cli && npm link 2>/dev/null) || true
        ok "CLI built (packages/cli/dist/cli.js)"
        if command -v infona >/dev/null 2>&1; then
          ok "\`infona\` is on PATH — try: infona kg list"
        else
          log "Run via: node packages/cli/dist/cli.js kg list"
          log "Or:     cd packages/cli && npm link"
        fi
      else
        log "CLI build skipped/failed — use: node packages/cli/dist/cli.js after npm run build"
      fi
    )
  fi
else
  log "Node/npm not found — skipped CLI build. Install Node 20+ then:"
  log "  npm ci && npm run build -w packages/cli && cd packages/cli && npm link"
fi

log ""
ok "Setup complete. Bare \`infona\` uses local open-access (no --local needed)."
log "Re-configure any time with:  infona init"
log ""
