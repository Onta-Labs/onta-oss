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
from cograph_client.graph.ontology_queries import type_uri
from cograph_client.resolver.schema_resolver import _safe_id

_ITV = EnrichmentExecutor._instance_triples_for_value
PHYS = "https://cograph.tech/entities/Physician/p1"
LOCATED_IN = "https://cograph.tech/types/Physician/attrs/located_in"


def test_primitive_value_stays_a_typed_literal():
    out = _ITV(PHYS, "Physician", "age", "42", "integer")
    assert out == [
        (PHYS, "https://cograph.tech/types/Physician/attrs/age",
         "42^^http://www.w3.org/2001/XMLSchema#integer")
    ]


def test_node_range_label_creates_and_links_a_node():
    out = _ITV(PHYS, "Physician", "located_in", "San Francisco", "City")
    target = f"https://cograph.tech/entities/City/{_safe_id('San Francisco')}"
    assert (PHYS, LOCATED_IN, target) in out                 # edge -> the node
    assert (target, RDF_TYPE, type_uri("City")) in out       # node typed City
    assert (target, RDFS_LABEL, "San Francisco") in out      # node keeps its label
    # the bug: never a dangling raw-string object in a node-valued slot
    assert (PHYS, LOCATED_IN, "San Francisco") not in out


def test_node_uri_matches_discovery_scheme():
    """The whole point: enrichment must mint the SAME URI discovery does, so the
    identical real-world thing is one SHARED node (idempotent), not a duplicate.
    Discovery mints entities/<datatype>/<_safe_id(value)> (schema_resolver ~L2318)."""
    out = _ITV(PHYS, "Physician", "located_in", "San Francisco", "City")
    edge = next(t for t in out if t[1] == LOCATED_IN)
    assert edge[2] == f"https://cograph.tech/entities/City/{_safe_id('San Francisco')}"


def test_already_resolved_iri_is_written_directly():
    """A value that already IS an entity IRI (e.g. a premium adapter that resolved
    it) keeps the legacy direct-edge behavior — no re-wrapping, no phantom node."""
    iri = "https://cograph.tech/entities/City/SF"
    out = _ITV(PHYS, "Physician", "located_in", iri, "City")
    assert out == [(PHYS, LOCATED_IN, iri)]
