#!/usr/bin/env bash
#
# OSS/proprietary boundary guardrail (MOE-21, Layer 4) — the last line of defense.
#
# Inspects the EXACT set of files npm would publish (`npm pack --dry-run`) for
# each public package, and refuses to proceed if any forbidden path is present.
# Static source checks (check_boundary.sh) can miss files pulled in via the
# package.json `files` field, build outputs, or copied assets — this catches
# them in the actual tarball.
#
# Run from npm-publish.yml AFTER `npm run build` so dist/ outputs are included.
# Exits non-zero (failing the publish job) if a forbidden path is found.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PACKAGES=(packages/cograph packages/cograph-mcp packages/onta-mcp packages/onta)

# Paths that must never appear inside a published OSS tarball.
FORBIDDEN='omnix-demo-tenant|cograph/auth/clerk|cograph/enrichment/(exa|perplexity|gs1)|cograph/billing|cograph/entitlement|/\.aws/|secretsmanager'

fail=0
for pkg in "${PACKAGES[@]}"; do
  dir="$ROOT/$pkg"
  [[ -d "$dir" ]] || { echo "skip: $pkg (not present)"; continue; }
  echo "Inspecting tarball for $pkg ..."
  # `npm pack --dry-run` prints the file list to stderr; capture both streams.
  files="$(cd "$dir" && npm pack --dry-run 2>&1)"
  hits="$(echo "$files" | grep -nE "$FORBIDDEN" || true)"
  if [[ -n "$hits" ]]; then
    echo "::error::FORBIDDEN PATH IN $pkg BUNDLE — refusing to publish"
    echo "$hits" | sed 's/^/    /'
    fail=1
  else
    echo "  ok — no forbidden paths in $pkg"
  fi
done

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "All npm bundles clean."
