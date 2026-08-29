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
- Do not put `entity_uri` or `mint_as` on `input`. Do not put a gold entity URI under any other key either (`uri`, `record.uri`, `existing_uris`, graph subjects). No input string may equal a gold entity URI.
- Gold entity URIs stay author-stable in gold only. The executor mints them in-task from the mention.
- Do not mention product `entity_uri`. Fixture URIs are author-stable slugs.
- `notes` and `input` must not contain any gold `type_id` (the leaf you assert, or the type you extend). Neighborhood seeds are compiler input, not a hint you write into the row.

## How to load

```python
from ontology_skills.holdout import load_holdout_bundle, execute_holdout_task
```

`load_holdout_bundle()` is the single loader. `execute_holdout_task` reuses the 487 `execute_task` loop and points it at this directory for one call.

```bash
PYTHONPATH=benchmarks/ontology-skills python -m ontology_skills.holdout \
  --task-id ho-et-01 --backend canned --canned path/to/canned.jsonl
```

`--task-id` is required. This CLI does not sweep. Live still needs a Bearer key and will not POST without one. There is no holdout canned bank in-tree and no published score.

24 tasks, 3 per SPEC family. All three SPEC splits appear in the set. No scores live in this tree. Do not invent any.
