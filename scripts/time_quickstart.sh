#!/usr/bin/env bash
# Time the advertised OSS quickstart on this machine (ONTA-549).
#
#   ./scripts/time_quickstart.sh
#
# Does not:
#   - docker system prune -a / docker builder prune (destructive on a shared daemon)
#   - stop compose projects that are not ours
#   - leave ~/.infona/config.json pointing at a timing stack
#
# Cold image path: `docker compose build --no-cache` (advertised). Labelled
# "warm daemon, empty project" when the builder cache is already empty.
# If 7474 / 7687 / 8000 are busy, boot uses 17474 / 17687 / 18000 — same
# images and healthchecks, noted in the report.
#
# Published split: docs/quickstart-timing.md. Claim: docs/_fragments/ONTA-549.md.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${INFONA_QS_RUN_DIR:-${TMPDIR:-/tmp}/infona-qs-${STAMP}}"
mkdir -p "${RUN_DIR}"
REPORT="${RUN_DIR}/report.md"
LOG="${RUN_DIR}/run.log"
OVERRIDE="${RUN_DIR}/ports.override.yml"
NPM_CACHE="${RUN_DIR}/npm-cache"
NPM_PREFIX="${RUN_DIR}/npm-prefix"
CLONE_DIR="${RUN_DIR}/clone"
PROJECT="${COMPOSE_PROJECT_NAME:-infona-onta549}"

API_HOST_PORT=""
NEO4J_HTTP_PORT=""
NEO4J_BOLT_PORT=""
USED_ALT_PORTS=0
CONFIG_BACKUP=""
CLONE_S="skipped"
NPM_S="skipped"
BUILD_S="skipped"
BOOT_S="skipped"
ASK_S="skipped"
REBUILD_S="skipped"
ASK_OK="n/a"

# Seconds, one decimal.
now() { python3 -c 'import time; print(f"{time.time():.3f}")'; }
elapsed() {
  python3 -c 'import sys; a=float(sys.argv[1]); b=float(sys.argv[2]); print(f"{b-a:.1f}")' "$1" "$2"
}
fmt_mmss() {
  python3 -c '
import sys
s=float(sys.argv[1])
m=int(s//60); r=s-60*m
print(f"{m}m {r:04.1f}s" if m else f"{r:.1f}s")
' "$1"
}

log() { printf '%s\n' "$*" | tee -a "${LOG}"; }
sec() { log ""; log "## $*"; }

port_busy() {
  local p="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"${p}" -sTCP:LISTEN >/dev/null 2>&1
    return $?
  fi
  python3 -c '
import socket, sys
s=socket.socket(); s.settimeout(0.3)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(0)
s.close(); sys.exit(1)
' "${p}"
}

write_override() {
  cat >"${OVERRIDE}" <<EOF
# Timing-only. Not part of the advertised compose file.
services:
  neo4j:
    ports: !override
      - "${NEO4J_HTTP_PORT}:7474"
      - "${NEO4J_BOLT_PORT}:7687"
  api:
    ports: !override
      - "${API_HOST_PORT}:8000"
EOF
}

compose() {
  docker compose -p "${PROJECT}" -f "${ROOT}/docker-compose.yml" "$@"
}

compose_up_files() {
  if [[ "${USED_ALT_PORTS}" == "1" ]]; then
    docker compose -p "${PROJECT}" \
      -f "${ROOT}/docker-compose.yml" \
      -f "${OVERRIDE}" \
      "$@"
  else
    compose "$@"
  fi
}

wait_healthy() {
  local url="$1" timeout_s="${2:-120}"
  local body err http deadline
  body="$(mktemp)"
  err="$(mktemp)"
  http="000"
  deadline=$((SECONDS + timeout_s))
  while (( SECONDS < deadline )); do
    http="$(
      curl -sS -o "${body}" -w '%{http_code}' \
        --connect-timeout 2 --max-time 5 \
        "${url}" 2>"${err}" || true
    )"
    [[ -z "${http}" ]] && http="000"
    if [[ "${http}" == "200" ]] && python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("neo4j") is True and d.get("status") == "healthy" else 1)
' < "${body}"; then
      rm -f "${body}" "${err}"
      return 0
    fi
    sleep 2
  done
  log "health last HTTP ${http}: $(tr '\n' ' ' < "${err}" | head -c 200)"
  rm -f "${body}" "${err}"
  return 1
}

restore_config() {
  if [[ -n "${CONFIG_BACKUP}" && -f "${CONFIG_BACKUP}" ]]; then
    mv -f "${CONFIG_BACKUP}" "${HOME}/.infona/config.json"
  fi
}

cleanup() {
  restore_config
  if command -v docker >/dev/null 2>&1; then
    compose_up_files down --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# --- header ---------------------------------------------------------------- #
: >"${LOG}"
sec "Environment"
UNAME="$(uname -srm)"
OS_DETAIL="${UNAME}"
if command -v sw_vers >/dev/null 2>&1; then
  OS_DETAIL="$(sw_vers -productName) $(sw_vers -productVersion) ($(sw_vers -buildVersion)); ${UNAME}"
fi
DOCKER_VER="$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo missing)"
COMPOSE_VER="$(docker compose version --short 2>/dev/null || echo missing)"
NODE_VER="$(node -v 2>/dev/null || echo missing)"
NPM_VER="$(npm -v 2>/dev/null || echo missing)"
PY_VER="$(python3 --version 2>/dev/null || echo missing)"
DOCKER_INFO="$(docker info --format 'os={{.OperatingSystem}} ncpu={{.NCPU}} mem={{.MemTotal}}' 2>/dev/null || true)"
CONTEXT="$(docker context show 2>/dev/null || echo default)"
HAS_PYTHON_SLIM=0
if docker image inspect python:3.12-slim >/dev/null 2>&1; then HAS_PYTHON_SLIM=1; fi
HAS_NEO4J=0
if docker image inspect neo4j:5-community >/dev/null 2>&1; then HAS_NEO4J=1; fi
BUILD_CACHE="$(docker system df --format '{{.Type}} {{.Size}}' 2>/dev/null | grep -i 'Build' || echo 'Build Cache unknown')"

log "stamp:          ${STAMP}"
log "host:           ${OS_DETAIL}"
log "docker:         ${DOCKER_VER}  context=${CONTEXT}  ${DOCKER_INFO}"
log "compose:        ${COMPOSE_VER}"
log "node/npm:       ${NODE_VER} / ${NPM_VER}"
log "python:         ${PY_VER}"
log "python slim:    $([[ ${HAS_PYTHON_SLIM} == 1 ]] && echo present || echo absent — pull is part of build)"
log "neo4j image:    $([[ ${HAS_NEO4J} == 1 ]] && echo present || echo absent — pull is part of boot)"
log "builder:        ${BUILD_CACHE}"
log "label:          warm daemon, empty project (no docker system prune -a)"
log "run dir:        ${RUN_DIR}"

if [[ ! -f "${ROOT}/.env" ]]; then
  cp "${ROOT}/.env.example" "${ROOT}/.env"
  log "created .env from .env.example (placeholder key — zero-key path)"
fi

T0="$(now)"

# --- prerequisites: clone -------------------------------------------------- #
sec "Prerequisites — git clone"
CLONE_S="skipped"
if [[ "${INFONA_QS_SKIP_CLONE:-0}" == "1" ]]; then
  log "skipped (INFONA_QS_SKIP_CLONE=1)"
else
  t1="$(now)"
  git clone "https://github.com/infona-ai/infona-oss.git" "${CLONE_DIR}" >>"${LOG}" 2>&1
  t2="$(now)"
  CLONE_S="$(elapsed "${t1}" "${t2}")"
  log "git clone (full): ${CLONE_S}s ($(fmt_mmss "${CLONE_S}"))"
fi

# --- prerequisites: npm CLI ------------------------------------------------ #
sec "Prerequisites — npm i -g @infona-ai/cli (isolated cold cache)"
NPM_S="skipped"
if [[ "${INFONA_QS_SKIP_NPM:-0}" == "1" ]]; then
  log "skipped (INFONA_QS_SKIP_NPM=1)"
elif ! command -v npm >/dev/null 2>&1; then
  log "npm missing — cannot time CLI install"
else
  t1="$(now)"
  npm_config_cache="${NPM_CACHE}" npm i -g --prefix "${NPM_PREFIX}" @infona-ai/cli >>"${LOG}" 2>&1
  t2="$(now)"
  NPM_S="$(elapsed "${t1}" "${t2}")"
  log "npm i -g (cold cache, isolated prefix): ${NPM_S}s ($(fmt_mmss "${NPM_S}"))"
  "${NPM_PREFIX}/bin/infona" --version >>"${LOG}" 2>&1 || true
fi

# --- build ----------------------------------------------------------------- #
sec "Build — docker compose build --no-cache"
if ! command -v docker >/dev/null 2>&1; then
  log "BLOCKED: docker is not available"
  BUILD_S="blocked"
  BOOT_S="blocked"
  ASK_S="blocked"
  REBUILD_S="skipped"
elif [[ "${INFONA_QS_SKIP_BUILD:-0}" == "1" ]]; then
  log "skipped (INFONA_QS_SKIP_BUILD=1) — using existing image"
  BUILD_S="skipped"
  REBUILD_S="skipped"
else
  t1="$(now)"
  compose build --no-cache api 2>&1 | tee -a "${LOG}"
  t2="$(now)"
  BUILD_S="$(elapsed "${t1}" "${t2}")"
  log "build --no-cache api: ${BUILD_S}s ($(fmt_mmss "${BUILD_S}"))"

  sec "Rebuild — docker compose build (warm cache)"
  t1="$(now)"
  compose build api 2>&1 | tee -a "${LOG}"
  t2="$(now)"
  REBUILD_S="$(elapsed "${t1}" "${t2}")"
  log "rebuild (warm): ${REBUILD_S}s ($(fmt_mmss "${REBUILD_S}"))"
fi

if [[ "${BUILD_S}" != "blocked" ]]; then
  # --- boot ---------------------------------------------------------------- #
  sec "Boot — compose up + /health"
  API_HOST_PORT=8000
  NEO4J_HTTP_PORT=7474
  NEO4J_BOLT_PORT=7687
  if port_busy 8000 || port_busy 7474 || port_busy 7687; then
    USED_ALT_PORTS=1
    API_HOST_PORT=18000
    NEO4J_HTTP_PORT=17474
    NEO4J_BOLT_PORT=17687
    write_override
    log "default ports busy — boot on ${API_HOST_PORT}/${NEO4J_HTTP_PORT}/${NEO4J_BOLT_PORT}"
    if port_busy "${API_HOST_PORT}" || port_busy "${NEO4J_HTTP_PORT}" || port_busy "${NEO4J_BOLT_PORT}"; then
      log "BLOCKED: alternate ports also busy"
      BOOT_S="blocked"
      ASK_S="blocked"
    fi
  else
    log "default ports free — advertised 8000/7474/7687"
  fi

  if [[ "${BOOT_S:-}" != "blocked" ]]; then
    HEALTH_URL="http://127.0.0.1:${API_HOST_PORT}/health"
    t1="$(now)"
    compose_up_files up -d --no-build 2>&1 | tee -a "${LOG}"
    if ! wait_healthy "${HEALTH_URL}" "${INFONA_OSS_UP_TIMEOUT:-180}"; then
      log "BLOCKED: API/Neo4j did not become healthy"
      compose_up_files logs --tail 40 api neo4j 2>&1 | tee -a "${LOG}" || true
      BOOT_S="blocked"
      ASK_S="blocked"
    else
      t2="$(now)"
      BOOT_S="$(elapsed "${t1}" "${t2}")"
      log "boot to healthy: ${BOOT_S}s ($(fmt_mmss "${BOOT_S}"))"
    fi
  fi

  # --- first result -------------------------------------------------------- #
  if [[ "${ASK_S:-}" != "blocked" ]]; then
    sec "First result — load prebuilt + zero-key ask"
    if [[ -f "${HOME}/.infona/config.json" ]]; then
      CONFIG_BACKUP="${RUN_DIR}/config.json.bak"
      cp "${HOME}/.infona/config.json" "${CONFIG_BACKUP}"
    fi
    export COMPOSE_PROJECT_NAME="${PROJECT}"
    if [[ "${USED_ALT_PORTS}" == "1" ]]; then
      export COMPOSE_FILE="${ROOT}/docker-compose.yml:${OVERRIDE}"
    fi
    export INFONA_API_URL="http://127.0.0.1:${API_HOST_PORT}"

    t1="$(now)"
    "${ROOT}/scripts/load_prebuilt_trials.sh" 2>&1 | tee -a "${LOG}"
    ASK_BIN="infona"
    if [[ -x "${NPM_PREFIX}/bin/infona" ]]; then
      ASK_BIN="${NPM_PREFIX}/bin/infona"
    elif ! command -v infona >/dev/null 2>&1; then
      ASK_BIN="npx --yes @infona-ai/cli"
    fi
    # Isolate HOME so a leftover ~/.infona/config.json (tenant / defaultKg)
    # cannot steal the stranger-path ask. A real first run has no such file.
    QS_HOME="${RUN_DIR}/home"
    mkdir -p "${QS_HOME}/.infona"
    ASK_OUT="${RUN_DIR}/ask.out"
    HOME="${QS_HOME}" \
      INFONA_API_URL="${INFONA_API_URL}" \
      INFONA_TENANT=default \
      "${ASK_BIN}" --tenant default ask \
        "Which Phase 3 NSCLC trials is AstraZeneca running?" \
        --kg trials >"${ASK_OUT}" 2>&1 || true
    tee -a "${LOG}" < "${ASK_OUT}"
    t2="$(now)"
    ASK_S="$(elapsed "${t1}" "${t2}")"
    if grep -qi "FLAURA2" "${ASK_OUT}"; then
      log "first result: ${ASK_S}s ($(fmt_mmss "${ASK_S}")) — FLAURA2 seen"
      ASK_OK=1
    else
      log "first result: ${ASK_S}s — FLAURA2 NOT seen"
      ASK_OK=0
      # Direct HTTP probe so a CLI config miss is not reported as "API failed".
      curl -sS -m 30 -X POST \
        "${INFONA_API_URL}/graphs/default/ask" \
        -H 'Content-Type: application/json' \
        -d '{"question":"Which Phase 3 NSCLC trials is AstraZeneca running?","kg_name":"trials"}' \
        | tee "${RUN_DIR}/ask.http.json" | tee -a "${LOG}" || true
      if grep -qi "FLAURA2" "${RUN_DIR}/ask.http.json" 2>/dev/null; then
        log "HTTP /ask returned FLAURA2 (CLI missed; see ask.out)"
        ASK_OK=1
      fi
    fi
    restore_config
    CONFIG_BACKUP=""
  fi
fi

T1="$(now)"
WALL="$(elapsed "${T0}" "${T1}")"

# --- report ---------------------------------------------------------------- #
num_or() { [[ "$1" =~ ^[0-9.]+$ ]] && echo "$1" || echo 0; }
SUM="$(python3 -c '
import sys
vals=[]
for a in sys.argv[1:]:
    try: vals.append(float(a))
    except ValueError: pass
print(f"{sum(vals):.1f}")
' "$(num_or "${CLONE_S}")" "$(num_or "${NPM_S}")" "$(num_or "${BUILD_S}")" "$(num_or "${BOOT_S}")" "$(num_or "${ASK_S}")")"

{
  echo "# Quickstart timing ${STAMP}"
  echo
  echo "- host: ${OS_DETAIL}"
  echo "- docker: ${DOCKER_VER} (${CONTEXT}; ${DOCKER_INFO})"
  echo "- label: warm daemon, empty project; \`docker compose build --no-cache\`"
  echo "- python:3.12-slim $([[ ${HAS_PYTHON_SLIM} == 1 ]] && echo cached || echo pulled during build)"
  echo "- neo4j:5-community $([[ ${HAS_NEO4J} == 1 ]] && echo cached || echo pulled during boot)"
  echo "- ports: $([[ ${USED_ALT_PORTS} == 1 ]] && echo "alternate ${API_HOST_PORT}/${NEO4J_HTTP_PORT}/${NEO4J_BOLT_PORT} (defaults busy)" || echo "advertised 8000/7474/7687")"
  echo "- wall (script): ${WALL}s ($(fmt_mmss "${WALL}"))"
  echo
  echo "| phase | seconds | clock |"
  echo "|---|---:|---|"
  echo "| prerequisites.clone | ${CLONE_S} | $( [[ "${CLONE_S}" =~ ^[0-9.]+$ ]] && fmt_mmss "${CLONE_S}" || echo "${CLONE_S}" ) |"
  echo "| prerequisites.npm | ${NPM_S} | $( [[ "${NPM_S}" =~ ^[0-9.]+$ ]] && fmt_mmss "${NPM_S}" || echo "${NPM_S}" ) |"
  echo "| build.no-cache | ${BUILD_S} | $( [[ "${BUILD_S}" =~ ^[0-9.]+$ ]] && fmt_mmss "${BUILD_S}" || echo "${BUILD_S}" ) |"
  echo "| boot.healthy | ${BOOT_S} | $( [[ "${BOOT_S}" =~ ^[0-9.]+$ ]] && fmt_mmss "${BOOT_S}" || echo "${BOOT_S}" ) |"
  echo "| first_result.ask | ${ASK_S} | $( [[ "${ASK_S}" =~ ^[0-9.]+$ ]] && fmt_mmss "${ASK_S}" || echo "${ASK_S}" ) |"
  echo "| **sum (clone→ask)** | **${SUM}** | **$(fmt_mmss "${SUM}")** |"
  echo "| rebuild.warm | ${REBUILD_S:-skipped} | $( [[ "${REBUILD_S:-}" =~ ^[0-9.]+$ ]] && fmt_mmss "${REBUILD_S}" || echo "${REBUILD_S:-skipped}" ) |"
  echo
  echo "FLAURA2: ${ASK_OK:-n/a}"
} | tee "${REPORT}"

log ""
log "report: ${REPORT}"
