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
                    "type": {"type": "uri", "value": "https://omnix.dev/types/Place"},
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
                "type": {"type": "uri", "value": "https://omnix.dev/types/Place"},
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
