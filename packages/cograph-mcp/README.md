# cograph-mcp

MCP (Model Context Protocol) server for [Cograph](https://cograph.cloud). Gives AI agents tools to query and ingest data into your knowledge graphs.

## Install / run

No install needed — use `npx`:

```bash
npx -y cograph-mcp
```

## Claude Desktop / Cursor / Claude Code

```json
{
  "mcpServers": {
    "cograph": {
      "command": "npx",
      "args": ["-y", "cograph-mcp"],
      "env": {
        "COGRAPH_API_KEY": "your-key",
        "COGRAPH_API_URL": "https://api.cograph.cloud"
      }
    }
  }
}
```

## Tools exposed

- `list_knowledge_graphs` — list available KGs and descriptions
- `ask` — ask a natural language question; returns the answer
- `ingest_csv` — ingest a CSV file by absolute path into a named KG
- `view_ontology` — show types, attributes, relationships across KGs

## Environment

- `COGRAPH_API_KEY` — required
- `COGRAPH_API_URL` — default `https://api.cograph.cloud`
- `COGRAPH_TENANT` — default `demo-tenant`

Legacy `OMNIX_*` vars are also accepted.

## License

MIT
