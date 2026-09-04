# Infona binder-bench on SGD

Constructed bind-then-one-skill task on Schema-Guided Dialogue
(Rastogi et al., AAAI 2020). **Not official SGD DST.** Do not quote Joint
Goal Accuracy.

Freeze: [SPEC.md](SPEC.md). Runbook: [EXPERIMENT.md](EXPERIMENT.md).

Type = SGD **service** (one schema). Test has **15 services that are not in
train**. Chance bind on the test schema list is `1/21`, not 50%.

```bash
PYTHONPATH=benchmarks/sgd-binder/src pytest benchmarks/sgd-binder/tests -q
python -m sgd_binder experiment-dry --out /tmp/sgd-binder-dry
```

Fetch published JSON (gitignored):

```bash
python -m sgd_binder fetch --dest benchmarks/sgd-binder/data
python -m sgd_binder experiment-run --arm 0.8b_bare --limit 50 \
  --data benchmarks/sgd-binder/data --out /tmp/sgd-arms \
  --model default_model
```

`INFONA_BINDER_API_KEY` or `TOGETHER_API_KEY`. Local MLX:
`INFONA_BINDER_BASE_URL=http://127.0.0.1:8000/v1`.
