# Infona binder-bench on Schema-Guided Dialogue

This is a **constructed** Infona task on Rastogi et al. SGD
(https://arxiv.org/abs/1909.05855). It is **not** official SGD dialogue-state
tracking. Do not quote Joint Goal Accuracy or DSTC8 numbers as Infona scores.

SGD gives a **schema per service** (slots + intents) and annotated dialogues.
Test includes **services absent from train** (15 APIs: Alarm_1, Trains_1, …).
That is the measurement VRDU n=2 could not make: bind-then-one-skill as the
ontology grows, including **unseen** schemas.

Data: https://github.com/google-research-datasets/dstc8-schema-guided-dialogue
(CC BY-SA 4.0). Official DST code:
https://github.com/google-research/google-research/tree/master/schema_guided_dst

## Mapping

| Infona | SGD |
| --- | --- |
| Type | **service** (one schema), opaque `type_k` |
| Skill | that service’s slot names + slot descriptions |
| Instance | last USER frame with nonempty `slot_values` per `(dialogue_id, service)` |
| Bind gold | that frame’s `service` |
| Extract gold | first listed value of each `state.slot_values` entry |
| Prompt | dialogue utterances up to that turn; **no** `service_name`, domain tokens, or gold slots |

Buses_1 vs Buses_3 are different Infona types (different slot names). Do not
collapse by domain.

## Freeze

1. Bind prompt is utterances only. Strip `service_name` (`Banks_1`), domain
   prefixes (`Banks`, `Hotels`, …), frame JSON, and gold `slot_values`.
2. Bind catalog is opaque `type_k` plus **slot-name sets**. No service names,
   no domain names, no service-level descriptions.
3. Exactly one skill after bind. Extract sees only that skill’s slots.
   Misbind writes empty predicted slots (recall miss).
4. Skills: slot names + slot descriptions from **schema JSON**. Train skills
   from train schemas. At test, Infona may load **test schemas** (the public
   interface). Never test-dialogue annotations. No few-shots from test.
5. **Dev unused** for train, model selection, early stopping, or calibration.
6. Headlines: Bind@service (overall + **seen** vs **unseen**). Slot
   `micro_f1` on predicted-bind dumps. Chance bind is `1/n_test_services`
   (~1/21), not 50%. Write that next to Bind@.
7. Do not claim official SGD DST / Joint Goal Accuracy. Do not claim
   Infona≫vanilla from VRDU n=2.

## Arms (same as VRDU mix)

`27b_bare`, `0.8b_bare` (utterances only), `0.8b_vanilla_ft` (train labels,
bare inference), `0.8b_ft_infona` (catalog + one skill at train and test).

Publish gate (docs only): Infona bind on **unseen** services ≫ vanilla-FT
unseen, and slot F1 follows. If both fail unseen, the story is still FT
memorization, not schema-guided Infona.
