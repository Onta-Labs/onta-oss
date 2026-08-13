#!/usr/bin/env bash
#
# OSS/proprietary boundary guardrail (MOE-21, Layer 1).
#
# infona-oss ships publicly: npm packages (@infona-ai/cli, @infona-ai/mcp) are
# published on release; the Python package (infona-client) is installable from
# this git repo until a PyPI publish exists; the repo ITSELF is public (so is
# any future sdist, which hatchling builds from VCS-tracked files, i.e.
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
#   CODE scope  — infona_client/ + packages/: what ships via npm (and git/PyPI
#                 for Python) and is copied into the ECS image. Import/module-
#                 path rules apply only here, since docs legitimately name
#                 proprietary paths.
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
SCAN_DIRS=(infona_client packages)
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
#
# The two guard scripts quote host and module-path strings in order to hunt for
# them, so they are exempt from the INFRA check only — never from the secret or
# contact checks. They have no reason to contain a credential, and a real key
# pasted into one would otherwise be invisible.
ALLOW_INFRA='^scripts/check_(boundary|npm_bundle)\.sh:'

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
GREP_ERR="$(mktemp)"
trap 'rm -f "$FILE_LIST" "$GREP_ERR"' EXIT

git ls-files -z --cached --others --exclude-standard > "$FILE_LIST" \
  || die "'git ls-files' failed (not a git repository, or git is unavailable)"
[[ -s "$FILE_LIST" ]] \
  || die "'git ls-files' returned no files"

# Hits are reported as `path:line:match` and parsed back positionally, so a
# colon or newline in a path would make that parse ambiguous — a file named
# `notes:3` can impersonate line 3 of a file named `notes` and borrow its
# escape-hatch marker. Rather than defend against it, refuse to run: no such
# path exists today, and both characters break plenty of other tooling.
if tr '\0' '\n' < "$FILE_LIST" | grep -q ':'; then
  die "a tracked path contains ':', which makes hit parsing ambiguous: $(
    tr '\0' '\n' < "$FILE_LIST" | grep ':' | head -3 | tr '\n' ' ')"
fi

# Whole per-file grep complaints that mean "skip this path", not "the scan is
# broken". Anchored on purpose — see the classification comment in
# run_repo_check for why an unanchored version is dangerous.
BENIGN_GREP_ERR='^grep: .+: (No such file or directory|Permission denied|Is a directory)$'

# --- Per-pattern self-test --------------------------------------------------
# Inferring health from error messages is not enough: a grep with a missing
# shared library, a BusyBox grep with no -o, or a container with no /bin/sh can
# all produce output that LOOKS like nothing was found. So prove the exact
# pipeline works before trusting it to report on the real tree. A scanner that
# cannot demonstrate it works is not evidence of a clean repo.
#
# The probe runs THE REAL PATTERN with THE REAL FLAGS against a string that must
# match it, not a stand-in regex. A generic canary would only prove that
# grep/xargs/sh execute; it would not catch a grep that runs but mishandles a
# `{n,}` interval, a long alternation, or `-i`, which is where these patterns
# actually live.
verify_pattern() {
  # $1 = pattern, $2 = flags, $3 = a string the pattern MUST match
  local pattern="$1" flags="${2:-}" sample="$3" canary out
  canary="$(mktemp)" || die "mktemp failed; cannot run the scanner self-test"
  printf '%s\n' "$sample" > "$canary" \
    || die "could not write the scanner self-test file"
  out="$(printf '%s\0' "$canary" | LC_ALL=C xargs -0r sh -c \
    'grep -IoHnE "$@"; rc=$?; [ "$rc" -le 1 ] || exit 99' \
    sh $flags -- "$pattern" 2>/dev/null)"
  rm -f "$canary"
  [[ -n "$out" ]] || die \
    "scanner self-test failed — grep did not match a string this pattern is \
known to match, so a clean result would be meaningless. Pattern: ${pattern:0:60}..."
}

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
  # $6 = a string $2 MUST match, used to prove the scan actually works
  #
  # Uses -o so each output row is ONE match, not the whole source line. That
  # makes $5 subtract at match level: a real address on the same line as a
  # stock placeholder is no longer suppressed by it.
  local label="$1" pattern="$2" flags="${3:-}" allow="${4:-}" ignore="${5:-}"
  local sample="${6:-}"
  local hits keys suppressed

  # Prove THIS pattern matches on THIS toolchain before believing a null result.
  verify_pattern "$pattern" "$flags" "$sample"

  # A grep that cannot RUN (BusyBox without -o, a locale error, a bad binary on
  # PATH) must not read as "nothing found" — that is the same fail-open class as
  # the git-ls-files hole, one layer down. Two things make it detectable:
  #
  #   * grep's own exit status. Bare `xargs grep` cannot be used for this: xargs
  #     collapses "no match" (1) and "error" (2) into its own 123. So each batch
  #     runs under a shell that maps 0/1 to success and anything higher to 99,
  #     which xargs then surfaces as 123 = a real failure.
  #   * stderr, captured rather than discarded, for a grep that complains but
  #     still exits 0.
  local status line benign_only
  : > "$GREP_ERR"
  # LC_ALL=C keeps ERE ranges, collation, and grep's OWN diagnostics
  # deterministic. A localized "No such file or directory" would not match
  # BENIGN_GREP_ERR, turning an ordinary unreadable path into a hard failure.
  hits="$(LC_ALL=C xargs -0r sh -c \
    'grep -IoHnE "$@"; rc=$?; [ "$rc" -le 1 ] || exit 99' \
    sh $flags -- "$pattern" < "$FILE_LIST" 2>"$GREP_ERR")"
  status=$?

  # A single unreadable path is not a broken environment: `--others` lists
  # untracked files, so a dangling symlink in a dev tree, or a temp file a
  # concurrent build removed between `git ls-files` and here, would otherwise
  # turn a clean run into a hard failure. Warn and skip exactly those.
  #
  # Everything about this classification is deliberately paranoid, because a
  # sloppy version of it re-opens the very fail-open it sits on top of:
  #   * matched with bash, NOT grep — the thing being diagnosed may BE a broken
  #     grep, in which case a grep-based classifier returns "no fatal lines"
  #     and waves the failure through;
  #   * ANCHORED to a whole per-file message, so a linker error ending in
  #     "...cannot open shared object file: No such file or directory" is fatal,
  #     not benign, and a file NAMED after a benign phrase cannot launder an
  #     unrelated error on itself;
  #   * status must be one of xargs' "a child exited non-zero" codes — GNU uses
  #     123, BSD uses 1 — which is what an unreadable file produces. 127 (no
  #     `sh`, no `grep`) is never benign. This allowlist is the weakest of the
  #     three checks precisely because it is the platform-dependent one, so it
  #     leans on `verify_toolchain` having already proven grep/xargs/sh work;
  #     a genuinely broken scanner never reaches this branch.
  if [[ "$status" -ne 0 || -s "$GREP_ERR" ]]; then
    benign_only=1
    [[ -s "$GREP_ERR" ]] || benign_only=0   # non-zero status, silent: fatal
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      [[ "$line" =~ $BENIGN_GREP_ERR ]] && continue
      benign_only=0
      break
    done < "$GREP_ERR"

    if [[ "$benign_only" -eq 1 && ( "$status" -eq 123 || "$status" -eq 1 ) ]]; then
      # ::warning:: so a skipped path is visible in the Actions UI rather than
      # buried in the log. Git stores only 644/755, so a CI checkout should
      # never hit this — if it does, something is worth looking at.
      echo "::warning::boundary scan skipped unreadable path(s): $(
        head -2 "$GREP_ERR" | tr '\n' ' ')"
    else
      die "grep failed while scanning (exit $status$(
        [[ -s "$GREP_ERR" ]] && printf ': %s' "$(head -2 "$GREP_ERR" | tr '\n' ' ')")"
    fi
  fi
  [[ -n "$hits" ]] || return 0

  # Drop matches on lines carrying the escape-hatch marker. -o discarded the
  # source line, so re-read it by path:line — once per unique LINE, not once
  # per match, or a data file with thousands of matches forks sed thousands of
  # times.
  keys="$(printf '%s\n' "$hits" | sed 's/^\([^:]*:[0-9]*\):.*/\1/' | sort -u)"
  suppressed="$(printf '%s\n' "$keys" | while IFS=: read -r hit_file hit_line; do
    [[ -n "$hit_file" && -n "$hit_line" && -r "$hit_file" ]] || continue
    src="$(sed -n "${hit_line}p" "$hit_file" 2>/dev/null)"
    printf '%s' "$src" | grep -qF -- "$MARKER" || continue
    # A marker excuses the hit a HUMAN put it next to. On a minified or
    # single-line data artifact every match shares line 1, so one marker
    # occurring anywhere in it — plausibly in scraped content nobody chose —
    # would disarm the entire file. Refuse to honor it on such a line.
    [[ "${#src}" -le 500 ]] || continue
    printf '%s:%s\n' "$hit_file" "$hit_line"
  done)"
  # NOTE: this guard is load-bearing, not just an optimization. awk's NR==FNR
  # idiom treats an EMPTY first file as "still reading keys", so every hit row
  # would be consumed as a key and nothing would be emitted — silently dropping
  # all findings. Do not remove it without restructuring the awk.
  if [[ -n "$suppressed" ]]; then
    # EXACT key compare on (path, line). `grep -vF` matched the key as an
    # unanchored substring, so a marker on `notes.md:3` also silenced a real
    # leak on `docs/notes.md:3` — the tree already holds a dozen such
    # suffix-colliding path pairs (README.md, package.json, LICENSE), and the
    # failure was completely silent. Paths cannot contain ':' (enforced above),
    # so $1 and $2 are always the path and line even when the MATCH has colons.
    hits="$(awk -F: '
      NR==FNR { seen[$1 ":" $2] = 1; next }
      !(($1 ":" $2) in seen)
    ' <(printf '%s\n' "$suppressed") <(printf '%s\n' "$hits"))"
  fi

  if [[ -n "$allow" ]]; then
    hits="$(printf '%s\n' "$hits" | grep -vE -- "$allow" || true)"
  fi
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
# `AKIA` is the long-lived access key; `ASIA` is the STS/SSO temporary one that
# actually shows up in pasted terminal output. Neptune and RDS writer endpoints
# are the same leak class as the ALB host that prompted all this — this is a
# Neptune/Aurora product, so those hostnames are live infrastructure too.
PAT_INFRA='infona-demo-tenant|secretsmanager|\.elb\.amazonaws\.com|dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com|[0-9]{12}\.dkr\.|\.neptune\.amazonaws\.com|\.rds\.amazonaws\.com|(AKIA|ASIA)[0-9A-Z]{16}|arn:aws:[a-z0-9-]*:[a-z0-9-]*:[0-9]{12}:'

# Secret-shaped literals. Case-SENSITIVE: every one of these prefixes is
# lowercase by the issuer's own format, so -i would only add false positives.
# Lengths are set so the `sk-or-...` style placeholders in .env.example and
# README stay clean while a real key does not. `sk_live_`/`sk_test_` is Clerk —
# the web app and backend share one Clerk instance, so after Anthropic and
# OpenRouter it is the credential most likely to be pasted here. The Postgres
# DSN needs a 12+ char password so local `postgres:postgres` / `u:p` test DSNs
# do not make this check noisy enough that someone weakens it.
PAT_SECRET='sk-ant-[A-Za-z0-9_-]{20,}|sk-or-v1-[A-Za-z0-9]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|sk_(live|test)_[A-Za-z0-9]{20,}|csk-[A-Za-z0-9]{20,}|ak_[A-Z0-9]{24}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|dop_v1_[a-f0-9]{40}|AIza[0-9A-Za-z_-]{30}|-----BEGIN [A-Z ]*PRIVATE KEY-----|postgres(ql)?://[^:@/[:space:]]+:[^@/[:space:]]{12,}@'

# A personal mailbox baked into public code or data. Outbound requests must
# identify the DEPLOYMENT (via config), never an individual — see
# infona_client/api/routes/lambda_functions.py::sec_user_agent. Free-mail
# domains only: a role address on a company domain is a legitimate contact.
PAT_CONTACT='[A-Za-z0-9._%+-]+@(gmail|googlemail|yahoo|hotmail|outlook|live|icloud|me|proton|protonmail|aol)\.[A-Za-z]{2,}'

# Textbook placeholder mailboxes in docstrings are not contacts. ANCHORED, so
# the stock name must be the WHOLE local part: a `poweruser@` or a
# `contact.test@` address is a real one and still caught. Applied to the
# extracted match, never to the source line.
PAT_CONTACT_OK=':(john|jane|jack|jill|alice|bob|carol|dave|foo|bar|test|example|someone|user)([._-](smith|doe|jones|q|public|user|example))?@'

# --- Checks -----------------------------------------------------------------

# 1. No imports from the proprietary parent `infona` namespace.
#    `infona\b` matches `import infona` / `from infona.x` but NOT
#    `infona_client` (underscore is a word char, so no boundary after
#    "infona"). The OSS package is `infona_client`; the parent is `infona`.
run_check "imports the proprietary 'infona' parent package (use infona_client or a plugin protocol)" \
  '(^|[[:space:]])(from|import)[[:space:]]+infona\b'

# 2. No references to proprietary-only source paths.
run_check "references a proprietary-only module path (lives in the parent repo, not OSS)" \
  'infona/auth/clerk|infona/enrichment/(exa|perplexity|gs1)|infona/billing|infona/entitlement'

# Strings each pattern MUST match. They live only in a temp file, never in the
# repo, and exist so a null result can be trusted: see verify_pattern. Each one
# deliberately exercises the awkward part of its pattern — a case-folded host,
# a `{n,}` interval, an alternation branch.
SAMPLE_INFRA='HOST=X.ELB.amazonaws.com'
SAMPLE_SECRET='K=sk-ant-api03-0123456789abcdefghij'  # boundary-ok: synthetic probe string, never present in the repo
SAMPLE_CONTACT='C=Someone.Real@Gmail.com'  # boundary-ok: synthetic probe string, never present in the repo

# 3. Proprietary host / AWS infrastructure — nowhere in the repo. The two
#    guard scripts are exempt HERE ONLY: they quote host strings to hunt them.
run_repo_check "references proprietary infrastructure (deployed host, ECR, Secrets Manager, AWS account)" \
  "$PAT_INFRA" "-i" "$ALLOW_INFRA" "" "$SAMPLE_INFRA"

# 4. Secret-shaped strings — nowhere in the repo, no path exempt.
run_repo_check "contains a secret-shaped string (API key / token / JWT)" \
  "$PAT_SECRET" "" "" "" "$SAMPLE_SECRET"

# 5. Personal contact address — nowhere in the repo except test fixtures.
run_repo_check "hardcodes a personal email address (use a deployment-configured contact)" \
  "$PAT_CONTACT" "-i" "$ALLOW_CONTACT" "$PAT_CONTACT_OK" "$SAMPLE_CONTACT"

if [[ "$fail" -ne 0 ]]; then
  echo ""
  echo "Boundary check FAILED. See docs/oss_proprietary_boundary.md (parent repo)"
  echo "and infona-oss/CONTRIBUTING.md for what is allowed in the OSS tree."
  echo "If a hit is genuinely fine, add '${MARKER} <reason>' to that line."
  exit 1
fi

echo "Boundary check passed - no proprietary leaks detected."
