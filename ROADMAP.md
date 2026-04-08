# Omnix Roadmap

## Current State (April 8, 2026)

- 5 KGs, 5 domains, 88% average accuracy on 20-question evals
- CSV ingestion pipeline (1 LLM call schema inference + deterministic mapping)
- NL query pipeline (ontology retrieval → example bank → SPARQL generation)
- Deployed on AWS ECS Fargate + Neptune
- 300 examples in RAG bank, 597 fine-tuning pairs

---

## Phase 1: Open-Source Launch Prep (D-3 to D-1)

### P0 — Must have for launch

- [ ] **Local graph DB support** — Docker Compose with Apache Fuseki so users don't need Neptune/AWS. `docker compose up` + `omnix ingest` should work out of the box.
- [ ] **Strip premium features** — move enrichment, text ingestion, Neptune-specific optimizations behind feature flags or into a separate package
- [ ] **Remove hardcoded AWS endpoints** — ALB URLs, ECR refs, Neptune endpoints all from env vars
- [ ] **Clean repo** — remove untracked CSVs, .bak files, eval reports from git history
- [ ] **Sample dataset** — include a small CSV in `examples/` for the quickstart
- [ ] **README rewrite** — open-source audience, 5-minute quickstart, GIF/video demo
- [ ] **.env.example** — document all required env vars
- [ ] **LICENSE** — Apache 2.0
- [ ] **CI green** — fix the failing Tests workflow

### P1 — Should have for launch

- [ ] **MCP server** — expose `ask`, `list_kgs`, `ingest` as MCP tools so AI agents (Claude, Cursor, etc.) can query knowledge graphs
- [ ] **CONTRIBUTING.md** — dev setup, code style, PR process
- [ ] **Integration test** — ingest sample CSV + ask 5 questions, assert >= 80%
- [ ] **Ollama support** — test and document local model usage (no API key needed)
- [ ] **Demo video** — 30-60 second screen recording for README

---

## Phase 2: Launch (D0 to D+7)

- [ ] Push repo to public
- [ ] Hacker News Show HN post
- [ ] Reddit (r/MachineLearning, r/Python, r/LocalLLaMA)
- [ ] Twitter/X thread with demo
- [ ] Blog post: "How we built a KG platform at 88% accuracy"
- [ ] Monitor issues, respond within 24 hours
- [ ] Ship hotfixes based on early feedback

---

## Phase 3: Post-Launch Community (D+7 to D+30)

- [ ] **Additional graph DB backends** — Blazegraph, Oxigraph, or RDFLib in-memory (based on community demand)
- [ ] **More LLM providers** — document Groq, Together, Fireworks, local vLLM
- [ ] **"Good first issue" backlog** — label 10+ issues for new contributors
- [ ] **Eval improvements** — better ground truth computation, natural language questions
- [ ] **Premium waitlist page** — collect emails for hosted version

---

## Phase 4: Premium Product (Month 2-3)

- [ ] **Managed infrastructure** — hosted Neptune, zero-config setup
- [ ] **Data enrichment** — 2-phase LLM enrichment as a service (the 60% → 88% jump)
- [ ] **Text/document ingestion** — fact extraction with noise filtering
- [ ] **Fine-tuned query model** — train on 597+ pairs for faster, cheaper, more accurate SPARQL
- [ ] **Global KG** — shared knowledge graph seeded with public datasets
- [ ] **Multi-tenant auth** — API keys, usage tracking, billing
- [ ] **Web dashboard** — visual ontology explorer, query playground

---

## Phase 5: Scale (Month 3-6)

- [ ] **Cross-KG queries** — ask questions that span multiple knowledge graphs
- [ ] **Incremental ingestion** — append new CSV rows without full reingest
- [ ] **Entity resolution** — "TX" = "Texas", dedup across data sources
- [ ] **Streaming ingestion** — real-time data from APIs, webhooks
- [ ] **Enterprise features** — SSO, audit logs, data governance, on-prem deployment
