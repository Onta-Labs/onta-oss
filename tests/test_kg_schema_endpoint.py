"""KG-scoped, population-aware schema endpoint (ONTA-418).

`view_ontology` is tenant-wide and DECLARATION-only, so an agent could not tell
which attributes are actually populated in a given context graph and guessed
names (`fda_indications` vs `indications`). `GET
/graphs/{tenant}/explore/kgs/{kg}/schema` answers that in ONE backend call.

The load-bearing properties under test:

  * whole-KG read, not a per-type fan-out, the stats graph is queried with the
    type binding DROPPED (2 queries), never once per type;
  * declared-but-empty types and attributes are INCLUDED and marked, never
    hidden (ONTA-248 / ONTA-258: hiding them made agents assert the type does
    not exist, and a transient throttle returns count 0 exactly like a
    genuinely-empty attribute);
  * the same predicate hygiene as the per-type summary (internal ER/batch
    predicates and legacy provenance companions stay off the payload);
  * a legacy KG without materialized stats falls back to ONE live scan.
"""
import os

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

os.environ["INFONA_API_KEYS"] = '{"test-key": "test-tenant"}'
os.environ["INFONA_NEPTUNE_ENDPOINT"] = "http://fake-neptune:8182"

from infona_client.api.app import create_app
from infona_client.graph.client import NeptuneClient

TENANT = "test-tenant"
KG = "test"
TYPES = "https://graph.infona.ai/types/"
ENTITIES = "https://graph.infona.ai/entities/"
ONTO = "https://graph.infona.ai/onto/"
SCHEMA_URL = f"/graphs/{TENANT}/explore/kgs/{KG}/schema"


@pytest.fixture
def mock_neptune():
    client = AsyncMock(spec=NeptuneClient)
    client.health.return_value = True
    client.update.return_value = None
    return client


@pytest.fixture
def client(mock_neptune):
    app = create_app()
    app.state.neptune_client = mock_neptune
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"X-API-Key": "test-key"}


def _rows(*binding_dicts):
    variables = []
    for b in binding_dicts:
        for k in b:
            if k not in variables:
                variables.append(k)
    return {
        "head": {"vars": variables},
        "results": {"bindings": [
            {k: {"value": v} for k, v in b.items()} for b in binding_dicts
        ]},
    }


def _is_entity_count_q(s):
    return "entityCount" in s and "forType" not in s


def _is_pred_stats_q(s):
    return "forType" in s and "forPred" in s


def _is_type_decl_q(s):
    return "#Class>" in s


def _is_attr_decl_q(s):
    return "?domain" in s and "#Property>" in s


def _is_live_scan_q(s):
    return "GROUP BY ?type ?p" in s


def _drug_stats():
    """A Drug type with one populated attribute, one empty one, one relationship."""
    return {
        "entity_count": _rows({"type": TYPES + "Drug", "ec": "120", "sp": "", "tp": ""}),
        "preds": _rows(
            {"type": TYPES + "Drug", "pred": TYPES + "Drug/attrs/indications",
             "cnt": "90", "rel": "0", "target": ""},
            {"type": TYPES + "Drug", "pred": TYPES + "Drug/attrs/brand_name",
             "cnt": "120", "rel": "0", "target": ""},
            {"type": TYPES + "Drug", "pred": ONTO + "manufacturer",
             "cnt": "60", "rel": "60", "target": TYPES + "Company"},
        ),
    }


def _route_factory(stats, decl_types=None, decl_attrs=None, live=None):
    def route(sparql, *a, **k):
        if _is_entity_count_q(sparql):
            return stats["entity_count"]
        if _is_pred_stats_q(sparql):
            return stats["preds"]
        if _is_type_decl_q(sparql):
            return decl_types if decl_types is not None else _rows()
        if _is_attr_decl_q(sparql):
            return decl_attrs if decl_attrs is not None else _rows()
        if _is_live_scan_q(sparql):
            return live if live is not None else _rows()
        return _rows()
    return route


def _by_name(payload):
    return {t["name"]: t for t in payload["types"]}


def test_schema_returns_population_per_type(client, mock_neptune, auth_headers):
    stats = _drug_stats()
    decl_types = _rows({"type": TYPES + "Drug", "label": "Drug",
                        "comment": "A pharmaceutical product", "parent": ""})
    decl_attrs = _rows(
        {"domain": TYPES + "Drug", "attr": TYPES + "Drug/attrs/indications",
         "attrLabel": "indications", "range": ""},
        {"domain": TYPES + "Drug", "attr": TYPES + "Drug/attrs/brand_name",
         "attrLabel": "brand_name", "range": ""},
    )
    mock_neptune.query.side_effect = _route_factory(stats, decl_types, decl_attrs)

    resp = client.get(SCHEMA_URL, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["kg"] == KG
    assert body["stats_source"] == "precomputed"

    drug = _by_name(body)["Drug"]
    assert drug["entity_count"] == 120
    assert drug["description"] == "A pharmaceutical product"
    attrs = {a["name"]: a for a in drug["attributes"]}
    # Real coverage, not a declaration list: `indications` is on 90 of 120.
    assert attrs["indications"]["count"] == 90
    assert attrs["indications"]["coverage_pct"] == 75.0
    assert attrs["indications"]["populated"] is True
    assert attrs["brand_name"]["coverage_pct"] == 100.0
    # Relationships are separated out and carry their target type.
    rels = {r["name"]: r for r in drug["relationships"]}
    assert rels["manufacturer"]["target_type"] == "Company"
    assert rels["manufacturer"]["coverage_pct"] == 50.0
    # Attributes are sorted by coverage desc so the agent reads the useful ones first.
    assert [a["name"] for a in drug["attributes"]] == ["brand_name", "indications"]


def test_declared_but_empty_type_is_included_and_marked(client, mock_neptune, auth_headers):
    # ONTA-258: a type declared in the ontology with zero instances in THIS KG
    # must still appear (marked), or the agent asserts it does not exist.
    stats = _drug_stats()
    decl_types = _rows(
        {"type": TYPES + "Drug", "label": "Drug", "comment": "", "parent": ""},
        {"type": TYPES + "ClinicalTrial", "label": "ClinicalTrial",
         "comment": "", "parent": ""},
    )
    mock_neptune.query.side_effect = _route_factory(stats, decl_types)

    body = client.get(SCHEMA_URL, headers=auth_headers).json()
    types = _by_name(body)
    assert "ClinicalTrial" in types
    trial = types["ClinicalTrial"]
    assert trial["entity_count"] == 0
    assert trial["populated"] is False
    assert trial["declared_only"] is True
    assert types["Drug"]["declared_only"] is False
    # Populated types sort first.
    assert body["types"][0]["name"] == "Drug"

    # include_empty=false is the OPT-IN that hides them.
    body = client.get(SCHEMA_URL, params={"include_empty": "false"},
                      headers=auth_headers).json()
    assert "ClinicalTrial" not in _by_name(body)


def test_declared_but_unpopulated_attribute_is_kept_and_marked(
    client, mock_neptune, auth_headers
):
    # ONTA-248: never drop a slot because count == 0, a transient Neptune
    # throttle returns 0 identically to genuinely-empty, so dropping makes the
    # attribute flicker in and out across identical calls. Annotate instead.
    stats = _drug_stats()
    decl_types = _rows({"type": TYPES + "Drug", "label": "Drug", "comment": "", "parent": ""})
    decl_attrs = _rows(
        {"domain": TYPES + "Drug", "attr": TYPES + "Drug/attrs/indications",
         "attrLabel": "indications", "range": ""},
        # Declared, zero instances anywhere in this KG.
        {"domain": TYPES + "Drug", "attr": TYPES + "Drug/attrs/fda_indications",
         "attrLabel": "fda_indications", "range": ""},
    )
    mock_neptune.query.side_effect = _route_factory(stats, decl_types, decl_attrs)

    drug = _by_name(client.get(SCHEMA_URL, headers=auth_headers).json())["Drug"]
    attrs = {a["name"]: a for a in drug["attributes"]}
    assert "fda_indications" in attrs
    assert attrs["fda_indications"]["count"] == 0
    assert attrs["fda_indications"]["populated"] is False
    # The populated one is unambiguously distinguishable, the whole point of
    # the tool (fda_indications vs indications).
    assert attrs["indications"]["populated"] is True


def test_declared_relationship_is_not_duplicated_by_its_declaration(
    client, mock_neptune, auth_headers
):
    # A populated relationship's INSTANCE predicate is `onto/<leaf>` while its
    # ontology declaration URI is `types/<T>/attrs/<leaf>` (the instance-edge
    # convention). Synthesizing declared slots naively would list it twice.
    stats = _drug_stats()
    decl_types = _rows({"type": TYPES + "Drug", "label": "Drug", "comment": "", "parent": ""})
    decl_attrs = _rows(
        {"domain": TYPES + "Drug", "attr": TYPES + "Drug/attrs/manufacturer",
         "attrLabel": "manufacturer", "range": TYPES + "Company"},
    )
    mock_neptune.query.side_effect = _route_factory(stats, decl_types, decl_attrs)

    drug = _by_name(client.get(SCHEMA_URL, headers=auth_headers).json())["Drug"]
    names = [r["name"] for r in drug["relationships"]] + [
        a["name"] for a in drug["attributes"]
    ]
    assert names.count("manufacturer") == 1
    rel = next(r for r in drug["relationships"] if r["name"] == "manufacturer")
    assert rel["count"] == 60


def test_internal_predicates_and_legacy_companions_are_hidden(
    client, mock_neptune, auth_headers
):
    # Rides `_assemble_summary`, so ER/batch internals and pre-ONTA-262
    # `attrs/<x>_source_url` provenance companions never reach the payload, # unlike the type-usage endpoint, which filters SYSTEM_PREDICATES only.
    stats = {
        "entity_count": _rows({"type": TYPES + "Drug", "ec": "10", "sp": "", "tp": ""}),
        "preds": _rows(
            {"type": TYPES + "Drug", "pred": TYPES + "Drug/attrs/indications",
             "cnt": "10", "rel": "0", "target": ""},
            {"type": TYPES + "Drug", "pred": TYPES + "Drug/attrs/indications_source_url",
             "cnt": "10", "rel": "0", "target": ""},
            {"type": TYPES + "Drug", "pred": ONTO + "batch_id",
             "cnt": "10", "rel": "0", "target": ""},
            {"type": TYPES + "Drug", "pred": "https://graph.infona.ai/er/erSignal_name",
             "cnt": "10", "rel": "0", "target": ""},
        ),
    }
    mock_neptune.query.side_effect = _route_factory(stats)

    drug = _by_name(client.get(SCHEMA_URL, headers=auth_headers).json())["Drug"]
    names = {a["name"] for a in drug["attributes"]}
    assert names == {"indications"}


def test_live_scan_fallback_is_one_query_for_the_whole_kg(
    client, mock_neptune, auth_headers
):
    # A legacy KG with no materialized stats must NOT degrade into one scan per
    # type, the fallback is a single GROUP BY ?type ?p pass.
    live = _rows(
        {"type": TYPES + "Drug", "p": "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
         "cnt": "4", "sample": "", "rel": "0", "geo": "0", "tmp": "0"},
        {"type": TYPES + "Drug", "p": TYPES + "Drug/attrs/indications",
         "cnt": "3", "sample": "", "rel": "0", "geo": "0", "tmp": "0"},
        {"type": TYPES + "Company", "p": "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
         "cnt": "2", "sample": "", "rel": "0", "geo": "0", "tmp": "0"},
        {"type": TYPES + "Company", "p": ONTO + "makes",
         "cnt": "2", "sample": ENTITIES + "Drug/aspirin", "rel": "2",
         "geo": "0", "tmp": "0"},
    )
    empty_stats = {"entity_count": _rows(), "preds": _rows()}
    scans = []

    base = _route_factory(empty_stats, live=live)

    def route(sparql, *a, **k):
        if _is_live_scan_q(sparql):
            scans.append(sparql)
        return base(sparql, *a, **k)

    mock_neptune.query.side_effect = route

    body = client.get(SCHEMA_URL, headers=auth_headers).json()
    assert body["stats_source"] == "live_scan"
    assert len(scans) == 1
    types = _by_name(body)
    assert types["Drug"]["entity_count"] == 4
    assert types["Drug"]["attributes"][0]["name"] == "indications"
    assert types["Company"]["relationships"][0]["target_type"] == "Drug"


def test_type_filter_and_min_coverage(client, mock_neptune, auth_headers):
    stats = {
        "entity_count": _rows(
            {"type": TYPES + "Drug", "ec": "100", "sp": "", "tp": ""},
            {"type": TYPES + "Company", "ec": "5", "sp": "", "tp": ""},
        ),
        "preds": _rows(
            {"type": TYPES + "Drug", "pred": TYPES + "Drug/attrs/brand_name",
             "cnt": "100", "rel": "0", "target": ""},
            {"type": TYPES + "Drug", "pred": TYPES + "Drug/attrs/notes",
             "cnt": "3", "rel": "0", "target": ""},
            {"type": TYPES + "Company", "pred": TYPES + "Company/attrs/name",
             "cnt": "5", "rel": "0", "target": ""},
        ),
    }
    mock_neptune.query.side_effect = _route_factory(stats)

    body = client.get(SCHEMA_URL, params={"type": "Drug"}, headers=auth_headers).json()
    assert [t["name"] for t in body["types"]] == ["Drug"]

    body = client.get(
        SCHEMA_URL, params={"type": "Drug", "min_coverage": 50}, headers=auth_headers
    ).json()
    drug = body["types"][0]
    assert [a["name"] for a in drug["attributes"]] == ["brand_name"]
    # Withheld, and the caller is TOLD, not silently shortened.
    assert drug["attributes_withheld"] == 1


def test_limit_caps_types_but_still_names_the_omitted_ones(
    client, mock_neptune, auth_headers
):
    stats = {
        "entity_count": _rows(
            {"type": TYPES + "Drug", "ec": "100", "sp": "", "tp": ""},
            {"type": TYPES + "Company", "ec": "50", "sp": "", "tp": ""},
            {"type": TYPES + "Trial", "ec": "10", "sp": "", "tp": ""},
        ),
        "preds": _rows(),
    }
    mock_neptune.query.side_effect = _route_factory(stats)

    body = client.get(SCHEMA_URL, params={"limit": 2}, headers=auth_headers).json()
    assert [t["name"] for t in body["types"]] == ["Drug", "Company"]
    assert body["total_types"] == 3
    assert body["truncated"] is True
    # The capped type still EXISTS as far as the agent can tell.
    assert body["omitted_type_names"] == ["Trial"]


def test_whole_kg_read_does_not_fan_out_per_type(client, mock_neptune, auth_headers):
    # The endpoint's reason to live: a constant number of queries regardless of
    # how many types the KG has (a client-side loop would be 1+N).
    stats = {
        "entity_count": _rows(*[
            {"type": f"{TYPES}T{i}", "ec": str(i + 1), "sp": "", "tp": ""}
            for i in range(12)
        ]),
        "preds": _rows(*[
            {"type": f"{TYPES}T{i}", "pred": f"{TYPES}T{i}/attrs/a",
             "cnt": "1", "rel": "0", "target": ""}
            for i in range(12)
        ]),
    }
    mock_neptune.query.side_effect = _route_factory(stats)

    body = client.get(SCHEMA_URL, headers=auth_headers).json()
    assert body["total_types"] == 12
    # 2 stats queries + 2 ontology-declaration queries. No per-type query.
    assert mock_neptune.query.await_count == 4


def test_declared_base_never_hides_a_populated_companion_shaped_attribute(
    client, mock_neptune, auth_headers
):
    # `_assemble_summary` classifies `<base>_<suffix>` as a LEGACY provenance
    # companion only when `<base>` is also present. Synthesizing a
    # declared-but-empty `data` would therefore make a REAL, populated
    # `data_provenance` vanish. Never trade a populated slot for an empty one.
    stats = {
        "entity_count": _rows({"type": TYPES + "Doc", "ec": "10", "sp": "", "tp": ""}),
        "preds": _rows(
            {"type": TYPES + "Doc", "pred": TYPES + "Doc/attrs/data_provenance",
             "cnt": "10", "rel": "0", "target": ""},
        ),
    }
    decl_types = _rows({"type": TYPES + "Doc", "label": "Doc", "comment": "", "parent": ""})
    decl_attrs = _rows(
        {"domain": TYPES + "Doc", "attr": TYPES + "Doc/attrs/data",
         "attrLabel": "data", "range": ""},
    )
    mock_neptune.query.side_effect = _route_factory(stats, decl_types, decl_attrs)

    doc = _by_name(client.get(SCHEMA_URL, headers=auth_headers).json())["Doc"]
    names = {a["name"] for a in doc["attributes"]}
    assert "data_provenance" in names
    # The declared-empty base is withheld precisely because keeping it would
    # have hidden the populated one.
    assert "data" not in names


def test_type_filter_is_case_insensitive_and_names_alternatives_on_a_miss(
    client, mock_neptune, auth_headers
):
    # The caller is usually an LLM: an exact-match miss would answer "no such
    # type", which is the conclusion this endpoint exists to prevent.
    stats = {
        "entity_count": _rows(
            {"type": TYPES + "Drug", "ec": "10", "sp": "", "tp": ""},
            {"type": TYPES + "Company", "ec": "2", "sp": "", "tp": ""},
        ),
        "preds": _rows(),
    }
    mock_neptune.query.side_effect = _route_factory(stats)

    body = client.get(SCHEMA_URL, params={"type": "drug"}, headers=auth_headers).json()
    assert [t["name"] for t in body["types"]] == ["Drug"]
    assert body["available_type_names"] == []

    body = client.get(SCHEMA_URL, params={"type": "Dr"}, headers=auth_headers).json()
    assert body["types"] == []
    assert body["available_type_names"] == ["Company", "Drug"]


def test_layered_declaration_collapses_onto_tenant_namespace_instances(
    client, mock_neptune, auth_headers
):
    # ONTA-397 layering: a Public-declared type whose instances were written
    # under the historical TENANT namespace must resolve to ONE entry, not a
    # populated orphan plus a phantom empty type. The tenant namespace is a
    # PREFIX of the public one, so the URI parse has to be longest-first.
    public = "https://graph.infona.ai/types/public/"
    stats = {
        "entity_count": _rows({"type": TYPES + "Drug", "ec": "8", "sp": "", "tp": ""}),
        "preds": _rows(
            {"type": TYPES + "Drug", "pred": TYPES + "Drug/attrs/brand_name",
             "cnt": "8", "rel": "0", "target": ""},
        ),
    }
    decl_types = _rows(
        {"type": public + "Drug", "label": "Drug",
         "comment": "Public-layer drug", "parent": ""},
    )
    decl_attrs = _rows(
        # Declared on the PUBLIC type URI while instances carry tenant-namespace
        # predicates; the declared-only slot must still surface.
        {"domain": public + "Drug", "attr": public + "Drug/attrs/atc_code",
         "attrLabel": "atc_code", "range": ""},
    )
    mock_neptune.query.side_effect = _route_factory(stats, decl_types, decl_attrs)

    body = client.get(SCHEMA_URL, headers=auth_headers).json()
    assert [t["name"] for t in body["types"]] == ["Drug"]
    drug = body["types"][0]
    # Population from the tenant-namespace instances, description from the
    # Public declaration, and the union of both layers' attributes.
    assert drug["entity_count"] == 8
    assert drug["description"] == "Public-layer drug"
    attrs = {a["name"]: a for a in drug["attributes"]}
    assert attrs["brand_name"]["populated"] is True
    assert attrs["atc_code"]["populated"] is False


def test_nested_attribute_uris_are_never_mistaken_for_types(
    client, mock_neptune, auth_headers
):
    # `…/types/Drug/attrs/x` lives under the type namespace but is a property
    # declaration, not a type.
    stats = {
        "entity_count": _rows(
            {"type": TYPES + "Drug", "ec": "3", "sp": "", "tp": ""},
            {"type": TYPES + "Drug/attrs/brand_name", "ec": "3", "sp": "", "tp": ""},
        ),
        "preds": _rows(),
    }
    mock_neptune.query.side_effect = _route_factory(stats)

    body = client.get(SCHEMA_URL, headers=auth_headers).json()
    assert [t["name"] for t in body["types"]] == ["Drug"]


def test_live_scan_fallback_applies_the_primary_type_guard(
    client, mock_neptune, auth_headers
):
    # The guard is what keeps the live and precomputed paths agreeing on
    # entity_count for multi-typed entities. Assert it is actually in the query.
    captured = []

    def route(sparql, *a, **k):
        if _is_live_scan_q(sparql):
            captured.append(sparql)
        return _rows()

    mock_neptune.query.side_effect = route

    client.get(SCHEMA_URL, headers=auth_headers)
    assert len(captured) == 1
    assert "FILTER NOT EXISTS" in captured[0]
    assert "STR(?type2) < STR(?type)" in captured[0]


def test_spatiotemporal_flags_survive(client, mock_neptune, auth_headers):
    stats = {
        "entity_count": _rows({"type": TYPES + "Venue", "ec": "3", "sp": "true", "tp": "1"}),
        "preds": _rows(),
    }
    mock_neptune.query.side_effect = _route_factory(stats)

    venue = _by_name(client.get(SCHEMA_URL, headers=auth_headers).json())["Venue"]
    assert venue["spatially_indexed"] is True
    assert venue["temporally_indexed"] is True
