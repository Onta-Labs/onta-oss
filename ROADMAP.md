# Roadmap

This is the public plan. It is **not** a launch checklist, and it is not a
pointer at a private document.

Infona leads with **trust**, not text-to-Cypher. When two sources disagree
about the same entity, Infona resolves it and shows **which source won**,
**why**, and **when that fact was last verified**.

What ships in this repo vs hosted Infona is in
**[docs/BOUNDARY.md](docs/BOUNDARY.md)**. Granular work lives in
[GitHub issues](https://github.com/infona-ai/infona-oss/issues) — open one
or pick one up. Do not add checkboxes here.

## Now

Three themes, in this order:

1. **Entity resolution, conflict, provenance, and freshness.** Same entity,
   two sources, one answer — plus the audit trail. Who won, why the winner
   won, and how stale the winning fact is.
2. **Honest eval.** Measure whether we actually resolve conflicts and keep
   provenance, not whether we can paraphrase a table. Public, synthetic
   fixtures only. No golden-string theatre.
3. **Inbound evidence.** Telemetry and use-case issues from people running
   this. The next cut is shaped by what breaks in the wild.

## Later — not this week

These are real follow-ups. Do **not** start them as drive-bys:

- **ONTA-550** — rename the `infona_client` Python package
- **ONTA-551** — narrow the public Python surface

## Not this product

We are not building:

- a **memory / context layer** for agents
- an **enrichment vendor** (you bring retrieval; see BOUNDARY)
- a **RAG wrapper** over a vector index
- **"chat with your CSV"**

Those paths exist elsewhere. This repo is a knowledge layer you can audit.
