"""Route tests for the tenant ontology API (ported by ONTA-527).

**What changed.** These cases used to drive the SPARQL ontology graph: writes
were asserted by scraping ``mock_neptune.update`` for a substring, and reads
were mocked by handing the route a ``full_ontology_detail_query``-shaped SPARQL
JSON document. Neo4j is the only backend now — ``/ontology`` reads and writes go
through :mod:`infona_client.graph.ontology_catalog` on the process ``GraphStore``
and emit no SPARQL at all — so every one of those assertions was checking a
builder that no longer runs.

They are re-pointed at the same properties over a SEEDED CATALOG: the write
landed (read it back through the catalog AND through the route), the response
shape is unchanged, the reads are tenant-scoped, and validation still fails
closed. ``mock_neptune.query/update.assert_not_called()`` is the proof the store
path ran rather than a resurrected SPARQL one — the routes still declare the
NeptuneClient dependency, so the mock exists but must never be touched.

Three capabilities did NOT survive the cutover and are pinned as strict xfails
rather than quietly dropped: attribute ALIASES (register / rename / retire),
per-type computed FUNCTIONS in the viewer payload, and a 4xx for a reserved
attribute name. Each xfail names the module and mechanism.

``GET /kgs/{kg}/types/{type}/usage`` was ported to GraphStore by ONTA-535
(via ``explore_store.type_summary``); its tests seed the catalog + instances
and assert ``mock_neptune.query`` is never called.
"""

from __future__ import annotations

import asyncio

import pytest

from infona_client.graph.iri import IRI_BASE
from infona_client.graph.kg_writer import insert_facts
from infona_client.graph.ontology_catalog import (
    list_attributes as cat_list_attributes,
    list_types as cat_list_types,
    upsert_attribute as cat_upsert_attribute,
    upsert_type as cat_upsert_type,
)
from infona_client.graph.ontology_queries import entity_uri
from infona_client.graph.store import get_graph_store

TENANT = "test-tenant"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
LABEL = "http://www.w3.org/2000/01/rdf-schema#label"


def _run(coro):
    """Drive a coroutine from a sync (TestClient) test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def store():
    """The process GraphStore conftest installed for this test."""
    return get_graph_store()


def _seed_type(store, name: str, *, description: str = "", parent: str | None = None):
    return _run(
        cat_upsert_type(
            name=name,
            description=description,
            parent_type=parent,
            layer="tenant",
            tenant_id=TENANT,
            store=store,
        )
    )


def _seed_attr(store, type_name: str, attr: str, *, datatype: str = "string",
               description: str = ""):
    return _run(
        cat_upsert_attribute(
            type_name=type_name,
            attr_name=attr,
            datatype=datatype,
            description=description,
            layer="tenant",
            tenant_id=TENANT,
            store=store,
        )
    )


def _catalog_types(store, tenant: str = TENANT) -> dict:
    return {t.name: t for t in _run(cat_list_types(tenant_id=tenant, store=store))}


def _catalog_attrs(store, tenant: str = TENANT) -> dict:
    return {
        (a.domain, a.name): a
        for a in _run(cat_list_attributes(tenant_id=tenant, store=store))
    }


def _seed_entities(store, kg: str, type_name: str, n: int):
    graph = f"{IRI_BASE}/graphs/{TENANT}/kg/{kg}"
    for i in range(n):
        uri = entity_uri(type_name, f"{type_name.lower()}-{i}")
        _run(
            insert_facts(
                None,
                graph,
                [
                    (uri, RDF_TYPE, f"{IRI_BASE}/types/{type_name}"),
                    (uri, LABEL, f"{type_name} {i}"),
                ],
                store=store,
            )
        )


# ---------------------------------------------------------------------------
# Writes — the type/attribute/subtype declarations land in the tenant catalog
# ---------------------------------------------------------------------------


def test_create_type(client, auth_headers, mock_neptune, store):
    """One POST declares the type AND its attributes in the tenant catalog.

    (Was: scrape the emitted SPARQL for "Place"/"coordinates" and count
    ``update`` calls. The write emits no SPARQL now, so read it back instead —
    which is a stronger check than the substring ever was.)
    """
    response = client.post(
        f"/graphs/{TENANT}/ontology/types",
        headers=auth_headers,
        json={
            "name": "Place",
            "description": "A geographic location",
            "attributes": [
                {"name": "place_name", "datatype": "string"},
                {"name": "coordinates", "datatype": "string"},
            ],
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["created"] == "Place"
    assert data["attributes"] == 2

    types = _catalog_types(store)
    assert "Place" in types
    assert types["Place"].description == "A geographic location"
    assert types["Place"].layer == "tenant"
    assert types["Place"].tenant_id == TENANT

    attrs = _catalog_attrs(store)
    assert ("Place", "place_name") in attrs
    assert ("Place", "coordinates") in attrs
    assert attrs[("Place", "coordinates")].kind == "literal"
    assert attrs[("Place", "coordinates")].datatype == "string"

    mock_neptune.update.assert_not_called()
    mock_neptune.query.assert_not_called()


def test_create_type_with_parent(client, auth_headers, mock_neptune, store):
    response = client.post(
        f"/graphs/{TENANT}/ontology/types",
        headers=auth_headers,
        json={"name": "Park", "parent_type": "Place"},
    )
    assert response.status_code == 201
    assert response.json()["created"] == "Park"
    # The hierarchy edge is the point of the route — assert it, not just the 201.
    assert _catalog_types(store)["Park"].parent_type == "Place"
    mock_neptune.update.assert_not_called()


def test_add_attributes(client, auth_headers, mock_neptune, store):
    _seed_type(store, "Place")
    response = client.post(
        f"/graphs/{TENANT}/ontology/types/Place/attributes",
        headers=auth_headers,
        json={"attributes": [{"name": "elevation", "datatype": "float"}]},
    )
    assert response.status_code == 201
    assert response.json()["attributes_added"] == 1
    elevation = _catalog_attrs(store)[("Place", "elevation")]
    assert elevation.kind == "literal"
    assert elevation.datatype == "float"
    mock_neptune.update.assert_not_called()


def test_add_subtype(client, auth_headers, mock_neptune, store):
    _seed_type(store, "Place")
    response = client.post(
        f"/graphs/{TENANT}/ontology/types/Place/subtypes",
        headers=auth_headers,
        json={"subtype": "Restaurant"},
    )
    assert response.status_code == 201
    assert response.json()["subtype"] == "Restaurant"
    assert _catalog_types(store)["Restaurant"].parent_type == "Place"
    mock_neptune.update.assert_not_called()


def test_relationship_attribute_keeps_its_target_type(
    client, auth_headers, mock_neptune, store
):
    """A type-ranged attribute is a relationship, and the range is preserved.

    The route's response shape flattens relationships into ``attributes`` with
    the target type as ``datatype`` (the pre-layered TypeResponse contract the
    CLI/Explorer read), so pin both halves: the catalog knows it is a
    relationship, and the wire shape still carries the target type name.
    """
    _seed_type(store, "Place")
    r = client.post(
        f"/graphs/{TENANT}/ontology/types/Place/attributes",
        headers=auth_headers,
        json={"attributes": [{"name": "located_in", "datatype": "Region"}]},
    )
    assert r.status_code == 201
    rel = _catalog_attrs(store)[("Place", "located_in")]
    assert rel.kind == "relationship"
    assert rel.range_type == "Region"

    detail = client.get(f"/graphs/{TENANT}/ontology/types/Place", headers=auth_headers)
    assert detail.status_code == 200
    by_name = {a["name"]: a for a in detail.json()["attributes"]}
    assert by_name["located_in"]["datatype"] == "Region"
    mock_neptune.query.assert_not_called()


# ---------------------------------------------------------------------------
# Reads — list / detail / schema off the tenant catalog
# ---------------------------------------------------------------------------


def test_list_types(client, auth_headers, mock_neptune, store):
    _seed_type(store, "Place", description="A location")
    response = client.get(f"/graphs/{TENANT}/ontology/types", headers=auth_headers)
    assert response.status_code == 200
    types = response.json()
    assert len(types) == 1
    assert types[0]["name"] == "Place"
    assert types[0]["description"] == "A location"
    mock_neptune.query.assert_not_called()


def test_list_types_is_scoped_to_the_calling_tenant(
    client, auth_headers, mock_neptune, store
):
    """A peer workspace's type must never surface on this tenant's read.

    The catalog is one store keyed by ``tenant_id``; this is the assertion the
    old named-graph ``FROM`` clause stood in for.
    """
    _seed_type(store, "Place", description="A location")
    _run(
        cat_upsert_type(
            name="PeerOnly",
            description="peer-secret",
            layer="tenant",
            tenant_id="peer-tenant",
            store=store,
        )
    )
    response = client.get(f"/graphs/{TENANT}/ontology/types", headers=auth_headers)
    assert response.status_code == 200
    names = {t["name"] for t in response.json()}
    assert names == {"Place"}
    assert "peer-secret" not in response.text
    mock_neptune.query.assert_not_called()


def test_get_type_detail(client, auth_headers, mock_neptune, store):
    """Detail folds the type, its declared attributes, and its subtypes."""
    _seed_type(store, "Place", description="A location")
    _seed_attr(store, "Place", "place_name")
    _seed_type(store, "Park", parent="Place")

    response = client.get(
        f"/graphs/{TENANT}/ontology/types/Place", headers=auth_headers
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Place"
    assert data["description"] == "A location"
    assert [a["name"] for a in data["attributes"]] == ["place_name"]
    assert data["subtypes"] == ["Park"]
    mock_neptune.query.assert_not_called()


def test_get_type_not_found(client, auth_headers, mock_neptune, store):
    _seed_type(store, "Place")
    response = client.get(
        f"/graphs/{TENANT}/ontology/types/Nonexistent", headers=auth_headers
    )
    assert response.status_code == 404
    mock_neptune.query.assert_not_called()


def test_get_full_schema(client, auth_headers, mock_neptune, store):
    _seed_type(store, "Place")
    _seed_attr(store, "Place", "place_name")
    response = client.get(f"/graphs/{TENANT}/ontology/schema", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "Place" in data["types"]
    assert data["types"]["Place"]["attributes"][0]["name"] == "place_name"
    assert data["types"]["Place"]["layer"] == "tenant"
    assert data["entitled"] is False
    assert data["tenant_id"] == TENANT
    mock_neptune.query.assert_not_called()


@pytest.mark.xfail(
    reason=(
        "LOST CAPABILITY (ONTA-527): type-attached FUNCTIONS are gone from the "
        "ontology payload, on BOTH sides. Read: they were projected out of the "
        "ontology graph by graph/ontology_queries.py::full_ontology_detail_query "
        "(?funcName) and folded in by graph/global_ontology.py::fetch_ontology; "
        "the replacement, api/routes/ontology.py::_workspace_ontology_store, "
        "hardcodes functions=[] on every WorkspaceOntologyType because "
        "ontology_catalog has no function records. Write: POST "
        "/graphs/{t}/functions still builds graph/queries.py::"
        "register_function_triple and awaits client.update, i.e. it is SPARQL-"
        "only and lands nowhere in production — here it 201s against the mock "
        "and stores nothing. Both halves need a catalog port; the ontology "
        "viewer currently reports every type as having no functions."
    ),
    strict=True,
)
def test_type_detail_lists_attached_functions(client, auth_headers, store):
    _seed_type(store, "Place")
    reg = client.post(
        f"/graphs/{TENANT}/functions",
        headers=auth_headers,
        json={
            "name": "calculate_distance",
            "entity_type": "Place",
            "endpoint_url": "https://example.com/distance",
        },
    )
    assert reg.status_code == 201, reg.text

    response = client.get(
        f"/graphs/{TENANT}/ontology/types/Place", headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["functions"] == ["calculate_distance"]


def test_create_type_rejects_a_reserved_attribute_name_with_a_4xx(
    client, auth_headers
):
    response = client.post(
        f"/graphs/{TENANT}/ontology/types",
        headers=auth_headers,
        json={
            "name": "Place",
            "description": "A geographic location",
            "attributes": [{"name": "name", "datatype": "string"}],
        },
    )
    assert response.status_code in (400, 422), response.text
    assert "name" in response.text


# ---------------------------------------------------------------------------
# Attribute aliases (ONTA-407a) — the whole lifecycle is a no-op on Neo4j
# ---------------------------------------------------------------------------
#
# Every alias op reaches graph/ontology_commit.py::commit_ontology, which
# dispatches to _commit_ontology_graph_store whenever a GraphStore is
# configured — i.e. always, in production. That function handles exactly
# UPSERT_TYPE / UPSERT_ATTRIBUTE / UPSERT_RELATIONSHIP / SET_SUBCLASS and drops
# everything else on the floor with an "ontology_store_op_skipped" warning. The
# routes report success regardless, so all four cases below fail on the CONTRACT
# (not on a SPARQL string), which is why they are ported-then-xfailed rather
# than deleted: they are the acceptance criteria for the catalog port.


@pytest.mark.xfail(
    reason=(
        "LOST CAPABILITY (ONTA-527): POST /ontology/aliases silently writes "
        "nothing. graph/ontology_commit.py::_commit_ontology_graph_store has no "
        "REGISTER_ALIAS branch — the mutation is logged as "
        "'ontology_store_op_skipped' — so graph/aliases.py::register_alias never "
        "runs, the route returns 201 with old_attr_uri/new_attr_uri = null, and "
        "GET /ontology/aliases (fetch_alias_map, still SPARQL) returns {}. "
        "Attribute rename/alias needs an OntoAttr-level catalog port."
    ),
    strict=True,
)
def test_register_and_list_aliases(client, auth_headers, store):
    """POST /aliases records old→new; GET returns the map."""
    from infona_client.graph.ontology_queries import attr_uri

    old_uri = attr_uri("Guest", "phone_num")
    new_uri = attr_uri("Guest", "phone")

    reg = client.post(
        f"/graphs/{TENANT}/ontology/aliases",
        headers=auth_headers,
        json={"type_name": "Guest", "from_slot": "phone_num", "to_slot": "phone"},
    )
    assert reg.status_code == 201, reg.text
    body = reg.json()
    assert body["from_slot"] == "phone_num"
    assert body["to_slot"] == "phone"
    assert body["old_attr_uri"] == old_uri
    assert body["new_attr_uri"] == new_uri

    listed = client.get(f"/graphs/{TENANT}/ontology/aliases", headers=auth_headers)
    assert listed.status_code == 200
    assert listed.json()["aliases"][old_uri] == new_uri


@pytest.mark.xfail(
    reason=(
        "LOST CAPABILITY (ONTA-527): aliasing a slot to ITSELF is no longer "
        "rejected. The 400 came from graph/aliases.py::register_alias raising "
        "ValueError, which the route maps to HTTP 400; on the GraphStore path "
        "_commit_ontology_graph_store never reaches that validation (no "
        "REGISTER_ALIAS branch), so the nonsense request is answered 201. A "
        "validation gate that only exists inside a skipped branch is no gate."
    ),
    strict=True,
)
def test_register_alias_rejects_self(client, auth_headers):
    response = client.post(
        f"/graphs/{TENANT}/ontology/aliases",
        headers=auth_headers,
        json={"type_name": "Guest", "from_slot": "phone", "to_slot": "phone"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"].lower()
    assert "different" in detail or "itself" in detail


@pytest.mark.xfail(
    reason=(
        "LOST CAPABILITY (ONTA-527): POST /ontology/aliases/rename is a no-op. "
        "RENAME_ATTRIBUTE has no branch in "
        "graph/ontology_commit.py::_commit_ontology_graph_store, so the new "
        "declaration is not written, the old one is not dropped, no alias edge "
        "is recorded, and the route still answers 201 with null URIs and an "
        "empty change_records list."
    ),
    strict=True,
)
def test_rename_attribute_via_api(client, auth_headers):
    """POST /aliases/rename renames the slot AND always leaves an alias."""
    from infona_client.graph.ontology_queries import attr_uri

    old_uri = attr_uri("Guest", "phone_num")
    new_uri = attr_uri("Guest", "phone")

    reg = client.post(
        f"/graphs/{TENANT}/ontology/aliases/rename",
        headers=auth_headers,
        json={
            "type_name": "Guest",
            "from_slot": "phone_num",
            "to_slot": "phone",
            "datatype": "string",
        },
    )
    assert reg.status_code == 201, reg.text
    body = reg.json()
    assert body["old_attr_uri"] == old_uri
    assert body["new_attr_uri"] == new_uri

    listed = client.get(f"/graphs/{TENANT}/ontology/aliases", headers=auth_headers)
    assert listed.json()["aliases"][old_uri] == new_uri


@pytest.mark.xfail(
    reason=(
        "LOST CAPABILITY with a data-integrity edge (ONTA-527): DELETE "
        "/ontology/aliases answers 200 {'retired': true} unconditionally. "
        "RETIRE_ALIAS has no branch in "
        "graph/ontology_commit.py::_commit_ontology_graph_store, so "
        "graph/aliases.py::retire_alias — which is what counts remaining "
        "instance references and raises AliasStillReferencedError → HTTP 409 — "
        "never runs. The route therefore reports a retirement that did not "
        "happen, and the refusal that protects un-backfilled instance data is "
        "gone with it."
    ),
    strict=True,
)
def test_retire_alias_conflict_when_refs_remain(client, auth_headers, store):
    """DELETE /aliases must 409 while instance triples still use the old slot."""
    from infona_client.graph.ontology_queries import attr_uri

    old_uri = attr_uri("Guest", "phone_num")
    guest = entity_uri("Guest", "g1")
    _run(
        insert_facts(
            None,
            f"{IRI_BASE}/graphs/{TENANT}/kg/main",
            [
                (guest, RDF_TYPE, f"{IRI_BASE}/types/Guest"),
                (guest, old_uri, "555-0100"),
            ],
            store=store,
        )
    )
    client.post(
        f"/graphs/{TENANT}/ontology/aliases",
        headers=auth_headers,
        json={"type_name": "Guest", "from_slot": "phone_num", "to_slot": "phone"},
    )

    resp = client.request(
        "DELETE",
        f"/graphs/{TENANT}/ontology/aliases",
        headers=auth_headers,
        json={"type_name": "Guest", "from_slot": "phone_num", "kg_name": "main"},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "alias_still_referenced"
    assert detail["remaining"] >= 1
    assert detail["old_attr_uri"] == old_uri


# ---------------------------------------------------------------------------
# /kgs/{kg}/type-counts  (GraphStore path — graph/explore_store.py::type_counts)
# ---------------------------------------------------------------------------


def test_type_counts_empty_kg(client, auth_headers, mock_neptune):
    response = client.get(
        f"/graphs/{TENANT}/kgs/empty/type-counts", headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json() == []
    mock_neptune.query.assert_not_called()


def test_type_counts_multiple_types_sorted(client, auth_headers, mock_neptune, store):
    """Ordered by entity count, descending — the Explorer rail's contract."""
    _seed_entities(store, "mentors", "Industry", 1)
    _seed_entities(store, "mentors", "Mentor", 4)
    _seed_entities(store, "mentors", "Skill", 2)

    response = client.get(
        f"/graphs/{TENANT}/kgs/mentors/type-counts", headers=auth_headers
    )
    assert response.status_code == 200
    data = response.json()
    assert [t["name"] for t in data] == ["Mentor", "Skill", "Industry"]
    assert data[0]["entity_count"] == 4
    mock_neptune.query.assert_not_called()


def test_type_counts_are_scoped_to_one_kg_of_one_tenant(
    client, auth_headers, mock_neptune, store
):
    """A sibling KG and a peer workspace both hold Mentors; neither is counted."""
    _seed_entities(store, "mentors", "Mentor", 2)
    _seed_entities(store, "other-kg", "Mentor", 5)
    peer_graph = f"{IRI_BASE}/graphs/peer-tenant/kg/mentors"
    peer = entity_uri("Mentor", "peer-0")
    _run(
        insert_facts(
            None,
            peer_graph,
            [(peer, RDF_TYPE, f"{IRI_BASE}/types/Mentor"), (peer, LABEL, "Peer")],
            store=store,
        )
    )

    response = client.get(
        f"/graphs/{TENANT}/kgs/mentors/type-counts", headers=auth_headers
    )
    assert response.status_code == 200
    data = response.json()
    assert [(t["name"], t["entity_count"]) for t in data] == [("Mentor", 2)]
    # The response shape is unchanged; the two index flags are deliberately NOT
    # asserted True/False here — knowledge_graphs.py::list_type_counts documents
    # them as always False on the GraphStore path "until stats port", so pinning
    # a value would cement a known gap.
    assert {"spatially_indexed", "temporally_indexed"} <= set(data[0])
    mock_neptune.query.assert_not_called()


# ---------------------------------------------------------------------------
# /kgs/{kg}/types/{name}/usage — GraphStore path (ONTA-535)
# ---------------------------------------------------------------------------
#
# Ported onto explore_store.type_summary + list_entities_by_type. Seeded
# through insert_facts / ontology_catalog; mock_neptune must never be called.


def test_type_usage_unknown_type_returns_404(client, auth_headers, mock_neptune):
    """Neither catalog declaration nor instances → 404."""
    response = client.get(
        f"/graphs/{TENANT}/kgs/mentors/types/Nope/usage",
        headers=auth_headers,
    )
    assert response.status_code == 404
    mock_neptune.query.assert_not_called()


def test_type_usage_combines_ontology_and_kg_counts(
    client, auth_headers, mock_neptune, store
):
    """Ontology description + populated attrs/rels + sample entities."""
    _seed_type(store, "Mentor", description="An ADPList mentor")
    _seed_attr(store, "Mentor", "level", datatype="string")
    _seed_attr(store, "Mentor", "headline", datatype="string")
    _seed_attr(store, "Mentor", "industry", datatype="Industry")  # relationship
    _seed_type(store, "Industry")

    graph = f"{IRI_BASE}/graphs/{TENANT}/kg/mentors"
    industry = entity_uri("Industry", "Tech")
    triples = [
        (industry, RDF_TYPE, f"{IRI_BASE}/types/Industry"),
        (industry, LABEL, "Tech"),
    ]
    # 3 mentors with level; 2 with headline; 2 with industry rel.
    for i in range(3):
        uri = entity_uri("Mentor", f"m{i}")
        triples.append((uri, RDF_TYPE, f"{IRI_BASE}/types/Mentor"))
        triples.append((uri, LABEL, f"Mentor {i}"))
        triples.append((uri, f"{IRI_BASE}/onto/level", "Senior"))
        if i < 2:
            triples.append((uri, f"{IRI_BASE}/onto/headline", f"Headline {i}"))
            triples.append((uri, f"{IRI_BASE}/onto/industry", industry))
    _run(insert_facts(None, graph, triples, store=store))

    response = client.get(
        f"/graphs/{TENANT}/kgs/mentors/types/Mentor/usage",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["name"] == "Mentor"
    assert data["description"] == "An ADPList mentor"
    assert data["entity_count"] == 3

    attrs_by_name = {a["name"]: a for a in data["attributes"]}
    assert "level" in attrs_by_name
    assert attrs_by_name["level"]["count"] == 3
    assert "headline" in attrs_by_name
    assert attrs_by_name["headline"]["count"] == 2
    # Reserved Entity key "name" is never an attribute column.
    assert "name" not in attrs_by_name

    assert len(data["relationships"]) == 1
    assert data["relationships"][0]["name"] == "industry"
    assert data["relationships"][0]["target_type"] == "Industry"
    assert data["relationships"][0]["count"] == 2

    assert len(data["samples"]) >= 1
    assert data["samples"][0]["uri"]
    mock_neptune.query.assert_not_called()


def test_type_usage_hides_system_predicates_by_default(
    client, auth_headers, mock_neptune, store
):
    """Internal/housekeeping keys never surface as attribute columns.

    On the GraphStore path the summary filter is ``is_internal_property_key``
    (same authority as grep/records). ``include_system`` is a SPARQL-branch
    opt-in and is ignored here — internals never become domain columns.
    """
    _seed_type(store, "Mentor")
    _seed_attr(store, "Mentor", "headline", datatype="string")

    graph = f"{IRI_BASE}/graphs/{TENANT}/kg/mentors"
    uri = entity_uri("Mentor", "m1")
    _run(
        insert_facts(
            None,
            graph,
            [
                (uri, RDF_TYPE, f"{IRI_BASE}/types/Mentor"),
                (uri, LABEL, "Some Mentor"),
                (uri, f"{IRI_BASE}/onto/headline", "Principal Engineer"),
                (uri, f"{IRI_BASE}/onto/ingested_at", "2026-04-28T00:00:00Z"),
                (uri, f"{IRI_BASE}/onto/source", "client"),
                (uri, f"{IRI_BASE}/onto/batch_id", "b-1"),
            ],
            store=store,
        )
    )

    for qs in ("", "?include_system=true"):
        response = client.get(
            f"/graphs/{TENANT}/kgs/mentors/types/Mentor/usage{qs}",
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        names = [a["name"] for a in response.json()["attributes"]]
        assert "headline" in names
        assert "ingested_at" not in names
        assert "source" not in names
        assert "batch_id" not in names
        assert "label" not in names
    mock_neptune.query.assert_not_called()
