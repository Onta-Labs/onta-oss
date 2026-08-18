# Quickstart time claim (ONTA-549)

**Keep: 10-minute quickstart.**

Measured 2026-08-17 on **macOS 26.5.1 + Colima** (Ubuntu 24.04 VM, 4 CPU,
6 GB; warm daemon, empty project, `docker compose build --no-cache`):

**1 min 42 s** from `git clone` to a zero-key `infona ask` returning
**FLAURA2** (cached-plan replay, not live inference).

| phase | clock |
|---|---|
| clone | 20s |
| `npm i -g @infona-ai/cli` | 1s |
| API image `--no-cache` | 54s |
| boot to `/health` | 8s |
| `oss_setup` npm ci + CLI build | 12s |
| load prebuilt + ask | 7s |
| **sum** | **1m 42s** |

Warm rebuild of the API image: **0.4s**. Native **Linux not measured** —
do not invent a number. `neo4j:5-community` was already on the daemon
(first-time pull is extra; 10 minutes still covers it).

Do **not** tighten the README heading to "1 minute." Do **not** start a
GHCR prebuild this week. 10 minutes is an honest upper bound.

Full method and caveats: [docs/quickstart-timing.md](../quickstart-timing.md).
Zero-key commands to paste: [ONTA-544.md](ONTA-544.md).
