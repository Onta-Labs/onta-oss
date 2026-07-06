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
from cograph_client.graph.ontology_queries import attr_uri, entity_uri, type_uri
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
# Discovery's promotion branch writes the relationship edge on the attrs/<leaf>
# attribute predicate. (Enrichment moved its instance edge to onto/<leaf> for
# NL-visibility in #126; that edge-predicate axis is separate from the NODE-minting
# convergence this change is about — see test_discovery_and_enrichment_mint_… .)
LOCATED_IN = attr_uri("Physician", "located_in")


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


async def test_discovery_and_enrichment_mint_the_identical_node(tmp_path):
    """Cross-rail NODE convergence (the whole point of this change): for the SAME
    fact — ``Physician.located_in = "San Francisco"`` with range ``City`` — discovery
    and enrichment mint the SAME target node: the SAME URI (via the shared
    ``entity_uri``), typed ``City``, labelled "San Francisco". So the real-world
    thing is ONE shared node across both rails, never a duplicate.

    Scope note: the relationship EDGE predicate is a SEPARATE axis — enrichment
    writes its instance edge on ``onto/<leaf>`` (NL-visibility, #126), discovery's
    promotion branch on ``attrs/<leaf>``. This change converges the NODE (identity +
    type + label), not the edge predicate; hence we compare the node-materialization
    triples (everything the enrichment rail asserts ABOUT the target), not the edge."""
    collected, _ = await _drive_promotion(tmp_path)
    target = entity_uri("City", "San Francisco")
    enrichment = EnrichmentExecutor._instance_triples_for_value(
        PHYS_URI, "Physician", "located_in", "San Francisco", "City",
    )
    # The triples enrichment asserts ABOUT the target node (its rdf:type + rdfs:label)
    # — i.e. the node materialization, subject == the target URI.
    node_triples = [t for t in enrichment if t[0] == target]
    assert node_triples, "enrichment sanity: node-linking must materialize the target"
    for triple in node_triples:
        assert triple in collected, f"discovery is missing the node triple {triple}"
    # Both rails also point an edge from the SAME subject at the SAME target node
    # (predicate differs by rail — that's the separate edge axis, see the docstring).
    assert any(s == PHYS_URI and o == target for (s, _p, o) in collected)
    assert any(s == PHYS_URI and o == target for (s, _p, o) in enrichment)


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
