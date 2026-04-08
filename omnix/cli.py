#!/usr/bin/env python3
"""Omnix CLI — manage knowledge graphs, ingest data, query, and evaluate.

Usage:
    omnix kg list
    omnix kg create <name> [--description TEXT]
    omnix kg delete <name>

    omnix ingest <file> [--kg NAME] [--format text|csv|json]
    omnix ingest --text "raw text" [--kg NAME]

    omnix ask "question" [--kg NAME]

    omnix eval <files...> --kg NAME [--questions 20]
    omnix eval --ontology-only --kg NAME
    omnix eval --query-only <files...> --kg NAME

    omnix ontology types
    omnix clear [--kg NAME] [--include-ontology]

Environment variables:
    OMNIX_API_URL       API base URL (default: http://localhost:8000)
    OMNIX_API_KEY       API key (default: dev-key-001)
    OMNIX_TENANT        Tenant ID (default: demo-tenant)
    OPENROUTER_API_KEY  Required for eval (LLM judge calls)
    OMNIX_EVAL_MODEL    Override eval judge model (default: deepseek/deepseek-v3.2)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

API_URL = os.environ.get("OMNIX_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("OMNIX_API_KEY", "dev-key-001")
TENANT = os.environ.get("OMNIX_TENANT", "demo-tenant")


def _headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY, "Content-Type": "application/json"}


def _base() -> str:
    return f"{API_URL}/graphs/{TENANT}"


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, default=str))


def _check(res: httpx.Response) -> dict:
    if res.status_code >= 400:
        print(f"Error {res.status_code}: {res.text}", file=sys.stderr)
        sys.exit(1)
    return res.json()


# ---------------------------------------------------------------------------
# Knowledge Graph commands
# ---------------------------------------------------------------------------

def kg_list(args: argparse.Namespace) -> None:
    res = httpx.get(f"{_base()}/kgs", headers=_headers(), timeout=10)
    kgs = _check(res)
    if not kgs:
        print("No knowledge graphs. Create one with: omnix kg create <name>")
        return
    for kg in kgs:
        desc = f" — {kg['description']}" if kg.get("description") else ""
        print(f"  {kg['name']:<20s} {kg['triple_count']:>6} triples{desc}")


def kg_create(args: argparse.Namespace) -> None:
    body = {"name": args.name}
    if args.description:
        body["description"] = args.description
    res = httpx.post(f"{_base()}/kgs", headers=_headers(), json=body, timeout=10)
    kg = _check(res)
    print(f"Created knowledge graph: {kg['name']}")


def kg_delete(args: argparse.Namespace) -> None:
    res = httpx.delete(f"{_base()}/kgs/{args.name}", headers=_headers(), timeout=30)
    _check(res)
    print(f"Deleted knowledge graph: {args.name}")


# ---------------------------------------------------------------------------
# Ingest commands
# ---------------------------------------------------------------------------

def ingest(args: argparse.Namespace) -> None:
    if args.text:
        content = args.text
        fmt = "text"
    elif args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)

        ext = path.suffix.lower()
        fmt = args.format

        if ext == ".pdf":
            _ingest_pdf(path, args.kg)
            return

        content = path.read_text()

        if not fmt:
            fmt_map = {".csv": "csv", ".json": "json", ".jsonl": "json", ".txt": "text"}
            fmt = fmt_map.get(ext, "text")

        if ext == ".csv" and fmt == "csv":
            _ingest_csv(content, args.kg)
            return
    else:
        print("Provide a file or --text", file=sys.stderr)
        sys.exit(1)

    if not fmt:
        fmt = "text"

    body: dict = {"content": content, "content_type": fmt, "source": "cli"}
    if args.kg:
        body["kg_name"] = args.kg

    print(f"Ingesting {fmt} ({len(content):,} chars)...")
    res = httpx.post(f"{_base()}/ingest", headers=_headers(), json=body, timeout=120)
    result = _check(res)
    print(f"  Entities extracted: {result.get('entities_extracted', 0)}")
    print(f"  Entities resolved:  {result.get('entities_resolved', 0)}")
    print(f"  Triples inserted:   {result.get('triples_inserted', 0)}")
    if result.get("types_created"):
        print(f"  Types created:      {', '.join(result['types_created'])}")
    if result.get("rejections"):
        print(f"  Rejections:         {len(result['rejections'])}")


def _ingest_csv(content: str, kg_name: str | None) -> None:
    """Two-step CSV ingestion: schema inference + deterministic row mapping."""
    import csv
    import io

    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)
    if not rows:
        print("CSV is empty", file=sys.stderr)
        sys.exit(1)

    headers = list(rows[0].keys())
    print(f"CSV: {len(rows)} rows, {len(headers)} columns")
    print(f"Columns: {', '.join(headers[:10])}{'...' if len(headers) > 10 else ''}")

    # Step 1: Infer schema
    print("Inferring schema from sample rows...")
    schema_body = {
        "headers": headers,
        "sample_rows": rows[:5],
        "total_rows": len(rows),
    }
    res = httpx.post(
        f"{_base()}/ingest/csv/schema",
        headers=_headers(),
        json=schema_body,
        timeout=120,
    )
    mapping = _check(res)
    print(f"  Entity type: {mapping['entity_type']}")
    for col in mapping.get("columns", []):
        role = col["role"]
        extra = f" → {col['target_type']}" if col.get("target_type") else f" ({col.get('datatype', 'string')})"
        print(f"    {col['column_name']:<25s} {role:<15s}{extra}")

    # Step 2: Insert rows in batches
    BATCH_SIZE = 50
    total_entities = 0
    total_triples = 0

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (len(rows) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} rows)...", end=" ", flush=True)

        body: dict = {"mapping": mapping, "rows": batch, "source": "cli"}
        if kg_name:
            body["kg_name"] = kg_name

        res = httpx.post(
            f"{_base()}/ingest/csv/rows",
            headers=_headers(),
            json=body,
            timeout=120,
        )
        result = _check(res)
        total_entities += result.get("entities_resolved", 0)
        total_triples += result.get("triples_inserted", 0)
        print(f"+{result.get('triples_inserted', 0)} triples")

    print(f"\nDone: {total_entities} entities, {total_triples} triples")


def _ingest_pdf(path: Path, kg_name: str | None) -> None:
    """Upload PDF for extraction via the demo API's PDF route."""
    # The PDF extraction happens on the Next.js frontend API, not the backend directly.
    # For CLI, we use the Anthropic API directly to extract text, then ingest.
    try:
        import anthropic
    except ImportError:
        print("anthropic package required for PDF ingestion. Install with: pip install anthropic", file=sys.stderr)
        sys.exit(1)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        print("ANTHROPIC_API_KEY environment variable required for PDF ingestion", file=sys.stderr)
        sys.exit(1)

    import base64
    pdf_bytes = path.read_bytes()
    b64 = base64.b64encode(pdf_bytes).decode()

    print(f"Extracting text from {path.name} ({len(pdf_bytes):,} bytes)...")
    client = anthropic.Anthropic(api_key=anthropic_key)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
                },
                {
                    "type": "text",
                    "text": "Extract all factual information from this PDF as structured plain text. "
                            "Include all names, dates, numbers, addresses, organizations, relationships. "
                            "Do not summarize. Output plain text only.",
                },
            ],
        }],
    )
    extracted = msg.content[0].text if msg.content[0].type == "text" else ""
    print(f"  Extracted {len(extracted):,} chars")

    body: dict = {"content": extracted, "content_type": "text", "source": f"pdf:{path.name}"}
    if kg_name:
        body["kg_name"] = kg_name

    print("Ingesting extracted text...")
    res = httpx.post(f"{_base()}/ingest", headers=_headers(), json=body, timeout=120)
    result = _check(res)
    print(f"  Entities: {result.get('entities_resolved', 0)}, Triples: {result.get('triples_inserted', 0)}")
    if result.get("types_created"):
        print(f"  Types: {', '.join(result['types_created'])}")


# ---------------------------------------------------------------------------
# Query commands
# ---------------------------------------------------------------------------

def ask(args: argparse.Namespace) -> None:
    question = args.question
    debug = args.debug
    model = getattr(args, "model", None)
    if model:
        print(f"Model: {model}")
    print(f"Q: {question}")
    print("Generating answer...")

    import time as _time
    t0 = _time.time()
    body: dict = {"question": question}
    if args.kg:
        body["kg_name"] = args.kg
    if model:
        body["model"] = model
    res = httpx.post(
        f"{_base()}/ask",
        headers=_headers(),
        json=body,
        timeout=60,
    )
    roundtrip_ms = round((_time.time() - t0) * 1000, 1)
    result = _check(res)

    print(f"\nA: {result.get('answer', 'No answer')}")

    if debug:
        print(f"\nSPARQL:\n{result.get('sparql', '')}")
        timing = result.get("timing", {})
        if timing:
            print(f"\n{'─' * 40}")
            print(f"{'Stage':<25s} {'Time':>10s}")
            print(f"{'─' * 40}")
            for key, val in timing.items():
                if key == "attempts":
                    print(f"{'Attempts':<25s} {int(val):>10d}")
                elif isinstance(val, str):
                    label = key.replace("_", " ").title()
                    print(f"{label:<25s} {val:>10s}")
                else:
                    label = key.replace("_ms", "").replace("_", " ").title()
                    print(f"{label:<25s} {val:>9.1f}ms")
            print(f"{'─' * 40}")
            print(f"{'Client roundtrip':<25s} {roundtrip_ms:>9.1f}ms")
    elif result.get("sparql") and args.debug is not None:
        print(f"\nSPARQL:\n{result['sparql']}")


# ---------------------------------------------------------------------------
# Ontology commands
# ---------------------------------------------------------------------------

def ontology_types(args: argparse.Namespace) -> None:
    res = httpx.get(f"{_base()}/ontology/types", headers=_headers(), timeout=10)
    types = _check(res)
    if not types:
        print("No ontology types defined.")
        return
    for t in types:
        parent = f" (subClassOf {t['parent_type']})" if t.get("parent_type") else ""
        desc = f" — {t['description']}" if t.get("description") else ""
        print(f"  {t['name']}{parent}{desc}")
        for attr in t.get("attributes", []):
            print(f"    .{attr['name']} ({attr['datatype']})")


def clear(args: argparse.Namespace) -> None:
    body: dict = {}
    if args.kg:
        body["kg_name"] = args.kg
        msg = f"Clear KG '{args.kg}'?"
    elif args.include_ontology:
        body["clear_ontology"] = True
        msg = "Clear EVERYTHING including ontology?"
    else:
        msg = "Clear all instance data (ontology preserved)?"

    if not args.yes:
        confirm = input(f"{msg} [y/N] ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return

    # Use the backend triples API directly for clearing
    if args.kg:
        res = httpx.delete(f"{_base()}/kgs/{args.kg}", headers=_headers(), timeout=30)
        if res.status_code < 400:
            print(f"Cleared KG: {args.kg}")
        else:
            print(f"Error: {res.text}", file=sys.stderr)
    else:
        # Clear instance data from default graph
        print("Clearing...")
        deleted = 0
        for _ in range(50):
            fetch_res = httpx.post(
                f"{_base()}/query",
                headers=_headers(),
                json={"query": f"SELECT ?s ?p ?o FROM <https://omnix.dev/graphs/{TENANT}> WHERE {{ ?s ?p ?o . FILTER(CONTAINS(STR(?s), '/entities/') || CONTAINS(STR(?s), '/onto/') || CONTAINS(STR(?s), '/kgs/')) }} LIMIT 1000"},
                timeout=30,
            )
            if fetch_res.status_code >= 400:
                break
            bindings = fetch_res.json().get("bindings", [])
            if not bindings:
                break
            triples = [{"subject": b["s"], "predicate": b["p"], "object": b["o"]} for b in bindings if b.get("s")]
            for i in range(0, len(triples), 100):
                httpx.request(
                    "DELETE", f"{_base()}/triples",
                    headers=_headers(),
                    json={"triples": triples[i:i+100]},
                    timeout=30,
                )
            deleted += len(triples)

        if args.include_ontology:
            for _ in range(50):
                fetch_res = httpx.post(
                    f"{_base()}/query",
                    headers=_headers(),
                    json={"query": f"SELECT ?s ?p ?o FROM <https://omnix.dev/graphs/{TENANT}> WHERE {{ ?s ?p ?o }} LIMIT 1000"},
                    timeout=30,
                )
                if fetch_res.status_code >= 400:
                    break
                bindings = fetch_res.json().get("bindings", [])
                if not bindings:
                    break
                triples = [{"subject": b["s"], "predicate": b["p"], "object": b["o"]} for b in bindings if b.get("s")]
                for i in range(0, len(triples), 100):
                    httpx.request(
                        "DELETE", f"{_base()}/triples",
                        headers=_headers(),
                        json={"triples": triples[i:i+100]},
                        timeout=30,
                    )
                deleted += len(triples)

        print(f"Deleted {deleted} triples")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(prog="omnix", description="Omnix Knowledge Graph CLI")
    sub = parser.add_subparsers(dest="command")

    # kg
    kg_parser = sub.add_parser("kg", help="Manage knowledge graphs")
    kg_sub = kg_parser.add_subparsers(dest="kg_command")

    kg_sub.add_parser("list", help="List knowledge graphs")

    kg_create_p = kg_sub.add_parser("create", help="Create a knowledge graph")
    kg_create_p.add_argument("name")
    kg_create_p.add_argument("--description", "-d", default="")

    kg_delete_p = kg_sub.add_parser("delete", help="Delete a knowledge graph")
    kg_delete_p.add_argument("name")

    # ingest
    ingest_p = sub.add_parser("ingest", help="Ingest data from file or text")
    ingest_p.add_argument("file", nargs="?", help="File to ingest (.csv, .json, .txt, .pdf)")
    ingest_p.add_argument("--text", "-t", help="Inline text to ingest")
    ingest_p.add_argument("--kg", help="Target knowledge graph name")
    ingest_p.add_argument("--format", "-f", choices=["text", "csv", "json"], help="Override format detection")

    # ask
    ask_p = sub.add_parser("ask", help="Ask a natural language question")
    ask_p.add_argument("question", help="The question to ask")
    ask_p.add_argument("--kg", help="Knowledge graph to query")
    ask_p.add_argument("-d", "--debug", action="store_true", help="Show SPARQL and latency breakdown")
    ask_p.add_argument("--model", "-m", help="Override query model (e.g., google/gemini-2.5-flash, meta-llama/llama-4-maverick)")

    # ontology
    onto_p = sub.add_parser("ontology", help="View ontology")
    onto_sub = onto_p.add_subparsers(dest="onto_command")
    onto_sub.add_parser("types", help="List ontology types")

    # clear
    clear_p = sub.add_parser("clear", help="Clear data")
    clear_p.add_argument("--kg", help="Clear a specific knowledge graph")
    clear_p.add_argument("--include-ontology", action="store_true", help="Also clear the ontology")
    clear_p.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    # eval
    eval_p = sub.add_parser("eval", help="Evaluate ontology quality and query accuracy")
    eval_p.add_argument("files", nargs="*", help="Source data files (for ground truth)")
    eval_p.add_argument("--kg", help="Knowledge graph to evaluate")
    eval_p.add_argument("--questions", "-n", type=int, default=20, help="Number of test questions (default: 20)")
    eval_p.add_argument("--model", "-m", help="Override query model")
    eval_p.add_argument("--ontology-only", action="store_true", help="Only evaluate ontology quality")
    eval_p.add_argument("--query-only", action="store_true", help="Only evaluate query accuracy")
    eval_p.add_argument("--cache-questions", action="store_true", help="Cache questions + ground truth for re-runs (skip regeneration)")
    eval_p.add_argument("--fast-judge", action="store_true", help="Use programmatic judge (numeric tolerance) instead of LLM judge")
    eval_p.add_argument("--concurrency", type=int, default=10, help="Max concurrent API calls (default: 10)")

    args = parser.parse_args()

    if args.command == "kg":
        if args.kg_command == "list":
            kg_list(args)
        elif args.kg_command == "create":
            kg_create(args)
        elif args.kg_command == "delete":
            kg_delete(args)
        else:
            kg_parser.print_help()
    elif args.command == "ingest":
        ingest(args)
    elif args.command == "ask":
        ask(args)
    elif args.command == "ontology":
        if args.onto_command == "types":
            ontology_types(args)
        else:
            onto_p.print_help()
    elif args.command == "clear":
        clear(args)
    elif args.command == "eval":
        import asyncio
        from omnix.eval import eval_cli
        asyncio.run(eval_cli(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
