#!/usr/bin/env python3
"""Generate OpenAPI spec and markdown API reference from the FastAPI app.

Usage:
    python scripts/generate_api_docs.py

Outputs:
    docs/openapi.json   — OpenAPI 3.x spec
    docs/API.md          — Markdown API reference
"""

import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from cograph_client.api.app import create_app

app = create_app()
spec = app.openapi()

docs_dir = Path(__file__).parent.parent / "docs"
docs_dir.mkdir(exist_ok=True)

# Write OpenAPI JSON
openapi_path = docs_dir / "openapi.json"
openapi_path.write_text(json.dumps(spec, indent=2))
print(f"OpenAPI spec: {openapi_path}")

# Generate markdown API reference
lines = [
    f"# {spec['info']['title']} API Reference",
    "",
    f"**Version:** {spec['info']['version']}",
    "",
    f"{spec['info'].get('description', '')}",
    "",
    "Auto-generated from the OpenAPI spec. Do not edit manually.",
    "",
    "## Interactive Docs",
    "",
    "When the server is running, visit:",
    "- Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)",
    "- ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)",
    "",
]

# Group by tags
tag_paths: dict[str, list] = {}
for path, methods in spec.get("paths", {}).items():
    for method, details in methods.items():
        if method in ("get", "post", "put", "delete", "patch"):
            tags = details.get("tags", ["Other"])
            for tag in tags:
                tag_paths.setdefault(tag, []).append((method.upper(), path, details))

for tag in sorted(tag_paths.keys()):
    lines.append(f"## {tag.title()}")
    lines.append("")
    for method, path, details in tag_paths[tag]:
        summary = details.get("summary", "")
        lines.append(f"### `{method} {path}`")
        lines.append("")
        if summary:
            lines.append(summary)
            lines.append("")
        desc = details.get("description", "")
        if desc:
            lines.append(desc)
            lines.append("")

        # Request body
        req_body = details.get("requestBody", {})
        if req_body:
            content = req_body.get("content", {})
            for ct, schema_info in content.items():
                ref = schema_info.get("schema", {}).get("$ref", "")
                if ref:
                    schema_name = ref.split("/")[-1]
                    lines.append(f"**Request body:** `{schema_name}`")
                    lines.append("")

        # Response
        responses = details.get("responses", {})
        for code, resp in responses.items():
            resp_desc = resp.get("description", "")
            lines.append(f"**{code}:** {resp_desc}")
        lines.append("")
        lines.append("---")
        lines.append("")

# Schemas section
schemas = spec.get("components", {}).get("schemas", {})
if schemas:
    lines.append("## Schemas")
    lines.append("")
    for name, schema in sorted(schemas.items()):
        lines.append(f"### {name}")
        lines.append("")
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        if props:
            lines.append("| Field | Type | Required | Description |")
            lines.append("|-------|------|----------|-------------|")
            for prop_name, prop_info in props.items():
                prop_type = prop_info.get("type", prop_info.get("$ref", "object"))
                if isinstance(prop_type, list):
                    prop_type = " | ".join(str(t) for t in prop_type)
                req = "Yes" if prop_name in required else "No"
                desc = prop_info.get("description", "")
                lines.append(f"| `{prop_name}` | {prop_type} | {req} | {desc} |")
            lines.append("")

api_md_path = docs_dir / "API.md"
api_md_path.write_text("\n".join(lines))
print(f"API reference: {api_md_path}")
