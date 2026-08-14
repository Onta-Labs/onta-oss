"""Explorer API — read-only endpoints that power the Infona Explorer web app.

Implementation lives in sibling ``explore_*.py`` modules. Every previously
importable name is re-exported here. Route handlers are registered on
``router`` so FastAPI paths stay identical.

Invariants other agents must not break:
- ``er_rebuild`` goes through ``rewrite_subject`` + ``refresh_after_write``.
- ``schedule_recompute`` evicts ``_summary_cache`` first (always).
- Relationship instance edges stay on ``onto/<leaf>``; this module is a reader.
"""

from __future__ import annotations

from fastapi import APIRouter

from infona_client.api.routes.explore_common import (  # noqa: F401
    RDF_NS,
    RDF_PROPERTY,
    RDF_TYPE,
    RDFS,
    RDFS_NS,
    _CORE_SLOT_PRED,
    _DRIFT_COVERAGE,
    _DRIFT_FLOOR_COUNT,
    _DRIFT_FLOOR_COV,
    _DRIFT_IS_CORE,
    _DRIFT_KEPT,
    _DRIFT_KEY,
    _DRIFT_KG,
    _DRIFT_NS,
    _DRIFT_POINT_KEPT,
    _DRIFT_POINT_OF,
    _DRIFT_QUARANTINED,
    _DRIFT_RECORDED_AT,
    _DRIFT_SOURCE_COUNT,
    _DRIFT_SUPPORT,
    _IndexFlagAccumulator,
    _LAYER_TYPE_NAMESPACES,
    _PRIMARY_TYPE_GUARD,
    _SCHEMA_COVERAGE_NOTE,
    _ST_FLAG_AGGREGATES,
    _STAT_CNT,
    _STAT_ENTITY_COUNT,
    _STAT_FOR_PRED,
    _STAT_FOR_TYPE,
    _STAT_REL,
    _STAT_SPATIAL,
    _STAT_TARGET,
    _STAT_TEMPORAL,
    _STATS_NS,
    _SUMMARY_BACKFILL_CONCURRENCY,
    _SUMMARY_TTL_SECONDS,
    _XSD,
    _XSD_DATE_URI,
    _XSD_DATETIME_URI,
    _assemble_summary,
    _coverage,
    _dedupe_undirected,
    _drift_history_graph_uri,
    _esc,
    _from_graphs,
    _is_core_slot,
    _is_sparql_client_type,
    _retired_sparql_client,
    _sorted_slots,
    _stat_node,
    _stats_graph_uri,
    _target_from_entity_uri,
    _to_float,
    _to_int,
    _type_leaf,
    _typed,
    _xsd_to_datatype,
    logger,
)
from infona_client.api.routes.explore_edges import (  # noqa: F401
    _live_edge_scan,
    _live_edge_scan_drift,
    _read_edges_from_stats,
    _read_edges_from_stats_drift,
)
from infona_client.api.routes.explore_entity import (
    er_rebuild as _er_rebuild,
    get_entity_detail_route as _get_entity_detail_route,
    search_explorer as _search_explorer,
)
from infona_client.api.routes.explore_recompute import (  # noqa: F401
    _build_drift_report,
    _persist_drift_history,
    recompute_kg_stats,
)
from infona_client.api.routes.explore_schedule import (  # noqa: F401
    _run_summary_backfill,
    _safe_recompute,
    drop_kg_stats,
    schedule_recompute,
    schedule_summary_backfill,
)
from infona_client.api.routes.explore_records import (
    _records_from_explore_store as _records_from_explore_store_impl,
    get_type_records as _get_type_records,
)
from infona_client.api.routes.explore_resolve import (  # noqa: F401
    _resolve_layered_type,
    invalidate_summary_cache,
)
from infona_client.api.routes.explore_scan import (  # noqa: F401
    _live_scan,
    _live_scan_all,
    _read_all_type_stats,
    _read_declared_schema,
    _read_type_stats,
)
from infona_client.api.routes.explore_schema import (
    _schema_from_graph_store as _schema_from_graph_store_impl,
    _type_edges_from_graph_store as _type_edges_from_graph_store_impl,
    get_drift_history as _get_drift_history,
    get_kg_schema as _get_kg_schema,
    get_type_edges as _get_type_edges,
    recompute_stats as _recompute_stats,
)
from infona_client.api.routes.explore_state import (  # noqa: F401
    _bg_tasks,
    _recompute_inflight,
    _recompute_pending,
    _summary_cache,
)
from infona_client.api.routes.explore_summary import (
    backfill_kg_summary,
    get_type_summary as _get_type_summary,
    read_kg_summary_from_stats as _read_kg_summary_from_stats,
)
from infona_client.graph.client import NeptuneClient  # noqa: F401 — residual allowlist
from infona_client.graph.iri import ENTITY_URI_PREFIX, IRI_BASE, ONTO_PRED_PREFIX, TYPE_URI_PREFIX  # noqa: F401
from infona_client.graph.queries import (  # noqa: F401 — public re-exports
    is_valid_type_name,
    skip_invalid_type_name,
)
from infona_client.graph.predicates import (  # noqa: F401
    ATTR_META_SUFFIXES,
    ER_NS,
    INTERNAL_ONTO_MARKERS as _INTERNAL_ONTO_MARKERS,
    ONTO_NORM_PREFIX,
    ONTO_PRED_PREFIX,
    SYSTEM_PREDICATES,
    companion_leaves as _companion_leaves,
    is_internal_predicate as _is_internal_predicate,
)

router = APIRouter(prefix="/graphs/{tenant}/explore")

# Re-bind route handlers on this module (same paths as before the extract).
get_type_summary = router.get("/kgs/{kg_name}/types/{type_name}/summary")(_get_type_summary)
get_kg_schema = router.get("/kgs/{kg_name}/schema")(_get_kg_schema)
get_type_edges = router.get("/kgs/{kg_name}/type-edges")(_get_type_edges)
recompute_stats = router.post("/kgs/{kg_name}/recompute-stats")(_recompute_stats)
get_drift_history = router.get("/kgs/{kg_name}/drift-history")(_get_drift_history)
get_type_records = router.get("/kgs/{kg_name}/types/{type_name}/records")(_get_type_records)
get_entity_detail_route = router.get("/kgs/{kg_name}/entities/{entity_id:path}")(
    _get_entity_detail_route
)
er_rebuild = router.post("/kgs/{kg_name}/er-rebuild")(_er_rebuild)
search_explorer = router.get("/search")(_search_explorer)

# Helpers re-exported under original names for tests / late imports.
_records_from_explore_store = _records_from_explore_store_impl
_schema_from_graph_store = _schema_from_graph_store_impl
_type_edges_from_graph_store = _type_edges_from_graph_store_impl
read_kg_summary_from_stats = _read_kg_summary_from_stats
