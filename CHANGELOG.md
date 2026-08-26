# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

From **0.1.19** onward, `infona-client` (PyPI), `@infona-ai/cli`, and
`@infona-ai/mcp` (npm) share one lockstep version. Earlier `0.1.x` numbers
were independent npm publishes — CLI and MCP did not bump together, and
Python was not in the release.

Notes for **0.1.17–0.1.20** and **0.1.21–0.1.42** are reconstructed from
git history and PR titles (this file was not kept current through those
patches). They are honest summaries, not contemporaneous release notes.

Git tags currently lag the shipped lockstep series. `v0.1.0` and `v0.1.16`
exist; `v0.1.16` points at a 2026-05-05 commit that predates the current
package layout and is **not** the npm `0.1.16` publish. `v0.1.17`–`v0.1.42`
are **not** tagged here: 0.1.17 and 0.1.18 were never a single monorepo
release (see below), and inventing 22 patch tags would mislabel history.
Lockstep commits that *did* ship: `c97ca13` (0.1.19), `81cc384` (0.1.20),
`1f46a29` (0.1.21), `7662ea7` (0.1.42). GitHub Releases still list
`v0.1.16` as latest. The integrator / founder can annotate those if they
want the tag series to catch up.

## [Unreleased]

### Fixed

- NL average/avg/mean + a numeric noun resolves the noun leaf (not a minted
  `average_<noun>` column) and grounds aggregate AVG. Post-plan repair also
  unwraps invented `average_*`/`avg_*`/`mean_*`/`total_*` keys onto a
  declared/populated leaf of the same noun.
- How-many + a row-returning list helper (`literal_compare`,
  `literal_values`) is rewritten to the named count twin before execute
  (same filters). Leftover list names still fail-close.
- `rewrite_subject` rekeys `:ValidityInterval` subject, `interval_id`,
  and `statement_id` so ER merge does not leave closed intervals on
  the loser URI.

### Added

- `DELETE /graphs/{tenant}/functions/{function_name}?entity_type=`
  detaches a TENANT-layer function attachment (404 if missing; Enhanced
  and Public attachments are refused).
- MCP `er_rebuild` (`Client.erRebuild`) runs the same second-pass
  entity-resolution path as `infona er rebuild`.
- GraphStore ingest batch rollback: a failed ingest removes subjects
  stamped with that `batch_id` via `delete_facts` (ONTA-528).

### Changed

- MCP `delete_function` and SDK `Client.deleteFunction` require
  `entity_type` (always sent as `?entity_type=`), matching
  `DELETE /graphs/{tenant}/functions/{function_name}`.
- Ask and explore GraphStore reads keep current valid-time literals
  (`valid_to` null/absent). Closed facts stay stored.
- Going-forward lockstep publishes create a `v*` git tag and GitHub
  Release after a successful npm + PyPI bump (notes point at this
  file). `v0.1.17`–`v0.1.42` are not backfilled.
- README and launch-surface docs: the product loop is schema → Neo4j →
  ask. `er rebuild` URI merge is applied. Authority-axis field winners
  are the current graph value (Austin HQ; San Francisco stored/closed);
  equal-trust `credit_rating` stays dual-current and flagged. Homepage
  eval table demoted to a pointer at `docs/EVAL.md`.
- README eval blurb no longer headlines 6/8. Query accuracy is a live
  always-LLM Cypher pin; the 2026-08-19 6/8 table is a historical n=8
  artifact in `docs/EVAL.md`. `scripts/run_public_eval.py` defaults to
  `--questions 32`. Live n=32 was not run in this change.
- Live OSS quickstart steps skip when `OPENROUTER_API_KEY` is empty
  (job `if:` cannot read `secrets`; empty key is not a failure).

## [0.1.21–0.1.42] - 2026-08-18 – 2026-08-25

Lockstep series of `@infona-ai/cli`, `@infona-ai/mcp`, and
`infona-client` (`1f46a29` … `7662ea7`). Reconstructed from git history
and PR titles. One section on purpose — these were daily lockstep bumps,
not 22 empty patch notes. GitHub tags/releases still lag (`v0.1.16` is
latest on GitHub).

### Added

- OSS launch stack (#436): zero-key cached-plan ask (FLAURA2, not live
  inference), synthetic messy-suppliers `er rebuild` fixture, public
  eval pin with visible misses, opt-in anonymous job telemetry,
  clean-room quickstart smoke, SECURITY.md / CoC / CLA / public ROADMAP.
  Root `package.json` matches the lockstep version; this changelog
  landed in that stack.
- Optional dlt extract: `POST /graphs/{tenant}/ingest/dlt`, MCP
  `ingest_dlt`, extra-gated handoff (ONTA-553).
- Connector catalog + scheduled reads (ONTA-555, #458).

### Changed

- Product graph path is Neo4j GraphStore / Cypher. Retired SPARQL
  readers on ontology, NLP, ask coverage, ER ingest, watch/layers,
  agent schema, normalize, invoke, search (#442, #445, #447–#451,
  #448). Suppression retractions stick on Neo4j (#453). Normalize
  rule-apply reads ported (ONTA-534, #455).

### Fixed

- NL→Cypher: postable Cerebras body + provider failover (#446).
- Invented relationship edges answering "No results found." (#456,
  follow-up #457).
- Count/aggregate asks dumping rows instead of a number: unique leaf
  values (#440), group-by SUM top-1 (#439), how-many (#438).
- Reserved `"Entity"` domain aborting every reconcile (#444).
- Failed normalize rule apply is durable, not a log line (#452).
- dlt extract SSRF guard; never resolve `env:` on the server.
- gitleaks allowlist for the public PostHog project token (#443).
- Query-intent token hygiene so eval-set lists cannot leak (#437).

## [0.1.20] - 2026-08-17

Lockstep patch of `@infona-ai/cli`, `@infona-ai/mcp`, and `infona-client`.
Reconstructed from `81cc384` and #421.

### Added

- In-repo lockstep plus an exact `@infona-ai/mcp` → `@infona-ai/cli` pin
  as a required test and a pre-upload publish gate (#421).
- Daily `release-lockstep.yml` compares latest npm and PyPI so a
  partial publish cannot sit unnoticed (#421).

## [0.1.19] - 2026-08-17

First lockstep release of `@infona-ai/cli`, `@infona-ai/mcp`, and
`infona-client`. Reconstructed from `c97ca13`, #420, and #419.

### Added

- `infona-client` publishes on the same version as the npm packages
  (#420). `scripts/sync_release_version.py` is the writer.
- `pypi-publish.yml` replaces `npm-publish.yml`: one bump, inspect npm
  and PyPI artifacts, publish all three (#420).

### Fixed

- `oss_up.sh` names a port-in-use collision instead of dumping raw
  Docker output (#419).

## [0.1.18] - 2026-08-17

`@infona-ai/cli` **only** (`a30d386`). There was no MCP `0.1.18` and no
PyPI `0.1.18`. Reconstructed. **Not tagged.**

### Added

- One-command OSS loop: `docker compose` runs Neo4j + API; `oss_up.sh`
  waits on `/health` and writes local CLI config (#418).
- First-hour CLI errors map to the next command (#418).
- MCP accepts the README JSON with no API key on localhost (#418).

### Changed

- README / hero / demo assets around the product loop (#411–#416).
- File-size budget ratchet and large-module extracts into sibling
  facades (#386, #394–#410).

### Removed

- Eval artifacts from the public OSS tree (#414).
- Stale SPARQL-era `ARCHITECTURE.md` (#415).

### Fixed

- `csv_llm` import of `EntitySpec` (#393).

## [0.1.17] - 2026-08-14 / 2026-08-17

Independent package numbers, **not** one monorepo release. Reconstructed.
**Not tagged.**

- `@infona-ai/cli@0.1.17` shipped 2026-08-14 (`a17d909`) while MCP was
  still `0.1.15`.
- `@infona-ai/mcp@0.1.17` shipped 2026-08-17 (`0b40dce`) while CLI was
  already `0.1.18`.

### Added

- Ontology authoring: Lambda functions, skill upload, user API sources
  (#391).

### Removed

- Web-discovery ingest from OSS (BYOR; callers register a fetcher)
  (#390).

### Changed

- Facade extracts: `schema_resolver`, `pipeline`, `memory_store`,
  `explore`, `csv_resolver`, `cypher_generate`, `client.ts`
  (#387–#392).

[Unreleased]: https://github.com/infona-ai/infona-oss/compare/7662ea7f4e94e973d9075afa8ada01ee6b77dc81...HEAD
[0.1.21–0.1.42]: https://github.com/infona-ai/infona-oss/compare/81cc384cffe23cb033c7765f35908aec1a093923...7662ea7f4e94e973d9075afa8ada01ee6b77dc81
[0.1.20]: https://github.com/infona-ai/infona-oss/compare/c97ca133a63f6a89ad08d0c05401503e2e7ef193...81cc384cffe23cb033c7765f35908aec1a093923
[0.1.19]: https://github.com/infona-ai/infona-oss/compare/a30d386f6a013420027cf1480673b53fa1454508...c97ca133a63f6a89ad08d0c05401503e2e7ef193
[0.1.18]: https://github.com/infona-ai/infona-oss/commit/a30d386f6a013420027cf1480673b53fa1454508
[0.1.17]: https://github.com/infona-ai/infona-oss/commit/0b40dcef18ac02fd29dcf1667bb5b263db626921
