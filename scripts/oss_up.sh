#!/usr/bin/env bash
# OSS one-command local loop: compose up + wait for API + CLI config.
#
# From the infona-oss repo root (or anywhere; this script cds to root):
#   ./scripts/oss_up.sh
#
# Never opens a browser. Does not start a second-shell uvicorn.
# OPENROUTER_API_KEY is optional at boot (graph routes work; /ask needs it).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

API_URL="${INFONA_API_URL:-http://localhost:8000}"
API_URL="${API_URL%/}"
HEALTH_URL="${API_URL}/health"
WAIT_SECS="${INFONA_OSS_UP_TIMEOUT:-90}"

log()  { printf '  %s\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
die()  { printf '  ✗ %s\n' "$*" >&2; exit 1; }

log "Infona OSS up"
log "  repo: ${ROOT}"
log "  API:  ${API_URL}"
log ""

if [[ ! -f "${ROOT}/.env" ]]; then
  if [[ ! -f "${ROOT}/.env.example" ]]; then
    die ".env is missing and .env.example was not found"
  fi
  cp "${ROOT}/.env.example" "${ROOT}/.env"
  die "Created .env from .env.example. Paste OPENROUTER_API_KEY into .env and re-run:
    ./scripts/oss_up.sh"
fi

if ! command -v docker >/dev/null 2>&1; then
  die "docker is required (https://docs.docker.com/get-docker/)"
fi
if ! docker compose version >/dev/null 2>&1; then
  die "docker compose is required (Docker Compose V2 plugin)"
fi
if ! command -v curl >/dev/null 2>&1; then
  die "curl is required to wait for ${HEALTH_URL}"
fi
if ! command -v python3 >/dev/null 2>&1; then
  die "python3 is required to parse ${HEALTH_URL} (need neo4j=true, not just HTTP 200)"
fi

log "Starting Neo4j + API (docker compose up -d --build)…"
COMPOSE_LOG="$(mktemp)"
if ! docker compose up -d --build 2>&1 | tee "${COMPOSE_LOG}"; then
  if grep -qiE 'port is already allocated|address already in use|bind: address already in use' "${COMPOSE_LOG}"; then
    rm -f "${COMPOSE_LOG}"
    die "A port this stack needs (7474 / 7687 / 8000) is already in use.
    Stop the other Neo4j or API (docker ps / lsof -iTCP:7687 -sTCP:LISTEN), then re-run.
    Only if an Infona API is already healthy on :8000, skip compose and run: infona init --local"
  fi
  rm -f "${COMPOSE_LOG}"
  die "docker compose up failed"
fi
rm -f "${COMPOSE_LOG}"

BODY="$(mktemp)"
ERR="$(mktemp)"
cleanup() { rm -f "${BODY}" "${ERR}"; }
trap cleanup EXIT

LAST_ERR="no health probe attempted"
HTTP="000"
deadline=$((SECONDS + WAIT_SECS))
log "Waiting for ${HEALTH_URL} (timeout ${WAIT_SECS}s)…"
while (( SECONDS < deadline )); do
  HTTP="$(
    curl -sS -o "${BODY}" -w '%{http_code}' \
      --connect-timeout 2 --max-time 5 \
      "${HEALTH_URL}" 2>"${ERR}" || true
  )"
  [[ -z "${HTTP}" ]] && HTTP="000"
  if [[ "${HTTP}" == "200" ]] && python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("neo4j") is True and d.get("status") == "healthy" else 1)
' < "${BODY}"; then
    ok "API ready (${HEALTH_URL}, neo4j healthy)"
    break
  fi
  err_txt="$(tr '\n' ' ' < "${ERR}" 2>/dev/null | head -c 240)"
  body_txt="$(tr '\n' ' ' < "${BODY}" 2>/dev/null | head -c 160)"
  LAST_ERR="HTTP ${HTTP}: ${err_txt} ${body_txt}"
  sleep 2
done

if [[ "${HTTP}" != "200" ]] || ! python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("neo4j") is True and d.get("status") == "healthy" else 1)
' < "${BODY}"; then
  log "Last error: ${LAST_ERR}"
  docker compose logs --tail 40 api 2>/dev/null || true
  die "API/Neo4j did not become healthy within ${WAIT_SECS}s (${HEALTH_URL}; need status=healthy and neo4j=true)"
fi

"${ROOT}/scripts/oss_setup.sh"

BIN="infona"
if ! command -v infona >/dev/null 2>&1; then
  BIN="npx @infona-ai/cli"
  log "infona not on PATH — using ${BIN} (or: npm i -g @infona-ai/cli)"
fi

log ""
ok "Local loop is up. Next:"
log "  ${BIN} ingest examples/trials.csv --kg trials"
log "  ${BIN} ask \"Which Phase 3 NSCLC trials is AstraZeneca running?\" --kg trials"
log "  ${BIN} export --kg trials -f json -o trials.json"
log ""
