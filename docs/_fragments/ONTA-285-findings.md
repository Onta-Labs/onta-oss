# ONTA-285 gitleaks findings

Scan: `gitleaks detect --source . --log-opts="--all"` (gitleaks 8.30.1) on
this worktree. **No live credentials** (keys, tokens, private keys) in
history. Do **not** `git filter-repo` / BFG from this ticket.

## Gitleaks hits (13, all false positives)

All `generic-api-key`. Allowlisted in `.gitleaks.toml`.

| Commit | Path | Class | Action |
| --- | --- | --- | --- |
| `e334700` / `41d84ad` | `tests/test_api_registry_routes.py` | Test Fernet: base64(`passphrase-for-tests`) | Allowlist the fixture value |
| `0d61a94` | `tests/test_check_boundary.py` | Planted fakes (`# boundary-ok`) | Allowlist `boundary-ok:` lines |
| `03090d6` | `tests/test_api_registry_byok_guard.py` | Planted detector probes | Allowlist those two strings |
| `c0314d7` | `cograph_client/auth/api_keys.py` | Parameter `api_key` next to `subject=` | Allowlist that call site |
| `d2dcd71` | `eval_holdout_v2/multitable_specs/medicare-part-d-pricing.json` | CSV column names (`spending_id`, `outlier_flag_2023`) | Allowlist that spec file |

## Residual in old blobs (already gone at HEAD)

Default gitleaks rules do not flag emails or ALB hostnames. Targeted
`git log -S` of the already-closed ONTA-285 items:

| First seen | Removed at HEAD | Path | Class | Action |
| --- | --- | --- | --- | --- |
| `74e464c` / `0d61a94^` | `0d61a94` (#268) | `omnix/` and `cograph_client/api/routes/lambda_functions.py` | Founder PII (Gmail in SEC User-Agent) | HEAD uses `INFONA_SEC_USER_AGENT`. Rewrite only if announce requires history purge. |
| `d2dcd71` | `d9c0ab3` (#414) | `eval_holdout_v2/cross_llm_runs/**/holdout_eval_multirun_3x.json` | Live AWS ALB URL in `api_url` | Tree gone at HEAD. Rewrite is a separate escalate. |

Synthetic `@gmail.com` in `tests/` (ER / CSV fixtures) stays. Not listed.

**Verdict:** history is clean of gitleaks-detectable credentials. Residual
PII/host in old blobs is known, HEAD-clean, and out of scope to rewrite here.
