"""Tests for relationship registration during ingestion and type placement.

Covers:
1. Relationships between entities register object properties in the ontology
2. same_as maps to existing types instead of creating duplicates
3. parent_type creates subtype relationships
4. Extraction prompt includes existing types

Ported to the production write path by ONTA-527. These used to read the SPARQL
strings handed to ``neptune.update``; Neo4j is the only backend now, so each
assertion moved to the thing the SPARQL was a proxy for:

* ontology facts (type minted / subclass edge / object property + its range) →
  the tenant ``ontology_catalog`` (``list_types`` / ``list_attributes``);
* instance facts (minted URI, typed literals, edges) → the KG itself via the
  ``explore_store`` read helpers.

``mock_neptune`` stays in the fixtures and each test asserts it was never CALLED
(or, for extractions carrying relationships, that the only surviving call is the
one still-broken flush — see ``_assert_no_sparql_write_but_the_rel_flush``);
that is what proves the store path ran rather than a leftover SPARQL write.
Ingest is given a per-KG ``instance_graph``: the tenant-level graph URI carries
no ``/kg/<kg>`` segment, so it cannot be resolved to a write scope.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from infona_client.resolver.schema_resolver import (
    SchemaResolver,
    EXTRACTION_SYSTEM,
    EXTRACTION_USER_TEMPLATE,
)
from infona_client.resolver.models import (
    ExtractedEntity,
    ExtractedAttribute,
    ExtractedRelationship,
    ExtractionResult,
    IngestResult,
)
from infona_client.resolver.verdict_cache import JsonVerdictCache
from infona_client.graph import ontology_catalog as oc
from infona_client.graph.explore_store import get_entity_detail, list_entities_by_type
from infona_client.graph.ontology_queries import entity_uri

TENANT = "test-tenant"
KG = "relationships"
#: Instance data needs a per-KG graph URI; the tenant graph alone has no scope.
KG_GRAPH = f"https://graph.infona.ai/graphs/{TENANT}/kg/{KG}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_neptune():
    client = AsyncMock()
    client.query.return_value = {
        "head": {"vars": []},
        "results": {"bindings": []},
    }
    client.update.return_value = None
    return client


@pytest.fixture
def mock_cache(tmp_path):
    return JsonVerdictCache(tmp_path / "cache.json")


# ---------------------------------------------------------------------------
# Store read helpers (what the SPARQL-string assertions became)
# ---------------------------------------------------------------------------


async def _types() -> dict[str, object]:
    """Tenant ontology types by name."""
    return {t.name: t for t in await oc.list_types(tenant_id=TENANT)}


async def _attrs() -> dict[tuple[str, str], object]:
    """Tenant ontology attributes keyed by ``(domain, name)``."""
    return {(a.domain, a.name): a for a in await oc.list_attributes(tenant_id=TENANT)}


async def _entities_of(type_name: str) -> list[str]:
    page = await list_entities_by_type(
        tenant_id=TENANT, kg=KG, type_name=type_name
    )
    return [e.id for e in page.entities]


async def _detail(type_name: str, raw_id: str):
    return await get_entity_detail(
        tenant_id=TENANT, kg=KG, entity_id=entity_uri(type_name, raw_id)
    )


def _assert_no_sparql_write_but_the_rel_flush(mock_neptune) -> None:
    """No SPARQL write survives except the known-broken relationship flush.

    ``assert_not_called()`` is the usual proof that the store path ran, but an
    extraction carrying RELATIONSHIPS still reaches
    ``batched_insert_triples(...) → neptune.update(...)`` in
    ``schema_resolver._resolve_and_insert`` step 4 — the bug pinned by the strict
    xfail on ``test_instance_triple_always_inserted``. This allows exactly that
    one shape and fails on any other surviving SPARQL write; it keeps passing
    once the flush is routed through ``insert_facts`` and the call list empties.

    "That one shape" is checked, not assumed: the rel flush writes ONLY
    ``onto/<leaf>`` edges, so a surviving statement that also carries an
    attribute predicate, an rdf:type or a label is the ENTITY write regressing
    back onto SPARQL — which this must still catch.
    """
    entity_write_markers = ("/attrs/", "22-rdf-syntax-ns#type", "rdf-schema#label")
    leftovers = [
        str(c.args[0])
        for c in mock_neptune.update.call_args_list
        if not c.args
        or "/onto/" not in str(c.args[0])
        or any(m in str(c.args[0]) for m in entity_write_markers)
    ]
    assert leftovers == [], f"unexpected SPARQL write(s) survived: {leftovers}"


# ---------------------------------------------------------------------------
# 1. Extraction prompt includes existing types
# ---------------------------------------------------------------------------

class TestExtractionPrompt:
    def test_system_prompt_has_entity_first_principle(self):
        assert "Entity-first principle" in EXTRACTION_SYSTEM

    def test_system_prompt_has_type_placement(self):
        assert "same_as" in EXTRACTION_SYSTEM
        assert "parent_type" in EXTRACTION_SYSTEM

    def test_user_template_has_existing_types_placeholder(self):
        assert "{existing_types}" in EXTRACTION_USER_TEMPLATE

    def test_user_template_has_same_as_field(self):
        assert "same_as" in EXTRACTION_USER_TEMPLATE

    def test_user_template_has_parent_type_field(self):
        assert "parent_type" in EXTRACTION_USER_TEMPLATE

    def test_system_prompt_has_domain_modeling_guidance(self):
        """The new domain-modeling blocks must be present (Cause 2): reify
        measurements, lift providers/orgs, subtypes with a description."""
        assert "Reify measurements" in EXTRACTION_SYSTEM
        assert "Lift providers / organizations" in EXTRACTION_SYSTEM
        assert "Subtypes with a description" in EXTRACTION_SYSTEM
        # The guidance names the concrete signals the resolver downstream relies on.
        assert "subtype_description" in EXTRACTION_SYSTEM
        assert "Organization" in EXTRACTION_SYSTEM

    def test_user_template_has_subtype_description_field(self):
        assert "subtype_description" in EXTRACTION_USER_TEMPLATE


# ---------------------------------------------------------------------------
# 2. Type placement: same_as
# ---------------------------------------------------------------------------

class TestTypePlacementSameAs:
    @pytest.mark.asyncio
    async def test_same_as_maps_to_existing_type(self, mock_neptune, mock_cache):
        """When LLM sets same_as, entity uses the existing type name."""
        resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)

        extraction = ExtractionResult(
            entities=[
                ExtractedEntity(
                    type_name="Home",
                    id="123-main",
                    same_as="Property",
                    attributes=[ExtractedAttribute(name="price", value="500000", datatype="integer")],
                ),
            ],
            relationships=[],
        )

        existing_types = {"Property": ""}
        existing_attrs = {"Property": {}}

        with patch.object(resolver, "_extract", return_value=extraction):
            with patch.object(resolver, "_fetch_ontology", return_value=(existing_types, existing_attrs)):
                result = await resolver.ingest(
                    "A home at 123 Main", TENANT, instance_graph=KG_GRAPH
                )

        # Should NOT have created a new "Home" type
        assert "Home" not in result.types_created
        assert "Home" not in await _types()
        # The entity is minted under the RESOLVED type, so its URI (and the node
        # it lands on) is entities/Property/…, never entities/Home/….
        assert await _entities_of("Property") == [entity_uri("Property", "123-main")]
        assert await _entities_of("Home") == []
        mock_neptune.update.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Type placement: parent_type
# ---------------------------------------------------------------------------

class TestTypePlacementParentType:
    @pytest.mark.asyncio
    async def test_parent_type_creates_subtype(self, mock_neptune, mock_cache):
        """When LLM sets parent_type, a subClassOf triple is created."""
        resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)

        extraction = ExtractionResult(
            entities=[
                ExtractedEntity(
                    type_name="Condo",
                    id="456-oak",
                    parent_type="Property",
                    attributes=[ExtractedAttribute(name="hoa_fee", value="450", datatype="integer")],
                ),
            ],
            relationships=[],
        )

        existing_types = {"Property": ""}
        existing_attrs = {"Property": {}}

        with patch.object(resolver, "_extract", return_value=extraction):
            with patch.object(resolver, "_fetch_ontology", return_value=(existing_types, existing_attrs)):
                result = await resolver.ingest(
                    "A condo at 456 Oak", TENANT, instance_graph=KG_GRAPH
                )

        assert "Condo" in result.types_created
        # The subclass edge itself, read back from the ontology.
        assert (await _types())["Condo"].parent_type == "Property"
        mock_neptune.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_parent_type_ignored_if_not_in_ontology(self, mock_neptune, mock_cache):
        """If parent_type references a type not in ontology, skip subtype creation."""
        resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)

        extraction = ExtractionResult(
            entities=[
                ExtractedEntity(
                    type_name="Spaceship",
                    id="falcon-9",
                    parent_type="Vehicle",  # Vehicle doesn't exist
                    attributes=[],
                ),
            ],
            relationships=[],
        )

        existing_types = {"Property": ""}
        existing_attrs = {"Property": {}}

        with patch.object(resolver, "_extract", return_value=extraction):
            with patch.object(resolver, "_fetch_ontology", return_value=(existing_types, existing_attrs)):
                result = await resolver.ingest(
                    "A spaceship", TENANT, instance_graph=KG_GRAPH
                )

        assert "Spaceship" in result.types_created
        # No subclass edge, and the phantom parent was NOT minted to hang one off.
        types = await _types()
        assert types["Spaceship"].parent_type is None
        assert "Vehicle" not in types
        mock_neptune.update.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Relationship registration as object properties
# ---------------------------------------------------------------------------

class TestRelationshipRegistration:
    @pytest.mark.asyncio
    async def test_relationship_registers_ontology_attribute(self, mock_neptune, mock_cache):
        """Relationships between entities should register as object properties in the ontology."""
        resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)

        extraction = ExtractionResult(
            entities=[
                ExtractedEntity(type_name="Person", id="john", attributes=[
                    # NOT ``name``: it is a RESERVED Entity property key
                    # (graph/facts.py RESERVED_ENTITY_PROPERTY_KEYS), so declaring
                    # it as an ontology attribute is refused outright.
                    ExtractedAttribute(name="full_name", value="John", datatype="string"),
                ]),
                ExtractedEntity(type_name="City", id="sf", attributes=[
                    ExtractedAttribute(name="full_name", value="San Francisco", datatype="string"),
                ]),
            ],
            relationships=[
                ExtractedRelationship(source_id="john", predicate="lives_in", target_id="sf"),
            ],
        )

        existing_types = {"Person": "", "City": ""}
        existing_attrs = {"Person": {}, "City": {}}

        with patch.object(resolver, "_extract", return_value=extraction):
            with patch.object(resolver, "_fetch_ontology", return_value=(existing_types, existing_attrs)):
                result = await resolver.ingest(
                    "John lives in SF", TENANT, instance_graph=KG_GRAPH
                )

        # The relationship should be registered as an ontology attribute
        assert "Person.lives_in" in result.attributes_added

        # …declared on Person as a RELATIONSHIP whose range is the City type —
        # what "insert_attribute(range=types/City)" used to spell in SPARQL.
        lives_in = (await _attrs())[("Person", "lives_in")]
        assert lives_in.kind == "relationship"
        assert lives_in.range_type == "City"
        assert lives_in.datatype is None
        _assert_no_sparql_write_but_the_rel_flush(mock_neptune)

    @pytest.mark.asyncio
    async def test_relationship_not_duplicated(self, mock_neptune, mock_cache):
        """If the relationship attribute already exists, don't re-register it."""
        resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)

        from infona_client.resolver.attribute_resolver import AttributeSchema

        extraction = ExtractionResult(
            entities=[
                ExtractedEntity(type_name="Person", id="john", attributes=[]),
                ExtractedEntity(type_name="City", id="sf", attributes=[]),
            ],
            relationships=[
                ExtractedRelationship(source_id="john", predicate="lives_in", target_id="sf"),
            ],
        )

        existing_types = {"Person": "", "City": ""}
        existing_attrs = {
            "Person": {"lives_in": AttributeSchema("lives_in", "City")},
            "City": {},
        }

        with patch.object(resolver, "_extract", return_value=extraction):
            with patch.object(resolver, "_fetch_ontology", return_value=(existing_types, existing_attrs)):
                result = await resolver.ingest(
                    "John lives in SF", TENANT, instance_graph=KG_GRAPH
                )

        # Should NOT re-register the attribute
        assert "Person.lives_in" not in result.attributes_added
        # …and no ontology write happened at all: the catalog is untouched by
        # this ingest (the declaration it would have written is the ONLY thing
        # this test's ontology would contain, since _fetch_ontology is mocked).
        assert ("Person", "lives_in") not in await _attrs()
        _assert_no_sparql_write_but_the_rel_flush(mock_neptune)

    @pytest.mark.asyncio
    async def test_relationship_upgrades_primitive_attribute(self, mock_neptune, mock_cache):
        """A predicate first seen as a primitive attribute, then carrying an
        entity object, must have its ontology range UPGRADED to the target type.

        Regression: without the upgrade the predicate keeps its ``xsd:string``
        range, so the schema-only Explorer overview can't draw the edge even
        though the per-type detail view shows it from instance data. The two
        views disagreed (RetailerSKU → Product line missing in the overview).
        """
        resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)

        from infona_client.resolver.attribute_resolver import AttributeSchema

        extraction = ExtractionResult(
            entities=[
                ExtractedEntity(type_name="Person", id="john", attributes=[]),
                ExtractedEntity(type_name="City", id="sf", attributes=[]),
            ],
            relationships=[
                ExtractedRelationship(source_id="john", predicate="lives_in", target_id="sf"),
            ],
        )

        existing_types = {"Person": "", "City": ""}
        # `lives_in` was previously registered as a primitive (string) attribute.
        existing_attrs = {
            "Person": {"lives_in": AttributeSchema("lives_in", "string")},
            "City": {},
        }
        # Put that prior state in the ONTOLOGY too, so the upgrade is a real
        # in-place range change rather than a first declaration.
        await oc.upsert_attribute(
            tenant_id=TENANT, type_name="Person", attr_name="lives_in", datatype="string"
        )
        before = (await _attrs())[("Person", "lives_in")]
        assert (before.kind, before.datatype) == ("literal", "string")

        with patch.object(resolver, "_extract", return_value=extraction):
            with patch.object(resolver, "_fetch_ontology", return_value=(existing_types, existing_attrs)):
                await resolver.ingest(
                    "John lives in SF", TENANT, instance_graph=KG_GRAPH
                )

        # The range was re-pointed at the City type — one declaration, upgraded
        # in place (the delete-then-insert the SPARQL builder used to emit).
        after = (await _attrs())[("Person", "lives_in")]
        assert after.kind == "relationship"
        assert after.range_type == "City"
        assert after.datatype is None
        assert len([k for k in await _attrs() if k == ("Person", "lives_in")]) == 1
        _assert_no_sparql_write_but_the_rel_flush(mock_neptune)

    async def test_instance_triple_always_inserted(self, mock_neptune, mock_cache):
        """Instance relationship triples should always be inserted regardless of ontology state."""
        resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)

        extraction = ExtractionResult(
            entities=[
                ExtractedEntity(type_name="Person", id="john", attributes=[]),
                ExtractedEntity(type_name="City", id="sf", attributes=[]),
            ],
            relationships=[
                ExtractedRelationship(source_id="john", predicate="lives_in", target_id="sf"),
            ],
        )

        existing_types = {"Person": "", "City": ""}
        existing_attrs = {"Person": {}, "City": {}}

        with patch.object(resolver, "_extract", return_value=extraction):
            with patch.object(resolver, "_fetch_ontology", return_value=(existing_types, existing_attrs)):
                result = await resolver.ingest(
                    "John lives in SF", TENANT, instance_graph=KG_GRAPH
                )

        assert result.triples_inserted > 0
        # The edge itself: john --lives_in--> sf. On the store path a
        # relationship Fact is what "onto/lives_in" maps to (graph/facts.py
        # classify_triple), so the edge is read back as an outgoing rel.
        john = await _detail("Person", "john")
        assert john is not None
        assert [(r.attr, r.other_id) for r in john.outgoing] == [
            ("lives_in", entity_uri("City", "sf"))
        ]
        mock_neptune.update.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Domain modeling: reified measurements, lifted orgs, described subtypes
# ---------------------------------------------------------------------------


class TestDomainModeling:
    """The extraction pipeline (Cause 2) must normalize a web/document ingest into
    a richer ontology — a Model, a lifted Organization, a Score REIFIED as its own
    entity, and a HumannessIndex SUBTYPE of Score carrying an rdfs:comment — rather
    than flattening everything onto one type. _extract is mocked, so this asserts
    the RESOLVE→INSERT behavior the strengthened prompt is meant to drive."""

    @staticmethod
    def _new_type_matcher(monkeypatch, resolver):
        """Force every proposed type to resolve as genuinely-new (DIFFERENT),
        hermetically — no LLM, no embeddings. Real behavior for brand-new types;
        subtyping is then driven by parent_chain in _link_parent."""
        from infona_client.resolver.models import MatchVerdict, TypeMatch

        async def fake_match(proposed_type, proposed_description, existing_types):
            return TypeMatch(
                proposed=proposed_type, resolved=proposed_type,
                verdict=MatchVerdict.DIFFERENT, confidence=1.0, is_new=True,
            )

        monkeypatch.setattr(resolver._type_matcher, "match", fake_match)

    @pytest.mark.asyncio
    async def test_json_ingest_reifies_score_lifts_org_and_describes_subtype(
        self, mock_neptune, mock_cache, monkeypatch,
    ):
        resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
        self._new_type_matcher(monkeypatch, resolver)

        DESC = "a score measuring how human a generated voice sounds"
        # The normalized shape the strengthened extractor is meant to produce from
        # a "TTS models and their scores" leaderboard ingest.
        extraction = ExtractionResult(
            entities=[
                ExtractedEntity(
                    type_name="Model", id="eleven-v3",
                    attributes=[ExtractedAttribute(name="modality", value="tts")],
                ),
                ExtractedEntity(
                    type_name="Organization", id="ElevenLabs",
                    attributes=[ExtractedAttribute(name="homepage", value="elevenlabs.io")],
                ),
                ExtractedEntity(
                    type_name="HumannessIndex", id="eleven-v3-humanness",
                    parent_chain=["Score"],
                    subtype_description=DESC,
                    attributes=[
                        ExtractedAttribute(name="value", value="87.5", datatype="float"),
                        ExtractedAttribute(
                            name="timestamp", value="2026-06-01T00:00:00Z",
                            datatype="datetime",
                        ),
                    ],
                ),
            ],
            relationships=[
                ExtractedRelationship(
                    source_id="eleven-v3", predicate="has_score",
                    target_id="eleven-v3-humanness",
                ),
                ExtractedRelationship(
                    source_id="eleven-v3-humanness", predicate="provided_by",
                    target_id="ElevenLabs",
                ),
            ],
        )

        content = json.dumps([{"title": "Eleven v3", "humanness": "87.5"}])
        with patch.object(resolver, "_extract", return_value=extraction):
            with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
                result = await resolver.ingest(
                    content, TENANT, content_type="json", instance_graph=KG_GRAPH
                )

        types = await _types()
        attrs = await _attrs()

        # --- types: Model, Organization, Score (synthesized parent) + the
        #     HumannessIndex subtype were all created.
        for t in ("Model", "Organization", "Score", "HumannessIndex"):
            assert t in result.types_created, f"{t} not created: {result.types_created}"
            assert t in types, f"{t} missing from the ontology: {sorted(types)}"

        # --- the subtype edge HumannessIndex subClassOf Score.
        assert types["HumannessIndex"].parent_type == "Score"
        assert types["Score"].parent_type is None

        # --- Score (the synthesized ancestor) carries NO description; only a
        #     minted SUBTYPE may. (The positive half — HumannessIndex carrying
        #     `subtype_description` — is a Neo4j-port gap, pinned by the strict
        #     xfails in tests/test_resolver_subtype_description.py.)
        assert types["Score"].description == ""

        # --- relationships became object properties (entity→entity edges), not
        #     scalar attributes: has_score (Model→HumannessIndex) and provided_by
        #     (HumannessIndex→Organization), each ranged at its target TYPE.
        assert "Model.has_score" in result.attributes_added
        assert "HumannessIndex.provided_by" in result.attributes_added
        has_score = attrs[("Model", "has_score")]
        provided_by = attrs[("HumannessIndex", "provided_by")]
        assert (has_score.kind, has_score.range_type) == ("relationship", "HumannessIndex")
        assert (provided_by.kind, provided_by.range_type) == ("relationship", "Organization")

        # --- the reified Score's measurement values are TYPED literals on the
        #     HumannessIndex entity (value:float, timestamp:datetime) — the
        #     measurement is an entity with its own attributes, not a bare scalar.
        assert attrs[("HumannessIndex", "value")].datatype == "float"
        assert attrs[("HumannessIndex", "timestamp")].datatype == "datetime"
        humanness = await _detail("HumannessIndex", "eleven-v3-humanness")
        assert humanness is not None
        assert humanness.primary_type == "HumannessIndex"
        # The datatype annotation is SPLIT OFF the value now (it used to be
        # stored inside it — the leak this file previously tolerated), so the
        # stored property is the native scalar.
        assert float(humanness.properties["value"]) == 87.5
        # (the validator coerces the datetime to xsd form, dropping the 'Z').
        assert str(humanness.properties["timestamp"]).startswith(
            "2026-06-01T00:00:00"
        )

        # --- all three entities resolved (Model, Organization, HumannessIndex)
        #     and each landed as a node of its own type.
        assert result.entities_resolved == 3
        assert await _entities_of("Model") == [entity_uri("Model", "eleven-v3")]
        assert await _entities_of("Organization") == [
            entity_uri("Organization", "ElevenLabs")
        ]

        # NOTE (deliberately no `mock_neptune.update.assert_not_called()` here):
        # this ingest DOES still call it once, because the has_score /
        # provided_by EDGES are flushed through the surviving
        # `batched_insert_triples` + `neptune.update` path and therefore never
        # reach the KG. That gap is pinned by the strict xfail on
        # TestRelationshipRegistration.test_instance_triple_always_inserted;
        # everything asserted above is the ontology + node half, which does land.

    @pytest.mark.asyncio
    async def test_plain_json_ingest_without_domain_signals_still_works(
        self, mock_neptune, mock_cache, monkeypatch,
    ):
        """Regression: an ordinary ingest whose extraction sets NO
        subtype_description / parent_chain / reified measurement still ingests
        cleanly — the new field + prompt must not break the common case."""
        resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
        self._new_type_matcher(monkeypatch, resolver)

        extraction = ExtractionResult(
            entities=[
                ExtractedEntity(
                    type_name="Article", id="a1",
                    attributes=[ExtractedAttribute(name="title", value="Hello")],
                ),
            ],
            relationships=[],
        )

        content = json.dumps([{"title": "Hello"}])
        with patch.object(resolver, "_extract", return_value=extraction):
            with patch.object(resolver, "_fetch_ontology", return_value=({}, {})):
                result = await resolver.ingest(
                    content, TENANT, content_type="json", instance_graph=KG_GRAPH
                )

        assert "Article" in result.types_created
        assert result.entities_resolved == 1
        assert await _entities_of("Article") == [entity_uri("Article", "a1")]
        # The minted type carries NO description (subtype_description defaulted
        # None → nothing to write).
        assert (await _types())["Article"].description == ""
        mock_neptune.update.assert_not_called()


# ---------------------------------------------------------------------------
# 6. ExtractedEntity model round-trips the new subtype_description field
# ---------------------------------------------------------------------------


class TestExtractedEntityModel:
    def test_subtype_description_round_trips(self):
        desc = "a score measuring how human a generated voice sounds"
        e = ExtractedEntity(
            type_name="HumannessIndex",
            id="x",
            parent_chain=["Score"],
            subtype_description=desc,
            attributes=[ExtractedAttribute(name="value", value="9.1", datatype="float")],
        )
        dumped = e.model_dump()
        assert dumped["subtype_description"] == desc
        # Round-trips back through validation unchanged.
        again = ExtractedEntity(**dumped)
        assert again.subtype_description == desc
        assert again.parent_chain == ["Score"]

    def test_subtype_description_defaults_none(self):
        e = ExtractedEntity(type_name="Article", id="a1")
        assert e.subtype_description is None
        # Parses from a payload that omits the field entirely (back-compat).
        from_payload = ExtractedEntity(**{"type_name": "Article", "id": "a1"})
        assert from_payload.subtype_description is None
