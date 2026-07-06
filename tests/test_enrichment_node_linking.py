"""Enrichment node-linking — fill a node-valued attribute with a real NODE, not a
dangling string (cross-rail consistency with discovery).

When the ontology declares an attribute's range as a TYPE (e.g.
``Physician.located_in -> City``), enrichment used to write the raw enriched string
("San Francisco") straight into the relationship slot — a dangling, unresolvable
edge, diverging from discovery which mints a City node for the identical fact. The
fix resolves the label to the SAME canonical ``entities/<Type>/<safe_id>`` URI
ingestion uses and creates/types the node, so the fact is ONE shared node across
both rails.
"""
from __future__ import annotations

from cograph_client.enrichment.executor import (
    RDF_TYPE,
    RDFS_LABEL,
    EnrichmentExecutor,
)
from cograph_client.graph.ontology_queries import _safe_id, entity_uri, type_uri

_ITV = EnrichmentExecutor._instance_triples_for_value
PHYS = "https://cograph.tech/entities/Physician/p1"
# A relationship INSTANCE edge lives on onto/<leaf> — the form the NL planner
# queries (the attrs/<leaf> attribute predicate is the LITERAL / declaration form).
ONTO_LOCATED_IN = "https://cograph.tech/onto/located_in"
ATTRS_LOCATED_IN = "https://cograph.tech/types/Physician/attrs/located_in"


def test_primitive_value_stays_a_typed_literal():
    # literals stay on the attrs/<leaf> attribute predicate (unchanged).
    out = _ITV(PHYS, "Physician", "age", "42", "integer")
    assert out == [
        (PHYS, "https://cograph.tech/types/Physician/attrs/age",
         "42^^http://www.w3.org/2001/XMLSchema#integer")
    ]


def test_node_range_label_creates_and_links_a_node():
    out = _ITV(PHYS, "Physician", "located_in", "San Francisco", "City")
    target = entity_uri("City", "San Francisco")
    assert (PHYS, ONTO_LOCATED_IN, target) in out            # edge -> node, on onto/<leaf>
    assert (target, RDF_TYPE, type_uri("City")) in out       # node typed City
    assert (target, RDFS_LABEL, "San Francisco") in out      # node keeps its label
    # never a dangling raw-string object in a node-valued slot
    assert (PHYS, ONTO_LOCATED_IN, "San Francisco") not in out
    # and the edge is NOT on the attrs/<leaf> attribute predicate (NL-invisible there)
    assert not any(t[1] == ATTRS_LOCATED_IN for t in out)


def test_node_uri_matches_discovery_scheme():
    """The whole point: enrichment must mint the SAME URI discovery does, so the
    identical real-world thing is one SHARED node (idempotent), not a duplicate.
    Both rails mint via the shared entity_uri (graph/ontology_queries), the SAME
    primitive discovery keys its entity URIs with (schema_resolver promotion branch)."""
    out = _ITV(PHYS, "Physician", "located_in", "San Francisco", "City")
    edge = next(t for t in out if t[1] == ONTO_LOCATED_IN)
    assert edge[2] == entity_uri("City", "San Francisco")
    # …and that shared minter is exactly prefix + _safe_id(value).
    assert edge[2] == f"https://cograph.tech/entities/City/{_safe_id('San Francisco')}"


def test_already_resolved_iri_is_written_directly():
    """A value that already IS an entity IRI (e.g. a premium adapter that resolved
    it) keeps the direct-edge behavior — on onto/<leaf>, no re-wrapping."""
    iri = "https://cograph.tech/entities/City/SF"
    out = _ITV(PHYS, "Physician", "located_in", iri, "City")
    assert out == [(PHYS, ONTO_LOCATED_IN, iri)]
