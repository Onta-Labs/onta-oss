# `suppliers-messy.csv` — synthetic merge / conflict fixture

**Synthetic.** Invented vendors (`Acme`, `Globex`, `Initech`), fake tax IDs,
and example.com emails. No real customer data. No spider-bench leakage.

This file exists so a stranger can see URI merge: six rows become three
suppliers, plus an explanatory field-conflict report (a winner, a reason, a
timestamp, and one leftover conflict the system refused to guess). Field
winners are not written as the sole current graph value.

## Intended entities

| Real-world supplier | Rows (name variants) | After `er rebuild` |
|---|---|---|
| Acme | `Acme Corp` / `ACME Corporation` / `Acme` | **one** Supplier |
| Globex | `Globex Inc` / `Globex Incorporated` | **one** Supplier |
| Initech | `Initech LLC` | singleton (control) |

Six rows → three real suppliers. The Acme / Globex collapses are intra-batch
fragments ingest cannot see mid-file; `infona er rebuild --kg suppliers`
is the second pass that absorbs them. URI merge **is** applied (6→3).

## Intended field conflicts (Acme only)

These lines are an **explanatory report**. They do not rewrite current
graph values. Both HQ literals stay live; validity intervals are not on
Neo4j yet.

**`headquarters` — reported winner.** ERP (`source_of_truth`, 2026-03-01, Austin)
beats the stale directory (`supplementary`, 2024-06-01, San Francisco).
Winner **Austin**, reason **authority**, provenance is the ERP row
(source + timestamp + authority).

**`credit_rating` — left unresolved.** ERP says `A`, CRM says `BBB`.
Same authority (`source_of_truth`), same timestamp. The only remaining
tiebreak would be a lexical guess (`A` vs `BBB`). The report **flags**
that pair instead of silently picking. A reviewer must decide.

Globex and Initech agree with themselves on every field.

## Quickstart

```bash
infona ingest examples/suppliers-messy.csv --kg suppliers
infona er rebuild --kg suppliers
```
