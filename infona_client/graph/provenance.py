"""Per-fact provenance substrate (ADR 0002 §4).

Every attribute assertion can carry provenance — source, timestamp,
confidence — queryable later for conflict resolution, explainability,
and wholesale undo of a bad source.

Encoding decision (Neptune has NO RDF-star, so a triple cannot be
annotated in place): a dedicated **companion provenance named graph** per
data graph (``<data-graph>/provenance``) holding one statement-metadata
node per (fact, source) assertion. Chosen over per-source named graphs
because it composes with the existing single-data-graph layout — instance
triples stay exactly where they are, and "undo a source" / conflict
resolution become SELECTs over one graph instead of a graph-per-source
fan-out.

Keying:
- ``statement_id = sha1(s|p|o)`` identifies the *fact* (over the raw
  strings as written to Neptune, typed-literal convention included), so
  all assertions of the same fact group trivially.
- The metadata node is keyed by ``sha1(s|p|o|source)`` — one node per
  fact *per source* — so two sources asserting the same fact each carry
  their own (source, timestamp, confidence) without cross-products on
  read, and dropping a source is a single filtered DELETE.

For a fact (s, p, o) asserted by ``source`` the provenance graph holds::

    <https://graph.infona.ai/prov/stmt/{sha1(s|p|o|source)}>
        prov:subject    <s> ;
        prov:predicate  <p> ;
        prov:object     o ;                       # literal or URI, as written
        prov:statement  "{sha1(s|p|o)}" ;
        prov:source     "crm_export.csv" ;
        prov:confidence "1.0"^^xsd:float ;
        prov:timestamp  "2026-06-09T00:00:00+00:00"^^xsd:dateTime ;
        prov:graph      <data graph the fact lives in> .

Triples are idempotent on Neptune: re-ingesting the same fact from the
same source rewrites the same node (a refreshed timestamp accumulates as
an additional literal — last-write-wins policies resolve over max).

Implementation lives in sibling ``provenance_*.py`` modules. Every
previously importable name is re-exported here.
"""

from __future__ import annotations

from infona_client.graph.provenance_assert import (  # noqa: F401 — public re-exports
    build_provenance_triples,
)
from infona_client.graph.provenance_events import (  # noqa: F401 — public re-exports
    _event_common,
    build_conflict_loss_triples,
    build_retraction_triples,
    build_rewrite_triples,
    build_supersession_triples,
    build_tombstone_triples,
)
from infona_client.graph.provenance_lineage import (  # noqa: F401 — public re-exports
    MergeLineage,
    Triple,
    _lineage_fact_uri,
    _term_from_binding,
    build_merge_lineage_triples,
    build_split_triples,
    fetch_merge_lineage,
    merge_event_uri,
    merge_lineage_query,
)
from infona_client.graph.provenance_query import (  # noqa: F401 — public re-exports
    ProvenanceRecord,
    _predicate_leaf,
    _strip_xsd,
    fetch_provenance,
    fetch_provenance_from_store,
    parse_provenance_records,
    provenance_query,
)
from infona_client.graph.provenance_uris import (  # noqa: F401 — public re-exports
    ATTR_META_NS,
    ATTR_META_SUFFIXES,
    EVENT_CONFLICT_LOSS,
    EVENT_MERGE,
    EVENT_RETRACT,
    EVENT_REWRITE,
    EVENT_SPLIT,
    EVENT_SUPERSEDE,
    EVENT_TOMBSTONE,
    IRI_BASE,
    LIN_O,
    LIN_OF_MERGE,
    LIN_ORIGIN,
    LIN_P,
    LIN_S,
    LINEAGE_NS,
    ORIGIN_CANONICAL,
    ORIGIN_MERGED,
    PROV_AFFECTED_TYPE,
    PROV_AUTHORITY,
    PROV_CONFIDENCE,
    PROV_EVENT,
    PROV_GRAPH,
    PROV_NS,
    PROV_OBJECT,
    PROV_PREDICATE,
    PROV_REASON,
    PROV_REWRITTEN_TO,
    PROV_SOURCE,
    PROV_STATEMENT,
    PROV_SUBJECT,
    PROV_SUPERSEDED_BY,
    PROV_TIMESTAMP,
    PROV_VALID_TO,
    SURFACE_FORM_SUFFIX,
    TRUTH_VERDICT_SUFFIX,
    TYPE_URI_PREFIX,
    _ATTRS_INFIX,
    _TYPES_PREFIX,
    _XSD,
    _as_iso,
    _assertion_uri,
    _event_uri,
    _host,
    attr_provenance_companion_uri,
    build_attribute_provenance_companions,
    build_surface_form_companion,
    build_truth_verdict_companion,
    companion_predicate_for,
    fetch_truth_verdict,
    legacy_attr_companion_uri,
    provenance_graph_uri,
    statement_id,
    truth_verdict_query,
)

__all__ = [
    "ATTR_META_NS",
    "ATTR_META_SUFFIXES",
    "EVENT_CONFLICT_LOSS",
    "EVENT_MERGE",
    "EVENT_RETRACT",
    "EVENT_REWRITE",
    "EVENT_SPLIT",
    "EVENT_SUPERSEDE",
    "EVENT_TOMBSTONE",
    "IRI_BASE",
    "LIN_O",
    "LIN_OF_MERGE",
    "LIN_ORIGIN",
    "LIN_P",
    "LIN_S",
    "LINEAGE_NS",
    "MergeLineage",
    "ORIGIN_CANONICAL",
    "ORIGIN_MERGED",
    "PROV_AFFECTED_TYPE",
    "PROV_AUTHORITY",
    "PROV_CONFIDENCE",
    "PROV_EVENT",
    "PROV_GRAPH",
    "PROV_NS",
    "PROV_OBJECT",
    "PROV_PREDICATE",
    "PROV_REASON",
    "PROV_REWRITTEN_TO",
    "PROV_SOURCE",
    "PROV_STATEMENT",
    "PROV_SUBJECT",
    "PROV_SUPERSEDED_BY",
    "PROV_TIMESTAMP",
    "PROV_VALID_TO",
    "ProvenanceRecord",
    "SURFACE_FORM_SUFFIX",
    "TRUTH_VERDICT_SUFFIX",
    "Triple",
    "TYPE_URI_PREFIX",
    "attr_provenance_companion_uri",
    "build_attribute_provenance_companions",
    "build_conflict_loss_triples",
    "build_merge_lineage_triples",
    "build_provenance_triples",
    "build_retraction_triples",
    "build_rewrite_triples",
    "build_split_triples",
    "build_supersession_triples",
    "build_surface_form_companion",
    "build_tombstone_triples",
    "build_truth_verdict_companion",
    "companion_predicate_for",
    "fetch_merge_lineage",
    "fetch_provenance",
    "fetch_provenance_from_store",
    "fetch_truth_verdict",
    "legacy_attr_companion_uri",
    "merge_event_uri",
    "merge_lineage_query",
    "parse_provenance_records",
    "provenance_graph_uri",
    "provenance_query",
    "statement_id",
    "truth_verdict_query",
]
