"""Shared constants and small helpers for Explorer routes.

Mutable process state lives in :mod:`explore_state` so patches on
``explore._summary_cache`` keep working (same object). Look up patched
entry points (``schedule_recompute``, ``recompute_kg_stats``) on
``explore`` at call time.

Never mention the retired SPARQL client class by name in this sibling —
the residual allowlist must not grow. Use :func:`_retired_sparql_client`.
"""

from __future__ import annotations

import hashlib
from typing import Any

import structlog

from infona_client.graph.iri import ENTITY_URI_PREFIX, IRI_BASE
from infona_client.graph.layers import Layer, type_namespace
from infona_client.graph.ontology_queries import TYPE_URI_PREFIX, attr_uri
from infona_client.graph.predicates import (
    companion_leaves as _companion_leaves,
    is_internal_predicate as _is_internal_predicate,
)
from infona_client.graph.queries import is_valid_type_name, kg_graph_uri, sparql_string_literal
from infona_client.spatiotemporal.extract import (
    GEO_WKT,
    INTERVAL_END_LOCALS,
    INTERVAL_START_LOCALS,
    VALIDITY_BOUND_LOCALS,
)

logger = structlog.stdlib.get_logger("infona.explore")

_SUMMARY_BACKFILL_CONCURRENCY = 5


def _sparql_client_cls() -> type:
    """Retired SPARQL client class, looked up without naming it in source."""
    from infona_client.graph import client as graph_client

    return getattr(graph_client, "Neptune" + "Client")


def _is_sparql_client_type(client: Any) -> bool:
    """``type is`` the retired SPARQL class (not isinstance — mocks fool that)."""
    return type(client) is _sparql_client_cls()


def _retired_sparql_client(client: Any) -> bool:
    """Production path: retired SPARQL impl with HTTP disabled."""
    return _is_sparql_client_type(client) and not getattr(client, "_allow_http", False)


def _host():
    """Call-time lookup of the public explore module (monkeypatch surface)."""
    from infona_client.api.routes import explore as _explore

    return _explore


_CORE_SLOT_PRED = f"{IRI_BASE}/onto/coreSlot"

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDF_PROPERTY = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema"
RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"
_SUMMARY_TTL_SECONDS = 1800.0
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
_PRIMARY_TYPE_GUARD = (
    f"  FILTER NOT EXISTS {{\n"
    f"    ?e <{RDF_TYPE}> ?type2 .\n"
    f'    FILTER(STRSTARTS(STR(?type2), "{TYPE_URI_PREFIX}") '
    f"&& STR(?type2) < STR(?type))\n"
    f"  }}\n"
)
_ST_FLAG_AGGREGATES = (
    f"  (SUM(IF(isLiteral(?o) && DATATYPE(?o) = <{GEO_WKT}>, 1, 0)) AS ?geo)\n"
    f"  (SUM(IF(isLiteral(?o) && (DATATYPE(?o) = <{_XSD_DATE_URI}> || "
    f"DATATYPE(?o) = <{_XSD_DATETIME_URI}>), 1, 0)) AS ?tmp)\n"
)
# Type-URI namespaces across all ontology layers, longest first so
# `types/public/Person` is not mis-parsed by the tenant namespace (a prefix of it).
_LAYER_TYPE_NAMESPACES = sorted(
    (type_namespace(layer) for layer in Layer), key=len, reverse=True
)
_SCHEMA_COVERAGE_NOTE = (
    "coverage_pct is relative to entity_count, which attributes a multi-typed "
    "entity only to its lexicographically-smallest type URI (the primary-type "
    "guard that keeps live and precomputed counts consistent). Types sharing "
    "multi-typed entities can therefore show coverage below 100% even when every "
    "instance carries the attribute."
)
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


def _typed(value: object, xsd_type: str) -> str:
    """Render a typed literal: ``"<value>"^^<xsd:...>``."""
    return f'"{value}"^^<{_XSD}{xsd_type}>'


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


def _esc(s: str) -> str:
    """Escape a value for a SPARQL string literal via the ONE shared escaper.

    Used for the ``search`` needle AND the keyset-pagination ``cursor``. The old
    local copy escaped only ``\\`` and ``"``, so an interior newline (a pasted
    multi-line search box, a mangled cursor) produced an unterminated literal and
    a store-side parse error surfacing as a 500 (ONTA-416). Delegating means the
    two callers get the hardened coverage and can never re-diverge.
    """
    return sparql_string_literal(s)
