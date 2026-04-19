# cograph

Node.js SDK and CLI for [Cograph](https://cograph.cloud) — turn raw data into a queryable knowledge graph.

## Install

```bash
npm install cograph
```

## SDK

```ts
import { Client, CographError } from "cograph";

const client = new Client({ apiKey: process.env.COGRAPH_API_KEY });

await client.ingest("sales.csv", { kg: "sales" });
const result = await client.ask("What's the average deal size by region?", { kg: "sales" });
console.log(result.answer);
```

### Constructor

```ts
new Client({
  apiKey?: string,    // env: COGRAPH_API_KEY
  baseUrl?: string,   // env: COGRAPH_API_URL (default: https://api.cograph.cloud)
  tenant?: string,    // env: COGRAPH_TENANT (default: demo-tenant)
})
```

### Methods

- `ingest(pathOrText, { kg?, contentType? })` — auto-detects CSV by extension and uses two-step schema/rows flow; otherwise sends raw content.
- `ask(question, { kg? })` — returns `{ answer, sparql?, ... }`.
- `listKgs()` — list knowledge graphs.
- `deleteKg(name)` — delete a knowledge graph.

All errors throw `CographError`.

## CLI

```bash
# List / create / delete knowledge graphs
npx cograph kg list
npx cograph kg create my-data --description "demo"
npx cograph kg delete my-data

# Ingest data
npx cograph ingest data.csv --kg my-data
npx cograph ingest --text "Alice works at Acme" --kg my-data

# Ask questions
npx cograph ask "How many companies?" --kg my-data
npx cograph ask "Top 5 deals" --kg my-data --debug

# Ontology + clear
npx cograph ontology types
npx cograph clear --kg my-data --yes
```

### Environment

- `COGRAPH_API_KEY` — required
- `COGRAPH_API_URL` — default `https://api.cograph.cloud`
- `COGRAPH_TENANT` — default `demo-tenant`

Legacy `OMNIX_*` vars are also accepted.

> PDF ingestion is not yet supported in the Node CLI. Use the Python CLI or POST raw bytes to the API.

## License

MIT
