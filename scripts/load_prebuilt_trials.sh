#!/usr/bin/env bash
# Load the shipped prebuilt trials graph (ONTA-544). No API key. No LLM.
#
# From the repo root (or anywhere; this script cds to root):
#   ./scripts/load_prebuilt_trials.sh
#
# Prefers `docker compose` (API image has the neo4j driver and can reach
# bolt://neo4j:7687). Falls back to host `python3 -m infona_client.graph.trials_snapshot`.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

TENANT="${INFONA_TENANT:-default}"
KG="${INFONA_PREBUILT_KG:-trials}"

log()  { printf '  %s\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
die()  { printf '  ✗ %s\n' "$*" >&2; exit 1; }

log "Load prebuilt trials graph (cached-plan replay, not live inference)"
log "  tenant=${TENANT} kg=${KG}"

run_module() {
  python3 -m infona_client.graph.trials_snapshot --tenant "${TENANT}" --kg "${KG}"
}

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  if docker compose ps --status running --services 2>/dev/null | grep -qx api \
     || docker compose ps --services --filter status=running 2>/dev/null | grep -qx api; then
    log "Loading via docker compose (api image)…"
    docker compose run --rm --no-deps \
      -e NEO4J_URI=bolt://neo4j:7687 \
      -e NEO4J_USER=neo4j \
      -e NEO4J_PASSWORD="${NEO4J_PASSWORD:-infona-dev-password}" \
      -e INFONA_GRAPH_BACKEND=neo4j \
      -v "${ROOT}/examples:/app/examples:ro" \
      api python -m infona_client.graph.trials_snapshot \
        --tenant "${TENANT}" --kg "${KG}"
    ok "Prebuilt kg '${KG}' is loaded. Ask (no key):"
    log "  infona ask \"Which Phase 3 NSCLC trials is AstraZeneca running?\" --kg ${KG}"
    log "  (cached-plan replay — not live inference)"
    exit 0
  fi
fi

export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
export NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
export NEO4J_USER="${NEO4J_USER:-neo4j}"
export NEO4J_PASSWORD="${NEO4J_PASSWORD:-infona-dev-password}"
export INFONA_GRAPH_BACKEND="${INFONA_GRAPH_BACKEND:-neo4j}"

if ! command -v python3 >/dev/null 2>&1; then
  die "python3 is required (or start the stack and re-run so docker compose can load)"
fi

log "Loading via host python3 (NEO4J_URI=${NEO4J_URI})…"
if ! run_module; then
  die "Could not load the prebuilt graph. Start the stack first:
    ./scripts/oss_up.sh
  or: docker compose up -d --build"
fi

ok "Prebuilt kg '${KG}' is loaded. Ask (no key):"
log "  infona ask \"Which Phase 3 NSCLC trials is AstraZeneca running?\" --kg ${KG}"
log "  (cached-plan replay — not live inference)"
