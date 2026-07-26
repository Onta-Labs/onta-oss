# Global-Public skill content (OSS seed)

Markdown skills shipped with the OSS package and loaded into `Layer.PUBLIC` by
`cograph_client.skills.registry.load_skill_dir`.

Layout — the type name is the DIRECTORY and the slug is the FILENAME, so a file
can never disagree with its own location:

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

Scope rule: content here is **universal and domain-agnostic** — guidance true of
the type anywhere. Curated vertical/premium content belongs in the
Global-Enhanced layer, which the proprietary package contributes at startup via
`register_skill_layer(Layer.ENHANCED, ...)`. Never add premium content here.

This directory intentionally ships nearly empty. An OSS install with no seed
content is a fully supported state (`load_skill_dir` returns `[]`), and a wrong
universal claim baked into every deployment's prompts is worse than no claim.
