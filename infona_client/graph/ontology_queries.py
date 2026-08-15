"""SPARQL query builders for ontology management.

Implementation lives in sibling ``ontology_queries_*.py`` modules. Every
previously importable name is re-exported here.

``entity_uri`` / ``_safe_id`` stay **defined** in this module — they are the
single instance-node mint (``tests/test_entity_uri_convergence.py``). Do not
move them to a sibling.
"""

from infona_client.graph.iri import (  # noqa: F401 — public re-exports
    ENTITY_URI_PREFIX,
    IRI_BASE,
    ONTO_BASE,
    TYPE_URI_PREFIX,
)
import hashlib  # noqa: F401 — public re-export (pre-extract import)
import re

from infona_client.graph.queries import (  # noqa: F401 — public re-exports
    require_valid_type_name,
    sparql_string_literal,
)
from infona_client.graph.ontology_queries_uris import (  # noqa: F401
    GEOSPARQL,
    INFONA_ONTO,
    PRIMITIVE_TYPES,
    RDF,
    RDFS,
    TEXT_KIND_FREE_TEXT,
    TEXT_KIND_NOT_TEXT,
    XSD,
    XSD_STRING,
    _DATATYPE_TO_XSD,
    _TYPES_URI,
    _datatype_to_xsd,
    _esc,
    attr_uri,
    type_uri,
    xsd_to_datatype,
)
from infona_client.graph.ontology_queries_version import ontology_version  # noqa: F401
from infona_client.graph.ontology_queries_mutate import (  # noqa: F401
    delete_attribute_declaration,
    insert_attribute,
    insert_subtype,
    insert_type,
    mark_core_slot,
    retract_object_property,
    set_object_property_range,
    upsert_attribute,
    upsert_attribute_text_kind,
    text_kind_map_query,
    upsert_type,
    upsert_type_comment,
)
from infona_client.graph.ontology_queries_select import (  # noqa: F401
    batch_entity_exists_query,
    entities_by_key_value_query,
    entity_exists_query,
    full_ontology_detail_query,
    get_attribute_range_query,
    get_full_ontology_query,
    get_subtypes_query,
    get_type_attributes_query,
    get_type_detail_query,
    get_type_functions_query,
    list_types_query,
    parent_map_query,
    with_subclass_closure,
)
from infona_client.graph.ontology_queries_rewrite import (  # noqa: F401
    SAME_AS,
    _CLOSURE_PATH,
    _ENTITIES_URI,
    _SAMEAS_PATH,
    _entity_ref_in_unsafe_slot,
    _rewrite_indirect_type_constraints,
    add_layer_from_clauses,
    rewrite_entity_ref_to_sameas_closure,
    rewrite_type_predicate_to_closure,
)


# --- Entity-node URI minting (the ONE place instance-node IRIs are built) -----
# Discovery (resolver/schema_resolver), CSV/JSON ingestion (resolver/csv_resolver),
# enrichment (enrichment/executor), and normalization (normalization/execute) all
# mint the SAME ``…/entities/<Type>/<slug>`` IRI for the same real-world thing, so
# a given (type, raw_id) always resolves to ONE shared node across every write rail
# — never a duplicate, never a dangling string in a node-valued slot. This lives
# here (not in a resolver module) because ontology_queries imports nothing from the
# resolver, so every rail can depend on it with no import cycle. A drift guard
# (tests/test_entity_uri_convergence.py) fails if any other module re-defines the
# id sanitizer or hand-builds this IRI inline.


def _safe_id(raw_id: str) -> str:
    """Sanitize a raw entity id into the URI-safe slug used for the
    ``…/entities/<Type>/<slug>`` tail: each non-``[A-Za-z0-9_-]`` char becomes
    ``_`` (per-char, not run-collapsed — ``"a  b"`` → ``"a__b"``), the result is
    capped at 200 chars, and an empty result becomes ``"unknown"``. Deterministic,
    so the same raw value always maps to the same node (free dedup)."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_id.strip())
    return safe[:200] if safe else "unknown"


def entity_uri(type_name: str, raw_id: str) -> str:
    """Canonical instance-node IRI for ``raw_id`` of ``type_name``:
    ``https://graph.infona.ai/entities/<type_name>/<_safe_id(raw_id)>``. The single
    source of truth every write rail mints entity nodes through — keep it byte-for-
    byte stable, since the slug is the node's identity (changing it orphans data)."""
    return f"{IRI_BASE}/entities/{type_name}/{_safe_id(raw_id)}"
