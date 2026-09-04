# Four-arm SGD run (constructed Infona task)

Not official DST. Headlines: Bind@service (overall / seen / unseen) and
slot `micro_f1` on predicted-bind dumps. Chance bind ≈ `1/21` on the test
schema list.

Same arms as the VRDU mix: `27b_bare`, `0.8b_bare`, `0.8b_vanilla_ft`,
`0.8b_ft_infona`. Models `Qwen/Qwen3.5-27B` and `Qwen/Qwen3.5-0.8B`.

Dev is unused. Infona test catalog **includes test schemas** (public
interfaces). Vanilla-FT inference stays bare, so unseen services are the
split that can show Infona ≫ FT memorization.

```bash
export PYTHONPATH=benchmarks/sgd-binder/src
python -m sgd_binder fetch --dest benchmarks/sgd-binder/data
python -m sgd_binder experiment-run --arm 0.8b_ft_infona \
  --model default_model --data benchmarks/sgd-binder/data \
  --out /tmp/sgd-arms --concurrency 2
```

This file does not contain Bind@ or F1 numbers.
