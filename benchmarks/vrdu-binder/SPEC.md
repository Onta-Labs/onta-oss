# Infona binder-bench spec v11

This is a constructed mix of two published VRDU mixed-template tests
(Wang et al., KDD 2023, https://arxiv.org/abs/2211.15421). It is not a
VRDU-published task. VRDU did not ship a bind label. Gold type is corpus
membership of those per-corpus test lists.

Data: https://github.com/google-research-datasets/vrdu
Eval toolkit: https://github.com/google-research/google-research/tree/master/vrdu

Infona here is a type-binder. Bind the most specific type, then run that
type's one skill. This bench is Bind@type then extract: two document types
(Registration vs Ad-buy), one skill each. FARA's three layouts are one
Infona type, not three leaves.

## Host

Exact published splits, unmodified test lists:

- Registration:
  `registration-form/few_shot-splits/FARA-lv2-mixed_template-train_200-test_300-valid_100-SD_{0,1,2}.json`
- Ad-buy (no `lv2` token):
  `ad-buy-form/few_shot-splits/DeepForm-mixed_template-train_200-test_300-valid_100-SD_{0,1,2}.json`

Official schema keys from each corpus `meta.json` (not paper Table 2 aliases):

- Registration: `file_date`, `foreign_principle_name`, `registrant_name`,
  `registration_num`, `signer_name`, `signer_title`
- Ad-buy: `advertiser`, `agency`, `contract_num`, `flight_from`, `flight_to`,
  `gross_amount`, `product`, `property`, `tv_address` plus line_item
  `channel`, `program_desc`, `program_start_date`, `program_end_date`,
  `sub_amount`

## Freeze

1. Prompt to the bind model is OCR tokens only. Strip `filename`,
   `file_path`, `annotations`, corpus/path prefixes (`registration-form/`,
   `ad-buy-form/`), and FARA vs DeepForm names. Opaque ids until after
   predictions are written.
2. The harness binds exactly one skill. Extract may include only that skill.
   No dump-both-then-pick-better.
3. Skills and few-shots for seed `SD_{0,1,2}` may be written only from that
   split's `train` filenames. Never valid, never test, never current-doc
   `annotations`. Skill bodies: official `meta.json` keys plus procedure.
   No corpus/type/template strings and no nicknames like `sk_reg` / `sk_adbuy`.
4. Unmodified published `test` lists. Misbind writes empty
   `results[filename]` (recall miss via stock
   `all_extractions.get(filename, [])`). Do not drop or rewrite a test doc.
5. Score with stock `python -m vrdu.evaluate --base_dirpath`. Do not patch
   google-research/google-research#1882.
6. Valid is unused for bind, prompt, redaction, model selection, or router
   train/calibration.
7. F1_wrong is a second forced-other-skill dump. Sanity only. Never a headline.
8. Gold-routed / oracle-type F1 never headlines.

## Headlines vs footnotes

On the slide:

- Bind@type accuracy on the constructed mix
- Per-corpus official `metric-micro_f1` from the predicted-bind dumps (one
  corpus at a time)

n=2 tax: two types. Chance bind accuracy is 50%. Do not compare this number
to a many-class router.

Footnotes only (unredacted vis+layout SOTA, not a paired Δ):

- Table 4 Mixed |D|=200 FormNet Registration 90.51
- LayoutLMv2 / FormNet Ad-buy 46.54 / 43.23

STL FormNet Registration 92.12 is Task 1. Exclude it.

Do not claim this is a published VRDU task. Do not claim Infona≫RAG or
8B+Infona≈27B.

Four-arm SD_0 scaffolding (32B bare, 0.8B bare, 0.8B vanilla-FT, 0.8B
FT+Infona) lives in [EXPERIMENT.md](EXPERIMENT.md). No scores are filled
in. Publish only if arm4 ≈ arm1 and arm4 ≫ arm3 on both headlines.

## Leak-honest choices this tree made

- Bind catalog is schema-key sets keyed by opaque `type_0` / `type_1`. Those
  ids never appear in skill bodies or in the OCR prompt.
- Gold type for a dump is the corpus of that dump's published test list.
- `RunSplit` keeps train and test only. Valid is not an attribute, so later
  stages cannot read it.
- Headline writer refuses `oracle` / `gold-routed` / `f1_wrong` metadata.
- If OCR tokens contain `FARA`, `DeepForm`, or a corpus path prefix, the bind
  prompt redacts those strings. That is stripping dataset names, not dropping
  the document.
- `KeywordBinder` / `KeywordExtractor` may write fixture dumps only. A
  published-split `*-test_predictions.json` requires the LLM adapters and
  `INFONA_BINDER_API_KEY`. Missing key refuses. No keyword fallback.
- The LLM bind catalog is `type_0` / `type_1` plus official keys. The LLM
  extract prompt is OCR tokens plus the one skill body.

## Out of scope

Live SD_0 scoring, downloading Qwen weights, GraphDelta JSON, OSKGC,
LettrIA, patching #1882, rewriting published test lists, oracle-type F1
as a headline script, auto-claiming the publish gate.
