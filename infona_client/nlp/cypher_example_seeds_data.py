"""Cypher few-shot seed table (ADR 0013 shapes).

Imported by :mod:`infona_client.nlp.cypher_example_seeds`. Keep under the
new-file cap. No eval-set strings.
"""
from __future__ import annotations

import re
from typing import Any

from infona_client.graph.rdfs_helpers import (
    ENTITIES_OF_TYPE_COUNT_CYPHER,
    LITERAL_AGGREGATE_CYPHER,
    LITERAL_ARGMAX_BY_DIM_CYPHER,
    LITERAL_COMPARE_COUNT_CYPHER,
    LITERAL_COMPARE_CYPHER,
    LITERAL_VALUES_CYPHER,
    RELATED_ENTITIES_CYPHER,
    RELATED_ENTITY_NAME_FILTER_CYPHER,
)
from infona_client.nlp.example_bank import detect_pattern_tags_cypher, is_benchmark_kg

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
_NUMERIC_COMPARE_COUNT = _one_line(LITERAL_COMPARE_COUNT_CYPHER)
_RELATED_NAME_LIST = _one_line(RELATED_ENTITY_NAME_FILTER_CYPHER)
_RELATED_NAME_COUNT = _as_count_subjects(RELATED_ENTITY_NAME_FILTER_CYPHER, "e")
_SUM_AVG_BARE = _one_line(LITERAL_AGGREGATE_CYPHER)
_RELATED_1HOP_LIST = _one_line(RELATED_ENTITIES_CYPHER)
_RELATED_1HOP_COUNT_TARGETS = _as_count_targets(RELATED_ENTITIES_CYPHER, "to_e")
_RELATED_NAME_AGG = _RELATED_NAME_FILTERED_AGG
_ARGMAX_BY_DIM = _one_line(LITERAL_ARGMAX_BY_DIM_CYPHER)

# Shape labels used by coverage tests (stable API).
SHAPE_COUNT_BY_TYPE = "count_by_type"
SHAPE_LITERAL_FILTER = "literal_filter"
SHAPE_NUMERIC_COMPARE = "numeric_compare"
SHAPE_RELATED_NAME_FILTER = "related_entity_name_filter"
SHAPE_SUM = "sum"
SHAPE_AVG = "avg"
SHAPE_RELATED_1HOP = "related_entities_1hop"
SHAPE_ARGMAX = "argmax_by_dim"
SHAPE_GRAPH_EXISTS = "graph_exists"
SHAPE_GRAPH_DEGREE = "graph_degree"
SHAPE_GRAPH_PATH = "graph_shortest_path"

REQUIRED_CYPHER_SHAPES: frozenset[str] = frozenset(
    {
        SHAPE_COUNT_BY_TYPE,
        SHAPE_LITERAL_FILTER,
        SHAPE_NUMERIC_COMPARE,
        SHAPE_RELATED_NAME_FILTER,
        SHAPE_SUM,
        SHAPE_AVG,
        SHAPE_RELATED_1HOP,
        SHAPE_ARGMAX,
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
    _seed(
        shape=SHAPE_ARGMAX,
        question=(
            "Which Product region_code group has the highest total of the price attribute?"
        ),
        kg_name="synthetic-cypher-shapes",
        cypher=_ARGMAX_BY_DIM,
        ontology_context="Type: Product\n  - region_code (string)\n  - price (float)",
    ),
    _seed(
        shape=SHAPE_GRAPH_EXISTS,
        question=(
            "Is the following triplet fact present in the knowledge graph "
            "(Yes/No)? (Widget A, made_by, Acme)"
        ),
        kg_name="synthetic-cypher-shapes",
        cypher=_one_line(
            """
MATCH (from_e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE toLower(coalesce(from_e.display_name, from_e.name, '')) = toLower($from_name)
MATCH (to_e:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE toLower(coalesce(to_e.display_name, to_e.name, '')) = toLower($to_name)
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(from_e)
MATCH (a)-[:OBJECT]->(to_e)
MATCH (a)-[:PREDICATE]->(p:Property {tenant_id: $tenant_id, kg: $kg})
WHERE p.name = $rel_attr
RETURN CASE WHEN count(a) > 0 THEN 'Yes' ELSE 'No' END AS answer
"""
        ),
        ontology_context="Type: Widget\n  - made_by → Maker\nType: Maker\n  - name (string)",
    ),
    _seed(
        shape=SHAPE_GRAPH_DEGREE,
        question=(
            "Which entity has the highest number of outgoing edges in the "
            "provided knowledge graph?"
        ),
        kg_name="synthetic-cypher-shapes",
        cypher=_one_line(
            """
MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg})
MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})-[:SUBJECT]->(e)
WITH e, count(a) AS deg
ORDER BY deg DESC
LIMIT 1
RETURN coalesce(e.display_name, e.name) AS name
"""
        ),
        ontology_context="Type: Entity\n  - related_to → Entity",
    ),
    _seed(
        shape=SHAPE_GRAPH_PATH,
        question="What is the shortest path between Widget A and Widget B?",
        kg_name="synthetic-cypher-shapes",
        cypher=_one_line(
            """
MATCH (s:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE toLower(coalesce(s.display_name, s.name, '')) = toLower($start_name)
MATCH (t:Entity {tenant_id: $tenant_id, kg: $kg})
WHERE toLower(coalesce(t.display_name, t.name, '')) = toLower($end_name)
MATCH p = shortestPath((s)-[:SUBJECT|OBJECT*..12]-(t))
RETURN [n IN nodes(p) WHERE n:Entity | coalesce(n.display_name, n.name)] AS path
"""
        ),
        ontology_context="Type: Widget\n  - related_to → Widget",
    ),
]
