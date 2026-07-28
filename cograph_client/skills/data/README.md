# Global-Public skill content (OSS seed) — reserved empty

This directory is the OSS seed path historically wired to `Layer.PUBLIC` by
`cograph_client.skills.registry.load_skill_dir`. It ships **empty on purpose**.

## Why empty

Product rule (founder / ONTA-396 / ONTA-400): **Public is attributes and
relationships only.** Skills, functions, and curated sources belong on
Enhanced (B) or Tenant (C). `LAYER_CONTENT_MATRIX` records this as a hard
**invariant** (`LAYER_A_CONTENT_ENFORCEMENT = "invariant"`).

Consequently:

* Do **not** add `data/<Type>/<slug>.md` skill files here. A non-empty seed
  fails CI (`tests/test_layer_content_guard.py`) and raises at runtime when
  `global_skills_by_layer()` first loads.
* `register_skill_layer(Layer.PUBLIC, skills)` refuses a **non-empty** list.
  An empty registration remains a no-op for this reserved-empty path.
* Curated skill content belongs on **Enhanced**, contributed by the proprietary
  package at startup via `register_skill_layer(Layer.ENHANCED, ...)`.

## Layout (if this directory is ever re-homed to a permitted layer)

```
data/
  Person/
    identity-basics.md      -> type_name="Person", slug="identity-basics"
  Organization/
    naming-conventions.md
```

Each file may open with a `---` front-matter block carrying flat `key: value`
pairs (`title`, `summary`, `enabled`); everything after the closing `---` is the
body. A file with no front matter is all body.

Never add premium / vertical content here either — that belongs in Enhanced via
the proprietary package, not in the OSS tree.
