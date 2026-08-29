# Holdout fixtures

Anti-overfit slice. The original skill author did not write this gold.

The main fixture teaches `VAT → registrationId` in the Company skill and then golds that same mapping. A model that memorizes the pair will look competent on `fixtures/tasks.jsonl` and still be wrong here.

## What is different

The type spine is still `Entity → Organization → Company`. The known leaf the main fixture never golds is `Carrier` (under `Company`). Receiving orgs are `Consignee`, not `Customer`. The freight edge is `HAULS_FOR`, not `SUPPLIES_TO`.

Tax numbers (VAT, EIN, GST) land on `taxId`. `SCAC` lands on `scac` and is the merge key for a fleet company. None of that writes `registrationId`.

Unseen branches use types that are not in the main gold set (`CrossDock`, `RailSiding`, `FleetContact`).

## Author rules

- Synthetic names only. Do not reuse Acme, Globex, Northwind, or any other main-fixture party.
- Gold is a `GraphDelta` with the same keys as `ontology_skills.graph_delta.GraphDelta.to_dict`.
- Identifier contract matches SPEC: short `type_id`, camelCase `attr`, full relation IRI on `adds`/`deletes`, full `https://graph.infona.ai/bench/ent/{slug}` entity URIs.
- Do not put `entity_uri` or `mint_as` on `input`. Every gold entity URI must already appear somewhere in `input`.
- Do not mention product `entity_uri`. Fixture URIs are author-stable slugs.
- `notes` and `input` must not contain any gold `type_id` (the leaf you assert, or the type you extend). Neighborhood seeds are compiler input, not a hint you write into the row.

## How to load

```python
from pathlib import Path
from ontology_skills.dataset import load_fixture_bundle

root = Path("benchmarks/ontology-skills/fixtures/holdout")
bundle = load_fixture_bundle(root / "ontology.json", root / "tasks.jsonl")
```

24 tasks, 3 per SPEC family, one of each SPEC split per family. No scores live in this tree. Do not invent any.
