#!/usr/bin/env bash
#
# OSS/proprietary boundary guardrail (MOE-21, Layer 1).
#
# cograph-oss is published publicly to npm + PyPI. Public publication is a
# one-way door. This script mechanically enforces that nothing proprietary has
# leaked into the OSS tree. It is run by CI (.github/workflows/boundary.yml) on
# every PR + push, and is safe to run locally:  bash scripts/check_boundary.sh
#
# Exit non-zero (and print the offending file:line) on ANY violation.
#
# Scope: source trees that get published or deployed —
#   cograph_client/  (Python package, copied into the ECS Docker build)
#   packages/        (TS SDK + MCP server, published to npm)
# We deliberately do NOT scan docs/, tests fixtures, or this script itself,
# which legitimately mention the forbidden patterns when describing them.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Directories to scan. Build artifacts and vendored deps are excluded.
SCAN_DIRS=(cograph_client packages)
EXCLUDES=(
  --exclude-dir=node_modules
  --exclude-dir=dist
  --exclude-dir=build
  --exclude-dir=__pycache__
  --exclude-dir=.pytest_cache
  --exclude-dir=coverage
  --exclude=package-lock.json
  --exclude=*.min.js
  --exclude=*.map
)

fail=0
report() {
  # $1 = human label, $2 = grep output (file:line:match)
  echo "::error::BOUNDARY VIOLATION — $1"
  echo "$2" | sed 's/^/    /'
  fail=1
}

run_check() {
  # $1 = label, $2 = extended regex
  local label="$1" pattern="$2" hits
  hits="$(grep -RnE "${EXCLUDES[@]}" -- "$pattern" "${SCAN_DIRS[@]}" 2>/dev/null || true)"
  if [[ -n "$hits" ]]; then
    report "$label" "$hits"
  fi
}

# 1. No imports from the proprietary parent `cograph` namespace.
#    `cograph\b` matches `import cograph` / `from cograph.x` but NOT
#    `cograph_client` (underscore is a word char, so no boundary after
#    "cograph"). The OSS package is `cograph_client`; the parent is `cograph`.
run_check "imports the proprietary 'cograph' parent package (use cograph_client or a plugin protocol)" \
  '(^|[[:space:]])(from|import)[[:space:]]+cograph\b'

# 2. No proprietary host / AWS infrastructure references.
run_check "references proprietary infrastructure (ALB host, Secrets Manager, AWS account)" \
  'omnix-demo-tenant-dev|secretsmanager|\.elb\.amazonaws\.com|AKIA[0-9A-Z]{16}'

# 3. No references to proprietary-only source paths.
run_check "references a proprietary-only module path (lives in the parent repo, not OSS)" \
  'cograph/auth/clerk|cograph/enrichment/(exa|perplexity|gs1)|cograph/billing|cograph/entitlement'

# 4. No hardcoded secret-shaped strings.
run_check "contains a secret-shaped string (API key / token / JWT)" \
  'sk-ant-[A-Za-z0-9]{16}|ak_[A-Z0-9]{24}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|dop_v1_[a-f0-9]{40}|AIza[0-9A-Za-z_-]{30}'

if [[ "$fail" -ne 0 ]]; then
  echo ""
  echo "Boundary check FAILED. See docs/oss_proprietary_boundary.md (parent repo)"
  echo "and cograph-oss/CONTRIBUTING.md for what is allowed in the OSS tree."
  exit 1
fi

echo "Boundary check passed — no proprietary leaks detected in: ${SCAN_DIRS[*]}"
