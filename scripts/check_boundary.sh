#!/usr/bin/env bash
#
# OSS/proprietary boundary guardrail (MOE-21, Layer 1).
#
# cograph-oss is published publicly to npm + PyPI, and the repo ITSELF is
# public (so is the sdist, which hatchling builds from VCS-tracked files, i.e.
# including tests/ and docs/). Public publication is a one-way door. This
# script mechanically enforces that nothing proprietary has leaked. It is run
# by CI (.github/workflows/boundary.yml) on every PR + push, and locally:
#
#     bash scripts/check_boundary.sh
#
# Exit 1 on a violation, exit 2 if the check could not run at all (never a
# silent green — a guard that fails open is worse than no guard).
#
# Two scopes, because the two failure modes differ:
#
#   CODE scope  — cograph_client/ + packages/: what ships to PyPI/npm and is
#                 copied into the ECS image. Import/module-path rules apply
#                 only here, since docs legitimately name proprietary paths.
#   REPO scope  — every git-tracked file, plus uncommitted ones so a local run
#                 catches a leak BEFORE it lands. A leaked ALB host or
#                 credential is just as public sitting in an eval artifact as
#                 in a module. This closes the gap that let an internal ALB
#                 hostname sit in eval_holdout_v2/cross_llm_runs/*.json while
#                 CI reported "no proprietary leaks detected".
#
# Escaping a legitimate hit: put the marker `boundary-ok:` on the line, with a
# reason after it. Prefer that over widening a pattern or exempting a path —
# it is line-scoped, greppable, and self-documenting. Note the marker suppresses
# EVERY match on its line, not just the one you meant, so do not put it on a
# line holding more than the hit you are excusing (never on a minified or
# single-line data file, where it would disarm the whole file).
#
# Self-tested by tests/test_check_boundary.py, which plants each leak class and
# asserts this script rejects it. Add a case there before relaxing anything.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Directories holding published/deployed source.
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

# Line-level escape hatch (see header).
MARKER='boundary-ok:'

# The only paths exempt wholesale, and only from the check named. Every entry
# is a place a real leak could hide, so justify additions and keep them narrow.
# Anchored, literal-dot prefixes.
ALLOW_ALL='^scripts/check_(boundary|npm_bundle)\.sh:'  # the guards state the patterns

# The ER suite tests email normalization, so its fixtures are realistic-looking
# free-mail addresses that no placeholder list can enumerate. Named FILES, not
# `^tests/`: a tree-wide exemption once let a real personal address be committed
# as a "fixture" in a brand-new test file, invisible to this check. A new file
# that needs it must be added here on purpose, or use the boundary-ok marker.
ALLOW_CONTACT='^tests/(resolver/er/test_(normalize|rebuild)\.py|test_csv_resolver\.py):'

fail=0
report() {
  # $1 = human label, $2 = grep output (file:line:match)
  echo "::error::BOUNDARY VIOLATION - $1"
  # Truncated: a hit can land on a multi-megabyte single-line eval artifact,
  # and this log is public. file:line is enough to go look.
  echo "$2" | cut -c1-200 | sed 's/^/    /'
  fail=1
}

die() {
  echo "::error::BOUNDARY CHECK COULD NOT RUN - $1"
  echo "Refusing to report success. Fix the environment and re-run."
  exit 2
}

# --- File list --------------------------------------------------------------
# Captured once, with git's exit status CHECKED. Previously a `|| true` here
# meant that running outside a git repo turned every repo-scope check into a
# no-op that still printed "Boundary check passed".
FILE_LIST="$(mktemp)"
trap 'rm -f "$FILE_LIST"' EXIT

git ls-files -z --cached --others --exclude-standard > "$FILE_LIST" \
  || die "'git ls-files' failed (not a git repository, or git is unavailable)"
[[ -s "$FILE_LIST" ]] \
  || die "'git ls-files' returned no files"

# --- Check runners ----------------------------------------------------------

run_check() {
  # $1 = label, $2 = extended regex — CODE scope, case-sensitive.
  local label="$1" pattern="$2" hits
  hits="$(grep -RnE "${EXCLUDES[@]}" -- "$pattern" "${SCAN_DIRS[@]}" 2>/dev/null || true)"
  if [[ -n "$hits" ]]; then
    report "$label (code scope)" "$hits"
  fi
}

run_repo_check() {
  # $1 = label
  # $2 = extended regex
  # $3 = extra grep flags ("-i" for host/contact patterns; DNS and mail are
  #      case-insensitive, so a capitalized host is the same live host)
  # $4 = path-allowlist regex applied to "file:line:" prefixes, or ""
  # $5 = regex of MATCHES to ignore, or ""
  #
  # Uses -o so each output row is ONE match, not the whole source line. That
  # makes $5 subtract at match level: a real address on the same line as a
  # stock placeholder is no longer suppressed by it.
  local label="$1" pattern="$2" flags="${3:-}" allow="${4:-}" ignore="${5:-}"
  local hits

  hits="$(xargs -0r grep -IoHnE $flags -- "$pattern" < "$FILE_LIST" 2>/dev/null || true)"
  [[ -n "$hits" ]] || return 0

  # Drop lines carrying the escape-hatch marker. Needs the SOURCE line, which
  # -o discarded, so re-read each hit's line by file:line.
  hits="$(printf '%s\n' "$hits" | while IFS= read -r hit; do
    [[ -n "$hit" ]] || continue
    local_file="${hit%%:*}"
    rest="${hit#*:}"
    local_line="${rest%%:*}"
    if [[ -r "$local_file" ]] \
      && sed -n "${local_line}p" "$local_file" 2>/dev/null | grep -qF -- "$MARKER"; then
      continue
    fi
    printf '%s\n' "$hit"
  done)"

  if [[ -n "$allow" ]]; then
    hits="$(printf '%s\n' "$hits" | grep -vE -- "$allow" || true)"
  fi
  hits="$(printf '%s\n' "$hits" | grep -vE -- "$ALLOW_ALL" || true)"
  if [[ -n "$ignore" ]]; then
    # -i: extraction runs case-insensitively, so the subtraction must too, or
    # `Jane.Doe@Gmail.com` is reported while `jane.doe@gmail.com` is not.
    hits="$(printf '%s\n' "$hits" | grep -viE -- "$ignore" || true)"
  fi

  # Collapse the blank line a fully-filtered stream leaves behind.
  hits="$(printf '%s' "$hits" | grep -v '^[[:space:]]*$' || true)"

  if [[ -n "$hits" ]]; then
    report "$label (repo scope)" "$hits"
  fi
}

# --- Patterns ---------------------------------------------------------------

# Proprietary infrastructure: deployed hosts, ECR, Secrets Manager, account
# IDs. Matched case-insensitively — DNS is case-insensitive, so an uppercased
# hostname is the same live endpoint, and `AWS::SecretsManager::Secret` is the
# CloudFormation spelling of the same thing. `secretsmanager` is deliberately
# one word: it matches the ARN, host, and boto client id, while leaving prose
# about "AWS Secrets Manager" in the deploy docs alone.
PAT_INFRA='omnix-demo-tenant|secretsmanager|\.elb\.amazonaws\.com|dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com|[0-9]{12}\.dkr\.|AKIA[0-9A-Z]{16}'

# Secret-shaped literals. Case-SENSITIVE: every one of these prefixes is
# lowercase by the issuer's own format, so -i would only add false positives.
# Lengths are set so the `sk-or-...` style placeholders in .env.example and
# README stay clean while a real key does not.
PAT_SECRET='sk-ant-[A-Za-z0-9_-]{20,}|sk-or-v1-[A-Za-z0-9]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|csk-[A-Za-z0-9]{20,}|ak_[A-Z0-9]{24}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|dop_v1_[a-f0-9]{40}|AIza[0-9A-Za-z_-]{30}'

# A personal mailbox baked into public code or data. Outbound requests must
# identify the DEPLOYMENT (via config), never an individual — see
# cograph_client/api/routes/lambda_functions.py::sec_user_agent. Free-mail
# domains only: a role address on a company domain is a legitimate contact.
PAT_CONTACT='[A-Za-z0-9._%+-]+@(gmail|googlemail|yahoo|hotmail|outlook|live|icloud|me|proton|protonmail|aol)\.[A-Za-z]{2,}'

# Textbook placeholder mailboxes in docstrings are not contacts. ANCHORED, so
# the stock name must be the WHOLE local part: `poweruser@gmail.com` and
# `contact.test@gmail.com` are real addresses and still caught. Applied to the
# extracted match, never to the source line.
PAT_CONTACT_OK=':(john|jane|jack|jill|alice|bob|carol|dave|foo|bar|test|example|someone|user)([._-](smith|doe|jones|q|public|user|example))?@'

# --- Checks -----------------------------------------------------------------

# 1. No imports from the proprietary parent `cograph` namespace.
#    `cograph\b` matches `import cograph` / `from cograph.x` but NOT
#    `cograph_client` (underscore is a word char, so no boundary after
#    "cograph"). The OSS package is `cograph_client`; the parent is `cograph`.
run_check "imports the proprietary 'cograph' parent package (use cograph_client or a plugin protocol)" \
  '(^|[[:space:]])(from|import)[[:space:]]+cograph\b'

# 2. No references to proprietary-only source paths.
run_check "references a proprietary-only module path (lives in the parent repo, not OSS)" \
  'cograph/auth/clerk|cograph/enrichment/(exa|perplexity|gs1)|cograph/billing|cograph/entitlement'

# 3. Proprietary host / AWS infrastructure — nowhere in the repo.
run_repo_check "references proprietary infrastructure (deployed host, ECR, Secrets Manager, AWS account)" \
  "$PAT_INFRA" "-i" "" ""

# 4. Secret-shaped strings — nowhere in the repo.
run_repo_check "contains a secret-shaped string (API key / token / JWT)" \
  "$PAT_SECRET" "" "" ""

# 5. Personal contact address — nowhere in the repo except test fixtures.
run_repo_check "hardcodes a personal email address (use a deployment-configured contact)" \
  "$PAT_CONTACT" "-i" "$ALLOW_CONTACT" "$PAT_CONTACT_OK"

if [[ "$fail" -ne 0 ]]; then
  echo ""
  echo "Boundary check FAILED. See docs/oss_proprietary_boundary.md (parent repo)"
  echo "and cograph-oss/CONTRIBUTING.md for what is allowed in the OSS tree."
  echo "If a hit is genuinely fine, add '${MARKER} <reason>' to that line."
  exit 1
fi

echo "Boundary check passed - no proprietary leaks detected."
