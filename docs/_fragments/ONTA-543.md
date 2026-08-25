# Quickstart — ER first (ONTA-543)

The clean `trials.csv` loop shows schema → graph → ask. This is the
**third command**: a messy table whose *point* is URI merge, plus
field-conflict winners applied as current graph values.

`examples/suppliers-messy.csv` is **synthetic** (Acme / Globex / Initech,
fake tax IDs). No real customer data, no spider-bench leakage.

```bash
infona ingest examples/suppliers-messy.csv --kg suppliers
infona er rebuild --kg suppliers
```

Ingest writes every row as its own Supplier fragment (intra-batch ER
cannot see siblings yet). `er rebuild` re-blocks the already-ingested
graph and collapses the fragments (6→3). Headquarters and credit_rating
then land as graph state: Austin is the current HQ (SF stored/closed);
equal-trust credit_rating stays dual-current and flagged.

A stranger should see something in this shape — URI merge, a winner, a
why, a timestamp, and one leftover conflict the system refused to guess:

```
Rebuilding entity resolution for suppliers…
  Supplier         6 → 3  (−3 fragments across 2 clusters)

  merge  https://graph.infona.ai/entities/Supplier/ERP-1001
         losers:     https://graph.infona.ai/entities/Supplier/CRM-4402, https://graph.infona.ai/entities/Supplier/DIR-8891
         reason:     signal-richest
         score:      1.00
         provenance: erp @ 2026-03-01T12:00:00+00:00 (source_of_truth)

  merge  https://graph.infona.ai/entities/Supplier/ERP-2001
         losers:     https://graph.infona.ai/entities/Supplier/CRM-5503
         reason:     signal-richest
         score:      1.00
         provenance: erp @ 2026-03-01T12:00:00+00:00 (source_of_truth)

  conflict  headquarters
         entity:     https://graph.infona.ai/entities/Supplier/ERP-1001
         winner:     Austin  (erp, source_of_truth, 2026-03-01T12:00:00+00:00)
         loser:      San Francisco  (directory, supplementary, 2024-06-01T00:00:00+00:00)
         reason:     authority

  unresolved  credit_rating
         entity:     https://graph.infona.ai/entities/Supplier/ERP-1001
         crm: BBB @ 2026-03-01T12:00:00+00:00 (source_of_truth)
         erp: A @ 2026-03-01T12:00:00+00:00 (source_of_truth)
         flagged: equal-trust sources — not silently guessed

Done. 3 fragments absorbed.
```

**How to read it**

- **merge** — three Acme name variants (and two Globex) became one
  entity each. The surviving URI is the signal-richest fragment; its
  `provenance` is the source row that won (erp, timestamp, authority).
  That URI collapse **is** applied to the graph.
- **conflict / headquarters** — two sources disagreed on one field.
  ERP is `source_of_truth`, the directory is a stale `supplementary`
  scrape. The report names **Austin** as winner on the **authority**
  axis. Austin is current; San Francisco stays stored and closed.
- **unresolved / credit_rating** — ERP says `A`, CRM says `BBB`.
  Same authority, same timestamp; a lexical pick would be a silent
  guess. The report **flags** that pair until a reviewer decides.
  Both stay dual-current.

Full fixture notes: [`examples/suppliers-messy.md`](../../examples/suppliers-messy.md).
The hermetic proof is `tests/test_suppliers_messy_fixture.py`.
