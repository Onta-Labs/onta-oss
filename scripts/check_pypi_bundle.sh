#!/usr/bin/env bash
#
# Refuse to publish a PyPI sdist/wheel that carries tests, eval dumps, or
# proprietary paths. Run after `uv build` (artifacts in dist/).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/dist"

FORBIDDEN='(^|/)(tests|eval_holdout_v2|eval_reports|\.github|packages)/|infona-demo-tenant|infona/auth/clerk|infona/enrichment/(exa|perplexity|gs1)|infona/billing|infona/entitlement'

shopt -s nullglob
wheels=("$DIST"/*.whl)
sdists=("$DIST"/*.tar.gz)
if [[ ${#wheels[@]} -eq 0 || ${#sdists[@]} -eq 0 ]]; then
  echo "no wheel+sdist in $DIST — run uv build first" >&2
  exit 2
fi

fail=0
list_archive() {
  python3 - "$1" <<'PY'
import sys, tarfile, zipfile
path = sys.argv[1]
if path.endswith(".whl"):
    with zipfile.ZipFile(path) as zf:
        print("\n".join(zf.namelist()))
else:
    with tarfile.open(path, "r:gz") as tf:
        print("\n".join(tf.getnames()))
PY
}

for artifact in "${wheels[@]}" "${sdists[@]}"; do
  echo "Inspecting $artifact ..."
  files="$(list_archive "$artifact")"
  hits="$(printf '%s\n' "$files" | grep -nE "$FORBIDDEN" || true)"
  if [[ -n "$hits" ]]; then
    echo "::error::FORBIDDEN PATH IN $(basename "$artifact") — refusing to publish"
    echo "$hits" | sed 's/^/    /'
    fail=1
  else
    echo "  ok — $(basename "$artifact")"
  fi
done

exit "$fail"
