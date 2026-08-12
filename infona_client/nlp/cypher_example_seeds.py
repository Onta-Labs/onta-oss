"""Committed Cypher few-shot seeds for the Neo4j ask path (ONTA-539).

The example bank remains a single JSONL (``eval_reports/example_bank.jsonl``).
These seeds **populate** the optional ``Example.cypher`` field with ADR 0013
shapes — they are not a second bank stack.

Coverage (open-data / synthetic only — never spider-bench / eval-mh):
  * count-by-type
  * literal equality filter
  * numeric compare
  * related-entity name filter
  * simple sum / avg
  * 1-hop related_entities

Rebuild::

    python -m infona_client.nlp.cypher_example_seeds

That merges seeds into the committed bank by ``(question, kg_name)`` identity
(refresh ``cypher`` + tags when the question already exists; otherwise append
a Cypher-only row reusing a sibling embedding when possible). Embeddings for
*new* questions require ``OPENROUTER_API_KEY``; matching existing rows reuses
their vectors (zero API cost).

Anti-overfit: no persona warehouse/SKU gold; only public open-data demo KGs
and abstract synthetic shapes.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any

from infona_client.nlp.example_bank import (
    DEFAULT_BANK_PATH,
    Example,
    ExampleBank,
    detect_pattern_tags_cypher,
    example_key,
    is_benchmark_kg,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ADR 0013-shaped Cypher bodies (parameterized; never hardcode tenant/kg)
# ---------------------------------------------------------------------------

# Compact one-liners for the prompt; full templates live in rdfs_helpers.

_COUNT_BY_TYPE = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
    "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE c.name IN $type_names OR c.id IN $type_names "
    "RETURN count(DISTINCT e) AS n"
)

_LITERAL_EQ = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
    "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE c.name IN $type_names OR c.id IN $type_names "
    "OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})"
    "-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE p.name = $prop_key AND a.literal_value = $prop_value "
    "WITH e, a WHERE a IS NOT NULL OR e[$prop_key] = $prop_value "
    "RETURN e.id AS id, e.name AS name, coalesce(a.literal_value, e[$prop_key]) AS literal_value "
    "ORDER BY e.id LIMIT $limit"
)

_NUMERIC_COMPARE = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
    "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE c.name IN $type_names OR c.id IN $type_names "
    "OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})"
    "-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE p.name = $prop_key "
    "WITH e, coalesce(a.literal_value, e[$prop_key]) AS raw WHERE raw IS NOT NULL "
    "WITH e, toFloat(CASE WHEN toString(raw) CONTAINS '^^' "
    "THEN split(toString(raw), '^^')[0] ELSE toString(raw) END) AS num "
    "WHERE num IS NOT NULL AND ("
    "($op = 'gt' AND num > $threshold) OR ($op = 'ge' AND num >= $threshold) OR "
    "($op = 'lt' AND num < $threshold) OR ($op = 'le' AND num <= $threshold) OR "
    "($op = 'eq' AND num = $threshold)) "
    "RETURN e.id AS id, e.name AS name, num AS value ORDER BY num, e.id LIMIT $limit"
)

_RELATED_NAME_FILTER = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
    "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE c.name IN $type_names OR c.id IN $type_names "
    "MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(e) "
    "MATCH (a)-[:OBJECT]->(t:Entity {tenant_id: $tenant_id, kg: $kg}) "
    "MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE p.name = $rel_attr AND ("
    "toLower(coalesce(t.display_name, '')) = toLower($target_name) OR "
    "toLower(coalesce(t.name, '')) = toLower($target_name) OR "
    "toLower(coalesce(t.display_name, t.name, '')) CONTAINS toLower($target_name)) "
    "RETURN DISTINCT e.id AS id, coalesce(e.title, e.name) AS title, "
    "coalesce(t.display_name, t.name) AS related_name ORDER BY e.id LIMIT $limit"
)

_SUM_AGG = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
    "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE c.name IN $type_names OR c.id IN $type_names "
    "OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})"
    "-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE p.name = $prop_key "
    "WITH e, coalesce(a.literal_value, e[$prop_key]) AS raw WHERE raw IS NOT NULL "
    "WITH e, toFloat(CASE WHEN toString(raw) CONTAINS '^^' "
    "THEN split(toString(raw), '^^')[0] ELSE toString(raw) END) AS num "
    "WHERE num IS NOT NULL WITH e, max(num) AS num "
    "RETURN sum(num) AS value"
)

_AVG_AGG = (
    "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
    "(c:Class {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE c.name IN $type_names OR c.id IN $type_names "
    "OPTIONAL MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})"
    "-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE p.name = $prop_key "
    "WITH e, coalesce(a.literal_value, e[$prop_key]) AS raw WHERE raw IS NOT NULL "
    "WITH e, toFloat(CASE WHEN toString(raw) CONTAINS '^^' "
    "THEN split(toString(raw), '^^')[0] ELSE toString(raw) END) AS num "
    "WHERE num IS NOT NULL WITH e, max(num) AS num "
    "RETURN avg(num) AS value"
)

_RELATED_1HOP = (
    "MATCH (from_e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->"
    "(fc:Class {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE fc.name IN $from_types OR fc.id IN $from_types "
    "MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(from_e) "
    "MATCH (a)-[:OBJECT]->(to_e:Entity {tenant_id: $tenant_id, kg: $kg}) "
    "MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg}) "
    "WHERE $rel_attr IS NULL OR p.name = $rel_attr "
    "RETURN from_e.id AS from_id, from_e.name AS from_name, "
    "to_e.id AS to_id, to_e.name AS to_name, p.name AS rel_type "
    "ORDER BY from_id, to_id LIMIT $limit"
)

# Shape labels used by coverage tests (stable API).
SHAPE_COUNT_BY_TYPE = "count_by_type"
SHAPE_LITERAL_FILTER = "literal_filter"
SHAPE_NUMERIC_COMPARE = "numeric_compare"
SHAPE_RELATED_NAME_FILTER = "related_entity_name_filter"
SHAPE_SUM = "sum"
SHAPE_AVG = "avg"
SHAPE_RELATED_1HOP = "related_entities_1hop"

REQUIRED_CYPHER_SHAPES: frozenset[str] = frozenset(
    {
        SHAPE_COUNT_BY_TYPE,
        SHAPE_LITERAL_FILTER,
        SHAPE_NUMERIC_COMPARE,
        SHAPE_RELATED_NAME_FILTER,
        SHAPE_SUM,
        SHAPE_AVG,
        SHAPE_RELATED_1HOP,
    }
)


def _seed(
    *,
    shape: str,
    question: str,
    kg_name: str,
    cypher: str,
    ontology_context: str,
    sparql: str = "",
) -> dict[str, Any]:
    if is_benchmark_kg(kg_name):
        raise ValueError(f"seed refuses benchmark KG {kg_name!r}")
    tags = detect_pattern_tags_cypher(cypher)
    return {
        "shape": shape,
        "question": question,
        "kg_name": kg_name,
        "cypher": cypher,
        "sparql": sparql,
        "ontology_context": ontology_context,
        "pattern_tags": tags,
    }


# Open-data questions match the committed SPARQL bank so refresh reuses embeddings.
CYPHER_SEEDS: list[dict[str, Any]] = [
    _seed(
        shape=SHAPE_COUNT_BY_TYPE,
        question="How many movies are in the dataset?",
        kg_name="imdb-movies",
        cypher=_COUNT_BY_TYPE,
        ontology_context="Type: Movie\n  - title (string)\n  - imdb_rating (float)",
    ),
    _seed(
        shape=SHAPE_COUNT_BY_TYPE,
        question="How many events are there in total?",
        kg_name="events-sf",
        cypher=_COUNT_BY_TYPE,
        ontology_context="Type: Event\n  - name (string)\n  - category (string)",
    ),
    _seed(
        shape=SHAPE_LITERAL_FILTER,
        question="How many events have the category 'Technology & AI'?",
        kg_name="events-sf",
        cypher=_LITERAL_EQ,
        ontology_context="Type: Event\n  - category (string)",
    ),
    _seed(
        shape=SHAPE_LITERAL_FILTER,
        question="How many complaints have the Product 'Student loan'?",
        kg_name="cfpb-complaints",
        cypher=_LITERAL_EQ,
        ontology_context="Type: ConsumerComplaint\n  - product (string)",
    ),
    _seed(
        shape=SHAPE_NUMERIC_COMPARE,
        question="How many coffee lots have a Total_Cup_Points score greater than 88?",
        kg_name="coffee-quality",
        cypher=_NUMERIC_COMPARE,
        ontology_context="Type: CoffeeLot\n  - Total_Cup_Points (float)",
    ),
    _seed(
        shape=SHAPE_NUMERIC_COMPARE,
        question="How many video games have global sales greater than 20 million?",
        kg_name="video-games",
        cypher=_NUMERIC_COMPARE,
        ontology_context="Type: VideoGame\n  - Global_Sales (float)",
    ),
    _seed(
        shape=SHAPE_RELATED_NAME_FILTER,
        question="How many movies were directed by Alfred Hitchcock?",
        kg_name="imdb-movies",
        cypher=_RELATED_NAME_FILTER,
        ontology_context=(
            "Type: Movie\n  - director → Person (predicate: director)\n"
            "Type: Person\n  - name (string)"
        ),
    ),
    _seed(
        shape=SHAPE_RELATED_NAME_FILTER,
        question="How many movies were directed by Christopher Nolan?",
        kg_name="imdb-movies",
        cypher=_RELATED_NAME_FILTER,
        ontology_context=(
            "Type: Movie\n  - director → Person (predicate: director)\n"
            "Type: Person\n  - name (string)"
        ),
    ),
    _seed(
        shape=SHAPE_SUM,
        question="What is the total Gross revenue of all movies starring Tom Hanks in any star position?",
        kg_name="imdb-movies",
        cypher=_SUM_AGG,
        ontology_context="Type: Movie\n  - gross (float)\n  - star → Person",
    ),
    _seed(
        shape=SHAPE_AVG,
        question="What is the average IMDB rating of movies directed by Steven Spielberg?",
        kg_name="imdb-movies",
        cypher=_AVG_AGG,
        ontology_context="Type: Movie\n  - imdb_rating (float)\n  - director → Person",
    ),
    _seed(
        shape=SHAPE_AVG,
        question="What is the average global sales for video games published by Electronic Arts?",
        kg_name="video-games",
        cypher=_AVG_AGG,
        ontology_context="Type: VideoGame\n  - Global_Sales (float)\n  - publisher → Publisher",
    ),
    _seed(
        shape=SHAPE_RELATED_1HOP,
        question="How many unique directors are there?",
        kg_name="imdb-movies",
        cypher=_RELATED_1HOP,
        ontology_context="Type: Movie\n  - director → Person\nType: Person",
    ),
    # Synthetic teaching rows (not persona/SKU gold): abstract shapes with
    # public-style type names so retrieval can surface them on novel KGs.
    _seed(
        shape=SHAPE_COUNT_BY_TYPE,
        question="How many entities of type Book are there?",
        kg_name="synthetic-cypher-shapes",
        cypher=_COUNT_BY_TYPE,
        ontology_context="Type: Book\n  - title (string)",
    ),
    _seed(
        shape=SHAPE_RELATED_1HOP,
        question="List related entities one hop from each Book via the author relationship",
        kg_name="synthetic-cypher-shapes",
        cypher=_RELATED_1HOP,
        ontology_context="Type: Book\n  - author → Person\nType: Person\n  - name (string)",
    ),
    _seed(
        shape=SHAPE_SUM,
        question="What is the total of the price attribute across all Product entities?",
        kg_name="synthetic-cypher-shapes",
        cypher=_SUM_AGG,
        ontology_context="Type: Product\n  - price (float)",
    ),
]


def seed_shapes_present(seeds: list[dict[str, Any]] | None = None) -> set[str]:
    """Return the set of shape labels present in the seed table."""
    return {s["shape"] for s in (seeds or CYPHER_SEEDS)}


def apply_cypher_seeds_to_examples(
    examples: list[Example],
    seeds: list[dict[str, Any]] | None = None,
) -> tuple[list[Example], dict[str, int]]:
    """Merge seed Cypher into an in-memory example list.

    * Matching ``(question, kg_name)`` → refresh ``cypher`` (and tags when empty
      or Cypher-only tags preferred).
    * Unmatched seeds → append Cypher-only ``Example`` rows. Embeddings are
      copied from a same-kg sibling when available, else left empty (caller may
      re-embed).

    Returns ``(updated_examples, stats)`` where stats has keys
    ``refreshed``, ``appended``, ``skipped_benchmark``.
    """
    seeds = seeds or CYPHER_SEEDS
    by_key = {ex.key: ex for ex in examples}
    # Prefer any non-empty embedding on the same kg for new synthetic rows.
    emb_by_kg: dict[str, list[float]] = {}
    for ex in examples:
        if ex.embedding and ex.kg_name not in emb_by_kg:
            emb_by_kg[ex.kg_name] = list(ex.embedding)
    # Fallback: first non-empty embedding in the bank (keeps cosine defined).
    any_emb: list[float] = []
    for ex in examples:
        if ex.embedding:
            any_emb = list(ex.embedding)
            break

    refreshed = 0
    appended = 0
    skipped_benchmark = 0
    out = list(examples)

    for seed in seeds:
        kg = seed.get("kg_name", "") or ""
        if is_benchmark_kg(kg):
            skipped_benchmark += 1
            continue
        q = seed["question"]
        key = example_key(q, kg)
        cypher = (seed.get("cypher") or "").strip()
        if not cypher:
            continue
        # Refuse SPARQL bodies masquerading as cypher.
        if re.search(r"(?i)\bSELECT\b.*\bWHERE\s*\{|\bPREFIX\b|\bFROM\s*<", cypher):
            logger.warning("Skipping seed with SPARQL-like body: %s", q[:60])
            continue

        existing = by_key.get(key)
        if existing is not None:
            changed = existing.refresh_from(
                seed.get("sparql", "") or "",
                seed.get("ontology_context", "") or "",
                cypher=cypher,
            )
            # Always ensure cypher is set even if refresh_from no-ops on equal text.
            if not (existing.cypher or "").strip():
                existing.cypher = cypher
                existing.pattern_tags = (
                    seed.get("pattern_tags")
                    or detect_pattern_tags_cypher(cypher)
                    or existing.pattern_tags
                )
                changed = True
            if changed:
                refreshed += 1
            continue

        emb = emb_by_kg.get(kg) or any_emb or []
        # For synthetic kg with no prior rows, reuse any_emb so retrieve matrix works.
        new_ex = Example(
            question=q,
            sparql=seed.get("sparql", "") or "",
            kg_name=kg,
            ontology_context=seed.get("ontology_context", "") or "",
            pattern_tags=list(
                seed.get("pattern_tags") or detect_pattern_tags_cypher(cypher)
            ),
            embedding=list(emb),
            cypher=cypher,
        )
        out.append(new_ex)
        by_key[key] = new_ex
        if emb and kg not in emb_by_kg:
            emb_by_kg[kg] = list(emb)
        appended += 1

    stats = {
        "refreshed": refreshed,
        "appended": appended,
        "skipped_benchmark": skipped_benchmark,
    }
    return out, stats


def merge_cypher_seeds_into_bank(
    bank_path: str | Path | None = None,
    *,
    seeds: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Load JSONL bank, apply seeds, save. Hermetic (no embed API)."""
    path = Path(bank_path) if bank_path else DEFAULT_BANK_PATH
    bank = ExampleBank(openrouter_api_key="", bank_path=path)
    bank.load()
    updated, stats = apply_cypher_seeds_to_examples(bank._examples, seeds=seeds)
    bank._examples = updated
    bank.save()
    stats["bank_size"] = bank.size
    stats["with_cypher"] = sum(1 for ex in bank._examples if (ex.cypher or "").strip())
    logger.info(
        "Cypher seeds merged into %s: refreshed=%d appended=%d with_cypher=%d/%d",
        path,
        stats["refreshed"],
        stats["appended"],
        stats["with_cypher"],
        stats["bank_size"],
    )
    return stats


def bank_cypher_shape_coverage(
    bank_path: str | Path | None = None,
) -> set[str]:
    """Detect which required shapes appear in the committed bank's cypher field.

    Heuristic: scan non-empty ``cypher`` bodies for structural signatures of
    each required shape (not seed metadata — the bank JSONL does not store
    ``shape`` labels).
    """
    path = Path(bank_path) if bank_path else DEFAULT_BANK_PATH
    if not path.is_file():
        return set()
    found: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        cy = (row.get("cypher") or "").strip()
        if not cy:
            continue
        if is_benchmark_kg(row.get("kg_name") or ""):
            continue
        cl = cy.lower()
        if "count(distinct" in cl or "count(*)" in cl or "count(e)" in cl:
            if "instance_of" in cl:
                found.add(SHAPE_COUNT_BY_TYPE)
        if "$prop_value" in cy or (
            "literal_value" in cl and ("= $prop_value" in cl or "=$prop_value" in cl)
        ):
            found.add(SHAPE_LITERAL_FILTER)
        if "$threshold" in cy or (
            "tofloat" in cl and any(op in cl for op in ("> $", "< $", ">= $", "<= $"))
        ):
            found.add(SHAPE_NUMERIC_COMPARE)
        if "$target_name" in cy or (
            "contains toLower" in cl.replace(" ", "")
            or "contains(tolower" in cl.replace(" ", "")
        ) and "[:object]" in cl:
            found.add(SHAPE_RELATED_NAME_FILTER)
        if re.search(r"\bsum\s*\(", cy, re.I):
            found.add(SHAPE_SUM)
        if re.search(r"\bavg\s*\(", cy, re.I):
            found.add(SHAPE_AVG)
        if (
            "from_e" in cl
            or ("[:object]" in cl and "[:subject]" in cl and "from_types" in cl)
            or ("$from_types" in cy and "$to_types" in cy)
            or ("$from_types" in cy and "to_e" in cl)
        ):
            found.add(SHAPE_RELATED_1HOP)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge ADR 0013 Cypher few-shot seeds into example_bank.jsonl (ONTA-539)."
    )
    parser.add_argument(
        "--bank",
        type=Path,
        default=None,
        help=f"Path to example_bank.jsonl (default: {DEFAULT_BANK_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Apply in memory and print stats without writing.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    path = args.bank or DEFAULT_BANK_PATH
    if args.dry_run:
        bank = ExampleBank(openrouter_api_key="", bank_path=path)
        bank.load()
        updated, stats = apply_cypher_seeds_to_examples(bank._examples)
        stats["bank_size"] = len(updated)
        stats["with_cypher"] = sum(1 for ex in updated if (ex.cypher or "").strip())
        print(json.dumps(stats, indent=2))
        return 0
    stats = merge_cypher_seeds_into_bank(path)
    print(json.dumps(stats, indent=2))
    coverage = bank_cypher_shape_coverage(path)
    missing = REQUIRED_CYPHER_SHAPES - coverage
    if missing:
        print(f"WARNING: missing shapes after merge: {sorted(missing)}")
        return 2
    print(f"OK: covered shapes {sorted(coverage)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
