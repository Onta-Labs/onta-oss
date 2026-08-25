# Roadmap

This is the public plan. It is **not** a launch checklist, and it is not a
pointer at a private document.

Infona leads with **trust**, not text-to-Cypher. URI merge is applied
today. Field winners are not the sole current Neo4j values yet —
valid-time is not ported. The remaining gap is writing that report as
graph state: which source won, why, and when the fact was last verified.

What ships in this repo vs hosted Infona is in
**[docs/BOUNDARY.md](docs/BOUNDARY.md)**. Granular work lives in
[GitHub issues](https://github.com/infona-ai/infona-oss/issues) — open one
or pick one up. Do not add checkboxes here.

## Now

Three themes, in this order:

1. **Entity resolution, conflict, provenance, and freshness.** Fragment
   URIs already collapse. Field-conflict winners as the sole current
   fact, plus valid-time, are the gap — same entity, two sources, one
   current answer as graph state, not only a rebuild report.
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
