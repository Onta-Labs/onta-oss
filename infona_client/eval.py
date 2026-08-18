#!/usr/bin/env python3
"""Infona Eval — automated evaluation of ontology quality and query accuracy.

Overview
========
This module evaluates two dimensions of the Infona ingestion + query pipeline:

  1. **Ontology Quality** — Does the system create a well-structured, reusable
     knowledge graph? Are entities properly decomposed (address → city/state/zip)?
     Are types connected in a hierarchy? Are predicates consistent?

  2. **Query Accuracy** — Can the system answer natural language questions correctly?
     Does ``/ask`` generate valid Cypher? Are answers factually correct against
     ground truth? We lead with trust, not text-to-Cypher: the judge scores the
     answer, not string-match on the generated query.

Architecture
============
The eval runs in a loop designed for iterative improvement:

    ┌──────────────────────────────────────────────────────────────────┐
    │  1. Ingest dataset(s) via CLI                                   │
    │  2. Eval ontology quality (LLM judge scores structure)          │
    │  3. LLM generates questions at 4 difficulty tiers               │
    │  4. Execute each question via /ask (always-LLM Cypher)          │
    │  5. LLM judge evaluates answers against source data             │
    │  6. Report: scores, failures, weak points                       │
    │  7. Fix gaps in pipeline → repeat from step 2                   │
    └──────────────────────────────────────────────────────────────────┘

Usage
=====
Python only. ``@infona-ai/cli`` has no eval subcommand — do not invent one.
After the API is up and a KG is ingested, call ``run_full_eval``.
Requires ``OPENROUTER_API_KEY`` for the LLM judge (and question generation).

Shipped example datasets: ``examples/trials.csv``, ``examples/bookstore.csv``.

    import asyncio
    from infona_client.eval import run_full_eval

    report = asyncio.run(run_full_eval(
        api_url="http://localhost:8000",
        api_key="dev-key-001",
        tenant="demo-tenant",
        kg_name="trials",
        dataset_paths=["examples/trials.csv"],
        num_questions=20,
    ))

Ontology quality only (skip ``/ask``)::

    report = asyncio.run(run_full_eval(
        api_url="http://localhost:8000",
        api_key="dev-key-001",
        tenant="demo-tenant",
        kg_name="trials",
        dataset_paths=["examples/trials.csv"],
        ontology_only=True,
    ))

Query accuracy only (ontology already ingested)::

    report = asyncio.run(run_full_eval(
        api_url="http://localhost:8000",
        api_key="dev-key-001",
        tenant="demo-tenant",
        kg_name="trials",
        dataset_paths=["examples/trials.csv"],
        query_only=True,
    ))

Multi-domain: two shipped CSVs, check ontology reuse::

    report = asyncio.run(run_full_eval(
        api_url="http://localhost:8000",
        api_key="dev-key-001",
        tenant="demo-tenant",
        kg_name="combined",
        dataset_paths=["examples/trials.csv", "examples/bookstore.csv"],
    ))

Question Difficulty Tiers
=========================
The LLM generates questions across 4 tiers to test increasing complexity:

  **Tier 1 — Count/Lookup** (basic aggregation)
      "How many clinical trials are there?"
      Tests: MATCH ... RETURN count(...), basic entity retrieval

  **Tier 2 — Filter** (WHERE clauses)
      "How many Phase 3 trials have enrollment over 500?"
      Tests: Comparison operators, attribute filtering

  **Tier 3 — Join/Relationship** (graph traversal)
      "Which sponsors have trials at Dana-Farber?"
      Tests: Relationship traversal, multi-entity queries

  **Tier 4 — Multi-hop/Complex** (chained reasoning)
      "What is the average enrollment of NSCLC trials listed by Phase 3 sponsors?"
      Tests: Multiple joins, aggregation over filtered relationships

Ontology Quality Dimensions
============================
The ontology judge scores 6 dimensions (each 0-10):

  **Decomposition** — Are composite values broken into entities?
      Bad:  Property.address = "123 Main St, Austin, TX"
      Good: Property → located_in → City("Austin") → located_in → State("TX")

  **Reusability** — Are entities created that other datasets would share?
      Bad:  Property.city_name = "Austin" (dead-end string)
      Good: City("Austin") as its own entity (reusable across domains)

  **Hierarchy** — Are types connected via subClassOf, not orphaned?
      Bad:  Broker and Person as unrelated types
      Good: Broker subClassOf Person

  **Predicate Consistency** — No duplicate predicates for the same relationship?
      Bad:  located_in AND is_located_in AND location_of
      Good: Single canonical predicate per relationship

  **Entity-First Compliance** — Are real-world things entities, not literals?
      Bad:  Property.agent = "John Smith" (string literal)
      Good: Property → listed_by → Person("John Smith")

  **Type Naming** — PascalCase, singular, descriptive?
      Bad:  "properties", "LISTING", "real_estate_listing"
      Good: "Property", "Listing"

LLM Judge Design
================
The judge is a separate LLM call that receives:
  - The question asked
  - The generated Cypher
  - The answer returned
  - A relevant slice of the source data (for ground truth derivation)

The judge does NOT have access to a pre-computed answer key. It derives ground truth
from the source data on the fly. This means it works for any dataset without manual
annotation.

For ontology quality, the judge receives:
  - The full ontology schema (types, attributes, relationships)
  - The source data sample (to understand what was ingested)
  - The 6 scoring dimensions with examples

The judge uses a reasoning model (DeepSeek R1 or Claude Sonnet 4.6) for accuracy.

Report Format
=============
The eval produces a JSON report and a human-readable summary::

    INFONA EVAL REPORT
    ════════════════════════════════════════════════════════════
    Dataset:      trials.csv (shipped example)
    KG:           trials
    Model:        deepseek/deepseek-v3.2

    ONTOLOGY QUALITY (45/60)
    ────────────────────────────────────────────────────────────
    Decomposition:          7/10  Address decomposed, but phone not
    Reusability:            8/10  City, State, ZipCode as entities
    Hierarchy:              6/10  Broker→Person missing
    Predicate Consistency:  9/10  No duplicates found
    Entity-First:           8/10  Agent is entity, but office is string
    Type Naming:            7/10  "Property" good, "ZipCode" → "PostalCode"?

    Weak points:
      - Property.office_name should be Company entity
      - No Broker→Person subtype relationship
      - Phone numbers stored as strings, not ContactInfo entity

    QUERY ACCURACY (16/20)
    ────────────────────────────────────────────────────────────
    Tier 1 (Count):      5/5   avg 1.2s
    Tier 2 (Filter):     4/5   avg 2.1s  ← enrollment filter missed
    Tier 3 (Join):       4/5   avg 3.4s  ← sponsor relationship failed
    Tier 4 (Multi-hop):  3/5   avg 4.8s  ← avg enrollment + filter + join

    Failed questions:
      Q7:  "How many Phase 3 trials with enrollment over 500?"
           Expected: 8  Got: 12  (filter not applied)
           Cypher: MATCH (t:Entity) ... RETURN count(t) — missing enrollment > 500
      ...
    ════════════════════════════════════════════════════════════

Extending the Eval
==================
To add a new ontology quality dimension:
  1. Add it to ONTOLOGY_DIMENSIONS in this file
  2. Add scoring criteria to ONTOLOGY_JUDGE_PROMPT
  3. Update OntologyScore dataclass

To add a new question tier:
  1. Add it to QUESTION_TIERS in this file
  2. Add generation instructions to QUESTION_GEN_PROMPT
  3. Update the report formatting in _format_report()

To change the judge model:
  Set INFONA_EVAL_MODEL env var (default: uses the same provider as query generation)
"""

from __future__ import annotations

from infona_client.eval_bank import rebuild_example_bank
from infona_client.eval_llm import _compute_ground_truth, _llm_call, _parse_json
from infona_client.eval_models import (
    EVAL_MODEL,
    EVAL_PROVIDER,
    OPENROUTER_URL,
    SOURCE_SAMPLE_CHARS,
    SOURCE_SAMPLE_ROWS,
    DatasetStats,
    EvalReport,
    ModelConfig,
    OntologyDimension,
    OntologyScore,
    QueryScore,
    QuestionResult,
)
from infona_client.eval_ontology import OntologyEvaluator
from infona_client.eval_prompts import (
    GROUND_TRUTH_PROMPT,
    ONTOLOGY_JUDGE_PROMPT,
    QUERY_JUDGE_PROMPT,
    QUESTION_GEN_PROMPT,
)
from infona_client.eval_query import QueryEvaluator
from infona_client.eval_report import TIER_NAMES, eval_cli, format_report, report_to_json
from infona_client.eval_run import run_full_eval

__all__ = [
    "EVAL_MODEL",
    "EVAL_PROVIDER",
    "OPENROUTER_URL",
    "SOURCE_SAMPLE_CHARS",
    "SOURCE_SAMPLE_ROWS",
    "TIER_NAMES",
    "ONTOLOGY_JUDGE_PROMPT",
    "QUESTION_GEN_PROMPT",
    "QUERY_JUDGE_PROMPT",
    "GROUND_TRUTH_PROMPT",
    "ModelConfig",
    "DatasetStats",
    "OntologyDimension",
    "OntologyScore",
    "QuestionResult",
    "QueryScore",
    "EvalReport",
    "OntologyEvaluator",
    "QueryEvaluator",
    "run_full_eval",
    "rebuild_example_bank",
    "format_report",
    "report_to_json",
    "eval_cli",
    "_llm_call",
    "_parse_json",
    "_compute_ground_truth",
]
