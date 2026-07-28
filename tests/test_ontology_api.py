def test_create_type(client, auth_headers, mock_neptune):
    response = client.post(
        "/graphs/test-tenant/ontology/types",
        headers=auth_headers,
        json={
            "name": "Place",
            "description": "A geographic location",
            "attributes": [
                {"name": "name", "datatype": "string"},
                {"name": "coordinates", "datatype": "string"},
            ],
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["created"] == "Place"
    assert data["attributes"] == 2
    # ONTA-403: one commit applies type + 2 attrs (+ revision + changelog).
    all_sparql = " ".join(c[0][0] for c in mock_neptune.update.call_args_list)
    assert "Place" in all_sparql
    assert "coordinates" in all_sparql
    assert mock_neptune.update.call_count >= 3


def test_create_type_with_parent(client, auth_headers, mock_neptune):
    response = client.post(
        "/graphs/test-tenant/ontology/types",
        headers=auth_headers,
        json={"name": "Park", "parent_type": "Place"},
    )
    assert response.status_code == 201
    assert response.json()["created"] == "Park"


def _detail_row(**cells):
    """One full_ontology_detail_query-shaped SPARQL JSON binding row."""
    out = {}
    for k, v in cells.items():
        if isinstance(v, str) and (v.startswith("http://") or v.startswith("https://")):
            out[k] = {"type": "uri", "value": v}
        else:
            out[k] = {"type": "literal", "value": str(v)}
    return out


def test_list_types(client, auth_headers, mock_neptune):
    # ONTA-397: list_types reads via fetch_ontology (full_ontology_detail_query
    # per visible layer). Empty global layers + one tenant type.
    mock_neptune.query.return_value = {
        "head": {"vars": ["type", "typeLabel", "typeComment"]},
        "results": {
            "bindings": [
                _detail_row(
                    type="https://cograph.tech/types/Place",
                    typeLabel="Place",
                    typeComment="A location",
                ),
            ]
        },
    }
    response = client.get("/graphs/test-tenant/ontology/types", headers=auth_headers)
    assert response.status_code == 200
    types = response.json()
    assert len(types) == 1
    assert types[0]["name"] == "Place"
    assert types[0]["description"] == "A location"


def test_get_type_detail(client, auth_headers, mock_neptune):
    # Single layered read: one full_ontology_detail_query result folds type +
    # attrs + functions. Subtypes come from the inverted subClassOf map.
    mock_neptune.query.return_value = {
        "head": {
            "vars": [
                "type", "typeLabel", "typeComment", "parent",
                "attr", "attrLabel", "attrComment", "range",
                "funcName",
            ]
        },
        "results": {
            "bindings": [
                _detail_row(
                    type="https://cograph.tech/types/Place",
                    typeLabel="Place",
                    typeComment="A location",
                    attr="https://cograph.tech/types/Place/attrs/name",
                    attrLabel="name",
                    range="http://www.w3.org/2001/XMLSchema#string",
                    funcName="calculate_distance",
                ),
                # Park subClassOf Place → subtypes of Place includes Park
                _detail_row(
                    type="https://cograph.tech/types/Park",
                    typeLabel="Park",
                    parent="https://cograph.tech/types/Place",
                ),
            ]
        },
    }
    response = client.get("/graphs/test-tenant/ontology/types/Place", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Place"
    assert len(data["attributes"]) == 1
    assert data["attributes"][0]["name"] == "name"
    assert data["subtypes"] == ["Park"]
    assert data["functions"] == ["calculate_distance"]


def test_get_type_not_found(client, auth_headers, mock_neptune):
    mock_neptune.query.return_value = {"head": {"vars": []}, "results": {"bindings": []}}
    response = client.get("/graphs/test-tenant/ontology/types/Nonexistent", headers=auth_headers)
    assert response.status_code == 404


def test_add_attributes(client, auth_headers, mock_neptune):
    response = client.post(
        "/graphs/test-tenant/ontology/types/Place/attributes",
        headers=auth_headers,
        json={"attributes": [{"name": "elevation", "datatype": "float"}]},
    )
    assert response.status_code == 201
    assert response.json()["attributes_added"] == 1


def test_add_subtype(client, auth_headers, mock_neptune):
    response = client.post(
        "/graphs/test-tenant/ontology/types/Place/subtypes",
        headers=auth_headers,
        json={"subtype": "Restaurant"},
    )
    assert response.status_code == 201
    assert response.json()["subtype"] == "Restaurant"


def test_get_full_schema(client, auth_headers, mock_neptune):
    mock_neptune.query.return_value = {
        "head": {"vars": ["type", "typeLabel", "attr", "attrLabel", "range", "funcName"]},
        "results": {"bindings": [
            _detail_row(
                type="https://cograph.tech/types/Place",
                typeLabel="Place",
                attr="https://cograph.tech/types/Place/attrs/name",
                attrLabel="name",
                range="http://www.w3.org/2001/XMLSchema#string",
                funcName="calculate_distance",
            ),
        ]},
    }
    response = client.get("/graphs/test-tenant/ontology/schema", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "Place" in data["types"]
    assert data["types"]["Place"]["attributes"][0]["name"] == "name"
    assert "calculate_distance" in data["types"]["Place"]["functions"]
    assert data["types"]["Place"]["layer"] == "tenant"
    assert data["entitled"] is False


# ---------------------------------------------------------------------------
# Attribute aliases (ONTA-407a)
# ---------------------------------------------------------------------------


def test_register_and_list_aliases(client, auth_headers, mock_neptune):
    """POST /aliases goes through commit_ontology; GET returns the map."""
    from cograph_client.graph.aliases import ALIAS_OF
    from cograph_client.graph.ontology_queries import attr_uri

    old_uri = attr_uri("Guest", "phone_num")
    new_uri = attr_uri("Guest", "phone")

    # After POST, the list endpoint queries alias_map — mock that SELECT.
    mock_neptune.query.return_value = {
        "head": {"vars": ["old", "new"]},
        "results": {
            "bindings": [
                {
                    "old": {"type": "uri", "value": old_uri},
                    "new": {"type": "uri", "value": new_uri},
                }
            ]
        },
    }

    reg = client.post(
        "/graphs/test-tenant/ontology/aliases",
        headers=auth_headers,
        json={
            "type_name": "Guest",
            "from_slot": "phone_num",
            "to_slot": "phone",
        },
    )
    assert reg.status_code == 201, reg.text
    body = reg.json()
    assert body["from_slot"] == "phone_num"
    assert body["to_slot"] == "phone"
    assert body["old_attr_uri"] == old_uri
    assert body["new_attr_uri"] == new_uri
    # One of the updates must be the alias INSERT DATA.
    all_sparql = " ".join(c[0][0] for c in mock_neptune.update.call_args_list)
    assert "aliasOf" in all_sparql or ALIAS_OF in all_sparql
    assert old_uri in all_sparql and new_uri in all_sparql

    listed = client.get("/graphs/test-tenant/ontology/aliases", headers=auth_headers)
    assert listed.status_code == 200
    assert listed.json()["aliases"][old_uri] == new_uri


def test_register_alias_rejects_self(client, auth_headers, mock_neptune):
    response = client.post(
        "/graphs/test-tenant/ontology/aliases",
        headers=auth_headers,
        json={
            "type_name": "Guest",
            "from_slot": "phone",
            "to_slot": "phone",
        },
    )
    assert response.status_code == 400
    assert "different" in response.json()["detail"].lower() or "itself" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# /kgs/{kg}/type-counts and /kgs/{kg}/types/{name}/usage
# ---------------------------------------------------------------------------


def _binding(**kwargs):
    """Build one SPARQL JSON binding row from {var: literal_or_uri}."""
    out = {}
    for k, v in kwargs.items():
        if isinstance(v, str) and (v.startswith("http://") or v.startswith("https://")):
            out[k] = {"type": "uri", "value": v}
        else:
            out[k] = {"type": "literal", "value": str(v)}
    return out


def _results(vars_, *rows):
    return {
        "head": {"vars": list(vars_)},
        "results": {"bindings": list(rows)},
    }


def test_type_counts_empty_kg(client, auth_headers, mock_neptune):
    mock_neptune.query.return_value = _results(["type", "cnt"])
    response = client.get(
        "/graphs/test-tenant/kgs/empty/type-counts",
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json() == []


def test_type_counts_multiple_types_sorted(client, auth_headers, mock_neptune):
    # Server orders by COUNT desc; this test confirms the response shape and
    # that nested URIs (e.g. /types/X/attrs/y) get filtered out of the list.
    mock_neptune.query.return_value = _results(
        ["type", "cnt"],
        _binding(type="https://cograph.tech/types/Mentor", cnt="988"),
        _binding(type="https://cograph.tech/types/Skill", cnt="412"),
        _binding(type="https://cograph.tech/types/Mentor/attrs/name", cnt="988"),
        _binding(type="https://cograph.tech/types/Industry", cnt="38"),
    )
    response = client.get(
        "/graphs/test-tenant/kgs/mentors/type-counts",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert [t["name"] for t in data] == ["Mentor", "Skill", "Industry"]
    assert data[0]["entity_count"] == 988


def test_type_usage_unknown_type_returns_404(client, auth_headers, mock_neptune):
    # Ontology lookup empty AND entity count is 0 → 404.
    mock_neptune.query.side_effect = [
        _results(["label", "comment", "parent"]),  # ontology empty
        _results(["attr", "attrLabel", "attrComment", "range"]),  # no attrs
        _results(["n"], _binding(n="0")),  # zero entities
    ]
    response = client.get(
        "/graphs/test-tenant/kgs/mentors/types/Nope/usage",
        headers=auth_headers,
    )
    assert response.status_code == 404


def test_type_usage_combines_ontology_and_kg_counts(client, auth_headers, mock_neptune):
    name_attr = "https://cograph.tech/types/Mentor/attrs/name"
    level_attr = "https://cograph.tech/types/Mentor/attrs/level"
    industry_attr = "https://cograph.tech/types/Mentor/attrs/industry"
    industry_target = "https://cograph.tech/types/Industry"

    mock_neptune.query.side_effect = [
        # 1) Ontology definition
        _results(
            ["label", "comment", "parent"],
            _binding(label="Mentor", comment="An ADPList mentor"),
        ),
        # 2) Attribute definitions in ontology
        _results(
            ["attr", "attrLabel", "attrComment", "range"],
            {
                "attr": {"type": "uri", "value": name_attr},
                "attrLabel": {"type": "literal", "value": "name"},
                "range": {"type": "uri", "value": "http://www.w3.org/2001/XMLSchema#string"},
            },
            {
                "attr": {"type": "uri", "value": level_attr},
                "attrLabel": {"type": "literal", "value": "level"},
                "range": {"type": "uri", "value": "http://www.w3.org/2001/XMLSchema#string"},
            },
            {
                "attr": {"type": "uri", "value": industry_attr},
                "attrLabel": {"type": "literal", "value": "industry"},
                "range": {"type": "uri", "value": industry_target},
            },
        ),
        # 3) Entity count for Mentor
        _results(["n"], _binding(n="988")),
        # 4) Predicate usage in KG
        _results(
            ["p", "cnt", "sample"],
            {
                "p": {"type": "uri", "value": name_attr},
                "cnt": {"type": "literal", "value": "988"},
                "sample": {"type": "literal", "value": "Karthikeyan"},
            },
            {
                "p": {"type": "uri", "value": level_attr},
                "cnt": {"type": "literal", "value": "412"},
                "sample": {"type": "literal", "value": "Senior"},
            },
            {
                "p": {"type": "uri", "value": industry_attr},
                "cnt": {"type": "literal", "value": "740"},
                "sample": {
                    "type": "uri",
                    "value": "https://cograph.tech/entities/Industry/Tech",
                },
            },
        ),
        # 5) Sample entities
        _results(
            ["e", "name", "title", "label", "headline"],
            {
                "e": {"type": "uri", "value": "https://cograph.tech/entities/Mentor/karthikeyan"},
                "name": {"type": "literal", "value": "Karthikeyan Rajasekaran"},
                "title": {"type": "literal", "value": "Principal Software Engineer"},
            },
        ),
    ]
    response = client.get(
        "/graphs/test-tenant/kgs/mentors/types/Mentor/usage",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Mentor"
    assert data["description"] == "An ADPList mentor"
    assert data["entity_count"] == 988
    # Two literal attributes (name, level), one relationship (industry).
    assert [a["name"] for a in data["attributes"]] == ["name", "level"]
    assert data["attributes"][0]["count"] == 988
    assert data["attributes"][1]["count"] == 412
    assert len(data["relationships"]) == 1
    assert data["relationships"][0]["name"] == "industry"
    assert data["relationships"][0]["target_type"] == "Industry"
    assert data["relationships"][0]["count"] == 740
    assert len(data["samples"]) == 1
    assert data["samples"][0]["label"] == "Karthikeyan Rajasekaran"


def test_type_usage_hides_system_predicates_by_default(client, auth_headers, mock_neptune):
    """Auto-attached system predicates (rdfs:label, ingested_at, source) are
    100% on every entity and crowd out the columns the user actually cares
    about. /type usage filters them out by default; ?include_system=true
    opts back in."""
    name_attr = "https://cograph.tech/types/Mentor/attrs/name"
    sys_label = "http://www.w3.org/2000/01/rdf-schema#label"
    sys_ingested = "https://cograph.tech/onto/ingested_at"
    sys_source = "https://cograph.tech/onto/source"

    def _build_responses():
        return [
            # ontology
            _results(
                ["label", "comment", "parent"],
                _binding(label="Mentor"),
            ),
            # attribute defs
            _results(
                ["attr", "attrLabel", "attrComment", "range"],
                {
                    "attr": {"type": "uri", "value": name_attr},
                    "attrLabel": {"type": "literal", "value": "name"},
                    "range": {"type": "uri", "value": "http://www.w3.org/2001/XMLSchema#string"},
                },
            ),
            # entity count
            _results(["n"], _binding(n="1000")),
            # predicate usage — three system + one user
            _results(
                ["p", "cnt", "sample"],
                {
                    "p": {"type": "uri", "value": sys_label},
                    "cnt": {"type": "literal", "value": "1000"},
                    "sample": {"type": "literal", "value": "Some Mentor"},
                },
                {
                    "p": {"type": "uri", "value": sys_ingested},
                    "cnt": {"type": "literal", "value": "1000"},
                    "sample": {"type": "literal", "value": "2026-04-28T00:00:00Z"},
                },
                {
                    "p": {"type": "uri", "value": sys_source},
                    "cnt": {"type": "literal", "value": "1000"},
                    "sample": {"type": "literal", "value": "client"},
                },
                {
                    "p": {"type": "uri", "value": name_attr},
                    "cnt": {"type": "literal", "value": "988"},
                    "sample": {"type": "literal", "value": "Karthikeyan"},
                },
            ),
            # samples
            _results(["e", "name", "title", "label", "headline"]),
        ]

    # Default: system predicates filtered out.
    mock_neptune.query.side_effect = _build_responses()
    response = client.get(
        "/graphs/test-tenant/kgs/mentors/types/Mentor/usage",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    names = [a["name"] for a in data["attributes"]]
    assert names == ["name"]
    assert "rdf-schema#label" not in names
    assert "ingested_at" not in names
    assert "source" not in names

    # Opt-in: all four predicates present.
    mock_neptune.reset_mock()
    mock_neptune.query.side_effect = _build_responses()
    response = client.get(
        "/graphs/test-tenant/kgs/mentors/types/Mentor/usage?include_system=true",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    names = [a["name"] for a in data["attributes"]]
    assert len(names) == 4
    assert "rdf-schema#label" in names
    assert "ingested_at" in names
    assert "source" in names
    assert "name" in names
