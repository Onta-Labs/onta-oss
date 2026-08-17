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

log "Starting Neo4j + API (docker compose up -d --build)…"
docker compose up -d --build

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
  if [[ "${HTTP}" == "200" ]]; then
    ok "API ready (${HEALTH_URL})"
    break
  fi
  err_txt="$(tr '\n' ' ' < "${ERR}" 2>/dev/null | head -c 240)"
  body_txt="$(tr '\n' ' ' < "${BODY}" 2>/dev/null | head -c 160)"
  LAST_ERR="HTTP ${HTTP}: ${err_txt} ${body_txt}"
  sleep 2
done

if [[ "${HTTP}" != "200" ]]; then
  log "Last error: ${LAST_ERR}"
  docker compose logs --tail 40 api 2>/dev/null || true
  die "API did not become healthy within ${WAIT_SECS}s (${HEALTH_URL})"
fi

"${ROOT}/scripts/oss_setup.sh"

log ""
ok "Local loop is up. Next:"
log "  infona ingest examples/trials.csv --kg trials"
log "  infona ask \"Which Phase 3 NSCLC trials is AstraZeneca running?\" --kg trials"
log "  infona export --kg trials -f json -o trials.json"
log ""
