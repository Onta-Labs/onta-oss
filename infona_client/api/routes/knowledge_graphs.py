"""Knowledge graph management — list, create, delete named graphs within a tenant.

Implementation lives in sibling ``knowledge_graphs_*.py`` modules. Every
previously importable name is re-exported here. Route handlers are registered
on ``router`` so FastAPI paths stay identical.

All KGs share the tenant's ontology but have separate instance data.
"""

from __future__ import annotations

from fastapi import APIRouter

from infona_client.graph.client import NeptuneClient  # noqa: F401 — residual allowlist
from infona_client.graph.iri import (  # noqa: F401 — previous module-level surface
    ENTITY_URI_PREFIX,
    IRI_BASE,
    ONTO_BASE,
    TYPE_URI_PREFIX,
)
from infona_client.graph.ontology_queries import (  # noqa: F401 — previous module-level surface
    get_type_attributes_query,
    type_uri,
)
from infona_client.graph.queries import (  # noqa: F401 — previous module-level surface
    _escape_literal,
    is_valid_kg_name,
    kg_graph_uri,
    kg_meta_uri,
    tenant_graph_uri,
)

from infona_client.api.routes.knowledge_graphs_common import (  # noqa: F401
    INFONA_ONTO,
    KG_TRIPLE_COUNT,
    KGCreate,
    KGInfo,
    NAME_ATTRS,
    RDF_TYPE,
    _kg_meta_uri,
    _live_triple_count,
    _neo4j_live_kg_counts,
    _skip_invalid_kg_name,
    _store_triple_count,
    invalidate_triple_count,
)
from infona_client.api.routes.knowledge_graphs_create import create_kg as _create_kg
from infona_client.api.routes.knowledge_graphs_delete import delete_kg as _delete_kg
from infona_client.api.routes.knowledge_graphs_list import (  # noqa: F401
    _enriching_kgs,
    _kg_stats_for,
    list_kgs as _list_kgs,
)
from infona_client.api.routes.knowledge_graphs_reindex import (
    ReindexAccepted,
    reindex_kg_semantic as _reindex_kg_semantic,
)
from infona_client.api.routes.knowledge_graphs_types import (  # noqa: F401
    AttributeUsage,
    EntitySample,
    RelationshipUsage,
    SYSTEM_PREDICATES,
    TypeCount,
    TypeUsage,
    _read_type_index_flags,
    _xsd_to_datatype,
    get_type_usage as _get_type_usage,
    list_type_counts as _list_type_counts,
)

router = APIRouter(prefix="/graphs/{tenant}/kgs")

# Re-bind route handlers on this module (same paths as before the extract).
list_kgs = router.get("", response_model=list[KGInfo])(_list_kgs)
create_kg = router.post("", response_model=KGInfo, status_code=201)(_create_kg)
delete_kg = router.delete("/{kg_name}")(_delete_kg)
reindex_kg_semantic = router.post(
    "/{kg_name}/search/reindex", response_model=ReindexAccepted, status_code=202
)(_reindex_kg_semantic)
list_type_counts = router.get(
    "/{kg_name}/type-counts", response_model=list[TypeCount]
)(_list_type_counts)
get_type_usage = router.get(
    "/{kg_name}/types/{type_name}/usage", response_model=TypeUsage
)(_get_type_usage)
