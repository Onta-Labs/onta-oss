"""Multi-typing retail domain tests: LoyaltyCustomer < Customer < Person.

Covers:
A) INGESTION — resolver stamps the leaf type, synthesizes ancestor chain,
   and ER merges cross-file records by email via chain-walk config lookup.
B) QUERYING — subclass-closure rewrite makes a Person query cover
   LoyaltyCustomer instances.

ONTA-527 port (part A only; part B is pure string rewriting and unchanged): the
ingestion tests used to grep the SPARQL handed to ``neptune.update`` for a leaf
type URI / an ``entities/LoyaltyCustomer/`` segment / a ``subClassOf``. On the
Neo4j path those facts live in the KG and the tenant ``ontology_catalog``, so
they are read back from there and ``neptune.update`` must never be called.
Ingest gets a per-KG ``instance_graph`` (the tenant-level URI has no write
scope), and the fixtures use ``full_name`` instead of ``name`` — ``name`` is a
RESERVED Entity property key (graph/facts.py) that the catalog refuses to
declare.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, call, patch

import pytest

from infona_client.resolver.models import (
    ExtractedAttribute,
    ExtractedEntity,
    ExtractionResult,
    IngestResult,
)
from infona_client.resolver.verdict_cache import JsonVerdictCache
from infona_client.resolver.er.types import (
    DEFAULT_CUSTOMER_CONFIG,
    ancestor_chain,
    config_for,
    config_for_with_hierarchy,
    primary_type,
)
from infona_client.graph import ontology_catalog as oc
from infona_client.graph.explore_store import get_entity_detail, list_entities_by_type
from infona_client.graph.ontology_queries import (
    entity_uri,
    rewrite_type_predicate_to_closure,
    with_subclass_closure,
)


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

TENANT = "test-tenant"
KG = "retail"
GRAPH = f"https://graph.infona.ai/graphs/{TENANT}"
#: Instance data needs a per-KG graph URI; the tenant graph alone has no scope.
KG_GRAPH = f"{GRAPH}/kg/{KG}"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

# Retail hierarchy under test
PARENT_OF = {
    "LoyaltyCustomer": "Customer",
    "Customer": "Person",
}


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
    client.batch_exists.return_value = set()
    return client


@pytest.fixture
def mock_cache(tmp_path):
    return JsonVerdictCache(tmp_path / "cache.json")


def _loyalty_customer(entity_id: str, email: str, name: str) -> ExtractedEntity:
    """Construct a LoyaltyCustomer ExtractedEntity."""
    return ExtractedEntity(
        type_name="LoyaltyCustomer",
        id=entity_id,
        parent_type="Customer",
        attributes=[
            ExtractedAttribute(name="email", value=email, datatype="string"),
            ExtractedAttribute(name="full_name", value=name, datatype="string"),
            ExtractedAttribute(name="loyalty_tier", value="Gold", datatype="string"),
        ],
    )


# ===========================================================================
# A) INGESTION TESTS
# ===========================================================================


class TestIngestionLeafTypeStamped:
    """The resolved entity carries its most-specific asserted type."""

    @pytest.mark.asyncio
    async def test_loyalty_customer_stamped_with_leaf_type(self, mock_neptune, mock_cache):
        """LoyaltyCustomer entity → rdf:type triple points to LoyaltyCustomer URI, not Customer."""
        from infona_client.resolver.schema_resolver import SchemaResolver

        resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)

        entity = _loyalty_customer("alice@retail.com", "alice@retail.com", "Alice Retail")
        extraction = ExtractionResult(entities=[entity], relationships=[])

        # Pre-populate ontology with Customer and Person; LoyaltyCustomer is new
        existing_types = {"Customer": "", "Person": ""}
        existing_attrs = {"Customer": {}, "Person": {}}

        with patch.object(resolver, "_extract", return_value=extraction):
            with patch.object(
                resolver, "_fetch_ontology", return_value=(existing_types, existing_attrs)
            ):
                result = await resolver.ingest(
                    "loyalty data", TENANT, instance_graph=KG_GRAPH
                )

        # LoyaltyCustomer should be created as a type
        assert "LoyaltyCustomer" in result.types_created

        # The asserted type is the LEAF: the node's primary type is
        # LoyaltyCustomer, and it is NOT stamped as a bare Customer.
        detail = await get_entity_detail(
            tenant_id=TENANT, kg=KG,
            entity_id=entity_uri("LoyaltyCustomer", "alice@retail.com"),
        )
        assert detail is not None
        assert detail.primary_type == "LoyaltyCustomer"
        assert "LoyaltyCustomer" in detail.labels
        customers = await list_entities_by_type(
            tenant_id=TENANT, kg=KG, type_name="Customer"
        )
        assert customers.total == 0, "the ancestor type must not be asserted directly"
        mock_neptune.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_entity_uri_uses_leaf_type(self, mock_neptune, mock_cache):
        """Entity URI is minted under /entities/LoyaltyCustomer/ (most-specific type)."""
        from infona_client.resolver.schema_resolver import SchemaResolver

        resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)

        entity = _loyalty_customer("bob@retail.com", "bob@retail.com", "Bob Retail")
        extraction = ExtractionResult(entities=[entity], relationships=[])

        existing_types = {"Customer": "", "Person": ""}
        existing_attrs = {"Customer": {}, "Person": {}}

        with patch.object(resolver, "_extract", return_value=extraction):
            with patch.object(
                resolver, "_fetch_ontology", return_value=(existing_types, existing_attrs)
            ):
                await resolver.ingest(
                    "loyalty data", TENANT, instance_graph=KG_GRAPH
                )

        # The minted URI is under the leaf type — entities/LoyaltyCustomer/…,
        # from the one shared entity_uri() minter.
        page = await list_entities_by_type(
            tenant_id=TENANT, kg=KG, type_name="LoyaltyCustomer"
        )
        assert [e.id for e in page.entities] == [
            entity_uri("LoyaltyCustomer", "bob@retail.com")
        ]
        assert page.entities[0].id.startswith(
            "https://graph.infona.ai/entities/LoyaltyCustomer/"
        )
        mock_neptune.update.assert_not_called()


class TestAncestorSynthesis:
    """Ancestor chain is inserted for new subtypes."""

    @pytest.mark.asyncio
    async def test_subtype_link_to_customer_inserted(self, mock_neptune, mock_cache):
        """Creating LoyaltyCustomer with parent_type=Customer inserts a subClassOf triple."""
        from infona_client.resolver.schema_resolver import SchemaResolver

        resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)

        entity = _loyalty_customer("carol@retail.com", "carol@retail.com", "Carol Retail")
        extraction = ExtractionResult(entities=[entity], relationships=[])

        existing_types = {"Customer": "", "Person": ""}
        existing_attrs = {"Customer": {}, "Person": {}}

        with patch.object(resolver, "_extract", return_value=extraction):
            with patch.object(
                resolver, "_fetch_ontology", return_value=(existing_types, existing_attrs)
            ):
                await resolver.ingest(
                    "loyalty data", TENANT, instance_graph=KG_GRAPH
                )

        types = {t.name: t for t in await oc.list_types(tenant_id=TENANT)}
        # The subclass edge links LoyaltyCustomer -> Customer.
        assert types["LoyaltyCustomer"].parent_type == "Customer"
        mock_neptune.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_ancestor_chain_pure_function(self):
        """ancestor_chain correctly walks LoyaltyCustomer -> Customer -> Person."""
        chain = ancestor_chain("LoyaltyCustomer", PARENT_OF)
        assert chain == ["LoyaltyCustomer", "Customer", "Person"]

    def test_ancestor_chain_unknown_type(self):
        """Unknown type with no parents returns single-element chain."""
        assert ancestor_chain("Widget", {}) == ["Widget"]

    def test_ancestor_chain_cycle_guarded(self):
        """Cyclic parent map terminates without infinite loop."""
        cyclic = {"A": "B", "B": "A"}
        chain = ancestor_chain("A", cyclic)
        # Must terminate; both should appear exactly once
        assert set(chain) == {"A", "B"}
        assert len(chain) == 2


class TestERViaChainWalk:
    """ER fires on LoyaltyCustomer by climbing to Customer's config."""

    def test_config_for_loyalty_customer_flat_is_none(self):
        """Flat config_for(LoyaltyCustomer) returns None — LoyaltyCustomer not in DEFAULTS_BY_TYPE."""
        # LoyaltyMember IS in DEFAULTS_BY_TYPE, but LoyaltyCustomer is NOT — the test
        # domain deliberately uses a novel leaf name to prove chain-walk is needed.
        result = config_for("LoyaltyCustomer")
        assert result is None, (
            "LoyaltyCustomer should not have a flat config — the chain-walk is the whole point"
        )

    def test_config_for_with_hierarchy_resolves_to_customer_config(self):
        """config_for_with_hierarchy climbs to Customer and returns its config."""
        cfg = config_for_with_hierarchy("LoyaltyCustomer", PARENT_OF)
        assert cfg is not None, "Expected a config via chain-walk"
        assert cfg is DEFAULT_CUSTOMER_CONFIG, (
            "LoyaltyCustomer -> Customer -> config should be DEFAULT_CUSTOMER_CONFIG"
        )
        assert "email" in cfg.signals
        assert "email" in cfg.decisive_signals

    def test_config_for_with_hierarchy_empty_map_is_none(self):
        """Without hierarchy info, LoyaltyCustomer has no config."""
        cfg = config_for_with_hierarchy("LoyaltyCustomer", {})
        assert cfg is None

    @pytest.mark.asyncio
    async def test_two_loyalty_records_merge_via_email(self, mock_neptune, mock_cache):
        """Two LoyaltyCustomer records with same email merge to one canonical URI.

        This is the key cross-file ER scenario. If ER were still flat (no chain-walk),
        config_for('LoyaltyCustomer') would be None and they would NOT merge — the
        resolver would mint two separate URIs. With hierarchy-aware ER they merge.
        """
        from infona_client.resolver.schema_resolver import SchemaResolver
        from infona_client.resolver.er.blocking import generate_block_keys
        from infona_client.resolver.er.normalize import DefaultNormalizer
        from infona_client.resolver.er.engine import extract_signals
        from infona_client.resolver.er.types import NormalizedSignals, MergeAction

        # Pre-suppose: LoyaltyCustomer exists in ontology, Customer + Person also.
        existing_types = {"LoyaltyCustomer": "", "Customer": "", "Person": ""}
        existing_attrs = {"LoyaltyCustomer": {}, "Customer": {}, "Person": {}}

        # First entity: loyalty program record
        loyalty_entity = ExtractedEntity(
            type_name="LoyaltyCustomer",
            id="dave@retail.com",
            attributes=[
                ExtractedAttribute(name="email", value="dave@retail.com", datatype="string"),
                ExtractedAttribute(name="full_name", value="Dave Retail", datatype="string"),
                ExtractedAttribute(name="loyalty_tier", value="Silver", datatype="string"),
            ],
        )
        # Second entity: CRM record for the same person (same email)
        crm_entity = ExtractedEntity(
            type_name="LoyaltyCustomer",
            id="dave@retail.com",
            attributes=[
                ExtractedAttribute(name="email", value="dave@retail.com", datatype="string"),
                ExtractedAttribute(name="full_name", value="David Retail", datatype="string"),
                ExtractedAttribute(name="crm_id", value="CRM-99", datatype="string"),
            ],
        )

        # Build the normalized signals for the first entity so the mock blocker
        # can return it as a candidate for the second.
        normalizer = DefaultNormalizer()
        raw1 = extract_signals(loyalty_entity)
        norm1 = normalizer.normalize(raw1)
        keys1 = generate_block_keys(norm1)

        canonical_uri = "https://graph.infona.ai/entities/LoyaltyCustomer/dave_retail_com-abc12345"

        # Mock the blocker: when asked for candidates, return the first entity
        # under the canonical URI with its normalized signals.
        from infona_client.resolver.er.engine import ERPipeline

        async def fake_candidates_with_signals(instance_graph, type_uri_arg, keys):
            # Return the first entity's signals as a candidate at the canonical URI
            return {canonical_uri: norm1}

        resolver = SchemaResolver(mock_neptune, "fake-key", mock_cache)
        resolver._er._blocker.candidates_with_signals = fake_candidates_with_signals

        # Ingest both entities in one batch.
        extraction = ExtractionResult(
            entities=[loyalty_entity, crm_entity],
            relationships=[],
        )

        with patch.object(resolver, "_extract", return_value=extraction):
            with patch.object(
                resolver, "_fetch_ontology", return_value=(existing_types, existing_attrs)
            ):
                # Also mock _fetch_parent_map to return the retail hierarchy
                with patch.object(
                    resolver, "_fetch_parent_map", return_value=dict(PARENT_OF)
                ):
                    result = await resolver.ingest(
                        "dual source retail data", TENANT, instance_graph=KG_GRAPH
                    )

        # Both entities extracted; after ER the second should merge onto the first.
        assert result.entities_extracted == 2

        # Both records landed on the ONE canonical node the blocker offered —
        # the merge, read back from the KG rather than grepped out of SPARQL.
        # If ER were flat (no chain-walk), LoyaltyCustomer would have no config
        # and two distinct URIs would be minted instead.
        merged = await get_entity_detail(
            tenant_id=TENANT, kg=KG, entity_id=canonical_uri
        )
        assert merged is not None, (
            "no node at the canonical URI: the ER merge did not happen"
        )
        # The CRM record's own attribute is on that shared node.
        assert merged.properties.get("crm_id") == "CRM-99"
        page = await list_entities_by_type(
            tenant_id=TENANT, kg=KG, type_name="LoyaltyCustomer"
        )
        assert [e.id for e in page.entities] == [canonical_uri]
        mock_neptune.update.assert_not_called()


class TestPrimaryType:
    """primary_type selects the most-specific leaf type."""

    def test_single_type_returned_as_is(self):
        assert primary_type(["LoyaltyCustomer"], PARENT_OF) == "LoyaltyCustomer"

    def test_leaf_dominates_ancestor(self):
        """LoyaltyCustomer dominates Customer because Customer is its ancestor."""
        result = primary_type(["LoyaltyCustomer", "Customer"], PARENT_OF)
        assert result == "LoyaltyCustomer"

    def test_leaf_dominates_grandparent(self):
        """LoyaltyCustomer dominates Person (grandparent via Customer)."""
        result = primary_type(["Person", "LoyaltyCustomer"], PARENT_OF)
        assert result == "LoyaltyCustomer"

    def test_empty_returns_none(self):
        assert primary_type([], PARENT_OF) is None

    def test_independent_types_deterministic(self):
        """Two unrelated types: deterministic tie-break."""
        result = primary_type(["Cat", "Dog"], {})
        # Longest chain wins; both length-1, so lexicographically smallest
        assert result == "Cat"


# ===========================================================================
# B) QUERYING TESTS — subclass closure
# ===========================================================================


class TestSubclassClosureQuery:
    """The query seam produces SPARQL that covers subtypes of a parent type."""

    def test_with_subclass_closure_returns_closure_path(self):
        """with_subclass_closure returns the rdf:type/rdfs:subClassOf* property path."""
        path = with_subclass_closure("Person")
        assert "subClassOf" in path
        assert "*" in path  # Kleene star
        assert "rdf-syntax-ns#type" in path

    def test_rewrite_form_a_bare_a_predicate(self):
        """Form A: ?var a <types/X> → closure predicate."""
        sparql = "SELECT ?x WHERE { ?x a <https://graph.infona.ai/types/Person> . }"
        rewritten = rewrite_type_predicate_to_closure(sparql)
        assert "subClassOf>*" in rewritten, "Expected closure path in rewritten SPARQL"
        assert "https://graph.infona.ai/types/Person" in rewritten

    def test_rewrite_form_b_explicit_rdf_type(self):
        """Form B: ?var <rdf:type> <types/X> → closure predicate."""
        rdf_type_full = f"<{RDF_TYPE}>"
        sparql = (
            f"SELECT ?x WHERE {{ ?x {rdf_type_full} "
            f"<https://graph.infona.ai/types/Customer> . }}"
        )
        rewritten = rewrite_type_predicate_to_closure(sparql)
        assert "subClassOf>*" in rewritten
        assert "https://graph.infona.ai/types/Customer" in rewritten

    def test_rewrite_is_idempotent(self):
        """Applying the rewrite twice yields the same result as applying it once."""
        sparql = "SELECT ?x WHERE { ?x a <https://graph.infona.ai/types/Person> . }"
        once = rewrite_type_predicate_to_closure(sparql)
        twice = rewrite_type_predicate_to_closure(once)
        assert once == twice

    def test_rewrite_covers_loyalty_customer_instances_under_person_query(self):
        """A query asking for Person instances gets rewritten to include subtypes.

        This proves the query-time closure: LoyaltyCustomer instances asserted as
        <entity> rdf:type <types/LoyaltyCustomer> are returned by a query over
        <types/Person> when the closure predicate is used — because
        LoyaltyCustomer rdfs:subClassOf Customer rdfs:subClassOf Person.
        """
        # Simulate a SPARQL query generated by the NL pipeline for "all Persons"
        person_type_uri = "https://graph.infona.ai/types/Person"
        sparql = f"SELECT ?x ?name WHERE {{ ?x a <{person_type_uri}> . ?x <https://graph.infona.ai/types/Person/attrs/name> ?name . }}"

        rewritten = rewrite_type_predicate_to_closure(sparql)

        # The type predicate must be the closure path
        assert "subClassOf>*" in rewritten, (
            "Rewritten query must contain the rdfs:subClassOf* closure path "
            "so LoyaltyCustomer instances (asserted as their leaf type) are "
            "returned by a Person-level query."
        )
        # The object type URI is preserved
        assert person_type_uri in rewritten

    def test_multiple_type_triples_both_rewritten(self):
        """All type triples in a JOIN query are rewritten, not just the first."""
        sparql = (
            "SELECT ?c ?o WHERE { "
            "?c a <https://graph.infona.ai/types/Customer> . "
            "?o a <https://graph.infona.ai/types/Order> . "
            "?c <https://graph.infona.ai/onto/placed> ?o . }"
        )
        rewritten = rewrite_type_predicate_to_closure(sparql)
        # Count closure rewrites
        count = rewritten.count("subClassOf>*")
        assert count == 2, f"Expected 2 closure rewrites, got {count}"

    def test_non_infona_type_uris_not_rewritten(self):
        """Predicate rewrite only fires for https://graph.infona.ai/types/ URIs."""
        sparql = "SELECT ?x WHERE { ?x a <http://schema.org/Person> . }"
        rewritten = rewrite_type_predicate_to_closure(sparql)
        # schema.org URI is not a infona types URI → should not be rewritten
        assert "subClassOf" not in rewritten
