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
    assert mock_neptune.update.call_count == 3  # 1 type + 2 attributes


def test_create_type_with_parent(client, auth_headers, mock_neptune):
    response = client.post(
        "/graphs/test-tenant/ontology/types",
        headers=auth_headers,
        json={"name": "Park", "parent_type": "Place"},
    )
    assert response.status_code == 201
    assert response.json()["created"] == "Park"


def test_list_types(client, auth_headers, mock_neptune):
    mock_neptune.query.return_value = {
        "head": {"vars": ["type", "label", "comment", "parent"]},
        "results": {
            "bindings": [
                {
                    "type": {"type": "uri", "value": "https://cograph.tech/types/Place"},
                    "label": {"type": "literal", "value": "Place"},
                    "comment": {"type": "literal", "value": "A location"},
                },
            ]
        },
    }
    response = client.get("/graphs/test-tenant/ontology/types", headers=auth_headers)
    assert response.status_code == 200
    types = response.json()
    assert len(types) == 1
    assert types[0]["name"] == "Place"


def test_get_type_detail(client, auth_headers, mock_neptune):
    mock_neptune.query.side_effect = [
        # type detail
        {"head": {"vars": ["label", "comment", "parent"]}, "results": {"bindings": [
            {"label": {"type": "literal", "value": "Place"}, "comment": {"type": "literal", "value": "A location"}},
        ]}},
        # attributes
        {"head": {"vars": ["attr", "attrLabel", "attrComment", "range"]}, "results": {"bindings": [
            {"attr": {"type": "uri", "value": "x"}, "attrLabel": {"type": "literal", "value": "name"},
             "range": {"type": "uri", "value": "http://www.w3.org/2001/XMLSchema#string"}},
        ]}},
        # subtypes
        {"head": {"vars": ["sub", "label"]}, "results": {"bindings": [
            {"sub": {"type": "uri", "value": "x"}, "label": {"type": "literal", "value": "Park"}},
        ]}},
        # functions
        {"head": {"vars": ["name", "endpoint", "desc"]}, "results": {"bindings": [
            {"name": {"type": "literal", "value": "calculate_distance"}},
        ]}},
    ]
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
            {
                "type": {"type": "uri", "value": "https://cograph.tech/types/Place"},
                "typeLabel": {"type": "literal", "value": "Place"},
                "attrLabel": {"type": "literal", "value": "name"},
                "range": {"type": "uri", "value": "http://www.w3.org/2001/XMLSchema#string"},
                "funcName": {"type": "literal", "value": "calculate_distance"},
            },
        ]},
    }
    response = client.get("/graphs/test-tenant/ontology/schema", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "Place" in data["types"]
    assert data["types"]["Place"]["attributes"][0]["name"] == "name"
    assert "calculate_distance" in data["types"]["Place"]["functions"]


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
