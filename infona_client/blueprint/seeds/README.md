# Blueprint seeds

Protocol packages that ship with `infona-client`. They are inspectable
directories with `blueprint.yaml` (ADR 0014). They are not a hosted
registry and they do not contain live instance records.

| Package | Path |
|---|---|
| Clinical Trials v0 (INF-566) | [`clinical-trials/`](clinical-trials/) |

Validate:

```bash
python -m infona_client.blueprint validate \
  infona_client/blueprint/seeds/clinical-trials
```

Install, inspect, uninstall, and fork live on
`/graphs/{tenant}/blueprints` (INF-575 / INF-579 / INF-578). Seeds are not a
hosted registry.
