#!/usr/bin/env bash
# CI helper for .github/workflows/oss-quickstart-smoke.yml (ONTA-287).
# Does not boot Docker — the workflow starts Neo4j + the API (or oss_up.sh).
#
#   SMOKE_MODE=mocked  health + load_prebuilt + zero-key ask → FLAURA2
#   SMOKE_MODE=live    health + ingest trials + pinned ask → FLAURA2
#
# Usage (from repo root, stack already up):
#   ./scripts/ci_quickstart_smoke.sh
#   SMOKE_MODE=live ./scripts/ci_quickstart_smoke.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

API_URL="${INFONA_API_URL:-http://127.0.0.1:8000}"
API_URL="${API_URL%/}"
HEALTH_URL="${API_URL}/health"
MODE="${SMOKE_MODE:-mocked}"
WAIT_SECS="${INFONA_OSS_UP_TIMEOUT:-90}"
HERO_Q='Which Phase 3 NSCLC trials is AstraZeneca running?'
HERO_KG="trials"
HERO_HIT="FLAURA2"
QUERY_MODEL="${INFONA_QUERY_MODEL:-google/gemini-2.5-flash}"

log()  { printf '  %s\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
die()  { printf '  ✗ %s\n' "$*" >&2; exit 1; }

health_ok() {
  local body="$1"
  python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("neo4j") is True and d.get("status") == "healthy" else 1)
' < "${body}"
}

wait_health() {
  local body err http last
  body="$(mktemp)"
  err="$(mktemp)"
  http="000"
  last="no health probe attempted"
  local deadline=$((SECONDS + WAIT_SECS))
  log "Waiting for ${HEALTH_URL} (timeout ${WAIT_SECS}s; need neo4j=true, status=healthy)…"
  while (( SECONDS < deadline )); do
    http="$(
      curl -sS -o "${body}" -w '%{http_code}' \
        --connect-timeout 2 --max-time 5 \
        "${HEALTH_URL}" 2>"${err}" || true
    )"
    [[ -z "${http}" ]] && http="000"
    if [[ "${http}" == "200" ]] && health_ok "${body}"; then
      ok "API ready (${HEALTH_URL}, neo4j healthy)"
      rm -f "${body}" "${err}"
      return 0
    fi
    last="HTTP ${http}: $(tr '\n' ' ' < "${err}" | head -c 160) $(tr '\n' ' ' < "${body}" | head -c 120)"
    sleep 2
  done
  log "Last error: ${last}"
  rm -f "${body}" "${err}"
  die "API/Neo4j did not become healthy within ${WAIT_SECS}s (${HEALTH_URL})"
}

run_infona() {
  if [[ -n "${INFONA:-}" ]]; then
    # shellcheck disable=SC2086
    ${INFONA} "$@"
  elif command -v infona >/dev/null 2>&1; then
    infona "$@"
  elif [[ -f "${ROOT}/packages/cli/dist/cli.js" ]]; then
    node "${ROOT}/packages/cli/dist/cli.js" "$@"
  else
    die "infona CLI not found (build packages/cli first)"
  fi
}

assert_haystack() {
  local haystack="$1" needle="$2" label="$3"
  if ! grep -Fqi -- "${needle}" <<<"${haystack}"; then
    printf '%s\n' "${haystack}" >&2
    die "${label}: expected to contain ${needle}"
  fi
}

is_real_key() {
  local key="${1:-}"
  [[ -n "${key}" ]] || return 1
  [[ "${key}" != *... ]] || return 1
  case "${key}" in
    sk-or-...|sk-ant-...|csk-...|changeme|your-key-here|replace-me) return 1 ;;
  esac
  return 0
}

MODE="${MODE,,}"
log "ONTA-287 quickstart smoke  mode=${MODE}  api=${API_URL}"

wait_health

if [[ "${MODE}" == "mocked" ]]; then
  if is_real_key "${OPENROUTER_API_KEY:-}" || is_real_key "${INFONA_OPENROUTER_API_KEY:-}"; then
    die "mocked smoke must not see a real OPENROUTER_API_KEY"
  fi
  if [[ -x "${ROOT}/scripts/load_prebuilt_trials.sh" ]]; then
    log "Loading prebuilt trials graph (no LLM)…"
    "${ROOT}/scripts/load_prebuilt_trials.sh"
  else
    die "scripts/load_prebuilt_trials.sh is missing (needs ONTA-544)"
  fi
  log "Zero-key ask (cached-plan path)…"
  ASK_OUT="$(run_infona --local ask "${HERO_Q}" --kg "${HERO_KG}" 2>&1)" || {
    printf '%s\n' "${ASK_OUT}" >&2
    die "zero-key infona ask failed"
  }
  printf '%s\n' "${ASK_OUT}"
  assert_haystack "${ASK_OUT}" "${HERO_HIT}" "zero-key ask"
  if ! grep -Eiq 'cached-plan|not live inference' <<<"${ASK_OUT}"; then
    printf '%s\n' "${ASK_OUT}" >&2
    die "zero-key ask must be labelled cached-plan replay (not live inference)"
  fi
  ok "zero-key ask returned ${HERO_HIT} (cached-plan replay)"
elif [[ "${MODE}" == "live" ]]; then
  if ! is_real_key "${OPENROUTER_API_KEY:-}" && ! is_real_key "${INFONA_OPENROUTER_API_KEY:-}"; then
    die "live smoke needs a real OPENROUTER_API_KEY (push-to-main / workflow_dispatch only)"
  fi
  log "Live ingest examples/trials.csv --kg ${HERO_KG}…"
  INGEST_OUT="$(run_infona --local ingest examples/trials.csv --kg "${HERO_KG}" --yes 2>&1)" || {
    printf '%s\n' "${INGEST_OUT}" >&2
    die "live infona ingest failed"
  }
  printf '%s\n' "${INGEST_OUT}"
  log "Live ask --model ${QUERY_MODEL} (temperature=0 on Cypher path)…"
  ASK_OUT="$(run_infona --local ask "${HERO_Q}" --kg "${HERO_KG}" --model "${QUERY_MODEL}" 2>&1)" || {
    printf '%s\n' "${ASK_OUT}" >&2
    die "live infona ask failed"
  }
  printf '%s\n' "${ASK_OUT}"
  assert_haystack "${ASK_OUT}" "${HERO_HIT}" "live ask"
  ok "live ask returned ${HERO_HIT} (model=${QUERY_MODEL})"
else
  die "SMOKE_MODE must be mocked or live (got ${MODE})"
fi
