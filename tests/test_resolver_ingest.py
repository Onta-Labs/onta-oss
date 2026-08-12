"""Integration test for the ingest API endpoint."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mock_anthropic_response():
    """Mock Anthropic response with extracted entities."""
    def make_response(text: str):
        mock = AsyncMock()
        content_block = MagicMock()
        content_block.text = text
        mock.content = [content_block]
        return mock
    return make_response


def test_ingest_endpoint_exists(client, auth_headers):
    """Verify the endpoint is registered and requires auth."""
    response = client.post("/graphs/test-tenant/ingest")
    assert response.status_code != 404


def test_ingest_requires_auth(client):
    response = client.post(
        "/graphs/test-tenant/ingest",
        json={"content": "test"},
    )
    assert response.status_code == 401


def test_ingest_requires_content(client, auth_headers):
    response = client.post(
        "/graphs/test-tenant/ingest",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 422


@patch("infona_client.api.routes.ingest.SchemaResolver")
def test_ingest_returns_result(mock_resolver_cls, client, auth_headers):
    """Test that ingest endpoint calls resolver and returns result."""
    from infona_client.resolver.models import IngestResult
    mock_instance = AsyncMock()
    mock_instance.ingest.return_value = IngestResult(
        entities_extracted=2,
        entities_resolved=2,
        triples_inserted=10,
        types_created=["Property"],
        attributes_added=["Property.price"],
    )
    mock_resolver_cls.return_value = mock_instance

    response = client.post(
        "/graphs/test-tenant/ingest",
        json={"content": "A 3-bedroom house at 123 Main St for $500,000", "source": "test"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["entities_extracted"] == 2
    assert data["triples_inserted"] == 10
    assert "Property" in data["types_created"]


# --- COG-52 / ADR 0003 Pass D: /ingest/csv/rows pre-registration -------------
#
# The pre-registration loop must write promoted types and EVERY core slot to
# the tenant ontology — including slots with ZERO data in the file — and mark
# each as a core slot (the enrichment work-queue hook, ADR 0003 §3).
# SchemaResolver is mocked (no LLM, no row insertion under test).
#
# ONTA-527 port: these used to read the generated SPARQL off the mocked Neptune
# client.
# Neo4j is the only backend now, so the pre-registration lands in the tenant
# ``ontology_catalog`` and is asserted there; ``mock_neptune.update`` must never
# be called at all, which is what proves the store path ran. The core-slot
# MARKER did not survive the port and is pinned by a strict xfail below.
#
# The mapping's identifier column is ``title``, not ``name``: ``name`` is a
# RESERVED Entity property key (graph/facts.py RESERVED_ENTITY_PROPERTY_KEYS) and
# the catalog refuses to declare it, which fails the request outright.


def _extension_mapping() -> dict:
    """A posted mapping carrying one promotion in the canonical
    dependent-identifier shape. issued_by/identifies/code have NO backing
    column — zero-data slots that must still be declared."""
    return {
        "entity_type": "Item",
        "columns": [
            {"column_name": "title", "role": "type_id", "datatype": "string",
             "attribute_name": "title"},
            {"column_name": "code", "role": "attribute", "datatype": "string",
             "attribute_name": "code"},
        ],
        "ontology_extensions": {
            "types": [
                {"type_name": "Code", "promoted_from_attribute": "code",
                 "held_for_review": True,  # applied anyway: confirm gate is client-side
                 "core_slots": [
                     {"name": "issued_by", "kind": "relationship", "target_type": "Issuer",
                      "why": "an identifier exists only relative to its issuer",
                      "dataset_constant": None},
                     {"name": "identifies", "kind": "relationship", "target_type": "Item"},
                     {"name": "code", "kind": "attribute"},
                 ],
                 "rejected": [{"name": "notes", "failed_test": "existence"}]},
            ],
        },
    }


def _post_extension_mapping(client, auth_headers, mapping=None):
    return client.post(
        "/graphs/test-tenant/ingest/csv/rows",
        json={"mapping": mapping or _extension_mapping(),
              "rows": [{"title": "One", "code": "C-1"}]},
        headers=auth_headers,
    )


async def _tenant_ontology():
    """(types by name, attributes by (domain, name)) for the test tenant."""
    from infona_client.graph import ontology_catalog as oc

    types = {t.name: t for t in await oc.list_types(tenant_id="test-tenant")}
    attrs = {
        (a.domain, a.name): a
        for a in await oc.list_attributes(tenant_id="test-tenant")
    }
    return types, attrs


@patch("infona_client.api.routes.ingest.SchemaResolver")
def test_csv_rows_preregisters_promoted_types_and_core_slots(
    mock_resolver_cls, client, auth_headers, mock_neptune,
):
    from infona_client.resolver.models import IngestResult

    mock_instance = AsyncMock()
    mock_instance._fetch_ontology.return_value = ({}, {})
    mock_instance._resolve_and_insert.return_value = IngestResult()
    mock_resolver_cls.return_value = mock_instance

    response = _post_extension_mapping(client, auth_headers)
    assert response.status_code == 200

    types, attrs = asyncio.run(_tenant_ontology())

    # The promoted type is declared, with its promotion provenance description.
    assert "Code" in types
    assert "promoted from attribute 'code'" in types["Code"].description
    # The issuer type exists even though NO column (and no row) references
    # it — zero instances, declared enrichment target.
    assert "Issuer" in types
    # Zero-data relationship slots are declared with their target as range…
    issued_by = attrs[("Code", "issued_by")]
    assert (issued_by.kind, issued_by.range_type) == ("relationship", "Issuer")
    identifies = attrs[("Code", "identifies")]
    assert (identifies.kind, identifies.range_type) == ("relationship", "Item")
    # …the id-string attribute slot too.
    code = attrs[("Code", "code")]
    assert (code.kind, code.datatype) == ("literal", "string")
    # Not one SPARQL write survived: the pre-registration ran on the store path.
    mock_neptune.update.assert_not_called()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PRODUCT BUG (Neo4j port): the coreSlot MARKER is dropped. The route "
        "emits OntologyOpKind.SET_CORE_SLOT per declared core slot, and "
        "ontology_commit._commit_ontology_graph_store has no branch for it — it "
        "logs 'ontology_store_op_skipped' and moves on, and the catalog carries "
        "no core-slot field to hold it. The slots themselves are declared, but "
        "the marker is what makes 'instances with empty core slots' queryable, "
        "so enrichment's work queue (ADR 0003 §3) has no hook on Neo4j."
    ),
)
@patch("infona_client.api.routes.ingest.SchemaResolver")
def test_csv_rows_marks_every_core_slot(
    mock_resolver_cls, client, auth_headers, mock_neptune,
):
    from infona_client.resolver.models import IngestResult

    mock_instance = AsyncMock()
    mock_instance._fetch_ontology.return_value = ({}, {})
    mock_instance._resolve_and_insert.return_value = IngestResult()
    mock_resolver_cls.return_value = mock_instance

    assert _post_extension_mapping(client, auth_headers).status_code == 200

    _types, attrs = asyncio.run(_tenant_ontology())
    for slot in ("issued_by", "identifies", "code"):
        assert getattr(attrs[("Code", slot)], "core_slot", False) is True, slot


@patch("infona_client.api.routes.ingest.SchemaResolver")
def test_csv_rows_core_slot_preregistration_uses_attribute_schema(
    mock_resolver_cls, client, auth_headers, mock_neptune,
):
    """Regression: pre-registered core slots must land in existing_attrs as
    AttributeSchema, NOT bare marker strings. A str there crashes the insert
    pass with `'str' object has no attribute 'datatype'` the moment any ingested
    entity of the extension type has an attribute matching the slot name."""
    from infona_client.resolver.attribute_resolver import AttributeSchema
    from infona_client.resolver.models import IngestResult

    mock_instance = AsyncMock()
    mock_instance._fetch_ontology.return_value = ({}, {})
    mock_instance._resolve_and_insert.return_value = IngestResult()
    mock_resolver_cls.return_value = mock_instance

    response = _post_extension_mapping(client, auth_headers)
    assert response.status_code == 200

    # existing_attrs is the 4th positional arg handed to _resolve_and_insert.
    existing_attrs = mock_instance._resolve_and_insert.call_args.args[3]
    code_slots = existing_attrs.get("Code", {})
    assert code_slots, "extension type 'Code' core slots were not pre-registered"
    for slot_name, schema in code_slots.items():
        assert isinstance(schema, AttributeSchema), (
            f"core slot {slot_name!r} stored as {type(schema).__name__}, "
            f"must be AttributeSchema (regression: bare 'core' string)"
        )
    # Relationship slots keep their target type as datatype; attribute slots are string.
    assert code_slots["issued_by"].datatype == "Issuer"
    assert code_slots["code"].datatype == "string"


@patch("infona_client.api.routes.ingest.SchemaResolver")
def test_csv_rows_without_extensions_writes_no_core_slots(
    mock_resolver_cls, client, auth_headers, mock_neptune,
):
    from infona_client.resolver.models import IngestResult

    mock_instance = AsyncMock()
    mock_instance._fetch_ontology.return_value = ({}, {})
    mock_instance._resolve_and_insert.return_value = IngestResult()
    mock_resolver_cls.return_value = mock_instance

    mapping = _extension_mapping()
    del mapping["ontology_extensions"]  # pre-COG-52 payload
    response = _post_extension_mapping(client, auth_headers, mapping)
    assert response.status_code == 200

    types, attrs = asyncio.run(_tenant_ontology())
    # Only the mapping's own type + columns were declared: no promoted type, no
    # zero-data target type, no extension slots.
    assert set(types) == {"Item"}
    assert set(attrs) == {("Item", "title"), ("Item", "code")}
    mock_neptune.update.assert_not_called()
