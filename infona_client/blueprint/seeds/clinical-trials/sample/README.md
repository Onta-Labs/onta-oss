# Sample — not the graph, not current

**SAMPLE.** Synthetic. Captured **2026-06-01**. Not a living ClinicalTrials.gov
extract. Not a Data Feed.

This directory exists so the sample is independently inspectable and
droppable (INF-587). The rows themselves live in the separated `sample`
section of `../blueprint.yaml`. Delete that section (and this directory)
and the rest of the Blueprint still validates.

## Rules this sample obeys

- **Separated.** Own section. Own directory.
- **Synthetic.** Ids are `SAMPLE-001` … `SAMPLE-008`. They are not
  `NCT` + eight digits — that form is reserved for real registry records.
- **Timestamped as data.** `captured_at: 2026-06-01` on the section, not
  only as a sentence here.
- **Size-capped.** 25 entities (8 trials, 6 organizations, 6 conditions,
  5 interventions). Investigators and facilities are omitted on purpose.
- **Marked sample on every surface.** Titles start with `(synthetic sample)`.
  Answers derived from these rows must say `sample, captured 2026-06-01`.
- **Never current.** Do not render a healthy freshness light. Do not let
  these rows feed a staleness gauge.

Sample values are literals only. Relationship edges (`lead_sponsor`,
`studies_condition`, …) appear after acquisition, not in this preview.

## Why 25

Enough to render an entity map and run two labelled questions. Not enough
to skip `acquire_condition_set`. A better preview is the force that turns
a Blueprint into a dump. The cap exists to lose that argument.
