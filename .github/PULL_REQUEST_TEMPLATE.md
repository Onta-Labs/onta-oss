## What

<!-- One or two sentences. Why this PR exists. -->

## OSS / hosted line ([docs/BOUNDARY.md](../docs/BOUNDARY.md))

Does this change what ships in OSS vs hosted Infona? **Yes / No** — if yes, say how.

- [ ] No `from infona.*` / `import infona.*`
- [ ] No default open-web page fetcher (BYOR — callers register their own)
- [ ] No proprietary hosts, platform keys, or parent-repo infra
- [ ] Instance writes go through `infona_client.graph.kg_writer` (`insert_facts` + `refresh_after_write`)
- [ ] Retrieval stays in `infona_client/retrieval/`

## Checks

- [ ] Targeted tests (and `pytest tests/test_file_size_budget.py` if you touched size-scanned paths)
- [ ] First-time author: signed [CLA.md](../CLA.md)
