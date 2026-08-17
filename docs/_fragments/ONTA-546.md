Eval is Python-only. There is no `infona eval` CLI (do not invent one).
After the API is up and a KG is ingested:

```python
from infona_client.eval import run_full_eval
report = await run_full_eval(
    api_url="http://localhost:8000", api_key="dev-key-001",
    tenant="demo-tenant", kg_name="trials",
    dataset_paths=["examples/trials.csv"], num_questions=20,
)
```

Shipped datasets: `examples/trials.csv`, `examples/bookstore.csv`.
`/ask` is always-LLM Cypher. Published score table (if present): [docs/EVAL.md](../EVAL.md).
