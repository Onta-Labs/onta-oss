"""Explorer API — read-only endpoints that power the Cograph Explorer web app.

All data comes from existing Neptune graphs; no new infra required. These
endpoints add convenience (bundling, coverage %, search) on top of the raw
ontology + KG queries already used by the CLI.

Type summaries are served from a precomputed per-KG **stats graph** (one
integer triple set per type, written at ingest / via recompute) so a read is
a couple of tiny lookups instead of a full instance scan. If stats are missing
for a type (e.g. a KG ingested before this existed), the endpoint falls back
to a live scan so it always returns correct data.
"""

from cograph_client.graph.iri import ENTITY_URI_PREFIX, IRI_BASE, ONTO_PRED_PREFIX, TYPE_URI_PREFIX
import asyncio
import hashlib
import time
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, Query

from cograph_client.api.deps import get_neptune_client
from cograph_client.auth.access import require_tenant_write
from cograph_client.auth.api_keys import TenantContext, get_tenant
from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.entitlement import layer_stack_for
from cograph_client.graph.kg_writer import refresh_after_write
from cograph_client.graph.layers import (
    Layer,
    fetch_types_by_layer,
    layer_type_uri,
    type_namespace,
)
# TYPE_URI_PREFIX was a local copy of the same literal; imported now so the
# prefix this module strips is by construction the one type_uri() mints.
from cograph_client.graph.ontology_queries import TYPE_URI_PREFIX, attr_uri, type_uri
from cograph_client.graph.parser import parse_sparql_results
from cograph_client.graph.queries import (
    is_valid_type_name,
    kg_graph_uri,
    require_valid_type_name,
    skip_invalid_type_name,
    sparql_string_literal,
    tenant_graph_uri,
)
from cograph_client.resolver import drift_control
from cograph_client.spatiotemporal.extract import (
    GEO_WKT,
    INTERVAL_END_LOCALS,
    INTERVAL_START_LOCALS,
    VALIDITY_BOUND_LOCALS,
)

logger = structlog.stdlib.get_logger("cograph.explore")

router = APIRouter(prefix="/graphs/{tenant}/explore")

# Ontology core-slot marker (ADR 0003 §3 / Pass D) — written by
# ontology_queries.mark_core_slot as `<attr_uri> <onto/coreSlot> "true"`. A core
# slot is EXEMPT from the ADR 0004 drift floor (always declared), so the edge
# filter must know whether the upgraded predicate carries this marker.
_CORE_SLOT_PRED = f"{IRI_BASE}/onto/coreSlot"


def _from_graphs(graph_uris: list[str]) -> str:
    """``FROM <g1> FROM <g2> …`` so a SPARQL default-graph union covers layers."""
    return " ".join(f"FROM <{g}>" for g in graph_uris)


def _is_core_slot(type_leaf: str, pred_leaf: str, core_slots: set[str]) -> bool:
    """Is the attribute URI for ``(type_leaf, pred_leaf)`` a declared core slot?

    A MEMBERSHIP TEST, so it answers False rather than raising (ONTA-425). Both
    leaves are DERIVED by string-slicing a stored URI, and a URI outside the
    expected shape slices to ``""`` — e.g. a bare ``…/onto/`` predicate, or a
    type URI outside the tenant namespace. Since ``core_slots`` only ever holds
    well-formed attribute URIs, a leaf that cannot mint one is by definition not
    in the set, and False is the correct answer, not an error. These call sites
    are drift-report enumerations over every stored edge: raising here would fail
    the whole report over one malformed row.
    """
    if not (is_valid_type_name(type_leaf) and is_valid_type_name(pred_leaf)):
        return False
    return attr_uri(type_leaf, pred_leaf) in core_slots


async def _resolve_layered_type(
    client: NeptuneClient, tenant: TenantContext, type_name: str
) -> tuple[str, str, Layer] | None:
    """Resolve ``type_name`` across the workspace LayerStack (ONTA-397).

    Returns ``(type_uri, owning_graph_uri, layer)`` for the winning definition
    under first-visible-layer-wins shadowing, or ``None`` if no visible layer
    declares the name. Used by Explorer ontology-touching reads so empty-tenant
    + populated Public still surfaces Public types.
    """
    stack = layer_stack_for(tenant)
    types_by_layer = await fetch_types_by_layer(client, stack)
    resolved = stack.resolve_type(type_name, types_by_layer)
    if resolved is None:
        return None
    layer, _ = resolved
    return (
        layer_type_uri(layer, type_name),
        stack.graph_uri_for(layer),
        layer,
    )

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDF_PROPERTY = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema"
RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"
# Predicate-hygiene: the ONE definition of "is this an internal/housekeeping
# predicate?" lives in cograph_client.graph.predicates and is shared with the NL
# `ask` render path (ER-internals-leak fix) so both surfaces apply the SAME rule.
# Re-exported here (including the private aliases some tests import) for
# back-compat with existing importers of `explore._is_internal_predicate`.
from cograph_client.graph.predicates import (  # noqa: E402
    ATTR_META_SUFFIXES,
    ER_NS,
    INTERNAL_ONTO_MARKERS as _INTERNAL_ONTO_MARKERS,
    ONTO_NORM_PREFIX,
    ONTO_PRED_PREFIX,
    SYSTEM_PREDICATES,
    companion_leaves as _companion_leaves,
    is_internal_predicate as _is_internal_predicate,
)

# In-memory hot cache on top of the persistent stats graph. Read-heavy data
# warmed on first read, busted whenever the underlying counts change.
#
# The TTL is a staleness *backstop*, NOT the invalidation mechanism: every
# in-process mutation that changes a typef's summary — ingest, ER rebuild, AND
# enrichment/dedupe apply — routes through `recompute_kg_stats` (via
# `schedule_recompute`), which explicitly evicts this cache for the affected KG
# (see below). So a short TTL bought nothing but extra Neptune round trips —
# every ~5 min an active Explorer session re-queried the stats graph for data
# that had not changed and would be evicted the moment it did. A longer backstop
# keeps tenant/KG switches served from memory across a working session while
# still self-healing if an external writer ever mutates the underlying graph
# without going through `recompute_kg_stats`.
_SUMMARY_TTL_SECONDS = 1800.0
_summary_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}

# --- Precomputed stats graph --------------------------------------------------
# Per (type, predicate): coverage count + entity-valued-object total, plus a
# per-type entity count. All integer literals → no string escaping needed.
_STATS_NS = f"{IRI_BASE}/stats/"
_STAT_FOR_TYPE = _STATS_NS + "forType"
_STAT_FOR_PRED = _STATS_NS + "forPred"
_STAT_CNT = _STATS_NS + "cnt"
_STAT_REL = _STATS_NS + "rel"
_STAT_TARGET = _STATS_NS + "targetType"
_STAT_ENTITY_COUNT = _STATS_NS + "entityCount"
# Per-type spatio-temporal index markers (COG-103 follow-up). A type is
# spatially indexed when instances carry geo:wktLiteral geometry (the one
# signal that puts an entity in the spatio-temporal index), temporally indexed
# when instances carry validity per the extract.py recognition rules — an
# explicit validity bound, or a complete start+end pair of date/dateTime
# values. Materialized as boolean triples on the type URI, only when true.
_STAT_SPATIAL = _STATS_NS + "spatiallyIndexed"
_STAT_TEMPORAL = _STATS_NS + "temporallyIndexed"
_XSD_DATE_URI = "http://www.w3.org/2001/XMLSchema#date"
_XSD_DATETIME_URI = "http://www.w3.org/2001/XMLSchema#dateTime"

# --- Drift history graph (COG-57) ---------------------------------------------
# The observe-only mode (ADR 0004 §7) computes the per-relationship coverage
# distribution on every recompute but only *logs* it (CloudWatch, 30-day
# retention) — so "collect enough data to set the floor from real data" was just
# log-scraping that ages out. COG-57 persists each recompute's distribution to a
# per-KG **drift-history named graph** instead: a durable, SPARQL-queryable store
# the floor can later be calibrated from (and the histogram built over).
#
# The graph is APPEND-only (one snapshot per recompute), never DROP+rewritten
# like the stats graph — the whole value is the distribution accumulating over
# time. Each snapshot node carries the runf's effective floors + kept/quarantined
# totals; each relationship in the distribution is a point node linked back to
# its snapshot. Integers/decimals/booleans are typed literals so a downstream
# query can aggregate them numerically without parsing.
_DRIFT_NS = f"{IRI_BASE}/drift/"
_DRIFT_RECORDED_AT = _DRIFT_NS + "recordedAt"      # xsd:dateTime
_DRIFT_KG = _DRIFT_NS + "kg"                        # kg name (provenance)
_DRIFT_FLOOR_COV = _DRIFT_NS + "floorCov"          # xsd:decimal
_DRIFT_FLOOR_COUNT = _DRIFT_NS + "floorCount"      # xsd:integer
_DRIFT_KEPT = _DRIFT_NS + "kept"                   # xsd:integer (count)
_DRIFT_QUARANTINED = _DRIFT_NS + "quarantined"     # xsd:integer (count)
_DRIFT_POINT_OF = _DRIFT_NS + "pointOf"            # point -> snapshot node
_DRIFT_KEY = _DRIFT_NS + "key"                     # "<TypeLeaf>.<predLeaf>"
_DRIFT_COVERAGE = _DRIFT_NS + "coverage"           # xsd:decimal (percent)
_DRIFT_SUPPORT = _DRIFT_NS + "support"             # xsd:integer
_DRIFT_SOURCE_COUNT = _DRIFT_NS + "sourceCount"    # xsd:integer
_DRIFT_IS_CORE = _DRIFT_NS + "isCoreSlot"          # xsd:boolean
_DRIFT_POINT_KEPT = _DRIFT_NS + "pointKept"        # xsd:boolean (per-relationship)
_XSD = "http://www.w3.org/2001/XMLSchema#"

# --- Primary-type attribution (COG-35, follow-up to ADR 0001 multi-typing) ----
# With multi-typing an instance can carry more than one asserted rdf:type (its
# `also_types` co-classifications, e.g. an entity asserted as both Employee and
# Guest). Grouping the stats scan by raw rdf:type would count such an instance
# once PER asserted type — double-counting it across the Explorer's per-type
# panels. ADR rule 5 says each instance is counted exactly once, under its
# "primary type" (the most-specific asserted type).
#
# This guard reproduces, in pure SPARQL over the KG graph alone, the choice made
# by resolver.er.types.primary_type for the data this system actually writes:
#
#   * Asserted co-types (`also_types`) are GENUINE INDEPENDENT classifications
#     (ADR rule 1) — siblings, never an asserted subtype + its ancestor (ancestors
#     are recovered via query-time subclass closure, never asserted). For equal-
#     depth siblings, primary_type tie-breaks to the LEXICOGRAPHICALLY SMALLEST
#     type name. Type URIs share the `…/types/` prefix, so URI string order equals
#     type-name order — the guard below picks the smallest-URI asserted type.
#
# An instance therefore contributes to ?type only when ?type is its smallest
# asserted type URI; the NOT EXISTS rejects every heavier co-type. For a single-
# typed instance the inner pattern can never bind a different `types/` type, so
# the NOT EXISTS is vacuously satisfied and behavior is byte-identical to before
# — which is the common case and must not regress.
#
# Caveat (documented, out of scope): this matches primary_type for INDEPENDENT
# co-types, the only multi-typing the resolver emits. It does NOT consult the
# subClassOf hierarchy (which lives in the ontology graph, not this KG scan), so
# if an asserted subtype + ancestor pair ever appeared it would attribute to the
# smaller URI rather than the deeper type. The resolver does not produce that
# shape today.
_PRIMARY_TYPE_GUARD = (
    f"  FILTER NOT EXISTS {{\n"
    f"    ?e <{RDF_TYPE}> ?type2 .\n"
    f'    FILTER(STRSTARTS(STR(?type2), "{TYPE_URI_PREFIX}") '
    f"&& STR(?type2) < STR(?type))\n"
    f"  }}\n"
)


def _target_from_entity_uri(obj: str) -> str | None:
    """Entity URIs are .../entities/{TargetType}/{id} → the target type leaf."""
    if not obj.startswith(ENTITY_URI_PREFIX):
        return None
    head = obj[len(ENTITY_URI_PREFIX):].split("/", 1)[0]
    return head or None


def _stats_graph_uri(tenant_id: str, kg_name: str) -> str:
    return kg_graph_uri(tenant_id, kg_name) + "/stats"


def _drift_history_graph_uri(tenant_id: str, kg_name: str) -> str:
    """Per-KG append-only graph holding the observe-only drift distribution (COG-57)."""
    return kg_graph_uri(tenant_id, kg_name) + "/drift-history"


def _stat_node(type_uri_str: str, pred_uri: str) -> str:
    h = hashlib.md5(f"{type_uri_str}|{pred_uri}".encode()).hexdigest()
    return f"{_STATS_NS}n/{h}"


def _coverage(count: int, entity_count: int) -> float:
    if not entity_count:
        return 0.0
    return round(count / entity_count * 100, 1)


# SPARQL aggregates shared by the recompute scan and the live fallback: per
# predicate, how many objects are geometry-typed (geo:wktLiteral) and how many
# are date/dateTime-typed. isLiteral() guards DATATYPE() so an IRI object
# contributes 0 instead of erroring the whole aggregate row.
_ST_FLAG_AGGREGATES = (
    f"  (SUM(IF(isLiteral(?o) && DATATYPE(?o) = <{GEO_WKT}>, 1, 0)) AS ?geo)\n"
    f"  (SUM(IF(isLiteral(?o) && (DATATYPE(?o) = <{_XSD_DATE_URI}> || "
    f"DATATYPE(?o) = <{_XSD_DATETIME_URI}>), 1, 0)) AS ?tmp)\n"
)


class _IndexFlagAccumulator:
    """Fold per-predicate (geo, temporal) counts into per-type index flags.

    Mirrors the spatio-temporal extraction rules (spatiotemporal.extract) at
    type granularity: spatial = any geometry-bearing predicate; temporal = an
    explicit validity bound, or BOTH ends of a start+end interval pair.
    """

    __slots__ = ("spatial", "_has_bound", "_has_start", "_has_end")

    def __init__(self) -> None:
        self.spatial = False
        self._has_bound = False
        self._has_start = False
        self._has_end = False

    def add(self, p_uri: str, geo: int, tmp: int) -> None:
        if geo > 0:
            self.spatial = True
        if tmp > 0:
            local = p_uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1].lower()
            if local in VALIDITY_BOUND_LOCALS:
                self._has_bound = True
            elif local in INTERVAL_START_LOCALS:
                self._has_start = True
            elif local in INTERVAL_END_LOCALS:
                self._has_end = True

    @property
    def temporal(self) -> bool:
        return self._has_bound or (self._has_start and self._has_end)

    def flags(self) -> dict:
        return {"spatially_indexed": self.spatial, "temporally_indexed": self.temporal}


def _xsd_to_datatype(uri: str) -> str:
    if not uri:
        return "string"
    if uri.startswith(TYPE_URI_PREFIX):
        return uri[len(TYPE_URI_PREFIX):]
    last = uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]
    return {"string": "string", "integer": "integer", "float": "float",
            "boolean": "boolean", "dateTime": "datetime", "Resource": "uri"}.get(last, "string")


def _assemble_summary(
    type_name: str,
    onto_row: dict,
    parent_type: str | None,
    entity_count: int,
    pred_records: list[dict],
    attr_defs: dict[str, dict[str, str]],
    index_flags: dict | None = None,
) -> dict:
    """Build the panel payload from per-predicate records (cnt + rel total).

    A predicate is a relationship if any of its objects are entities
    (``rel > 0``) or the ontology declares its range as a type. Target type
    and datatype come from the ontology definitions.
    """
    attributes = []
    relationships = []
    # LEGACY companion classification (ONTA-262): graphs written before the
    # attr_meta namespace carry per-attribute provenance companions on the
    # ATTRIBUTE namespace (`attrs/<attr>_<suffix>`), indistinguishable from
    # domain attributes by URI shape alone (is_internal_predicate can't catch
    # them). Classify them set-wise from this type's full leaf set — a leaf is a
    # companion iff it is `<base>_<suffix>` AND `<base>` is itself present — and
    # keep them off the panel. Applied to literal-valued records only (below) so
    # a real relationship can never be misclassified.
    def _display_leaf(rec: dict) -> str:
        p = rec.get("p", "")
        return attr_defs.get(p, {}).get("name") or p.rstrip("/").split("/")[-1]

    legacy_companions = _companion_leaves(_display_leaf(r) for r in pred_records)
    for r in pred_records:
        p_uri = r.get("p", "")
        cnt = r.get("cnt", 0)
        rel = r.get("rel", 0)
        defn = attr_defs.get(p_uri, {})
        name = defn.get("name") or p_uri.rstrip("/").split("/")[-1]
        rng = defn.get("range", "")
        cov = _coverage(cnt, entity_count)
        # A predicate is a relationship when any of its objects are entities
        # (rel > 0) or the ontology declares an entity range. Determine this
        # FIRST so the internal-predicate filter can spare a same-named
        # relationship from the literal-only housekeeping markers (FIX 2).
        is_rel = rel > 0 or rng.startswith(TYPE_URI_PREFIX)
        if _is_internal_predicate(p_uri, is_relationship=is_rel):
            continue
        if not is_rel and name in legacy_companions:
            continue
        if is_rel:
            # Prefer the ontology-declared range; fall back to the target type
            # captured from a sample object's entity URI.
            target = rng[len(TYPE_URI_PREFIX):] if rng.startswith(TYPE_URI_PREFIX) else None
            if not target:
                target = r.get("target") or None
            avg_degree = round(rel / entity_count, 2) if entity_count else 0.0
            relationships.append({
                "name": name,
                "predicate_uri": p_uri,
                "target_type": target,
                "count": cnt,
                "coverage_pct": cov,
                "avg_degree": avg_degree,
            })
        else:
            attributes.append({
                "name": name,
                "predicate_uri": p_uri,
                "datatype": _xsd_to_datatype(rng),
                "count": cnt,
                "coverage_pct": cov,
            })
    flags = index_flags or {}
    return {
        "name": type_name,
        "description": onto_row.get("comment", ""),
        "parent_type": parent_type,
        "entity_count": entity_count,
        "attributes": attributes,
        "relationships": relationships,
        "spatially_indexed": bool(flags.get("spatially_indexed")),
        "temporally_indexed": bool(flags.get("temporally_indexed")),
    }


async def _live_scan(
    client: NeptuneClient, kg_graph: str, t_uri: str
) -> tuple[int, list[dict], dict]:
    """Fallback: one instance scan → (entity_count, per-predicate records, flags).

    Used only when precomputed stats are absent for a type. The rdf:type row
    yields the entity count; ``rel`` is the entity-valued object total; flags
    are the per-type spatio-temporal index markers (same rules as recompute).
    """
    pred_sparql = (
        f"SELECT ?p (COUNT(DISTINCT ?e) AS ?cnt) (SAMPLE(?o) AS ?sample)\n"
        f'  (SUM(IF(STRSTARTS(STR(?o), "{ENTITY_URI_PREFIX}"), 1, 0)) AS ?rel)\n'
        f"{_ST_FLAG_AGGREGATES}"
        f"FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> <{t_uri}> .\n"
        f"  ?e ?p ?o .\n"
        # Primary-type attribution: when ?e is multi-typed, count it only under
        # its smallest asserted type URI so the fallback matches the precomputed
        # stats (see _PRIMARY_TYPE_GUARD). Single-typed: vacuously satisfied.
        f"  FILTER NOT EXISTS {{\n"
        f"    ?e <{RDF_TYPE}> ?type2 .\n"
        f'    FILTER(STRSTARTS(STR(?type2), "{TYPE_URI_PREFIX}") '
        f'&& STR(?type2) < "{t_uri}")\n'
        f"  }}\n"
        f"}} GROUP BY ?p ORDER BY DESC(?cnt)"
    )
    _, rows = parse_sparql_results(await client.query(pred_sparql))
    entity_count = 0
    records: list[dict] = []
    facc = _IndexFlagAccumulator()
    for r in rows:
        p_uri = r.get("p", "")
        try:
            cnt = int(r.get("cnt", "0"))
        except ValueError:
            cnt = 0
        try:
            rel = int(r.get("rel", "0"))
        except ValueError:
            rel = 0
        if p_uri == RDF_TYPE:
            entity_count = cnt
            continue
        # Skip internal/housekeeping predicates so they never become user-facing
        # attributes/relationships (onto/batch_id, er/blockKey, er/erSignal_*, …).
        # A predicate carrying entity-valued objects (rel > 0) is a relationship,
        # exempt from the literal-only housekeeping markers (FIX 2) — so a real
        # relationship named e.g. `onto/source` is kept, not hidden.
        if _is_internal_predicate(p_uri, is_relationship=rel > 0):
            continue
        try:
            geo = int(r.get("geo", "0") or "0")
        except ValueError:
            geo = 0
        try:
            tmp = int(r.get("tmp", "0") or "0")
        except ValueError:
            tmp = 0
        if geo or tmp:
            facc.add(p_uri, geo, tmp)
        records.append({"p": p_uri, "cnt": cnt, "rel": rel,
                        "target": _target_from_entity_uri(r.get("sample", ""))})
    return entity_count, records, facc.flags()


async def _read_type_stats(
    client: NeptuneClient, tenant_id: str, kg_name: str, t_uri: str
) -> tuple[int, list[dict], dict] | None:
    """Read precomputed stats for one type, or None if not materialized."""
    stats = _stats_graph_uri(tenant_id, kg_name)
    ec_q = (
        f"SELECT ?ec ?sp ?tp FROM <{stats}> WHERE {{\n"
        f"  <{t_uri}> <{_STAT_ENTITY_COUNT}> ?ec .\n"
        f"  OPTIONAL {{ <{t_uri}> <{_STAT_SPATIAL}> ?sp }}\n"
        f"  OPTIONAL {{ <{t_uri}> <{_STAT_TEMPORAL}> ?tp }}\n"
        f"}}"
    )
    pred_q = (
        f"SELECT ?pred ?cnt ?rel ?target FROM <{stats}> WHERE {{\n"
        f"  ?s <{_STAT_FOR_TYPE}> <{t_uri}> ; <{_STAT_FOR_PRED}> ?pred ; <{_STAT_CNT}> ?cnt .\n"
        f"  OPTIONAL {{ ?s <{_STAT_REL}> ?rel }}\n"
        f"  OPTIONAL {{ ?s <{_STAT_TARGET}> ?target }}\n"
        f"}}"
    )
    ec_raw, pred_raw = await asyncio.gather(client.query(ec_q), client.query(pred_q))
    _, ec_rows = parse_sparql_results(ec_raw)
    if not ec_rows:
        return None
    try:
        entity_count = int(ec_rows[0].get("ec", "0"))
    except ValueError:
        entity_count = 0
    # "1"^^xsd:boolean is an equally valid true — accept both lexical forms so
    # a future writer/backfill touching the stats graph can't silently read False.
    flags = {
        "spatially_indexed": ec_rows[0].get("sp", "") in ("true", "1"),
        "temporally_indexed": ec_rows[0].get("tp", "") in ("true", "1"),
    }
    _, pred_rows = parse_sparql_results(pred_raw)
    records: list[dict] = []
    for r in pred_rows:
        try:
            cnt = int(r.get("cnt", "0"))
        except ValueError:
            cnt = 0
        try:
            rel = int(r.get("rel", "0"))
        except ValueError:
            rel = 0
        target_uri = r.get("target", "")
        target = target_uri[len(TYPE_URI_PREFIX):] if target_uri.startswith(TYPE_URI_PREFIX) else None
        records.append({"p": r.get("pred", ""), "cnt": cnt, "rel": rel, "target": target})
    return entity_count, records, flags


# --- Whole-KG schema reads (ONTA-418) -----------------------------------------
# The per-type reads above bind ONE type URI by choice; the underlying stats are
# already materialized for every (type, predicate) pair in the KG. Dropping the
# binding turns the same two queries into a whole-KG schema read, so the
# population-aware schema endpoint costs a constant 3-4 queries instead of the
# 1+N round trips a client-side fan-out over the per-type summary would.

# Type-URI namespaces across all ontology layers, longest first so
# `types/public/Person` is not mis-parsed by the tenant namespace (a prefix of it).
_LAYER_TYPE_NAMESPACES = sorted(
    (type_namespace(layer) for layer in Layer), key=len, reverse=True
)


def _type_leaf(uri: str) -> str | None:
    """Bare type name for a type URI in ANY layer namespace, else ``None``.

    Rejects nested URIs (``…/types/Person/attrs/email``), which are attribute
    declarations rather than types. Same guard the per-type reads apply with
    their ``"/" in leaf`` check, extended to the layered namespaces.
    """
    for ns in _LAYER_TYPE_NAMESPACES:
        if uri.startswith(ns):
            leaf = uri[len(ns):]
            return leaf if leaf and "/" not in leaf else None
    return None


async def _read_all_type_stats(
    client: NeptuneClient, tenant_id: str, kg_name: str
) -> dict[str, tuple[int, list[dict], dict]]:
    """Precomputed stats for EVERY type in a KG → ``{type_uri: (count, records, flags)}``.

    :func:`_read_type_stats` with the type binding dropped: two queries over the
    tiny stats graph, grouped by type. Returns ``{}`` when the KG has no
    materialized entity counts (legacy KG), so the caller can fall back to
    :func:`_live_scan_all`.
    """
    stats = _stats_graph_uri(tenant_id, kg_name)
    ec_q = (
        f"SELECT ?type ?ec ?sp ?tp FROM <{stats}> WHERE {{\n"
        f"  ?type <{_STAT_ENTITY_COUNT}> ?ec .\n"
        f"  OPTIONAL {{ ?type <{_STAT_SPATIAL}> ?sp }}\n"
        f"  OPTIONAL {{ ?type <{_STAT_TEMPORAL}> ?tp }}\n"
        f"}}"
    )
    pred_q = (
        f"SELECT ?type ?pred ?cnt ?rel ?target FROM <{stats}> WHERE {{\n"
        f"  ?s <{_STAT_FOR_TYPE}> ?type ; <{_STAT_FOR_PRED}> ?pred ; <{_STAT_CNT}> ?cnt .\n"
        f"  OPTIONAL {{ ?s <{_STAT_REL}> ?rel }}\n"
        f"  OPTIONAL {{ ?s <{_STAT_TARGET}> ?target }}\n"
        f"}}"
    )
    ec_raw, pred_raw = await asyncio.gather(client.query(ec_q), client.query(pred_q))
    _, ec_rows = parse_sparql_results(ec_raw)
    out: dict[str, tuple[int, list[dict], dict]] = {}
    for r in ec_rows:
        t_uri = r.get("type", "")
        if _type_leaf(t_uri) is None:
            continue
        # Accept both boolean lexical forms (see _read_type_stats).
        out[t_uri] = (
            _to_int(r.get("ec")),
            [],
            {
                "spatially_indexed": r.get("sp", "") in ("true", "1"),
                "temporally_indexed": r.get("tp", "") in ("true", "1"),
            },
        )
    if not out:
        return {}
    _, pred_rows = parse_sparql_results(pred_raw)
    for r in pred_rows:
        entry = out.get(r.get("type", ""))
        if entry is None:
            continue
        target_uri = r.get("target", "")
        entry[1].append({
            "p": r.get("pred", ""),
            "cnt": _to_int(r.get("cnt")),
            "rel": _to_int(r.get("rel")),
            "target": (
                target_uri[len(TYPE_URI_PREFIX):]
                if target_uri.startswith(TYPE_URI_PREFIX) else None
            ),
        })
    return out


async def _live_scan_all(
    client: NeptuneClient, kg_graph: str
) -> dict[str, tuple[int, list[dict], dict]]:
    """Fallback for a KG with no materialized stats: ONE whole-KG scan.

    Same ``GROUP BY ?type ?p`` shape :func:`recompute_kg_stats` uses (including
    the primary-type attribution guard), so the fallback and the precomputed
    path agree. One query for the whole KG, never one per type.
    """
    scan = (
        f"SELECT ?type ?p (COUNT(DISTINCT ?e) AS ?cnt) (SAMPLE(?o) AS ?sample)\n"
        f'  (SUM(IF(STRSTARTS(STR(?o), "{ENTITY_URI_PREFIX}"), 1, 0)) AS ?rel)\n'
        f"{_ST_FLAG_AGGREGATES}"
        f"FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> ?type .\n"
        f"  ?e ?p ?o .\n"
        f'  FILTER(STRSTARTS(STR(?type), "{TYPE_URI_PREFIX}"))\n'
        f"{_PRIMARY_TYPE_GUARD}"
        f"}} GROUP BY ?type ?p"
    )
    _, rows = parse_sparql_results(await client.query(scan))
    counts: dict[str, int] = {}
    records: dict[str, list[dict]] = {}
    faccs: dict[str, _IndexFlagAccumulator] = {}
    for r in rows:
        t_uri = r.get("type", "")
        if _type_leaf(t_uri) is None:
            continue
        p_uri = r.get("p", "")
        cnt = _to_int(r.get("cnt"))
        rel = _to_int(r.get("rel"))
        if p_uri == RDF_TYPE:
            counts[t_uri] = cnt
            continue
        # Same hygiene as the per-type live scan: housekeeping predicates never
        # become user-facing attributes/relationships.
        if _is_internal_predicate(p_uri, is_relationship=rel > 0):
            continue
        geo, tmp = _to_int(r.get("geo")), _to_int(r.get("tmp"))
        if geo or tmp:
            faccs.setdefault(t_uri, _IndexFlagAccumulator()).add(p_uri, geo, tmp)
        records.setdefault(t_uri, []).append({
            "p": p_uri,
            "cnt": cnt,
            "rel": rel,
            "target": _target_from_entity_uri(r.get("sample", "")),
        })
    out: dict[str, tuple[int, list[dict], dict]] = {}
    for t_uri in set(counts) | set(records):
        facc = faccs.get(t_uri)
        out[t_uri] = (
            counts.get(t_uri, 0),
            records.get(t_uri, []),
            facc.flags() if facc else {},
        )
    return out


async def _read_declared_schema(
    client: NeptuneClient, graph_uris: list[str]
) -> tuple[dict[str, dict], dict[str, dict[str, dict[str, str]]]]:
    """Declared types + attribute definitions across the visible ontology layers.

    Returns ``({type_uri: {label, comment, parent}}, {type_uri: {attr_uri:
    {name, range}}})``: the ``get_type_summary`` ontology queries with the
    ``<t_uri>`` binding replaced by a selected ``?type`` / ``?domain``.

    Both reads degrade to empty on failure (mirroring
    ``fetch_types_by_layer``): the declarations decorate the population data,
    so an ontology hiccup must not sink the schema read.
    """
    from_clause = _from_graphs(graph_uris)
    type_q = (
        f"SELECT ?type ?label ?comment ?parent {from_clause} WHERE {{\n"
        f"  ?type <{RDF_TYPE}> <{RDFS}#Class> .\n"
        f"  ?type <{RDFS}#label> ?label .\n"
        f"  OPTIONAL {{ ?type <{RDFS}#comment> ?comment }}\n"
        f"  OPTIONAL {{ ?type <{RDFS}#subClassOf> ?parent }}\n"
        f"}}"
    )
    attr_q = (
        f"SELECT ?domain ?attr ?attrLabel ?range {from_clause} WHERE {{\n"
        f"  ?attr <{RDF_TYPE}> <{RDF_PROPERTY}> .\n"
        f"  ?attr <{RDFS}#domain> ?domain .\n"
        f"  ?attr <{RDFS}#label> ?attrLabel .\n"
        f"  OPTIONAL {{ ?attr <{RDFS}#range> ?range }}\n"
        f"}}"
    )
    type_raw, attr_raw = await asyncio.gather(
        client.query(type_q), client.query(attr_q), return_exceptions=True
    )
    types: dict[str, dict] = {}
    if isinstance(type_raw, BaseException):
        logger.warning("schema_type_declarations_failed", exc_info=type_raw)
    else:
        _, type_rows = parse_sparql_results(type_raw)
        for r in type_rows:
            t_uri = r.get("type", "")
            if _type_leaf(t_uri) is None:
                continue
            types.setdefault(t_uri, {
                "label": r.get("label", ""),
                "comment": r.get("comment", ""),
                "parent": r.get("parent", ""),
            })
    attrs: dict[str, dict[str, dict[str, str]]] = {}
    if isinstance(attr_raw, BaseException):
        logger.warning("schema_attr_declarations_failed", exc_info=attr_raw)
    else:
        _, attr_rows = parse_sparql_results(attr_raw)
        for r in attr_rows:
            domain, a_uri = r.get("domain", ""), r.get("attr", "")
            if not a_uri or _type_leaf(domain) is None:
                continue
            attrs.setdefault(domain, {})[a_uri] = {
                "name": r.get("attrLabel", ""),
                "range": r.get("range", ""),
            }
    return types, attrs


def _dedupe_undirected(pairs: list[tuple[str, str]]) -> list[dict]:
    """Collapse directed (src, tgt) type pairs into undirected edges.

    The overview graph is undirected, so A→B and B→A are one line. Sorting each
    pair keys both directions to the same bucket. Weight is constant for now —
    the overview encodes magnitude via node size, not edge weight.
    """
    by_pair: dict[tuple[str, str], dict] = {}
    for s, t in pairs:
        if not s or not t or s == t:
            continue
        a, c = sorted((s, t))
        by_pair[(a, c)] = {"source": a, "target": c, "weight": 70}
    return list(by_pair.values())


async def _read_edges_from_stats(
    client: NeptuneClient, tenant_id: str, kg_name: str
) -> list[tuple[str, str]] | None:
    """Type→type edges from the precomputed stats graph, or None if unmaterialized.

    Reads the SAME instance-derived ``targetType`` the per-type summary uses, so
    the overview and the detail view agree by construction. Returns None (not [])
    when no stat rows carry a target, so the caller can fall back to a live scan.
    """
    stats = _stats_graph_uri(tenant_id, kg_name)
    q = (
        f"SELECT DISTINCT ?src ?tgt FROM <{stats}> WHERE {{\n"
        f"  ?s <{_STAT_FOR_TYPE}> ?src ; <{_STAT_TARGET}> ?tgt .\n"
        f"}}"
    )
    _, rows = parse_sparql_results(await client.query(q))
    if not rows:
        return None
    out: list[tuple[str, str]] = []
    for r in rows:
        su, tu = r.get("src", ""), r.get("tgt", "")
        if su.startswith(TYPE_URI_PREFIX) and tu.startswith(TYPE_URI_PREFIX):
            out.append((su[len(TYPE_URI_PREFIX):], tu[len(TYPE_URI_PREFIX):]))
    return out


async def _read_edges_from_stats_drift(
    client: NeptuneClient, tenant_id: str, kg_name: str
) -> list[tuple[str, str]] | None:
    """ADR 0004 drift-gated variant of :func:`_read_edges_from_stats`.

    Same instance-derived ``targetType`` edges, but each is additionally tagged
    with its **support** (the ``_STAT_REL`` entity-valued-object total), the
    **source type's entity count** (``entityCount``), and whether the upgraded
    predicate is a **core slot** (the ``onto/coreSlot`` marker that
    ``mark_core_slot`` writes in the ontology graph, keyed by the predicate URI).

    An edge is kept only when ``drift_control.should_declare(support,
    source_count, is_core_slot)`` is True — i.e. it clears the coverage+count
    floor, or is a core slot (exempt). Below-floor edges (the
    ``ManufacturerPartNumber.issuedby -> Retailer`` 6%-coverage shape) are
    excluded from the overview. Returns None when no stat rows carry a target,
    so the caller can fall back to a live scan (which is unfiltered — a fresh KG
    without materialized stats predates drift control and must not regress).

    Only invoked when the ``OMNIX_DRIFT_CONTROL`` flag is ON; with the flag OFF
    the caller takes the unchanged :func:`_read_edges_from_stats` path.
    """
    stats = _stats_graph_uri(tenant_id, kg_name)
    onto = tenant_graph_uri(tenant_id)
    # Per targeted edge: source type, target type, support (rel), and the
    # predicate URI (so we can join the ontology core-slot marker). entityCount
    # for the source type lives on a separate stat triple, joined in by URI.
    q = (
        f"SELECT DISTINCT ?src ?tgt ?pred ?rel ?ec FROM <{stats}> WHERE {{\n"
        f"  ?s <{_STAT_FOR_TYPE}> ?src ; <{_STAT_TARGET}> ?tgt ; <{_STAT_FOR_PRED}> ?pred .\n"
        f"  OPTIONAL {{ ?s <{_STAT_REL}> ?rel }}\n"
        f"  OPTIONAL {{ ?src <{_STAT_ENTITY_COUNT}> ?ec }}\n"
        f"}}"
    )
    # Core-slot markers from the ontology graph, keyed by predicate (attr) URI.
    core_q = (
        f"SELECT DISTINCT ?attr FROM <{onto}> WHERE {{\n"
        f"  ?attr <{_CORE_SLOT_PRED}> ?v .\n"
        f"}}"
    )
    rows_raw, core_raw = await asyncio.gather(client.query(q), client.query(core_q))
    _, rows = parse_sparql_results(rows_raw)
    if not rows:
        return None
    _, core_rows = parse_sparql_results(core_raw)
    core_slots = {r.get("attr", "") for r in core_rows if r.get("attr")}

    out: list[tuple[str, str]] = []
    for r in rows:
        su, tu = r.get("src", ""), r.get("tgt", "")
        if not (su.startswith(TYPE_URI_PREFIX) and tu.startswith(TYPE_URI_PREFIX)):
            continue
        try:
            support = int(r.get("rel", "0") or "0")
        except ValueError:
            support = 0
        try:
            source_count = int(r.get("ec", "0") or "0")
        except ValueError:
            source_count = 0
        # ?pred is the INSTANCE predicate URI (…/onto/<pred>), not an attr URI.
        # core_slots holds ontology attr URIs, so derive the matching attr URI
        # from the source-type leaf + predicate leaf (same join the live scan does).
        src_leaf = su[len(TYPE_URI_PREFIX):]
        p_uri = r.get("pred", "")
        pred_leaf = (
            p_uri[len(ONTO_PRED_PREFIX):] if p_uri.startswith(ONTO_PRED_PREFIX)
            else p_uri.rstrip("/").split("/")[-1]
        )
        is_core = _is_core_slot(src_leaf, pred_leaf, core_slots)
        if not drift_control.should_declare(support, source_count, is_core):
            continue
        out.append((su[len(TYPE_URI_PREFIX):], tu[len(TYPE_URI_PREFIX):]))
    return out


async def _live_edge_scan(client: NeptuneClient, kg_graph: str) -> list[tuple[str, str]]:
    """Fallback: derive type→type edges straight from instance triples.

    Used when stats aren't materialized yet (KG ingested before stats existed).
    Target type comes from the object entity URI leaf, matching the summary.
    """
    q = (
        f"SELECT DISTINCT ?type ?o FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> ?type .\n"
        f"  ?e ?p ?o .\n"
        f'  FILTER(STRSTARTS(STR(?type), "{TYPE_URI_PREFIX}"))\n'
        f'  FILTER(STRSTARTS(STR(?o), "{ENTITY_URI_PREFIX}"))\n'
        f"}}"
    )
    _, rows = parse_sparql_results(await client.query(q))
    out: list[tuple[str, str]] = []
    for r in rows:
        tu = r.get("type", "")
        src = tu[len(TYPE_URI_PREFIX):] if tu.startswith(TYPE_URI_PREFIX) else ""
        if not src or "/" in src:  # skip nested URIs like .../attrs/x
            continue
        tgt = _target_from_entity_uri(r.get("o", ""))
        if tgt:
            out.append((src, tgt))
    return out


async def _live_edge_scan_drift(
    client: NeptuneClient, kg_graph: str, tenant_id: str
) -> list[tuple[str, str]]:
    """ADR 0004 drift-gated variant of :func:`_live_edge_scan`.

    Used (flag ON only) when a KG has NO materialized stats graph — legacy KGs
    ingested before stats/drift control existed. Without a floor here the
    ``ManufacturerPartNumber.issuedby -> Retailer`` 6%-coverage drift shape still
    surfaces in the overview for un-materialized KGs (the production gap this
    fixes); the flag-OFF path keeps the unfiltered :func:`_live_edge_scan`.

    Derives type→type edges straight from instance triples, but applies the SAME
    support floor as :func:`_read_edges_from_stats_drift`. Per (source type,
    predicate, target type) it computes the support (``COUNT(DISTINCT`` source
    entity), and per source type the entity count; an edge is kept only when
    ``drift_control.should_declare(support, source_count, is_core)`` is True.
    ``is_core`` reads the ontology ``onto/coreSlot`` marker, keyed by
    ``attr_uri(srcLeaf, predLeaf)`` (the instance predicate ``…/onto/<predName>``
    maps to the ontology attr ``…/types/<srcLeaf>/attrs/<predName>``).
    """
    onto = tenant_graph_uri(tenant_id)
    # (1) Per (source type, predicate, target type) support = distinct source
    # entities carrying that entity-valued object. Target leaf derived in SPARQL
    # from the object entity URI (…/entities/<TargetType>/<id>).
    edge_q = (
        f"SELECT ?type ?p ?tgt (COUNT(DISTINCT ?e) AS ?support) FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> ?type .\n"
        f"  ?e ?p ?o .\n"
        f'  FILTER(STRSTARTS(STR(?type), "{TYPE_URI_PREFIX}"))\n'
        f'  FILTER(STRSTARTS(STR(?o), "{ENTITY_URI_PREFIX}"))\n'
        f'  BIND(REPLACE(STR(?o), "^.*/entities/([^/]+)/.*$", "$1") AS ?tgt)\n'
        f"}} GROUP BY ?type ?p ?tgt"
    )
    # (2) Per source type entity count (source_count for the coverage ratio).
    count_q = (
        f"SELECT ?type (COUNT(DISTINCT ?e) AS ?ec) FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> ?type .\n"
        f'  FILTER(STRSTARTS(STR(?type), "{TYPE_URI_PREFIX}"))\n'
        f"}} GROUP BY ?type"
    )
    # (3) Core-slot markers from the ontology graph, keyed by attr URI — SAME
    # query shape as _read_edges_from_stats_drift.
    core_q = (
        f"SELECT DISTINCT ?attr FROM <{onto}> WHERE {{\n"
        f"  ?attr <{_CORE_SLOT_PRED}> ?v .\n"
        f"}}"
    )
    edge_raw, count_raw, core_raw = await asyncio.gather(
        client.query(edge_q), client.query(count_q), client.query(core_q)
    )

    _, count_rows = parse_sparql_results(count_raw)
    source_counts: dict[str, int] = {}
    for r in count_rows:
        tu = r.get("type", "")
        if not tu.startswith(TYPE_URI_PREFIX):
            continue
        try:
            source_counts[tu] = int(r.get("ec", "0") or "0")
        except ValueError:
            source_counts[tu] = 0

    _, core_rows = parse_sparql_results(core_raw)
    core_slots = {r.get("attr", "") for r in core_rows if r.get("attr")}

    _, edge_rows = parse_sparql_results(edge_raw)
    out: list[tuple[str, str]] = []
    for r in edge_rows:
        tu = r.get("type", "")
        src = tu[len(TYPE_URI_PREFIX):] if tu.startswith(TYPE_URI_PREFIX) else ""
        if not src or "/" in src:  # skip nested URIs like .../attrs/x
            continue
        tgt = r.get("tgt", "")
        if not tgt or "/" in tgt:
            continue
        try:
            support = int(r.get("support", "0") or "0")
        except ValueError:
            support = 0
        source_count = source_counts.get(tu, 0)
        p_uri = r.get("p", "")
        pred_leaf = (
            p_uri[len(ONTO_PRED_PREFIX):] if p_uri.startswith(ONTO_PRED_PREFIX)
            else p_uri.rstrip("/").split("/")[-1]
        )
        is_core = _is_core_slot(src, pred_leaf, core_slots)
        if not drift_control.should_declare(support, source_count, is_core):
            continue
        out.append((src, tgt))
    return out


async def recompute_kg_stats(client: NeptuneClient, tenant_id: str, kg_name: str) -> dict:
    """Recompute the stats graph for a KG in one whole-KG scan.

    Run at ingest time (or via the recompute endpoint / backfill). Replaces the
    KG's stats graph atomically and busts the in-memory cache for its types.
    """
    kg = kg_graph_uri(tenant_id, kg_name)
    stats = _stats_graph_uri(tenant_id, kg_name)
    scan = (
        f"SELECT ?type ?p (COUNT(DISTINCT ?e) AS ?cnt) (SAMPLE(?o) AS ?sample)\n"
        f'  (SUM(IF(STRSTARTS(STR(?o), "{ENTITY_URI_PREFIX}"), 1, 0)) AS ?rel)\n'
        f"{_ST_FLAG_AGGREGATES}"
        f"FROM <{kg}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> ?type .\n"
        f"  ?e ?p ?o .\n"
        f'  FILTER(STRSTARTS(STR(?type), "{TYPE_URI_PREFIX}"))\n'
        f"{_PRIMARY_TYPE_GUARD}"
        f"}} GROUP BY ?type ?p"
    )
    _, rows = parse_sparql_results(await client.query(scan))

    entity_counts: dict[str, int] = {}
    total_edges = 0  # sum of entity-valued-object totals across predicate rows
    triples: list[str] = []
    # ADR 0004: raw type-level relationship declarations (type_uri, pred_uri,
    # support). Source counts are resolved AFTER the loop, once entity_counts is
    # fully populated (rows are grouped by type+pred, so a type's rdf:type count
    # row may come after its predicate rows). Only collected when the flag is ON.
    drift_enabled = drift_control.drift_control_enabled()
    rel_decls: list[tuple[str, str, int]] = []
    flag_accs: dict[str, _IndexFlagAccumulator] = {}
    # LEGACY companion classification (ONTA-262): pre-pass building each type's
    # literal-predicate leaf set so the loop below can skip per-attribute
    # provenance companions written on the old ATTRIBUTE namespace
    # (`attrs/<attr>_<suffix>` → instance predicate `onto/<attr>_<suffix>`),
    # which URI-shape filtering can't catch. Only literal-valued rows (rel == 0)
    # participate, so a real relationship is never misclassified. Keeps freshly
    # recomputed stats clean of legacy companions the same way the internal-
    # predicate filter does (assembly filters too, as a backstop).
    type_pred_leaves: dict[str, set[str]] = {}
    for r in rows:
        p = r.get("p", "")
        if not p or p == RDF_TYPE:
            continue
        try:
            r_rel = int(r.get("rel", "0"))
        except ValueError:
            r_rel = 0
        if r_rel > 0:
            continue
        type_pred_leaves.setdefault(r.get("type", ""), set()).add(
            p.rstrip("/").split("/")[-1]
        )
    legacy_companions_by_type = {
        t: _companion_leaves(leaves) for t, leaves in type_pred_leaves.items()
    }
    for r in rows:
        type_uri_str = r.get("type", "")
        leaf = type_uri_str[len(TYPE_URI_PREFIX):] if type_uri_str.startswith(TYPE_URI_PREFIX) else ""
        if not leaf or "/" in leaf:  # skip nested URIs like .../attrs/x
            continue
        p_uri = r.get("p", "")
        try:
            cnt = int(r.get("cnt", "0"))
        except ValueError:
            cnt = 0
        try:
            rel = int(r.get("rel", "0"))
        except ValueError:
            rel = 0
        if p_uri == RDF_TYPE:
            entity_counts[type_uri_str] = cnt
            continue
        # Don't materialize stats for internal/housekeeping predicates — they
        # must never reach the Explorer's Attributes/Relationships panel
        # (onto/batch_id, er/blockKey, er/erSignal_*, rdf*/rdfs*). Filtering here
        # keeps freshly recomputed stats clean (assembly filters too, as a
        # backstop for already-materialized stats). A predicate with entity-valued
        # objects (rel > 0) is a relationship, exempt from the literal-only
        # housekeeping markers (FIX 2) so a real `onto/source` edge is materialized.
        if _is_internal_predicate(p_uri, is_relationship=rel > 0):
            continue
        if rel == 0 and p_uri.rstrip("/").split("/")[-1] in legacy_companions_by_type.get(
            type_uri_str, ()
        ):
            continue
        # Per-type spatio-temporal index flags, from the same scan rows.
        try:
            geo = int(r.get("geo", "0") or "0")
        except ValueError:
            geo = 0
        try:
            tmp = int(r.get("tmp", "0") or "0")
        except ValueError:
            tmp = 0
        if geo or tmp:
            flag_accs.setdefault(type_uri_str, _IndexFlagAccumulator()).add(p_uri, geo, tmp)
        total_edges += rel
        node = _stat_node(type_uri_str, p_uri)
        stat = (
            f"<{node}> <{_STAT_FOR_TYPE}> <{type_uri_str}> ; "
            f"<{_STAT_FOR_PRED}> <{p_uri}> ; "
            f"<{_STAT_CNT}> {cnt} ; <{_STAT_REL}> {rel}"
        )
        target = _target_from_entity_uri(r.get("sample", ""))
        if target:
            stat += f" ; <{_STAT_TARGET}> <{TYPE_URI_PREFIX}{target}>"
        triples.append(stat + " .")
        # A type-level relationship is a predicate carrying entity-valued
        # objects (rel > 0). Those are the declarations the drift floor gates.
        if drift_enabled and rel > 0:
            rel_decls.append((type_uri_str, p_uri, rel))
    pred_row_count = len(triples)
    for type_uri_str, n in entity_counts.items():
        triples.append(f"<{type_uri_str}> <{_STAT_ENTITY_COUNT}> {n} .")
    # Materialize the per-type index markers (only when true — absence = false).
    for type_uri_str, facc in flag_accs.items():
        if facc.spatial:
            triples.append(f"<{type_uri_str}> <{_STAT_SPATIAL}> true .")
        if facc.temporal:
            triples.append(f"<{type_uri_str}> <{_STAT_TEMPORAL}> true .")

    if triples:
        body = "\n".join(triples)
        update = (
            f"DROP SILENT GRAPH <{stats}> ;\n"
            f"INSERT DATA {{ GRAPH <{stats}> {{\n{body}\n}} }}"
        )
    else:
        update = f"DROP SILENT GRAPH <{stats}>"
    await client.update(update)

    for key in [k for k in _summary_cache if k[0] == tenant_id and k[1] == kg_name]:
        _summary_cache.pop(key, None)

    # Ingest changed the data → the KG's stored triple count is stale. Drop it
    # so the next `list_kgs` recomputes (and re-stores) it once. Local import
    # avoids an import cycle between this module and knowledge_graphs.
    from cograph_client.api.routes.knowledge_graphs import invalidate_triple_count
    await invalidate_triple_count(client, tenant_id, kg_name)

    # Materialize the per-KG dashboard summary (entity/edge totals + per-type
    # breakdown) into the durable store, so the dashboard reads a tiny relational
    # row instead of querying Neptune. This rides the one shared write/refresh
    # path — it is not a second computation. Best-effort: a store hiccup must not
    # fail the recompute (reads fall back to lazy backfill from the stats graph).
    type_breakdown = {
        (t[len(TYPE_URI_PREFIX):] if t.startswith(TYPE_URI_PREFIX) else t): n
        for t, n in entity_counts.items()
    }
    try:
        from cograph_client.graph.kg_stats_store import KgStats, get_kg_stats_store
        from cograph_client.graph.kg_summary import resolve_summary

        store = get_kg_stats_store()
        prev = await store.get(tenant_id, kg_name)
        # Regenerate the one-line summary only when the type SET changed since it
        # was last generated (or there's none yet) — an enrichment write that
        # just fills attributes on existing types keeps the existing line instead
        # of paying for an LLM call. A transient miss keeps the old line and
        # leaves the signature stale so the next recompute retries.
        ai_description, ai_description_types = await resolve_summary(
            prev.ai_description if prev else "",
            prev.ai_description_types if prev else [],
            type_breakdown,
            kg_name,
        )

        await store.upsert(
            KgStats(
                tenant_id=tenant_id,
                kg_name=kg_name,
                entity_count=sum(entity_counts.values()),
                edge_count=total_edges,
                type_breakdown=type_breakdown,
                ai_description=ai_description,
                ai_description_types=ai_description_types,
            )
        )
    except Exception:  # noqa: BLE001 — store write is best-effort
        logger.warning("kg_stats_store_upsert_failed", kg=kg_name, exc_info=True)

    result = {"types": len(entity_counts), "predicate_rows": pred_row_count}
    if drift_enabled:
        result["drift"] = await _build_drift_report(
            client, tenant_id, kg_name, rel_decls, entity_counts
        )
    return result


async def _build_drift_report(
    client: NeptuneClient,
    tenant_id: str,
    kg_name: str,
    rel_decls: list[tuple[str, str, int]],
    entity_counts: dict[str, int],
) -> dict:
    """Build + log the ADR 0004 drift report for a recompute pass (flag ON only).

    Resolves each raw relationship declaration into the ``{key, support,
    source_count, is_core_slot}`` shape ``drift_control.drift_report`` consumes:
    source_count is the source type's entity count; ``is_core_slot`` reads the
    ``onto/coreSlot`` marker (keyed by predicate URI) from the ontology graph.
    The report (effective floors + kept/quarantined split) is returned and
    logged so the drift dashboard / tenant changelog can read it.
    """
    onto = tenant_graph_uri(tenant_id)
    core_q = (
        f"SELECT DISTINCT ?attr FROM <{onto}> WHERE {{\n"
        f"  ?attr <{_CORE_SLOT_PRED}> ?v .\n"
        f"}}"
    )
    _, core_rows = parse_sparql_results(await client.query(core_q))
    core_slots = {r.get("attr", "") for r in core_rows if r.get("attr")}

    declarations: list[dict] = []
    for type_uri_str, pred_uri, support in rel_decls:
        type_leaf = type_uri_str[len(TYPE_URI_PREFIX):] if type_uri_str.startswith(TYPE_URI_PREFIX) else type_uri_str
        pred_leaf = pred_uri.rstrip("/").split("/")[-1]
        declarations.append({
            "key": f"{type_leaf}.{pred_leaf}",
            "support": support,
            "source_count": entity_counts.get(type_uri_str, 0),
            # pred_uri is …/onto/<pred>; core_slots holds ontology attr URIs, so
            # match on attr_uri(type_leaf, pred_leaf), not the raw predicate URI.
            "is_core_slot": _is_core_slot(type_leaf, pred_leaf, core_slots),
        })
    report = drift_control.drift_report(declarations)
    logger.info(
        "drift_report",
        tenant=tenant_id,
        kg=kg_name,
        observe_only=drift_control.observe_only(),
        floor_cov=report["floor_cov"],
        floor_count=report["floor_count"],
        kept=report["kept"],
        quarantined=report["quarantined"],
        quarantine=report["quarantine"],
        # Full per-relationship coverage distribution — the observe-only signal
        # the floor should ultimately be set from (not the hand-tuned 20%).
        coverages=report["coverages"],
    )
    # COG-57: also persist the distribution to a durable, queryable store so it
    # survives past CloudWatch's 30-day log retention. Best-effort — a history
    # write must never fail a recompute (it is observability, not correctness).
    await _persist_drift_history(client, tenant_id, kg_name, report)
    return report


def _typed(value: object, xsd_type: str) -> str:
    """Render a typed literal: ``"<value>"^^<xsd:...>``."""
    return f'"{value}"^^<{_XSD}{xsd_type}>'


async def _persist_drift_history(
    client: NeptuneClient, tenant_id: str, kg_name: str, report: dict
) -> None:
    """Append one drift-report snapshot to the per-KG drift-history graph (COG-57).

    Writes the run's effective floors + kept/quarantined totals as a snapshot
    node, plus one point node per relationship in ``report["coverages"]`` (the
    full distribution, kept and quarantined alike). APPEND-only — never DROPs the
    graph — so the distribution accumulates across recomputes and tenants/KGs,
    which is the data ADR 0004 needs to set ``OMNIX_DRIFT_FLOOR_COV`` from a real
    histogram instead of the hand-calibrated 20%.

    Wrapped in try/except: persistence is observability, so a Neptune write
    failure here is logged and swallowed rather than failing the recompute.
    """
    hist = _drift_history_graph_uri(tenant_id, kg_name)
    snap = f"{_DRIFT_NS}snap/{uuid.uuid4().hex}"
    recorded_at = datetime.now(timezone.utc).isoformat()

    triples = [
        f"<{snap}> <{_DRIFT_RECORDED_AT}> {_typed(recorded_at, 'dateTime')} ; "
        f'<{_DRIFT_KG}> "{_esc(kg_name)}" ; '
        f"<{_DRIFT_FLOOR_COV}> {_typed(report['floor_cov'], 'decimal')} ; "
        f"<{_DRIFT_FLOOR_COUNT}> {_typed(report['floor_count'], 'integer')} ; "
        f"<{_DRIFT_KEPT}> {_typed(report['kept'], 'integer')} ; "
        f"<{_DRIFT_QUARANTINED}> {_typed(report['quarantined'], 'integer')} ."
    ]
    for c in report.get("coverages", []):
        pt = f"{snap}/p/{hashlib.md5(c['key'].encode()).hexdigest()}"
        triples.append(
            f"<{pt}> <{_DRIFT_POINT_OF}> <{snap}> ; "
            f'<{_DRIFT_KEY}> "{_esc(c["key"])}" ; '
            f"<{_DRIFT_COVERAGE}> {_typed(c['coverage'], 'decimal')} ; "
            f"<{_DRIFT_SUPPORT}> {_typed(c['support'], 'integer')} ; "
            f"<{_DRIFT_SOURCE_COUNT}> {_typed(c['source_count'], 'integer')} ; "
            f"<{_DRIFT_IS_CORE}> {_typed(str(bool(c['is_core_slot'])).lower(), 'boolean')} ; "
            f"<{_DRIFT_POINT_KEPT}> {_typed(str(bool(c['kept'])).lower(), 'boolean')} ."
        )

    body = "\n".join(triples)
    try:
        await client.update(f"INSERT DATA {{ GRAPH <{hist}> {{\n{body}\n}} }}")
    except Exception:
        logger.warning("drift_history_persist_failed", tenant=tenant_id, kg=kg_name, exc_info=True)


async def read_kg_summary_from_stats(
    client: NeptuneClient, tenant_id: str, kg_name: str
):
    """Aggregate a KG's per-type stats graph into KG-level totals.

    Reads the *precomputed* stats graph (tiny — one row per type / per
    type-predicate), NOT the full instance graph, so this is cheap. Returns
    ``(entity_total, edge_total, {type_leaf: count})`` or ``None`` when the KG
    has no materialized entity counts yet (e.g. a KG that predates the stats
    graph and has never been recomputed) — the caller should then schedule a
    recompute. This is the source the dashboard-summary store is backfilled from.
    """
    stats = _stats_graph_uri(tenant_id, kg_name)
    ec_q = f"SELECT ?t ?ec FROM <{stats}> WHERE {{ ?t <{_STAT_ENTITY_COUNT}> ?ec }}"
    _, ec_rows = parse_sparql_results(await client.query(ec_q))
    if not ec_rows:
        return None
    breakdown: dict[str, int] = {}
    entity_total = 0
    for r in ec_rows:
        t = r.get("t", "")
        leaf = t[len(TYPE_URI_PREFIX):] if t.startswith(TYPE_URI_PREFIX) else t
        try:
            n = int(r.get("ec", "0"))
        except (ValueError, TypeError):
            n = 0
        breakdown[leaf] = n
        entity_total += n
    edge_q = f"SELECT (SUM(?rel) AS ?total) FROM <{stats}> WHERE {{ ?s <{_STAT_REL}> ?rel }}"
    _, edge_rows = parse_sparql_results(await client.query(edge_q))
    try:
        edge_total = int(edge_rows[0].get("total", "0")) if edge_rows else 0
    except (ValueError, TypeError):
        edge_total = 0
    return entity_total, edge_total, breakdown


async def backfill_kg_summary(
    client: NeptuneClient, tenant_id: str, kg_name: str, *, persist: bool = True
):
    """Seed the durable dashboard-summary store for one KG from existing stats.

    Used to lazily fill rows for KGs that predate the store (the first time
    they're listed) without a fresh whole-KG scan. Returns the upserted
    :class:`KgStats`, or ``None`` if the KG's stats graph isn't materialized yet
    (the caller schedules a recompute, which will populate the store directly).

    ``persist=False`` computes the SAME row and returns it WITHOUT storing it
    (ONTA-452). A read-only member gets identical numbers on the listing while
    the lazy materialization stays a write only writers perform.
    """
    agg = await read_kg_summary_from_stats(client, tenant_id, kg_name)
    if agg is None:
        return None
    entity_total, edge_total, breakdown = agg
    from cograph_client.graph.kg_stats_store import KgStats, get_kg_stats_store

    row = KgStats(
        tenant_id=tenant_id,
        kg_name=kg_name,
        entity_count=entity_total,
        edge_count=edge_total,
        type_breakdown=breakdown,
    )
    if not persist:
        return row
    await get_kg_stats_store().upsert(row)
    return row


async def drop_kg_stats(client: NeptuneClient, tenant_id: str, kg_name: str) -> None:
    """Drop a KG's precomputed stats graph and evict its in-memory summaries.

    Called when a KG is deleted. The stats graph URI is derived from the KG
    name, so without this a KG later recreated under the same name would serve
    the deleted graph's stale counts until the next recompute lands.
    """
    stats = _stats_graph_uri(tenant_id, kg_name)
    hist = _drift_history_graph_uri(tenant_id, kg_name)
    # Drop the drift-history graph too (COG-57): its URI is derived from the KG
    # name, so a KG recreated under the same name would otherwise inherit the
    # deleted KG's distribution. Matches the stats-graph cleanup rationale above.
    await client.update(f"DROP SILENT GRAPH <{stats}> ; DROP SILENT GRAPH <{hist}>")
    for key in [k for k in _summary_cache if k[0] == tenant_id and k[1] == kg_name]:
        _summary_cache.pop(key, None)
    # Drop the materialized dashboard-summary row too — its key is derived from
    # the KG name, so a KG recreated under the same name would otherwise inherit
    # the deleted KG's counts. Best-effort, matching the cache eviction above.
    try:
        from cograph_client.graph.kg_stats_store import get_kg_stats_store

        await get_kg_stats_store().delete(tenant_id, kg_name)
    except Exception:  # noqa: BLE001
        logger.warning("kg_stats_store_delete_failed", kg=kg_name, exc_info=True)


# Background recompute: the whole-KG scan takes ~15s, longer than the ALB
# response timeout, so we never want a request to block on it. The Neptune
# client is an app-state singleton, so a fire-and-forget task is safe.
_bg_tasks: set = set()


async def _safe_recompute(client: NeptuneClient, tenant_id: str, kg_name: str) -> None:
    try:
        await recompute_kg_stats(client, tenant_id, kg_name)
    except Exception:
        pass  # best-effort; reads fall back to a live scan until it succeeds


#: KGs with a recompute already in flight. Repeated scheduling for the SAME KG
#: COALESCES instead of stacking N whole-KG scans. Load-bearing since ONTA-452:
#: a read-only member's ``GET /kgs`` may schedule a recompute on a stats miss
#: (otherwise they would see a permanent, indistinguishable 0), so that path
#: must not be spammable into repeated full scans.
_recompute_inflight: set[tuple[str, str]] = set()

#: KGs whose recompute was requested WHILE one was already in flight. Deferred,
#: never dropped: the in-flight scan may have read the KG before the newer
#: write landed, so discarding the request would persist pre-write numbers
#: permanently (nothing re-triggers: ``recompute_kg_stats`` upserts a durable
#: store row and ``_kg_stats_for`` only schedules on a store MISS). A set, so at
#: most ONE follow-up is queued per KG however many requests pile up: the
#: reader-reachable path stays bounded at one in-flight plus one queued scan.
_recompute_pending: set[tuple[str, str]] = set()


def schedule_recompute(client: NeptuneClient, tenant_id: str, kg_name: str) -> None:
    """Fire-and-forget a stats recompute (used by the endpoint + ingest hook).

    Coalesced per (tenant, KG): while a scan is in flight, further requests do
    not stack up N whole-KG scans, but they are not dropped either. They mark
    the KG pending, and exactly one follow-up scan runs when the current one
    finishes. Dropping them would be a correctness bug, not just a lost
    refresh: the concurrent-batch ingest path (``refresh_after_write`` per
    ``POST /ingest/csv/rows``) and the ``POST /recompute-stats`` that the CLI
    fires right after the last batch both land inside the ~15s scan, so the
    last writer's numbers would never be persisted.
    """
    key = (tenant_id, kg_name)
    if key in _recompute_inflight:
        # Defer, don't discard: the running scan may predate this caller's write.
        _recompute_pending.add(key)
        return
    _recompute_inflight.add(key)
    try:
        task = asyncio.create_task(_safe_recompute(client, tenant_id, kg_name))
    except Exception:
        # No running loop (a sync caller): never leave the key stuck marked
        # in-flight, or this KG could never be recomputed again.
        _recompute_inflight.discard(key)
        raise
    _bg_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _bg_tasks.discard(t)
        # Order matters: clear the in-flight marker BEFORE re-scheduling, or the
        # re-schedule below would see itself as in-flight and fall into the
        # pending branch, losing the deferred request for good. Done callbacks
        # run on the event loop with no await points between these statements,
        # so no request can slip in mid-sequence.
        _recompute_inflight.discard(key)
        if key in _recompute_pending:
            _recompute_pending.discard(key)
            try:
                schedule_recompute(client, tenant_id, kg_name)
            except Exception:  # noqa: BLE001, best-effort; never raise into the loop
                logger.warning(
                    "recompute_rerun_schedule_failed", kg=kg_name, exc_info=True
                )

    task.add_done_callback(_done)


# Cap on concurrent summary-generation LLM calls in a single backfill sweep, so a
# tenant with many pre-existing KGs doesn't fan out one call per KG at once.
_SUMMARY_BACKFILL_CONCURRENCY = 5


def schedule_summary_backfill(rows: list) -> None:
    """Fire-and-forget generation of any missing one-line KG summaries.

    Kept OFF the ``list_kgs`` hot path (the file's stated rule: never compute the
    summary live on a request) — the descriptions are persisted by the background
    task and appear on the NEXT list. Recompute fills them at write time going
    forward, so this only covers rows that predate the feature. No-op when
    nothing is pending.
    """
    pending = [r for r in rows if not r.ai_description and r.type_breakdown]
    if not pending:
        return
    task = asyncio.create_task(_run_summary_backfill(pending))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _run_summary_backfill(pending: list) -> None:
    """Generate + persist summaries for the given stats rows (bounded fan-out).

    Best-effort throughout: a generation miss or store hiccup just leaves the
    line blank for the next sweep. Concurrency is capped so a big tenant can't
    fire one LLM call per KG simultaneously."""
    from cograph_client.graph.kg_stats_store import get_kg_stats_store
    from cograph_client.graph.kg_summary import generate_kg_summary

    sem = asyncio.Semaphore(_SUMMARY_BACKFILL_CONCURRENCY)

    async def _one(row):
        async with sem:
            return await generate_kg_summary(row.kg_name, row.type_breakdown)

    summaries = await asyncio.gather(
        *(_one(r) for r in pending), return_exceptions=True
    )
    store = get_kg_stats_store()
    for row, desc in zip(pending, summaries):
        if isinstance(desc, str) and desc:
            row.ai_description = desc
            row.ai_description_types = sorted(row.type_breakdown)
            try:
                await store.upsert(row)
            except Exception:  # noqa: BLE001 — persistence is a cache warm, not required
                logger.warning("kg_summary_backfill_upsert_failed", kg=row.kg_name)


@router.get("/kgs/{kg_name}/types/{type_name}/summary")
async def get_type_summary(
    kg_name: str,
    type_name: str,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Bundle all Explorer panel data for one type in one call.

    Serves from precomputed stats (fast); falls back to a live scan if stats
    for this type are not yet materialized. All percentages are relative to
    entity_count.

    A ``type_name`` that cannot sit inside an IRI is a 422 (ONTA-425), rejected
    here rather than three store round trips later, so the caller is told what is
    wrong instead of getting a 500 out of the store's parser.
    """
    require_valid_type_name(type_name)
    cache_key = (tenant.tenant_id, kg_name, type_name)
    cached = _summary_cache.get(cache_key)
    if cached is not None and (time.monotonic() - cached[0]) < _SUMMARY_TTL_SECONDS:
        return cached[1]

    # Layered resolve (ONTA-397): Public/Enhanced types are visible when the
    # tenant graph is empty. Instance counts still use the tenant-namespace
    # type URI for historical data, plus the winning layer URI.
    kg_graph = kg_graph_uri(tenant.tenant_id, kg_name)
    resolved = await _resolve_layered_type(client, tenant, type_name)
    if resolved is not None:
        t_uri, onto_graph, _layer = resolved
    else:
        # No layered declaration — fall back to tenant URI so live-scan of
        # instance-only types (no schema yet) still works.
        onto_graph = tenant_graph_uri(tenant.tenant_id)
        t_uri = type_uri(type_name)

    onto_sparql = (
        f"SELECT ?label ?comment ?parent FROM <{onto_graph}> WHERE {{\n"
        f"  <{t_uri}> <{RDFS}#label> ?label .\n"
        f"  OPTIONAL {{ <{t_uri}> <{RDFS}#comment> ?comment }}\n"
        f"  OPTIONAL {{ <{t_uri}> <{RDFS}#subClassOf> ?parent }}\n"
        f"}}"
    )
    attr_def_sparql = (
        f"SELECT ?attr ?attrLabel ?range FROM <{onto_graph}> WHERE {{\n"
        f"  ?attr <{RDF_TYPE}> <{RDF_PROPERTY}> .\n"
        f"  ?attr <{RDFS}#domain> <{t_uri}> .\n"
        f"  ?attr <{RDFS}#label> ?attrLabel .\n"
        f"  OPTIONAL {{ ?attr <{RDFS}#range> ?range }}\n"
        f"}}"
    )

    # Ontology lookups (tiny) + precomputed stats, all concurrent.
    onto_raw, attr_def_raw, stats = await asyncio.gather(
        client.query(onto_sparql),
        client.query(attr_def_sparql),
        _read_type_stats(client, tenant.tenant_id, kg_name, t_uri),
    )

    _, onto_rows = parse_sparql_results(onto_raw)
    onto_row = onto_rows[0] if onto_rows else {}
    parent_uri = onto_row.get("parent", "")
    parent_type = parent_uri.rstrip("/").split("/")[-1] if parent_uri else None

    _, attr_def_rows = parse_sparql_results(attr_def_raw)
    attr_defs: dict[str, dict[str, str]] = {
        r["attr"]: {"name": r.get("attrLabel", ""), "range": r.get("range", "")}
        for r in attr_def_rows
        if r.get("attr")
    }

    if stats is not None:
        entity_count, pred_records, index_flags = stats
    else:
        # Stats not materialized for this type — fall back to a live scan.
        entity_count, pred_records, index_flags = await _live_scan(client, kg_graph, t_uri)

    result = _assemble_summary(
        type_name, onto_row, parent_type, entity_count, pred_records, attr_defs,
        index_flags=index_flags,
    )
    _summary_cache[cache_key] = (time.monotonic(), result)
    return result


# Note (ONTA-418): the whole-KG schema read below deliberately does NOT use
# `_summary_cache`. That memo is per-PROCESS and evicted only in-process, so on
# a multi-task ECS service a caller can read up to `_SUMMARY_TTL_SECONDS`
# (30 min) of stale coverage from a task that never saw the write. The stats-graph
# read is 2 tiny queries, so the schema endpoint just pays them every time rather
# than handing an agent stale "which attributes are populated" data.
_SCHEMA_COVERAGE_NOTE = (
    "coverage_pct is relative to entity_count, which attributes a multi-typed "
    "entity only to its lexicographically-smallest type URI (the primary-type "
    "guard that keeps live and precomputed counts consistent). Types sharing "
    "multi-typed entities can therefore show coverage below 100% even when every "
    "instance carries the attribute."
)


def _sorted_slots(slots: list[dict], min_coverage: float) -> tuple[list[dict], int]:
    """Coverage-desc slots + how many the floor dropped.

    Marks each slot `populated` instead of removing zero-count ones: a count of
    0 is returned identically by a genuinely-empty attribute and by a transient
    Neptune throttle, so dropping on `count == 0` makes attributes flicker in and
    out across identical calls (ONTA-248). Only an EXPLICIT `min_coverage > 0`
    filters, and the caller is told how many were withheld.
    """
    kept, omitted = [], 0
    for s in slots:
        s["populated"] = s.get("count", 0) > 0
        if min_coverage > 0 and s.get("coverage_pct", 0.0) < min_coverage:
            omitted += 1
            continue
        kept.append(s)
    kept.sort(key=lambda s: (-s.get("coverage_pct", 0.0), s.get("name", "")))
    return kept, omitted


@router.get("/kgs/{kg_name}/schema")
async def get_kg_schema(
    kg_name: str,
    type_names: list[str] | None = Query(
        None, alias="type",
        description="Repeatable. Narrows to these type names (drill-in).",
    ),
    min_coverage: float = Query(
        0.0, ge=0.0, le=100.0,
        description="Withhold attributes/relationships below this coverage PERCENT.",
    ),
    include_empty: bool = Query(
        True, description="Include types declared in the ontology with 0 instances here."
    ),
    limit: int = Query(50, ge=1, le=500, description="Max types returned (entity_count desc)."),
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Population-aware schema for ONE KG: every type with its POPULATED slots.

    The whole-KG counterpart of ``/types/{type}/summary``, same per-type shape,
    assembled by the same ``_assemble_summary`` (so the same predicate hygiene
    applies: internal ER/batch predicates and legacy per-attribute provenance
    companions never surface), but for every type in one request. This is a
    BACKEND join on purpose: the stats it reads are already materialized for the
    whole KG, so it costs 3-4 queries, where a client-side loop over the per-type
    summary would be 1+N round trips.

    Declared-but-empty types and attributes are INCLUDED and marked
    (``populated: false`` / ``declared_only: true``), never hidden. Hiding them
    made agents assert "that type does not exist" or substitute a wrong type
    (ONTA-248 / ONTA-258). ``min_coverage`` is the one filter that withholds
    slots, and it only acts when the caller explicitly sets it.

    Not exposed here: sample VALUES. Nothing serves them over HTTP today (the NL
    pipeline computes them inside ``/ask`` only). Deliberately out of scope.
    """
    stack = layer_stack_for(tenant)
    kg_graph = kg_graph_uri(tenant.tenant_id, kg_name)

    (declared_types, declared_attrs), stats_by_uri = await asyncio.gather(
        _read_declared_schema(client, stack.visible_graph_uris()),
        _read_all_type_stats(client, tenant.tenant_id, kg_name),
    )
    stats_source = "precomputed"
    if not stats_by_uri:
        # Legacy KG with no materialized stats: ONE whole-KG scan, not one per type.
        stats_by_uri = await _live_scan_all(client, kg_graph)
        stats_source = "live_scan"

    # Collapse type URIs onto type NAMES. Declarations shadow by layer precedence
    # (first visible layer wins); instance stats pick the URI carrying the most
    # entities, so a Public declaration whose instances were written under the
    # historical tenant namespace resolves to ONE entry rather than a populated
    # orphan plus a phantom empty type.
    layer_rank = {type_namespace(layer): i for i, layer in enumerate(stack.layers)}

    def _rank(uri: str) -> int:
        for ns in _LAYER_TYPE_NAMESPACES:
            if uri.startswith(ns):
                return layer_rank.get(ns, len(layer_rank))
        return len(layer_rank)

    decl_uri_by_name: dict[str, str] = {}
    for t_uri in declared_types:
        leaf = _type_leaf(t_uri)
        if leaf is None:
            continue
        current = decl_uri_by_name.get(leaf)
        if current is None or _rank(t_uri) < _rank(current):
            decl_uri_by_name[leaf] = t_uri
    stats_uri_by_name: dict[str, str] = {}
    for t_uri, (count, _recs, _flags) in stats_by_uri.items():
        leaf = _type_leaf(t_uri)
        if leaf is None:
            continue
        current = stats_uri_by_name.get(leaf)
        if current is None or count > stats_by_uri[current][0]:
            stats_uri_by_name[leaf] = t_uri

    # Case-INSENSITIVE, because the caller is usually an LLM that may lowercase a
    # type name. An exact-match miss would answer "no such type", which is the
    # very failure mode this endpoint exists to prevent.
    wanted = {t.lower() for t in (type_names or []) if t}
    all_names = sorted(set(decl_uri_by_name) | set(stats_uri_by_name))
    names = [n for n in all_names if n.lower() in wanted] if wanted else all_names

    assembled: list[dict] = []
    for name in names:
        d_uri = decl_uri_by_name.get(name)
        s_uri = stats_uri_by_name.get(name)
        onto_row = declared_types.get(d_uri, {}) if d_uri else {}
        parent_uri = onto_row.get("parent", "")
        parent_type = parent_uri.rstrip("/").split("/")[-1] if parent_uri else None
        # Attribute declarations from the winning layer AND from the URI the
        # instances actually use, so the predicate→name/range lookup resolves in
        # either namespace.
        attr_defs: dict[str, dict[str, str]] = {}
        for u in (d_uri, s_uri):
            if u:
                attr_defs.update(declared_attrs.get(u, {}))
        entity_count, records, flags = (
            stats_by_uri[s_uri] if s_uri else (0, [], {})
        )
        records = list(records)
        # DECLARED-but-unpopulated slots: synthesized as count-0 records so the
        # agent sees every attribute the schema promises (marked unpopulated),
        # never a silently shortened list. Deduped by display name because a
        # populated relationship's instance predicate (`onto/<leaf>`) differs
        # from its ontology declaration URI (`types/<T>/attrs/<leaf>`).
        real_names = {
            (attr_defs.get(r.get("p", ""), {}).get("name")
             or r.get("p", "").rstrip("/").split("/")[-1])
            for r in records
        }
        seen = {n.lower() for n in real_names}
        for a_uri, defn in attr_defs.items():
            nm = defn.get("name") or a_uri.rstrip("/").split("/")[-1]
            if not nm or nm.lower() in seen:
                continue
            # Do not synthesize a base name that would retroactively turn a
            # POPULATED `<nm>_<suffix>` record into a legacy provenance companion:
            # `_assemble_summary`'s set-wise classifier hides `<base>_<suffix>`
            # only when `<base>` is present, so adding an empty `data` would make
            # a real, populated `data_provenance` disappear. Never trade a
            # populated slot for a declared-empty one.
            if any(f"{nm}_{sfx}" in real_names for sfx in ATTR_META_SUFFIXES):
                continue
            seen.add(nm.lower())
            records.append({"p": a_uri, "cnt": 0, "rel": 0, "target": None})

        summary = _assemble_summary(
            name, onto_row, parent_type, entity_count, records, attr_defs,
            index_flags=flags,
        )
        summary["attributes"], attrs_omitted = _sorted_slots(
            summary["attributes"], min_coverage
        )
        summary["relationships"], rels_omitted = _sorted_slots(
            summary["relationships"], min_coverage
        )
        summary["populated"] = entity_count > 0
        summary["declared_only"] = s_uri is None
        summary["attributes_withheld"] = attrs_omitted
        summary["relationships_withheld"] = rels_omitted
        assembled.append(summary)

    # Drop zero-instance types only on explicit request, and never when the
    # caller named the types it wants.
    if not include_empty and not wanted:
        assembled = [t for t in assembled if t["entity_count"] > 0]

    assembled.sort(key=lambda t: (-t["entity_count"], t["name"]))
    kept = assembled[:limit]
    return {
        "kg": kg_name,
        "types": kept,
        "total_types": len(assembled),
        "truncated": len(assembled) > len(kept),
        # Names only: cheap, and it keeps "this type EXISTS" true even when the
        # cap withholds its slots (the caller can drill in with ?type=).
        "omitted_type_names": [t["name"] for t in assembled[limit:]],
        # A `type=` filter that matched nothing answers with the names that DO
        # exist, so a typo reads as "you meant one of these" instead of the
        # "that type does not exist" conclusion this endpoint exists to prevent.
        "available_type_names": all_names if (wanted and not assembled) else [],
        "stats_source": stats_source,
        "coverage_note": _SCHEMA_COVERAGE_NOTE,
    }


@router.get("/kgs/{kg_name}/type-edges")
async def get_type_edges(
    kg_name: str,
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Undirected type→type edges for the Explorer overview graph.

    Derived from instance data (the precomputed stats graph, with a live-scan
    fallback) rather than the ontology's declared ``rdfs:range``. This keeps the
    overview consistent with the per-type detail view: a relationship that
    exists in the data but whose ontology range was never upgraded to a type
    URI (e.g. a predicate first seen as a primitive attribute) is now drawn in
    both places. Returns ``[{source, target, weight}]``.

    ADR 0004 (flag ``OMNIX_DRIFT_CONTROL``): when ON, the stats read also
    respects the support floor — a low-support drift edge (e.g.
    ``ManufacturerPartNumber.issuedby -> Retailer`` at 6% coverage) is excluded
    from the overview, while high-coverage and core-slot edges are kept. With
    the flag OFF the read is byte-identical to before (no filtering).
    """
    # ACT only when enabled AND not observe-only. Observe-only collects the
    # coverage distribution (via the recompute drift report) without touching the
    # overview, so the floor can be set from real data before it filters anything.
    drift_on = drift_control.drift_control_enabled() and not drift_control.observe_only()
    if drift_on:
        edges = await _read_edges_from_stats_drift(client, tenant.tenant_id, kg_name)
    else:
        edges = await _read_edges_from_stats(client, tenant.tenant_id, kg_name)
    if edges is None:
        # No materialized stats graph (legacy KG). The live scan must honor the
        # drift floor too when ACTING, else below-floor drift edges leak into the
        # overview for un-materialized KGs. Observe-only / flag OFF: unchanged scan.
        kg_graph = kg_graph_uri(tenant.tenant_id, kg_name)
        if drift_on:
            edges = await _live_edge_scan_drift(client, kg_graph, tenant.tenant_id)
        else:
            edges = await _live_edge_scan(client, kg_graph)
    return _dedupe_undirected(edges)


@router.post("/kgs/{kg_name}/recompute-stats")
async def recompute_stats(
    kg_name: str,
    tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Schedule a recompute of the precomputed type-stats for a KG.

    Returns immediately; the ~15s whole-KG scan runs in the background so it
    never hits the ALB response timeout.

    Mutating: it rewrites the per-KG stats graph (and schedules a whole-KG
    scan), so ``require_tenant_write`` refuses a ``reader`` member with 403
    (ONTA-451). Being allowlisted in the write-path convergence guard — it IS
    the stats action rather than a writer of instance data — says nothing about
    who may trigger it.
    """
    schedule_recompute(client, tenant.tenant_id, kg_name)
    return {"status": "scheduled", "kg": kg_name}


@router.get("/kgs/{kg_name}/drift-history")
async def get_drift_history(
    kg_name: str,
    limit: int = Query(100, ge=1, le=1000),
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Read the accumulated observe-only drift distribution for a KG (COG-57).

    Returns the persisted recompute snapshots (newest first), each with the run's
    effective floors, kept/quarantined totals, and the full per-relationship
    coverage distribution. This is the durable, queryable replacement for
    log-scraping CloudWatch — the data ADR 0004 sets ``OMNIX_DRIFT_FLOOR_COV``
    from. Raw distribution access only; histogram/floor analysis is done offline.
    """
    hist = _drift_history_graph_uri(tenant.tenant_id, kg_name)
    q = (
        f"SELECT ?snap ?recordedAt ?floorCov ?floorCount ?kept ?quarantined "
        f"?key ?coverage ?support ?sourceCount ?isCore ?pointKept\n"
        f"FROM <{hist}> WHERE {{\n"
        f"  ?snap <{_DRIFT_RECORDED_AT}> ?recordedAt ;\n"
        f"        <{_DRIFT_FLOOR_COV}> ?floorCov ;\n"
        f"        <{_DRIFT_FLOOR_COUNT}> ?floorCount ;\n"
        f"        <{_DRIFT_KEPT}> ?kept ;\n"
        f"        <{_DRIFT_QUARANTINED}> ?quarantined .\n"
        f"  OPTIONAL {{\n"
        f"    ?pt <{_DRIFT_POINT_OF}> ?snap ;\n"
        f"        <{_DRIFT_KEY}> ?key ;\n"
        f"        <{_DRIFT_COVERAGE}> ?coverage ;\n"
        f"        <{_DRIFT_SUPPORT}> ?support ;\n"
        f"        <{_DRIFT_SOURCE_COUNT}> ?sourceCount ;\n"
        f"        <{_DRIFT_IS_CORE}> ?isCore ;\n"
        f"        <{_DRIFT_POINT_KEPT}> ?pointKept .\n"
        f"  }}\n"
        f"}} ORDER BY DESC(?recordedAt)"
    )
    try:
        _, rows = parse_sparql_results(await client.query(q))
    except Exception:
        logger.warning("drift_history_read_failed", tenant=tenant.tenant_id, kg=kg_name, exc_info=True)
        return {"kg": kg_name, "snapshots": []}

    # Reassemble flat (snapshot × point) rows into nested snapshots, preserving
    # the recordedAt-desc order. A snapshot with no points (empty distribution)
    # still appears, with coverages == [].
    snapshots: dict[str, dict] = {}
    for r in rows:
        sid = r.get("snap", "")
        if not sid:
            continue
        snap = snapshots.get(sid)
        if snap is None:
            snap = {
                "recorded_at": r.get("recordedAt", ""),
                "floor_cov": _to_float(r.get("floorCov")),
                "floor_count": _to_int(r.get("floorCount")),
                "kept": _to_int(r.get("kept")),
                "quarantined": _to_int(r.get("quarantined")),
                "coverages": [],
            }
            snapshots[sid] = snap
        if r.get("key"):
            snap["coverages"].append({
                "key": r["key"],
                "coverage": _to_float(r.get("coverage")),
                "support": _to_int(r.get("support")),
                "source_count": _to_int(r.get("sourceCount")),
                "is_core_slot": r.get("isCore") == "true",
                "kept": r.get("pointKept") == "true",
            })
    return {"kg": kg_name, "snapshots": list(snapshots.values())[:limit]}


def _to_int(v: str | None) -> int:
    try:
        return int(v) if v not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def _to_float(v: str | None) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


@router.get("/kgs/{kg_name}/types/{type_name}/records")
async def get_type_records(
    kg_name: str,
    type_name: str,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Paged entity instances for the Explorer Data table (COG-100).

    Returns one page of instances of ``type_name``, ordered deterministically
    by entity URI (``ORDER BY ?e``) with keyset pagination via ``cursor`` (the
    last entity URI from the previous page).  For each entity the endpoint
    fetches all attribute values, excluding ``rdf:type`` and
    ``SYSTEM_PREDICATES``.  Attribute predicates are resolved to display names
    via the ontology (same ``attr_def`` query shape as ``get_type_summary``).
    The row ``name`` is the declared ``attrs/name`` attribute value when present
    (ingest stores the human-readable name there; ``rdfs:label`` holds the
    opaque entity-id slug), else ``rdfs:label``, else the entity-URI leaf.

    Response shape::

        {
            "columns": ["name", "<attr1>", ...],
            "rows": [{"id": "<uri>", "name": "...", "<attr1>": "...", ...}],
            "total": <int>,
            "next_cursor": "<uri>" | null,
        }

    Never errors on an empty/missing type; returns the empty sentinel instead.
    A type name that could not exist at all — one carrying a character no IRI may
    contain — is a different thing from a type with no rows, and is a 422
    (ONTA-425). The sentinel keeps covering every name that is merely absent.
    """
    require_valid_type_name(type_name)
    _EMPTY = {"columns": ["name"], "rows": [], "total": 0, "next_cursor": None}

    kg_graph = kg_graph_uri(tenant.tenant_id, kg_name)
    resolved = await _resolve_layered_type(client, tenant, type_name)
    if resolved is not None:
        t_uri, onto_graph, _layer = resolved
    else:
        onto_graph = tenant_graph_uri(tenant.tenant_id)
        t_uri = type_uri(type_name)

    # --- (1) attribute display-name map from ontology (same as get_type_summary) ---
    attr_def_sparql = (
        f"SELECT ?attr ?attrLabel ?range FROM <{onto_graph}> WHERE {{\n"
        f"  ?attr <{RDF_TYPE}> <{RDF_PROPERTY}> .\n"
        f"  ?attr <{RDFS}#domain> <{t_uri}> .\n"
        f"  ?attr <{RDFS}#label> ?attrLabel .\n"
        f"  OPTIONAL {{ ?attr <{RDFS}#range> ?range }}\n"
        f"}}"
    )

    # --- (2) entity page: keyset pagination ordered by ?e URI ---
    cursor_filter = f'  FILTER(STR(?e) > "{_esc(cursor)}")\n' if cursor else ""
    entities_sparql = (
        f"SELECT DISTINCT ?e FROM <{kg_graph}> WHERE {{\n"
        f"  ?e <{RDF_TYPE}> <{t_uri}> .\n"
        f"{_PRIMARY_TYPE_GUARD}"
        f"{cursor_filter}"
        f"}} ORDER BY ?e LIMIT {limit}"
    )

    # --- (3) total count: try stats graph first, fall back to COUNT query ---
    stats_graph = _stats_graph_uri(tenant.tenant_id, kg_name)
    total_sparql = (
        f"SELECT ?ec FROM <{stats_graph}> WHERE {{\n"
        f"  <{t_uri}> <{_STAT_ENTITY_COUNT}> ?ec\n"
        f"}}"
    )

    attr_def_raw, entity_raw, total_raw = await asyncio.gather(
        client.query(attr_def_sparql),
        client.query(entities_sparql),
        client.query(total_sparql),
    )

    _, attr_def_rows = parse_sparql_results(attr_def_raw)
    # Column budget.  Ontology-DECLARED attributes are always shown (they are the
    # type's schema — including enriched attrs like ``company`` that may sit on
    # only a handful of entities), so they are exempt from this cap.  The cap
    # only bounds the *extra* non-declared predicates discovered on the page, so
    # one rogue entity with dozens of ad-hoc predicates can't blow up the table.
    # Raised from 12 → 24 so a wide-but-legitimate declared schema isn't crowded
    # out and there's still headroom for a few observed-but-undeclared columns.
    _MAX_COLS = 24
    # Map ONTO pred URI → label.  We also need the instance predicate URI which
    # is `…/onto/<predLeaf>`.  Build both directions.  ``declared_display`` is the
    # ordered list of declared-attribute display labels that ALWAYS become
    # columns (deduped, alphabetical for a stable order — coverage isn't carried
    # by the attr-def query, so we don't pay an extra round-trip to rank by it).
    attr_label_by_onto: dict[str, str] = {}  # onto attr URI → label
    attr_label_by_pred: dict[str, str] = {}  # onto pred URI → label (instance triples)
    declared_display: list[str] = []
    declared_display_set: set[str] = set()
    for r in attr_def_rows:
        a_uri = r.get("attr", "")
        label = r.get("attrLabel") or a_uri.rstrip("/").split("/")[-1]
        if not a_uri:
            continue
        attr_label_by_onto[a_uri] = label
        # instance predicate URI: …/onto/<leaf>  where leaf is the last segment of
        # the attr URI (attrs/<leaf> → <leaf>)
        pred_leaf = a_uri.rstrip("/").split("/")[-1]
        inst_pred = ONTO_PRED_PREFIX + pred_leaf
        attr_label_by_pred[inst_pred] = label
        # ``name`` is rendered from rdfs:label as the first column; never let a
        # declared attribute literally named "name" duplicate it.
        if label != "name" and label not in declared_display_set:
            declared_display_set.add(label)
            declared_display.append(label)
    declared_display.sort()
    # LEGACY companion classification (ONTA-262): enrichment used to DECLARE the
    # per-attribute provenance companions (`<attr>_source_url` / `_provenance` /
    # `_verified_at`) as first-class schema, so on un-migrated KGs they'd become
    # always-shown declared columns. Classify them set-wise (`<base>_<suffix>`
    # with `<base>` present among declared labels + "name") and keep them out of
    # the table — they are metadata of the base attribute, not columns.
    legacy_companion_labels = _companion_leaves([*declared_display, "name"])
    if legacy_companion_labels:
        declared_display = [
            c for c in declared_display if c not in legacy_companion_labels
        ]
        declared_display_set -= legacy_companion_labels

    _, entity_rows = parse_sparql_results(entity_raw)
    entity_uris = [r.get("e", "") for r in entity_rows if r.get("e")]
    if not entity_uris:
        # No instances on this page — still need a total
        _, total_rows = parse_sparql_results(total_raw)
        total = _to_int(total_rows[0].get("ec") if total_rows else None)
        if not total:
            # Fall back to a COUNT query if stats absent
            count_sparql = (
                f"SELECT (COUNT(DISTINCT ?e) AS ?n) FROM <{kg_graph}> WHERE {{\n"
                f"  ?e <{RDF_TYPE}> <{t_uri}> .\n"
                f"{_PRIMARY_TYPE_GUARD}"
                f"}}"
            )
            _, cnt_rows = parse_sparql_results(await client.query(count_sparql))
            total = _to_int(cnt_rows[0].get("n") if cnt_rows else None)
        return {**_EMPTY, "total": total}

    # --- (4) fetch attribute values for the page entities ---
    uri_values = " ".join(f"<{u}>" for u in entity_uris)
    values_sparql = (
        f"SELECT ?e ?p ?o FROM <{kg_graph}> WHERE {{\n"
        f"  VALUES ?e {{ {uri_values} }}\n"
        f"  ?e ?p ?o .\n"
        f'  FILTER(?p != <{RDF_TYPE}>)\n'
        f"}}"
    )

    # Total count and attribute values fetched concurrently
    values_raw, total_raw2 = await asyncio.gather(
        client.query(values_sparql),
        client.query(total_sparql),
    )

    _, values_rows = parse_sparql_results(values_raw)

    # Determine total
    _, total_rows2 = parse_sparql_results(total_raw2)
    total = _to_int(total_rows2[0].get("ec") if total_rows2 else None)
    if not total:
        count_sparql = (
            f"SELECT (COUNT(DISTINCT ?e) AS ?n) FROM <{kg_graph}> WHERE {{\n"
            f"  ?e <{RDF_TYPE}> <{t_uri}> .\n"
            f"{_PRIMARY_TYPE_GUARD}"
            f"}}"
        )
        _, cnt_rows = parse_sparql_results(await client.query(count_sparql))
        total = _to_int(cnt_rows[0].get("n") if cnt_rows else None)

    # --- (5) assemble rows ---
    # Collect per-entity: label + attribute values keyed by display name.
    # ``_name_attr`` captures the instance value of the declared "name" attribute
    # (``…/onto/name`` ← ``attrs/name``): these entities carry their real,
    # human-readable name THERE. ``rdfs:label`` holds the opaque entity-id slug
    # (ingest writes ``(entity_uri, rdfs:label, entity.id)``), so attrs/name is
    # the PREFERRED name source — rdfs:label is only the fallback below it. We
    # don't render attrs/name as a SEPARATE column (it would duplicate the first
    # "name" column); its value feeds the first column instead.
    LABEL_PRED = f"{RDFS}#label"
    entity_data: dict[str, dict] = {
        u: {"_label": None, "_name_attr": None, "_attrs": {}} for u in entity_uris
    }
    # Column order: declared attributes ALWAYS first (schema columns, not subject
    # to the frequency cap), then any extra non-declared predicates observed on
    # the page — bounded by _MAX_COLS so a stray entity can't inflate the table.
    col_display: list[str] = list(declared_display)
    col_set: set[str] = set(declared_display)
    extra_count = 0

    def _display_of(p_uri: str) -> str:
        # Resolve display name: check attr_label_by_pred (instance pred) first,
        # then attr_label_by_onto (onto attr URI), then fall back to the URI leaf.
        return (
            attr_label_by_pred.get(p_uri)
            or attr_label_by_onto.get(p_uri)
            or p_uri.rstrip("/").split("/")[-1]
        )

    # LEGACY companion classification for OBSERVED (non-declared) predicates on
    # this page (ONTA-262): discovery used to stamp companions as ordinary
    # attribute-namespace instance triples, so an un-migrated KG surfaces them
    # here as extra columns. Classify set-wise over every literal-valued display
    # name observed on the page plus the declared labels (a companion's base may
    # be declared while the companion is only observed, or vice versa).
    observed_literal_displays = {
        _display_of(r.get("p", ""))
        for r in values_rows
        if r.get("p", "") not in (LABEL_PRED, RDF_TYPE)
        and not r.get("o", "").startswith(ENTITY_URI_PREFIX)
    }
    observed_companions = _companion_leaves(
        observed_literal_displays | declared_display_set | {"name"}
    )

    for r in values_rows:
        e_uri = r.get("e", "")
        p_uri = r.get("p", "")
        o_val = r.get("o", "")
        if not e_uri or e_uri not in entity_data:
            continue
        if p_uri == LABEL_PRED:
            entity_data[e_uri]["_label"] = o_val
            continue
        # Internal/housekeeping predicates (onto/batch_id, er/blockKey,
        # er/erSignal_*, rdf*/rdfs*) must never become data-table columns — same
        # filter the summary panel uses. rdfs:label is intercepted above (it is
        # the row name); the real attrs/name predicate (…/onto/name) is NOT
        # internal and still flows through to the name-precedence logic below.
        # An entity-valued object marks a relationship, exempt from the
        # literal-only housekeeping markers (FIX 2) so a real `onto/source` edge
        # to an entity isn't hidden from the table.
        is_rel = o_val.startswith(ENTITY_URI_PREFIX)
        if _is_internal_predicate(p_uri, is_relationship=is_rel):
            continue
        display = _display_of(p_uri)
        # Legacy per-attribute provenance companions are metadata, not columns
        # (literal-valued only — a relationship can never be misclassified).
        if not is_rel and display in observed_companions:
            continue
        # "name" is rendered in the first column; a declared/instance predicate
        # named "name" (e.g. …/onto/name ← attrs/name) must not become a SEPARATE
        # column. But its value is the entity's real, human-readable name —
        # capture it so the first column can PREFER it over the slug-shaped
        # rdfs:label.
        if display == "name":
            if entity_data[e_uri]["_name_attr"] is None:
                entity_data[e_uri]["_name_attr"] = o_val
            continue
        if display not in col_set and extra_count < _MAX_COLS:
            col_set.add(display)
            col_display.append(display)
            extra_count += 1
        entity_data[e_uri]["_attrs"][display] = o_val

    columns = ["name"] + col_display
    rows = []
    for u in entity_uris:
        d = entity_data[u]
        # Name precedence: the declared "name" attribute's value (attrs/name)
        # FIRST, else rdfs:label, else the URI slug. Ingest writes
        # `(entity_uri, rdfs:label, entity.id)` — i.e. rdfs:label IS the opaque
        # entity-id slug — while the human-readable name lives in attrs/name. So
        # attrs/name must win over rdfs:label, otherwise the row degrades to the
        # slug (e.g. "4akvVWgTcS") even when a real name is present.
        label = d["_name_attr"] or d["_label"] or u.rstrip("/").split("/")[-1]
        row: dict = {"id": u, "name": label}
        for col in col_display:
            # Declared columns with no value on this entity render blank.
            row[col] = d["_attrs"].get(col, "")
        rows.append(row)

    next_cursor = entity_uris[-1] if len(entity_uris) == limit else None

    return {
        "columns": columns,
        "rows": rows,
        "total": total,
        "next_cursor": next_cursor,
    }


@router.post("/kgs/{kg_name}/er-rebuild")
async def er_rebuild(
    kg_name: str,
    tenant: TenantContext = Depends(require_tenant_write),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Second-pass entity resolution (MOE-22): collapse intra-batch fragments.

    Mutating: a real ER merge (``rewrite_subject``) plus post-write housekeeping,
    so ``require_tenant_write`` refuses a ``reader`` member with 403 (ONTA-451).

    Re-runs ER over the already-ingested KG so same-entity rows that couldn't
    see each other's index triples mid-batch now merge. Runs synchronously and
    returns per-type before/after counts (the merge volume is modest). Stale
    type-stats are recomputed in the background afterward so the Explorer
    reflects the new counts without blocking this response.
    """
    from cograph_client.resolver.er.rebuild import rebuild_kg

    instance_graph = kg_graph_uri(tenant.tenant_id, kg_name)
    report = await rebuild_kg(client, instance_graph)
    # Shared post-write housekeeping path (kg_writer.refresh_after_write):
    # merge changed counts, not the type schema → affected_types=() (no
    # re-embed; still cache-invalidates + recomputes Explorer type-stats).
    await refresh_after_write(
        client, tenant_id=tenant.tenant_id, kg_name=kg_name, affected_types=()
    )
    return {"status": "complete", "kg": kg_name, **report}


@router.get("/search")
async def search_explorer(
    kg_name: str = Query(..., alias="kg"),
    q: str = Query(..., min_length=1),
    kind: str = Query("type", pattern="^(type|attr)$"),
    tenant: TenantContext = Depends(get_tenant),
    client: NeptuneClient = Depends(get_neptune_client),
):
    """Search types or attributes by name substring.

    kind=type  — returns matching type names + their instance counts.
    kind=attr  — returns every type that has an attribute matching the query.

    Ontology side is layered (ONTA-397): Public/Enhanced declarations are
    visible under the caller's LayerStack; same-name collisions collapse by
    first-visible-layer-wins when assembling the result set.
    """
    stack = layer_stack_for(tenant)
    from_clause = _from_graphs(stack.visible_graph_uris())
    kg_graph = kg_graph_uri(tenant.tenant_id, kg_name)
    q_lower = q.lower()

    if kind == "type":
        # Prefer the layered resolver so shadowing is explicit and one name
        # never appears twice across tenant + Public.
        types_by_layer = await fetch_types_by_layer(client, stack)
        all_names: set[str] = set()
        for layer_map in types_by_layer.values():
            all_names.update(layer_map)
        matched = sorted(n for n in all_names if q_lower in n.lower())

        results = []
        for type_name in matched:
            # Fail SOFT here, unlike the single-type routes above (ONTA-425).
            # These names come back from the ONTOLOGY, not from the caller, and
            # this loop is an ENUMERATION: letting `layer_type_uri` raise on one
            # corrupt stored name would 422 the whole search for every other
            # type, the all-or-nothing failure onta-oss#274 had to fix for KG
            # names. Skipping keeps the corruption observable in logs (and the
            # bad type genuinely unqueryable) without taking the listing down.
            if skip_invalid_type_name(type_name, "explore_search"):
                continue
            resolved = stack.resolve_type(type_name, types_by_layer)
            if resolved is None:
                continue
            layer, _ = resolved
            t_uri = layer_type_uri(layer, type_name)
            # Also count instances typed under the bare tenant URI (historical
            # writes) so a Public type with tenant-namespace instances is not
            # reported as empty solely because of the namespace split.
            tenant_t_uri = type_uri(type_name)
            count_uris = [t_uri] if t_uri == tenant_t_uri else [t_uri, tenant_t_uri]
            entity_count = 0
            for cu in count_uris:
                count_sparql = (
                    f"SELECT (COUNT(DISTINCT ?e) AS ?n) FROM <{kg_graph}> WHERE {{\n"
                    f"  ?e <{RDF_TYPE}> <{cu}> .\n"
                    f"  FILTER NOT EXISTS {{\n"
                    f"    ?e <{RDF_TYPE}> ?type2 .\n"
                    f'    FILTER(STRSTARTS(STR(?type2), "{TYPE_URI_PREFIX}") '
                    f'&& STR(?type2) < "{cu}")\n'
                    f"  }}\n"
                    f"}}"
                )
                try:
                    _, count_rows = parse_sparql_results(await client.query(count_sparql))
                    entity_count += int(count_rows[0].get("n", "0")) if count_rows else 0
                except Exception:
                    pass
            results.append({
                "name": type_name,
                "entity_count": entity_count,
                "layer": layer.value,
            })
        return results

    # kind == "attr" — union of visible layer graphs; dedupe by (attr, type name).
    sparql = (
        f"SELECT DISTINCT ?attrLabel ?type ?typeLabel {from_clause} WHERE {{\n"
        f"  ?attr <{RDF_TYPE}> <http://www.w3.org/1999/02/22-rdf-syntax-ns#Property> .\n"
        f"  ?attr <{RDFS}#label> ?attrLabel .\n"
        f"  ?attr <{RDFS}#domain> ?type .\n"
        f"  ?type <{RDFS}#label> ?typeLabel .\n"
        f'  FILTER(CONTAINS(LCASE(STR(?attrLabel)), "{_esc(q_lower)}"))\n'
        f"}}"
    )
    _, rows = parse_sparql_results(await client.query(sparql))
    seen: set[tuple[str, str]] = set()
    out = []
    for r in rows:
        attr_name = r.get("attrLabel", "")
        type_name = r.get("typeLabel", "")
        if not attr_name or not type_name:
            continue
        key = (attr_name, type_name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"attr_name": attr_name, "type_name": type_name})
    return out


def _esc(s: str) -> str:
    """Escape a value for a SPARQL string literal via the ONE shared escaper.

    Used for the ``search`` needle AND the keyset-pagination ``cursor``. The old
    local copy escaped only ``\\`` and ``"``, so an interior newline (a pasted
    multi-line search box, a mangled cursor) produced an unterminated literal and
    a store-side parse error surfacing as a 500 (ONTA-416). Delegating means the
    two callers get the hardened coverage and can never re-diverge.
    """
    return sparql_string_literal(s)
