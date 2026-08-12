# What’s open source vs hosted

Short answer for OSS users. The long engineering map lives in the monorepo
(`docs/oss_proprietary_boundary.md`); this page is the public contract.

## You get in this repo (OSS)

| Capability | Notes |
|------------|--------|
| **Ingest** CSV / JSON / text → knowledge graph | Deterministic row mapping after one schema pass |
| **Ontology** auto-extend + English evolve | Workspace-local types, attributes, relationships |
| **Ask** natural language → exact graph query | Cypher on Neo4j by default |
| **MCP + CLI + HTTP API** | Same backend routes everywhere |
| **Search / grep** over your graph | |
| **Clean, dedupe-at-write, conflict policy** | |
| **Export** JSON / CSV | `GET …/kgs/{kg}/export` · `infona export` |
| **Plugin seams** | Auth, enrichment adapters, fetchers — you wire them |
| **Free / no-key data sources** | NPPES, ClinicalTrials.gov, Open Food Facts, … |
| **Bring-your-own-key** registry entries | Dormant until *your* key is in the env (e.g. FRED, GeoNames free username) |

## Bring your own retrieval (important)

**This OSS build does not register an open-web page fetcher by default.**

Enrichment and research that need to *fetch arbitrary URLs* will politely
decline unless **you** register a fetcher (or use the hosted product). That is
intentional:

- OSS = structure, resolve, query **your** data and **your** retrieval
- Hosted = optional paid web search / render ladders (Firecrawl, Perplexity, …)

Wikidata and other free adapters that do not need open-web scrape still work.

## Hosted-only (Infona cloud / premium)

| Capability | Why not OSS |
|------------|-------------|
| Managed API keys Infona provisions and bills | Definition of premium |
| Open-web enrichment ladder (paid scrape / search) | Cost + abuse surface |
| Curated **Enhanced** ontology content (layer B) | Product content |
| Production Clerk auth packaging | Hosted identity |
| Explorer web app polish / review-queue UIs | Hosted UI |
| Plans, quotas, self-serve billing | Commercial |

The **mechanism** for layers, entitlement checks, and enrichment protocols is
open; the **content and managed ops** are not.

## Local graph store

| Track | Use |
|-------|-----|
| **Neo4j 5 Community** (`docker compose up -d neo4j`) | **Default / production path** — Cypher, GraphStore |
| Fuseki (optional compose profile `legacy-sparql`) | Legacy SPARQL path only — not for new work |

Set `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD` (see `.env.example`). Never
expect a platform-held Neo4j password in OSS.

## License

Apache 2.0 — see [LICENSE](../LICENSE) and [NOTICE](../NOTICE).
