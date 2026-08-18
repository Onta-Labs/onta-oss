# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

From **0.1.19** onward, `infona-client` (PyPI), `@infona-ai/cli`, and
`@infona-ai/mcp` (npm) share one lockstep version. Earlier `0.1.x` numbers
were independent npm publishes — CLI and MCP did not bump together, and
Python was not in the release.

Notes for **0.1.17–0.1.20** are reconstructed from git history and PR
titles (this file did not exist when those versions shipped). They are
honest summaries, not contemporaneous release notes.

Git tags currently lag the shipped lockstep series. `v0.1.0` and `v0.1.16`
exist; `v0.1.16` points at a 2026-05-05 commit that predates the current
package layout and is **not** the npm `0.1.16` publish. `v0.1.17`–`v0.1.20`
are **not** tagged here: 0.1.17 and 0.1.18 were never a single monorepo
release (see below), and inventing those tags would mislabel history.
Lockstep commits that *did* ship: `0d0d3ee` (0.1.19), `c5a320a` (0.1.20).
The integrator / founder can annotate those if they want the tag series
to catch up.

## [Unreleased]

### Added

- This changelog.

### Fixed

- Root `package.json` now matches the lockstep version (`0.1.20`).
  `scripts/sync_release_version.py` includes that manifest so the skew
  cannot recur.

## [0.1.20] - 2026-08-17

Lockstep patch of `@infona-ai/cli`, `@infona-ai/mcp`, and `infona-client`.
Reconstructed from `c5a320a` and #421.

### Added

- In-repo lockstep plus an exact `@infona-ai/mcp` → `@infona-ai/cli` pin
  as a required test and a pre-upload publish gate (#421).
- Daily `release-lockstep.yml` compares latest npm and PyPI so a
  partial publish cannot sit unnoticed (#421).

## [0.1.19] - 2026-08-17

First lockstep release of `@infona-ai/cli`, `@infona-ai/mcp`, and
`infona-client`. Reconstructed from `0d0d3ee`, #420, and #419.

### Added

- `infona-client` publishes on the same version as the npm packages
  (#420). `scripts/sync_release_version.py` is the writer.
- `pypi-publish.yml` replaces `npm-publish.yml`: one bump, inspect npm
  and PyPI artifacts, publish all three (#420).

### Fixed

- `oss_up.sh` names a port-in-use collision instead of dumping raw
  Docker output (#419).

## [0.1.18] - 2026-08-17

`@infona-ai/cli` **only** (`817d7f2`). There was no MCP `0.1.18` and no
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

- `@infona-ai/cli@0.1.17` shipped 2026-08-14 (`a2ba83d`) while MCP was
  still `0.1.15`.
- `@infona-ai/mcp@0.1.17` shipped 2026-08-17 (`dcdcff6`) while CLI was
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

[Unreleased]: https://github.com/infona-ai/infona-oss/compare/c5a320a5c4f574486beee539b8444455194a1d32...HEAD
[0.1.20]: https://github.com/infona-ai/infona-oss/compare/0d0d3ee403abd956cf4a1d34f5db21fc0a703370...c5a320a5c4f574486beee539b8444455194a1d32
[0.1.19]: https://github.com/infona-ai/infona-oss/compare/dcdcff659888c680fea7bf4934fd6177f5c159ea...0d0d3ee403abd956cf4a1d34f5db21fc0a703370
[0.1.18]: https://github.com/infona-ai/infona-oss/commit/817d7f2e4d037e1875232177020d1443d140bc1b
[0.1.17]: https://github.com/infona-ai/infona-oss/commit/dcdcff659888c680fea7bf4934fd6177f5c159ea
