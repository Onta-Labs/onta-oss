"""NL→Cypher generators (templates + LLM helpers).

Implementation lives in sibling ``cypher_*.py`` modules. Every previously
importable name is re-exported here.

Invariants other agents must not break:
- Production /ask is always LLM Cypher (never fixture short-circuit).
- Money-leaf hard-bind is unique-resolve only.
- Never drop THIS-KG populated types from planning context.
"""

from __future__ import annotations

from infona_client.graph.rdfs_helpers import (  # noqa: F401
    LITERAL_COMPARE_CYPHER,
    RELATED_ENTITY_NAME_FILTER_CYPHER,
    TEMPLATE_LITERAL_COMPARE,
    TEMPLATE_RELATED_ENTITY_NAME_FILTER,
)
from infona_client.nlp.cypher_patterns import (  # noqa: F401
    COUNT_BY_TYPE_CYPHER,
    COUNT_TOTAL_CYPHER,
    FILTER_PROP_EQ_CYPHER,
    HOP_OUT_CYPHER,
    LIST_BY_TYPE_CYPHER,
    TEMPLATE_COUNT_BY_TYPE,
    TEMPLATE_COUNT_TOTAL,
    TEMPLATE_FILTER_PROP_EQ,
    TEMPLATE_HOP_OUT,
    TEMPLATE_LIST_BY_TYPE,
)
from infona_client.nlp.cypher_rel_resolve import (  # noqa: F401
    _attr_is_relationship,
    _literal_leaves_in_section,
    _ontology_section_for_type,
    _relationship_leaves_in_section,
    _relationship_specs_in_section,
    _resolve_relationship_attr,
    _score_range_type_precision,
)
from infona_client.nlp.cypher_schema import (
    format_schema_types_for_cypher,
    neo4j_ask_enabled,
    ontology_from_graph_store,
    records_to_bindings,
)
from infona_client.nlp.cypher_stub_agg import (  # noqa: F401
    try_aggregate_query,
    try_deterministic_cypher,
)
from infona_client.nlp.cypher_stub_basic import (  # noqa: F401
    try_list_query,
    try_stub_count_query,
)
from infona_client.nlp.cypher_stub_filter import (  # noqa: F401
    try_filter_query,
    try_numeric_filter_query,
    try_related_name_filter_query,
)
from infona_client.nlp.cypher_stub_rel import (  # noqa: F401
    try_hop_query,
    try_made_by_filter_query,
)
from infona_client.nlp.cypher_types import (  # noqa: F401
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    _SAFE_PROP_RE,
    _camel_words,
    _normalize_type_token,
    _singularize_token,
    extract_type_activity_from_ontology,
    extract_type_names_from_ontology,
    guess_type_name,
    match_type_name,
    resolve_type_name,
    resolve_type_name_async,
)

__all__ = [
    "COUNT_BY_TYPE_CYPHER",
    "COUNT_TOTAL_CYPHER",
    "DEFAULT_LIST_LIMIT",
    "FILTER_PROP_EQ_CYPHER",
    "HOP_OUT_CYPHER",
    "LIST_BY_TYPE_CYPHER",
    "LITERAL_COMPARE_CYPHER",
    "MAX_LIST_LIMIT",
    "RELATED_ENTITY_NAME_FILTER_CYPHER",
    "TEMPLATE_LITERAL_COMPARE",
    "TEMPLATE_RELATED_ENTITY_NAME_FILTER",
    "TEMPLATE_COUNT_BY_TYPE",
    "TEMPLATE_COUNT_TOTAL",
    "TEMPLATE_FILTER_PROP_EQ",
    "TEMPLATE_HOP_OUT",
    "TEMPLATE_LIST_BY_TYPE",
    "extract_type_names_from_ontology",
    "format_schema_types_for_cypher",
    "guess_type_name",
    "match_type_name",
    "neo4j_ask_enabled",
    "ontology_from_graph_store",
    "records_to_bindings",
    "resolve_type_name",
    "resolve_type_name_async",
    "try_deterministic_cypher",
    "try_filter_query",
    "try_hop_query",
    "try_list_query",
    "try_made_by_filter_query",
    "try_numeric_filter_query",
    "try_related_name_filter_query",
    "try_stub_count_query",
    "try_aggregate_query",
]
