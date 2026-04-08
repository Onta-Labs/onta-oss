"""Omnix MCP Server — expose knowledge graph tools to AI agents.

Usage:
    # Run standalone
    python -m omnix.mcp_server

    # Or add to Claude Code / Cursor MCP config:
    {
      "mcpServers": {
        "omnix": {
          "command": "python",
          "args": ["-m", "omnix.mcp_server"],
          "env": {
            "OMNIX_API_URL": "http://localhost:8000",
            "OMNIX_API_KEY": "dev-key-001"
          }
        }
      }
    }
"""

import os

import httpx
from mcp.server.fastmcp import FastMCP

API_URL = os.environ.get(
    "OMNIX_API_URL",
    "http://localhost:8000",
)
API_KEY = os.environ.get("OMNIX_API_KEY", "dev-key-001")
TENANT = os.environ.get("OMNIX_TENANT", "demo-tenant")

mcp = FastMCP(
    "Omnix Knowledge Graph",
    instructions=(
        "Omnix is a knowledge graph platform. Use these tools to query "
        "structured data across multiple knowledge graphs using natural language."
    ),
)


def _headers() -> dict:
    return {"X-API-Key": API_KEY, "Content-Type": "application/json"}


def _base() -> str:
    return f"{API_URL}/graphs/{TENANT}"


@mcp.tool()
def list_knowledge_graphs() -> str:
    """List all available knowledge graphs and their descriptions."""
    r = httpx.get(f"{_base()}/kgs", headers=_headers(), timeout=15)
    r.raise_for_status()
    kgs = r.json()
    if not kgs:
        return "No knowledge graphs found."
    lines = []
    for kg in kgs:
        name = kg.get("name", "?")
        desc = kg.get("description", "")
        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
    return "\n".join(lines)


@mcp.tool()
def ask(question: str, kg_name: str = "") -> str:
    """Ask a natural language question against a knowledge graph.

    Args:
        question: The natural language question to ask (e.g., "How many events are in San Francisco?")
        kg_name: Name of the knowledge graph to query. Use list_knowledge_graphs to see available KGs.
    """
    body = {"question": question}
    if kg_name:
        body["kg_name"] = kg_name
    r = httpx.post(f"{_base()}/ask", headers=_headers(), json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    answer = data.get("answer", "No answer")
    explanation = data.get("explanation", "")
    result = f"Answer: {answer}"
    if explanation:
        result += f"\nExplanation: {explanation}"
    return result


@mcp.tool()
def ingest_csv(file_path: str, kg_name: str) -> str:
    """Ingest a CSV file into a knowledge graph. The schema is automatically inferred.

    Args:
        file_path: Absolute path to the CSV file to ingest.
        kg_name: Name for the knowledge graph (e.g., "sales-data", "customer-records").
    """
    import subprocess
    import sys

    env = os.environ.copy()
    env["OMNIX_API_URL"] = API_URL
    env["OMNIX_API_KEY"] = API_KEY
    env["OMNIX_TENANT"] = TENANT
    result = subprocess.run(
        [sys.executable, "-m", "omnix.cli", "ingest", file_path, "--kg", kg_name],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0:
        return f"Ingestion failed:\n{output}"
    return f"Ingestion complete:\n{output}"


@mcp.tool()
def view_ontology() -> str:
    """View the ontology (types, attributes, relationships) across all knowledge graphs."""
    r = httpx.get(f"{_base()}/ontology/types", headers=_headers(), timeout=15)
    r.raise_for_status()
    types = r.json()
    if not types:
        return "No ontology types defined yet."
    lines = []
    for t in types:
        name = t.get("name", "?")
        attrs = t.get("attributes", [])
        rels = t.get("relationships", [])
        lines.append(f"Type: {name}")
        if attrs:
            lines.append(f"  Attributes: {', '.join(a.get('name', '?') for a in attrs)}")
        if rels:
            lines.append(f"  Relationships: {', '.join(r.get('predicate', '?') + ' -> ' + r.get('target_type', '?') for r in rels)}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
