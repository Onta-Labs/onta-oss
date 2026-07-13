# @onta/mcp

MCP (Model Context Protocol) server for [Onta](https://onta.sh). Gives AI agents tools to query, search, ingest, enrich, and manage your context graphs (knowledge graphs) in natural language.

## Install / run

No install needed — use `npx`:

```bash
npx -y @onta/mcp
```

## Claude Desktop / Cursor / Claude Code

```json
{
  "mcpServers": {
    "onta": {
      "command": "npx",
      "args": ["-y", "@onta/mcp"],
      "env": {
        "ONTA_API_KEY": "your-key",
        "ONTA_API_URL": "https://api.onta.sh",
        "ONTA_TENANT": "demo-tenant"
      }
    }
  }
}
```

## Tools exposed

The server registers **15** tools:

- `agent` — the single conversational front door to the Ask-AI agent. Send a natural-language message; the agent classifies intent and either answers a question, asks a clarifying question, or proposes a multi-step plan (enrich attributes, clean/normalize values, merge duplicates, inspect/extend the ontology). A plan is **not executed** until you confirm it by calling `agent` again with the returned `plan_id` as `confirm_plan_id`. Planning is free; any paid step a plan contains (e.g. web enrichment) is authorized server-side at execute time, so confirming honors your tenant's entitlements.
- `list_knowledge_graphs` — list available KGs and their descriptions.
- `ask` — ask a natural-language question against a context graph; returns the answer (and an explanation when available).
- `search` — semantic + keyword (hybrid) search over the free-text attributes of entities: find *which* entities mention/discuss a topic, with a matching snippet as the citation. Use `ask` for aggregate or structured questions.
- `view_ontology` — show the ontology (types, attributes, relationships) across your context graphs.
- `create_knowledge_graph` — create a new, empty KG (optionally with a description).
- `delete_knowledge_graph` — delete a KG and all of its data (irreversible).
- `ingest_csv` — ingest a CSV file by absolute path into a named KG; the schema is inferred automatically. Set `join_on` to merge each row onto the existing entity that carries the same key value instead of minting duplicates.
- `evolve_ontology` — resolve a fuzzy natural-language ontology-evolution ask (no exact names needed); auto-applies high-confidence changes and returns a summary plus any proposals to confirm.
- `apply_ontology_change` — confirm and commit a single proposal returned by `evolve_ontology`.
- `apply_ontology_changes` — confirm and commit several proposals from `evolve_ontology` in one call (one round-trip instead of N; idempotent, per-proposal outcomes).
- `schedule` — set up a recurring standing alert / scheduled refresh (or `list` existing ones): watch values on a cadence and deliver a change payload to a webhook only when they change.
- `list_jobs` — list background jobs (enrichment, dedupe, reconciliation, web-discovery) for the tenant; use it to check on async work the `agent` tool kicked off.
- `get_job` — full record + live progress of a single background job by id (returns instantly with current status).
- `wait_for_job` — block server-side until a background job settles (or a bounded timeout), then return its status + progress — so one call covers a whole wait window instead of polling `get_job` in a loop.

> Enrichment, cleaning/normalization and duplicate-merging are reached **through
> the `agent` tool** — it plans them and, on confirm, runs them as background
> jobs, so any paid step stays authorized server-side at execute time. Use
> `list_jobs` / `get_job` / `wait_for_job` to watch those jobs finish.

## Environment

- `ONTA_API_KEY` — required
- `ONTA_API_URL` — default `https://api.onta.sh`
- `ONTA_TENANT` — default `demo-tenant`

Legacy `COGRAPH_*` and `OMNIX_*` vars are also accepted. Precedence is
`ONTA_*` → `COGRAPH_*` → `OMNIX_*`, so existing configs keep working unchanged.

## License

Apache-2.0. See [LICENSE](./LICENSE).
