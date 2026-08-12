"""Committed Cypher few-shot seeds for the Neo4j ask path (ONTA-539).

The example bank remains a single JSONL (``eval_reports/example_bank.jsonl``).
These seeds **populate** the optional ``Example.cypher`` field with ADR 0013
shapes — they are not a second bank stack.

Coverage (open-data / synthetic only — never spider-bench / eval-mh):
  * count-by-type
  * literal equality filter (+ filtered count)
  * numeric compare (+ filtered count)
  * related-entity name filter (+ filtered count / filtered agg)
  * simple sum / avg
  * 1-hop related_entities (+ count-distinct targets)

**Q ↔ Cypher fidelity:** open-data seeds that match an existing SPARQL bank
row MUST return the same coarse answer shape (COUNT→count(, SUM→sum(,
AVG→avg(, filtered count still returns a scalar count — never a bare list).
Poison few-shots that mis-teach the LLM are guarded by
``tests/test_example_bank_cypher_fidelity.py``.

Templates are composed from ``infona_client.graph.rdfs_helpers`` (canonical
ADR 0013 Cypher) — do not fork stripped copies of those constants.

Rebuild::

    python -m infona_client.nlp.cypher_example_seeds

That merges seeds into the committed bank by ``(question, kg_name)`` identity
(refresh ``cypher`` + tags when the question already exists; otherwise append
a Cypher-only row reusing a sibling embedding when possible). Embeddings for
*new* questions require ``OPENROUTER_API_KEY``; matching existing rows reuses
their vectors (zero API cost).

**Synthetic embedding reuse:** newly appended synthetic rows copy a sibling
embedding (same-kg if present, else any bank vector) so the retrieve matrix
stays well-defined without an embed API call. Cosine scores for those rows
are **not** meaningful until a real re-embed; they are still language-filtered
into the cypher pool. Prefer open-data rows (real embeds) for production
retrieval quality; treat synthetic rows as shape coverage / teaching scaffolds.

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

from infona_client.graph.rdfs_helpers import (
    ENTITIES_OF_TYPE_COUNT_CYPHER,
    LITERAL_AGGREGATE_CYPHER,
    LITERAL_COMPARE_CYPHER,
    LITERAL_VALUES_CYPHER,
    RELATED_ENTITIES_CYPHER,
    RELATED_ENTITY_NAME_FILTER_CYPHER,
)
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
# Compose teaching variants from canonical rdfs_helpers templates
# ---------------------------------------------------------------------------


def _one_line(cypher: str) -> str:
    """Collapse multi-line template whitespace for compact few-shot bodies."""
    return " ".join((cypher or "").split())


def _swap_return(cypher: str, new_return: str) -> str:
    """Replace the terminal RETURN … (incl. ORDER BY / LIMIT) of a template."""
    text = (cypher or "").strip()
    if not text:
        return new_return.strip()
    rewritten = re.sub(r"(?is)\bRETURN\b.+$", new_return.strip(), text)
    return _one_line(rewritten)


def _as_count_subjects(list_cypher: str, entity_alias: str = "e") -> str:
    """List-returning template → ``count(DISTINCT <alias>) AS n`` (scalar)."""
    return _swap_return(list_cypher, f"RETURN count(DISTINCT {entity_alias}) AS n")


def _as_count_targets(related_cypher: str, target_alias: str = "to_e") -> str:
    """1-hop related_entities list → count DISTINCT targets (unique directors)."""
    return _swap_return(
        related_cypher, f"RETURN count(DISTINCT {target_alias}) AS n"
    )


# Filtered aggregate: related-entity name gate + literal property agg.
# Composes the structure of RELATED_ENTITY_NAME_FILTER_CYPHER with the
# numeric extraction / $agg_op CASE from LITERAL_AGGREGATE_CYPHER — not a
# fork of either body in isolation; both shapes stay import-sourced for the
# unfiltered cases.
_RELATED_NAME_FILTERED_AGG = _one_line(
    """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})-[:INSTANCE_OF]->(c:Class {
  tenant_id: $tenant_id, kg: $kg
})
WHERE c.name IN $type_names OR c.id IN $type_names
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(e)
MATCH (a)-[:OBJECT]->(t:Entity {tenant_id: $tenant_id, kg: $kg})
MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $rel_attr
  AND (
    toLower(coalesce(t.display_name, '')) = toLower($target_name)
    OR toLower(coalesce(t.name, '')) = toLower($target_name)
    OR toLower(replace(coalesce(t.name, ''), '_', ' ')) = toLower($target_name)
    OR toLower(coalesce(t.display_name, t.name, '')) CONTAINS toLower($target_name)
  )
OPTIONAL MATCH (la:Assertion {tenant_id: $tenant_id, kg: $kg, subject_id: e.id})
  -[:PREDICATE]->(lp:Property {tenant_id: $tenant_id, kg: $kg})
WHERE lp.name = $prop_key
WITH e, coalesce(la.literal_value, e[$prop_key]) AS raw
WHERE raw IS NOT NULL
WITH e, toFloat(
  CASE
    WHEN toString(raw) CONTAINS '^^' THEN split(toString(raw), '^^')[0]
    ELSE toString(raw)
  END
) AS num
WHERE num IS NOT NULL
WITH e, max(num) AS num
RETURN CASE
  WHEN $agg_op = 'sum' THEN sum(num)
  WHEN $agg_op = 'avg' THEN avg(num)
  WHEN $agg_op = 'min' THEN min(num)
  WHEN $agg_op = 'max' THEN max(num)
  ELSE null
END AS value
"""
)

# Canonical templates (compact one-liners for the prompt).
_COUNT_BY_TYPE = _one_line(ENTITIES_OF_TYPE_COUNT_CYPHER)
_LITERAL_EQ_LIST = _one_line(LITERAL_VALUES_CYPHER)
_LITERAL_EQ_COUNT = _as_count_subjects(LITERAL_VALUES_CYPHER, "e")
_NUMERIC_COMPARE_LIST = _one_line(LITERAL_COMPARE_CYPHER)
_NUMERIC_COMPARE_COUNT = _as_count_subjects(LITERAL_COMPARE_CYPHER, "e")
_RELATED_NAME_LIST = _one_line(RELATED_ENTITY_NAME_FILTER_CYPHER)
_RELATED_NAME_COUNT = _as_count_subjects(RELATED_ENTITY_NAME_FILTER_CYPHER, "e")
_SUM_AVG_BARE = _one_line(LITERAL_AGGREGATE_CYPHER)
_RELATED_1HOP_LIST = _one_line(RELATED_ENTITIES_CYPHER)
_RELATED_1HOP_COUNT_TARGETS = _as_count_targets(RELATED_ENTITIES_CYPHER, "to_e")
_RELATED_NAME_AGG = _RELATED_NAME_FILTERED_AGG

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


# Open-data questions match the committed SPARQL bank so refresh reuses
# embeddings. Cypher bodies match SPARQL coarse shape (count/agg/filter).
CYPHER_SEEDS: list[dict[str, Any]] = [
    # --- bare type counts (SPARQL COUNT of type) ---
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
    # --- filtered COUNT via related-entity name (not a list) ---
    # SPARQL: COUNT movies WHERE director name CONTAINS …
    _seed(
        shape=SHAPE_RELATED_NAME_FILTER,
        question="How many movies were directed by Alfred Hitchcock?",
        kg_name="imdb-movies",
        cypher=_RELATED_NAME_COUNT,
        ontology_context=(
            "Type: Movie\n  - director → Person (predicate: director)\n"
            "Type: Person\n  - name (string)"
        ),
    ),
    _seed(
        shape=SHAPE_RELATED_NAME_FILTER,
        question="How many movies were directed by Christopher Nolan?",
        kg_name="imdb-movies",
        cypher=_RELATED_NAME_COUNT,
        ontology_context=(
            "Type: Movie\n  - director → Person (predicate: director)\n"
            "Type: Person\n  - name (string)"
        ),
    ),
    # SPARQL uses event_category → EventCategory name (related, not literal).
    _seed(
        shape=SHAPE_RELATED_NAME_FILTER,
        question="How many events have the category 'Technology & AI'?",
        kg_name="events-sf",
        cypher=_RELATED_NAME_COUNT,
        ontology_context=(
            "Type: Event\n  - event_category → EventCategory\n"
            "Type: EventCategory\n  - name (string)"
        ),
    ),
    # SPARQL: product → Product name equality (related entity).
    _seed(
        shape=SHAPE_RELATED_NAME_FILTER,
        question="How many complaints have the Product 'Student loan'?",
        kg_name="cfpb-complaints",
        cypher=_RELATED_NAME_COUNT,
        ontology_context=(
            "Type: ConsumerComplaint\n  - product → Product\n"
            "Type: Product\n  - name (string)"
        ),
    ),
    # --- filtered COUNT via numeric compare (not a list) ---
    _seed(
        shape=SHAPE_NUMERIC_COMPARE,
        question="How many coffee lots have a Total_Cup_Points score greater than 88?",
        kg_name="coffee-quality",
        cypher=_NUMERIC_COMPARE_COUNT,
        ontology_context="Type: CoffeeLot\n  - Total_Cup_Points (float)",
    ),
    _seed(
        shape=SHAPE_NUMERIC_COMPARE,
        question="How many video games have global sales greater than 20 million?",
        kg_name="video-games",
        cypher=_NUMERIC_COMPARE_COUNT,
        ontology_context="Type: VideoGame\n  - Global_Sales (float)",
    ),
    # --- filtered aggregates (related-name gate + sum/avg) ---
    _seed(
        shape=SHAPE_SUM,
        question=(
            "What is the total Gross revenue of all movies starring "
            "Tom Hanks in any star position?"
        ),
        kg_name="imdb-movies",
        cypher=_RELATED_NAME_AGG,
        ontology_context="Type: Movie\n  - gross (float)\n  - star → Person",
    ),
    _seed(
        shape=SHAPE_AVG,
        question="What is the average IMDB rating of movies directed by Steven Spielberg?",
        kg_name="imdb-movies",
        cypher=_RELATED_NAME_AGG,
        ontology_context="Type: Movie\n  - imdb_rating (float)\n  - director → Person",
    ),
    _seed(
        shape=SHAPE_AVG,
        question=(
            "What is the average global sales for video games published by "
            "Electronic Arts?"
        ),
        kg_name="video-games",
        cypher=_RELATED_NAME_AGG,
        ontology_context=(
            "Type: VideoGame\n  - Global_Sales (float)\n  - publisher → Organization"
        ),
    ),
    # --- COUNT DISTINCT related targets (unique directors) ---
    _seed(
        shape=SHAPE_RELATED_1HOP,
        question="How many unique directors are there?",
        kg_name="imdb-movies",
        cypher=_RELATED_1HOP_COUNT_TARGETS,
        ontology_context="Type: Movie\n  - director → Person\nType: Person",
    ),
    # --- Synthetic teaching rows (shape-matched questions; not persona gold) ---
    # New synthetic questions reuse a sibling embedding at seed time — see
    # module docstring. Real re-embed optional when OPENROUTER_API_KEY present.
    _seed(
        shape=SHAPE_COUNT_BY_TYPE,
        question="How many entities of type Book are there?",
        kg_name="synthetic-cypher-shapes",
        cypher=_COUNT_BY_TYPE,
        ontology_context="Type: Book\n  - title (string)",
    ),
    _seed(
        shape=SHAPE_LITERAL_FILTER,
        question="List Product entities whose status attribute equals the given value",
        kg_name="synthetic-cypher-shapes",
        cypher=_LITERAL_EQ_LIST,
        ontology_context="Type: Product\n  - status (string)",
    ),
    _seed(
        shape=SHAPE_LITERAL_FILTER,
        question="How many Product entities have status equal to the given value?",
        kg_name="synthetic-cypher-shapes",
        cypher=_LITERAL_EQ_COUNT,
        ontology_context="Type: Product\n  - status (string)",
    ),
    _seed(
        shape=SHAPE_NUMERIC_COMPARE,
        question="List Widget entities whose weight attribute is greater than a threshold",
        kg_name="synthetic-cypher-shapes",
        cypher=_NUMERIC_COMPARE_LIST,
        ontology_context="Type: Widget\n  - weight (float)",
    ),
    _seed(
        shape=SHAPE_RELATED_NAME_FILTER,
        question="List Book entities related via author to a Person named by target_name",
        kg_name="synthetic-cypher-shapes",
        cypher=_RELATED_NAME_LIST,
        ontology_context="Type: Book\n  - author → Person\nType: Person\n  - name (string)",
    ),
    _seed(
        shape=SHAPE_RELATED_1HOP,
        question="List related entities one hop from each Book via the author relationship",
        kg_name="synthetic-cypher-shapes",
        cypher=_RELATED_1HOP_LIST,
        ontology_context="Type: Book\n  - author → Person\nType: Person\n  - name (string)",
    ),
    _seed(
        shape=SHAPE_SUM,
        question="What is the total of the price attribute across all Product entities?",
        kg_name="synthetic-cypher-shapes",
        cypher=_SUM_AVG_BARE,
        ontology_context="Type: Product\n  - price (float)",
    ),
    _seed(
        shape=SHAPE_AVG,
        question="What is the average of the price attribute across all Product entities?",
        kg_name="synthetic-cypher-shapes",
        cypher=_SUM_AVG_BARE,
        ontology_context="Type: Product\n  - price (float)",
    ),
]


def seed_shapes_present(seeds: list[dict[str, Any]] | None = None) -> set[str]:
    """Return the set of shape labels present in the seed table."""
    return {s["shape"] for s in (seeds or CYPHER_SEEDS)}


# ---------------------------------------------------------------------------
# Coarse answer-shape classifiers (SPARQL ↔ Cypher fidelity)
# ---------------------------------------------------------------------------

# Returned as frozensets of tags so multi-tag bodies (e.g. filtered sum) work.
_SHAPE_COUNT = "count"
_SHAPE_SUM = "sum"
_SHAPE_AVG = "avg"
_SHAPE_LIST = "list"
_SHAPE_HAS_FILTER = "filtered"


def classify_sparql_shape(sparql: str) -> frozenset[str]:
    """Coarse answer shape of a SPARQL body (for Q↔Cypher fidelity)."""
    s = sparql or ""
    tags: set[str] = set()
    if re.search(r"(?i)\bCOUNT\s*\(", s):
        tags.add(_SHAPE_COUNT)
    if re.search(r"(?i)\bSUM\s*\(", s):
        tags.add(_SHAPE_SUM)
    if re.search(r"(?i)\bAVG\s*\(", s):
        tags.add(_SHAPE_AVG)
    # List / projection of variables without an outer aggregation.
    if not tags and re.search(r"(?i)\bSELECT\b", s):
        tags.add(_SHAPE_LIST)
    # Do NOT match bare ``<`` / ``>`` — SPARQL IRIs use angle brackets
    # (``?x a <https://…/types/Movie>``) and would false-positive every typed
    # query as "filtered". Rely on FILTER / UNION / explicit relational ops
    # outside IRIs (``?n > 88``, ``FILTER(?x >= 1)``).
    if re.search(r"(?i)\bFILTER\b|\bUNION\b", s):
        tags.add(_SHAPE_HAS_FILTER)
    elif re.search(
        r"(?i)[?$]\w+\s*(?:>=|<=|!=|<>|>|<)\s*(?:[?$]\w+|[0-9.'\"-])",
        s,
    ):
        tags.add(_SHAPE_HAS_FILTER)
    return frozenset(tags)


def classify_cypher_shape(cypher: str) -> frozenset[str]:
    """Coarse answer shape of a Cypher body (for Q↔Cypher fidelity)."""
    c = cypher or ""
    cl = c.lower()
    tags: set[str] = set()
    if re.search(r"(?i)\bcount\s*\(", c):
        tags.add(_SHAPE_COUNT)
    if re.search(r"(?i)\bsum\s*\(", c):
        tags.add(_SHAPE_SUM)
    # $agg_op CASE form from LITERAL_AGGREGATE / filtered-agg composition.
    if re.search(r"(?i)\bavg\s*\(", c) or (
        "$agg_op" in c and re.search(r"(?i)avg", c)
    ):
        tags.add(_SHAPE_AVG)
    # Bare $agg_op CASE covers sum too when sum( is only inside the CASE arm.
    if "$agg_op" in c and re.search(r"(?i)\bsum\s*\(", c):
        tags.add(_SHAPE_SUM)
    if not tags & {_SHAPE_COUNT, _SHAPE_SUM, _SHAPE_AVG}:
        if re.search(r"(?i)\bRETURN\b", c):
            tags.add(_SHAPE_LIST)
    if (
        "$prop_value" in c
        or "$threshold" in c
        or "$target_name" in c
        or "$op" in c
        or "contains toLower" in cl.replace(" ", "")
        or "contains(tolower" in cl.replace(" ", "")
    ):
        tags.add(_SHAPE_HAS_FILTER)
    return frozenset(tags)


def sparql_cypher_shape_compatible(
    sparql: str, cypher: str
) -> tuple[bool, str]:
    """Return (ok, reason). Fails when answer shapes disagree.

    Rules (coarse, hermetic — not full semantic equivalence):
      * SPARQL COUNT requires Cypher count(
      * SPARQL SUM requires Cypher sum( (or $agg_op CASE with sum arm)
      * SPARQL AVG requires Cypher avg( (or $agg_op CASE with avg arm)
      * SPARQL aggregation must not be answered by a bare list Cypher
      * When SPARQL is filtered and Cypher is an aggregation/count, Cypher
        should also carry a filter signature (param or CONTAINS) — bare
        type-count / bare sum against a filtered SPARQL is poison.
    """
    if not (sparql or "").strip() or not (cypher or "").strip():
        return True, "skip (missing sparql or cypher)"
    ss = classify_sparql_shape(sparql)
    cs = classify_cypher_shape(cypher)
    agg = {_SHAPE_COUNT, _SHAPE_SUM, _SHAPE_AVG}

    sparql_agg = ss & agg
    cypher_agg = cs & agg

    if sparql_agg and not cypher_agg:
        return False, (
            f"SPARQL aggregation {sorted(sparql_agg)} but Cypher has no "
            f"matching agg (cypher tags={sorted(cs)}) — list/empty body mis-teaches"
        )
    if _SHAPE_COUNT in sparql_agg and _SHAPE_COUNT not in cypher_agg:
        return False, "SPARQL COUNT requires Cypher count("
    if _SHAPE_SUM in sparql_agg and _SHAPE_SUM not in cypher_agg:
        return False, "SPARQL SUM requires Cypher sum("
    if _SHAPE_AVG in sparql_agg and _SHAPE_AVG not in cypher_agg:
        return False, "SPARQL AVG requires Cypher avg("

    # Filtered SPARQL aggregation answered by an unfiltered Cypher agg is poison
    # (e.g. "total gross of movies starring Tom Hanks" → bare sum of all movies).
    if sparql_agg and _SHAPE_HAS_FILTER in ss and _SHAPE_HAS_FILTER not in cs:
        return False, (
            "SPARQL is filtered aggregation/count but Cypher lacks filter "
            "signature ($prop_value/$threshold/$target_name/$op/CONTAINS)"
        )
    return True, "ok"


def apply_cypher_seeds_to_examples(
    examples: list[Example],
    seeds: list[dict[str, Any]] | None = None,
) -> tuple[list[Example], dict[str, int]]:
    """Merge seed Cypher into an in-memory example list.

    * Matching ``(question, kg_name)`` → refresh ``cypher`` (and tags when empty
      or Cypher-only tags preferred).
    * Unmatched seeds → append Cypher-only ``Example`` rows. Embeddings are
      copied from a same-kg sibling when available, else left empty (caller may
      re-embed). **Synthetic reuse is intentional** (see module docstring);
      cosine ranks for those rows are not production-quality until re-embedded.

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
            # Fidelity: when the row already has SPARQL, refuse a shape-mismatched
            # cypher so seed merges cannot re-poison the bank.
            if (existing.sparql or "").strip():
                ok, reason = sparql_cypher_shape_compatible(existing.sparql, cypher)
                if not ok:
                    logger.warning(
                        "Skipping shape-mismatched seed for %r (%s): %s",
                        q[:60],
                        kg,
                        reason,
                    )
                    continue
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
            elif (existing.cypher or "").strip() != cypher:
                # refresh_from should have written it; belt-and-suspenders.
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
        compact = cl.replace(" ", "")
        if "$target_name" in cy or (
            ("contains tolower" in compact or "contains(tolower" in compact)
            and ("[:object]" in cl or "-[:object]->" in cl)
        ):
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
    # Fidelity scan of dual-language rows.
    bad = 0
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        sp = (row.get("sparql") or "").strip()
        cy = (row.get("cypher") or "").strip()
        if not sp or not cy:
            continue
        ok, reason = sparql_cypher_shape_compatible(sp, cy)
        if not ok:
            bad += 1
            print(f"FIDELITY FAIL: {row.get('question', '')[:80]!r}: {reason}")
    if bad:
        print(f"WARNING: {bad} dual-language row(s) failed shape fidelity")
        return 3
    print("OK: dual-language shape fidelity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
