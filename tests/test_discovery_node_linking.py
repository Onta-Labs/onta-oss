"""Discovery node-linking — when an attribute's declared range is an EXISTING type
(e.g. ``Physician.located_in -> City``), discovery's promotion branch must mint the
target as a FIRST-CLASS node: the edge PLUS the node's ``rdf:type`` + ``rdfs:label``.

Regression: ``schema_resolver``'s promotion branch used to write ONLY the edge, so
the promoted ``City`` node was bare — untyped, unlabelled, invisible to a
"list all Cities" query — even though the edge pointed at it. Enrichment already did
this correctly (``executor._instance_triples_for_value``); this is the discovery
mirror, minting the IDENTICAL shared node via ``graph.ontology_queries.entity_uri``.

This path is low-frequency through the live LLM (the soft-extraction prompt prefers
emitting real entities over promotable attributes), so the scenario is CONSTRUCTED
directly — seed a ``City`` type + a ``Physician.located_in -> City`` attribute, then
drive ``_resolve_and_insert_entity`` so the promotion branch fires — rather than
relying on the decomp harness / a live extraction.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from cograph_client.enrichment.executor import EnrichmentExecutor
from cograph_client.graph.ontology_queries import entity_uri, type_uri
from cograph_client.resolver.attribute_resolver import AttributeSchema
from cograph_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    IngestResult,
)
from cograph_client.resolver.schema_resolver import SchemaResolver
from cograph_client.resolver.verdict_cache import JsonVerdictCache

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

PHYS_URI = entity_uri("Physician", "p1")
# Discovery's promotion branch writes the relationship INSTANCE edge on onto/<leaf>
# — the only predicate the NL planner queries a type-ranged attribute on. (attrs/<leaf>
# is the property DECLARATION; an instance edge written there is invisible to NL — the
# #123 bug, fixed for enrichment in #126 and for discovery's promotion branch here.)
LOCATED_IN = "https://cograph.tech/onto/located_in"


async def _drive_promotion(tmp_path):
    """Fire ``schema_resolver``'s promotion branch and return
    ``(collected_triples, IngestResult)``.

    The entity is a ``Physician`` whose ``located_in`` attribute is DECLARED with
    range ``City`` (an existing type) and carries a plain-LABEL value ("San
    Francisco"). ``resolve_attribute`` reuses the declared ``City`` range, so the
    branch ``resolved.datatype not in PRIMITIVE_TYPES and … in existing_types``
    fires. ``_collect_triples`` captures the batch so no Neptune write is needed.
    """
    resolver = SchemaResolver(AsyncMock(), "fake-key", JsonVerdictCache(tmp_path / "c.json"))
    result = IngestResult()
    collected: list[tuple[str, str, str]] = []
    await resolver._resolve_and_insert_entity(
        entity=ExtractedEntity(
            type_name="Physician",
            id="p1",
            attributes=[
                ExtractedAttribute(name="located_in", value="San Francisco", datatype="City"),
            ],
        ),
        resolved_type="Physician",
        entity_uri=PHYS_URI,
        is_duplicate=False,
        graph_uri="https://omnix.dev/graphs/test",
        existing_types={"Physician": "", "City": ""},
        existing_attrs={
            "Physician": {"located_in": AttributeSchema("located_in", "City")},
            "City": {},
        },
        source="test",
        result=result,
        _collect_triples=collected,
    )
    return collected, result


async def test_promoted_target_is_a_first_class_node(tmp_path):
    """The fix: the promoted node gets its edge AND its rdf:type + rdfs:label."""
    collected, _ = await _drive_promotion(tmp_path)
    target = entity_uri("City", "San Francisco")

    assert (PHYS_URI, LOCATED_IN, target) in collected              # edge -> the node
    assert (target, RDF_TYPE, type_uri("City")) in collected        # THE FIX: typed City
    assert (target, RDFS_LABEL, "San Francisco") in collected       # THE FIX: labelled
    # The bug: never a bare node in a node-valued slot, never a dangling raw string.
    assert (PHYS_URI, LOCATED_IN, "San Francisco") not in collected


async def test_promotion_records_node_target_type_for_refresh(tmp_path):
    """Part 3: the minted node's TYPE is surfaced so post-write housekeeping
    re-embeds / re-stats ``City`` NOW — not only on ``City``'s next write.

    Before the fix, ``affected_types`` carried only the SUBJECT type, so the newly
    minted ``City`` node stayed stale. It now flows through ``node_target_types`` →
    ``affected_types()`` → the ingest route's ``refresh_after_write``."""
    _, result = await _drive_promotion(tmp_path)
    assert "City" in result.node_target_types
    assert "City" in result.affected_types()


async def test_discovery_and_enrichment_emit_the_identical_fact(tmp_path):
    """Cross-rail parity (the whole point): for the SAME fact —
    ``Physician.located_in = "San Francisco"`` with range ``City`` — discovery and
    enrichment now emit the IDENTICAL triples: the relationship edge on
    ``onto/located_in`` PLUS the target node's rdf:type + rdfs:label, all minted
    through the shared ``entity_uri``. Node identity AND the edge predicate converged
    (the predicate via this fix — discovery previously wrote the edge on
    ``attrs/<leaf>``, invisible to NL), so the fact is ONE shared, NL-queryable node
    across both rails, never a duplicate and never an unreachable edge."""
    collected, _ = await _drive_promotion(tmp_path)
    enrichment = EnrichmentExecutor._instance_triples_for_value(
        PHYS_URI, "Physician", "located_in", "San Francisco", "City",
    )
    # FULL parity now (not just the node materialization): every triple enrichment
    # emits for this fact — the onto/<leaf> edge AND the node's rdf:type/rdfs:label —
    # is emitted verbatim by discovery too.
    assert enrichment, "enrichment sanity: node-linking must emit the fact"
    for triple in enrichment:
        assert triple in collected, f"discovery is missing enrichment's triple {triple}"
    # And specifically the relationship edge is on onto/<leaf>, identical on both rails.
    target = entity_uri("City", "San Francisco")
    assert (PHYS_URI, LOCATED_IN, target) in collected
    assert (PHYS_URI, LOCATED_IN, target) in enrichment


def test_ingest_result_affected_types_unions_all_three_sources():
    """``IngestResult.affected_types()`` — the single set both ingest routes hand to
    ``refresh_after_write`` — unions created types + the SUBJECT type of each added
    attribute + the node-TARGET types (deduped; entries without a ``.`` skipped)."""
    r = IngestResult(
        types_created=["Physician"],
        attributes_added=["Physician.specialty", "bare_no_dot"],
        node_target_types=["City", "City"],
    )
    assert r.affected_types() == {"Physician", "City"}


@patch("cograph_client.api.routes.ingest.refresh_after_write", new_callable=AsyncMock)
@patch("cograph_client.api.routes.ingest.SchemaResolver")
def test_ingest_route_refreshes_node_target_types(
    mock_resolver_cls, mock_refresh, client, auth_headers
):
    """Route wiring (Part 3): when discovery mints a ``City`` node for a node-valued
    fill, ``City`` rides ``node_target_types`` into the ``/ingest`` route's
    ``refresh_after_write`` (via ``affected_types()``), so the fresh node is
    re-embedded / re-stat'd now — not only on ``City``'s next write."""
    inst = AsyncMock()
    inst.ingest.return_value = IngestResult(
        entities_extracted=1,
        entities_resolved=1,
        triples_inserted=3,
        types_created=["Physician"],
        attributes_added=["Physician.located_in"],
        node_target_types=["City"],
    )
    mock_resolver_cls.return_value = inst

    resp = client.post(
        "/graphs/test-tenant/ingest",
        json={"content": "a physician located in San Francisco", "source": "t", "kg_name": "k"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert mock_refresh.await_count == 1
    # Subject type AND the minted target-node type are both refreshed.
    assert mock_refresh.await_args.kwargs["affected_types"] == {"Physician", "City"}
