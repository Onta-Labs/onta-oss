"""Reusable RDF-semantic query helpers (ADR 0013 §9 / golden-query harness).

Compose these from NL planners and app code. Helpers are **scope-bound** and
return answer rows as plain dicts — never SPARQL strings, never free-form
Assertion scans without subject/type constraint as the default tool.

Two surfaces (same semantics, different backends):

1. **AssertionMemoryStore** helpers (hermetic golden suite) — take an explicit
   store + tenant_id/kg.
2. **Cypher templates** (+ optional GraphSession natives) for Neo4j /
   MemoryGraphStore explore paths — registered in
   :mod:`infona_client.graph.schema_bootstrap`.

Golden cases compare **answers**, not query text.

Implementation lives in sibling ``rdfs_helpers_*.py`` modules. Every previously
importable name is re-exported here.
"""

from __future__ import annotations

from infona_client.graph.rdfs_helpers_memory import (  # noqa: F401 — public re-exports
    MappingLike,
    _compare,
    _project_assertion,
    asserted_types,
    assertions_for_subject,
    count_entities_of_type,
    entities_of_type,
    entities_with_literal_filter,
    fact_provenance,
    literal_value,
    object_value,
    parent_classes,
    project_rows,
    reverse_object_assertions,
    subclass_of,
    subclass_of_closure,
)
from infona_client.graph.rdfs_helpers_session import (  # noqa: F401 — public re-exports
    _since_passes,
    assertion_to_history_row,
    session_assertion_history,
    session_assertions_for_subject,
    session_entities_of_type,
    session_literal_values,
    session_object_values,
    subclass_closure,
    subproperty_closure,
)
from infona_client.graph.rdfs_helpers_templates import (  # noqa: F401 — public re-exports
    ASSERTIONS_FOR_SUBJECT_CYPHER,
    CLASS_SUBCLASS_DESCENDANTS_CYPHER,
    ENTITIES_OF_TYPE_COUNT_CYPHER,
    ENTITIES_OF_TYPE_CYPHER,
    LITERAL_AGGREGATE_CYPHER,
    LITERAL_COMPARE_CYPHER,
    LITERAL_VALUES_COUNT_CYPHER,
    LITERAL_VALUES_CYPHER,
    RELATED_ENTITIES_CYPHER,
    RELATED_ENTITY_NAME_FILTER_CYPHER,
    RELATED_ENTITY_NAME_FILTER_INVERSE_CYPHER,
    SUBCLASS_OF_CLOSURE_CYPHER,
    SUBPROPERTY_DESCENDANTS_CYPHER,
    TEMPLATE_ASSERTIONS_FOR_SUBJECT,
    TEMPLATE_ENTITIES_OF_TYPE,
    TEMPLATE_ENTITIES_OF_TYPE_COUNT,
    TEMPLATE_LITERAL_AGGREGATE,
    TEMPLATE_LITERAL_COMPARE,
    TEMPLATE_LITERAL_VALUES,
    TEMPLATE_LITERAL_VALUES_COUNT,
    TEMPLATE_RELATED_ENTITIES,
    TEMPLATE_RELATED_ENTITY_NAME_FILTER_INVERSE,
    TEMPLATE_RELATED_ENTITY_NAME_FILTER,
    TEMPLATE_SUBCLASS_OF_CLOSURE,
    _PARENT_FIELD_RE,
    _TYPE_LINE_RE,
    descendants_of,
    extract_subclass_map_from_ontology,
    semantic_templates,
    type_names_with_subclasses,
)

__all__ = [
    "ASSERTIONS_FOR_SUBJECT_CYPHER",
    "CLASS_SUBCLASS_DESCENDANTS_CYPHER",
    "ENTITIES_OF_TYPE_COUNT_CYPHER",
    "ENTITIES_OF_TYPE_CYPHER",
    "LITERAL_VALUES_COUNT_CYPHER",
    "LITERAL_VALUES_CYPHER",
    "RELATED_ENTITIES_CYPHER",
    "RELATED_ENTITY_NAME_FILTER_CYPHER",
    "RELATED_ENTITY_NAME_FILTER_INVERSE_CYPHER",
    "SUBCLASS_OF_CLOSURE_CYPHER",
    "SUBPROPERTY_DESCENDANTS_CYPHER",
    "TEMPLATE_ASSERTIONS_FOR_SUBJECT",
    "TEMPLATE_ENTITIES_OF_TYPE",
    "TEMPLATE_ENTITIES_OF_TYPE_COUNT",
    "TEMPLATE_LITERAL_COMPARE",
    "TEMPLATE_LITERAL_AGGREGATE",
    "LITERAL_AGGREGATE_CYPHER",
    "TEMPLATE_LITERAL_VALUES",
    "TEMPLATE_LITERAL_VALUES_COUNT",
    "TEMPLATE_RELATED_ENTITIES",
    "TEMPLATE_RELATED_ENTITY_NAME_FILTER_INVERSE",
    "TEMPLATE_RELATED_ENTITY_NAME_FILTER",
    "TEMPLATE_SUBCLASS_OF_CLOSURE",
    "LITERAL_COMPARE_CYPHER",
    "assertion_to_history_row",
    "assertions_for_subject",
    "asserted_types",
    "count_entities_of_type",
    "descendants_of",
    "entities_of_type",
    "entities_with_literal_filter",
    "extract_subclass_map_from_ontology",
    "fact_provenance",
    "literal_value",
    "object_value",
    "parent_classes",
    "project_rows",
    "reverse_object_assertions",
    "semantic_templates",
    "session_assertion_history",
    "session_assertions_for_subject",
    "session_entities_of_type",
    "session_literal_values",
    "session_object_values",
    "subclass_closure",
    "subclass_of",
    "subclass_of_closure",
    "subproperty_closure",
    "type_names_with_subclasses",
]
