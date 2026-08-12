"""Caller-supplied IRI segments cannot break out of a generated IRI.

ONTA-425 (``type_name``) and ONTA-422 (``tenant``) are the two remaining halves
of the defect ONTA-414 fixed for ``kg_name``: a name interpolated straight into
``<https://graph.infona.ai/…>`` inside generated SPARQL, with no check that it can
legally sit there. A ``>`` closes the IRI and everything after it is parsed as
SPARQL — on a read path that is a cross-graph read or a 500, and on a write path
it lands in ``client.update``, where ``;`` starts a second operation and
``DROP ALL`` needs no IRI of its own.

The guards live in the URI BUILDERS (``type_uri`` / ``attr_uri`` /
``layer_type_uri`` / ``tenant_graph_uri`` / ``kg_graph_uri``), so a route that
takes a name off a path segment or a request body without its own pattern still
fails closed. Two properties this file pins hardest, because both are places the
earlier fix went wrong:

1. **Fail SOFT when enumerating.** infona-oss#274 had to undo an ONTA-414
   regression where one pre-existing malformed name 422'd an entire listing. Any
   read that fans out over stored names must skip the bad one, not raise.
2. **No stricter than it has to be.** The type/attribute rule is deliberately
   NOT a slug pattern: it was checked against the live registry, where two
   attribute names in daily use contain ``/``.
"""

import os
import re
from unittest.mock import AsyncMock

import pytest
import structlog
from fastapi.testclient import TestClient

os.environ["INFONA_API_KEYS"] = '{"test-key": "test-tenant"}'
os.environ["INFONA_NEPTUNE_ENDPOINT"] = "http://fake-neptune:8182"

from infona_client.api.app import create_app
from infona_client.graph.client import NeptuneClient
from infona_client.graph.layers import Layer, layer_type_uri
from infona_client.graph.ontology_queries import attr_uri, insert_type, type_uri
from infona_client.graph.queries import (
    GRAPH_URI_PREFIX,
    InvalidGraphIdentifier,
    InvalidKGName,
    InvalidTenantId,
    InvalidTypeName,
    is_valid_tenant_id,
    is_valid_type_name,
    kg_graph_uri,
    tenant_graph_uri,
)

TENANT = "test-tenant"
KG = "movies"
VICTIM = "https://graph.infona.ai/graphs/victim"

# Payloads that break OUT of `<…>`. Each is a real escape, not a mutation of one:
# the first appends a second dataset clause naming another workspace's graph, the
# second ends the enclosing operation and starts a store-wide destructive one,
# the third comments out the rest of the generated line, and the rest are the raw
# characters SPARQL's IRIREF production forbids.
BREAKOUT_PAYLOADS = [
    f"Movie> <{VICTIM}",
    "Movie> {} }; DROP ALL ;",
    "Movie> #",
    "Movie>",
    "Movie<",
    'Movie"',
    "Movie{x}",
    "Movie|x",
    "Movie^x",
    "Movie`x",
    "Movie\\x",
    "Movie name",  # a space is illegal in an IRIREF just like the rest
    "Movie\n",
    "Movie\r\n",
    "Movie\t",
    "\x00Movie",
    "Movie\x7f",      # DEL and the C1 block are IRI-illegal too: RFC 3987's
    "Movie\x85",      # ucschar starts at U+00A0, so everything below it that is
    "Movie\x9f",      # not already covered by \x00-\x20 is equally illegal.
    "",
]

# Names that MUST keep working. Shape-checked against the live registry rather
# than by grepping this repo (the ONTA-414 verification mistake that produced
# infona-oss#274): 158 type names and 714 attribute names across the two live
# workspaces, none carrying an IRIREF-illegal character. `city/town` and
# `county/parish` are REAL attribute names in production today — a
# `[A-Za-z0-9_-]+` rule of the kind `kg_name` uses would have broken them for no
# security gain, since `/` cannot escape `<…>`.
REAL_NAMES = [
    "Movie",
    "ClinicalTrial",
    "Person",
    "entity_count",
    "phone-number",
    "city/town",
    "county/parish",
    "Ärztin",          # non-ASCII is legal in an IRI and an LLM may well mint it
    "price(usd)",
    "share%",
    "Type#1",
    "no\xa0break",   # U+00A0 is the first ucschar: legal, and NOT swept up by
                       # the C1 range that ends one codepoint earlier
]


def test_the_forbidden_range_stops_exactly_where_ucschar_begins():
    """Boundary pin for the ``\\x7f-\\x9f`` extension.

    One codepoint too far and every name containing a non-breaking space is
    rejected; one codepoint short and DEL slips through to the store. Both are
    silent until production, so the boundary is asserted rather than eyeballed.
    """
    assert is_valid_type_name("a\x9fb") is False
    assert is_valid_type_name("a\xa0b") is True


# ---------------------------------------------------------------------------
# ONTA-425: type and attribute names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", BREAKOUT_PAYLOADS)
def test_type_uri_refuses_a_name_that_cannot_sit_in_an_iri(payload):
    assert is_valid_type_name(payload) is False
    with pytest.raises(InvalidTypeName):
        type_uri(payload)


@pytest.mark.parametrize("payload", BREAKOUT_PAYLOADS)
def test_attr_uri_validates_both_of_its_segments(payload):
    with pytest.raises(InvalidTypeName):
        attr_uri(payload, "email")
    with pytest.raises(InvalidTypeName):
        attr_uri("Person", payload)


@pytest.mark.parametrize("payload", BREAKOUT_PAYLOADS)
@pytest.mark.parametrize("layer", list(Layer))
def test_layer_type_uri_refuses_in_every_layer(layer, payload):
    """Not just the TENANT branch: the Public/Enhanced f-string is the one every
    Explorer read resolves through, and a global declaration is exactly as
    interpolatable as a tenant one."""
    with pytest.raises(InvalidTypeName):
        layer_type_uri(layer, payload)


@pytest.mark.parametrize("name", REAL_NAMES)
def test_real_names_still_mint_their_uri(name):
    assert is_valid_type_name(name) is True
    assert type_uri(name) == f"https://graph.infona.ai/types/{name}"
    assert attr_uri("Address", name) == (
        f"https://graph.infona.ai/types/Address/attrs/{name}"
    )
    for layer in Layer:
        assert layer_type_uri(layer, name).endswith(name)


def test_the_write_path_builder_refuses_rather_than_emitting_the_injection():
    """The concrete escalation, at the builder that feeds ``client.update``.

    Without the guard this returns an ``INSERT DATA`` whose text carries a
    statement separator followed by a store-wide ``DROP ALL`` — the payload is
    no longer inside the IRI, it IS the query.
    """
    payload = "Movie> <x> <y> . }} ; DROP ALL ; INSERT DATA {{ GRAPH <x> {{ <a"
    with pytest.raises(InvalidTypeName):
        insert_type(tenant_graph_uri(TENANT), payload)
    # And the pre-fix behaviour is genuinely dangerous, not merely malformed:
    # spelled out so the test states what it is preventing.
    assert "DROP ALL" in f"https://graph.infona.ai/types/{payload}"


# ---------------------------------------------------------------------------
# ONTA-422: the workspace segment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "acme> <" + VICTIM,
        "acme> {} }; DROP ALL ;",
        "acme>",
        "acme name",
        "acme\n",
        "victim/kg/secret",      # repoints the IRI without leaving <…>
        "acme%2f..%2fvictim",    # percent-encoding could spell either of the above
        ".",
        "..",
        "",
    ],
)
def test_graph_uri_builders_refuse_a_crafted_workspace(payload):
    assert is_valid_tenant_id(payload) is False
    with pytest.raises(InvalidTenantId):
        tenant_graph_uri(payload)
    with pytest.raises(InvalidTenantId):
        kg_graph_uri(payload, KG)


@pytest.mark.parametrize(
    "tenant_id", ["demo-tenant", "default", "spider-bench", "ab", "Test_1", "x"]
)
def test_workspace_ids_the_slug_rule_would_reject_still_work(tenant_id):
    """The guard is structural, NOT ``TENANT_ID_RE``.

    A self-hosted deployment picks its own workspace id with nothing validating
    it, and ids predating the 3-40-char lowercase slug rule keep working.
    Enforcing the slug shape here would make an existing workspace's data
    unreachable — the infona-oss#274 mistake, one layer down.
    """
    assert is_valid_tenant_id(tenant_id) is True
    assert tenant_graph_uri(tenant_id) == GRAPH_URI_PREFIX + tenant_id
    assert kg_graph_uri(tenant_id, KG) == f"{GRAPH_URI_PREFIX}{tenant_id}/kg/{KG}"


def test_kg_graph_uri_still_validates_its_kg_half():
    """The ONTA-414 guard is untouched by the tenant one being added beside it."""
    with pytest.raises(InvalidKGName):
        kg_graph_uri(TENANT, f"movies> FROM <{VICTIM}")


# ---------------------------------------------------------------------------
# The exception family and its single 422 handler
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [InvalidKGName, InvalidTypeName, InvalidTenantId])
def test_every_member_is_a_value_error_and_shares_one_base(exc):
    assert issubclass(exc, InvalidGraphIdentifier)
    assert issubclass(exc, ValueError)  # pre-existing `except ValueError` still catches


def test_one_handler_registration_covers_the_whole_family():
    """Registered on the BASE, so a new member cannot regress to an opaque 500.

    Starlette resolves a handler by walking the exception's MRO, so this is what
    makes ``InvalidTypeName`` and ``InvalidTenantId`` render as 422 without their
    own registrations.
    """
    app = create_app()
    assert InvalidGraphIdentifier in app.exception_handlers
    for exc in (InvalidKGName, InvalidTypeName, InvalidTenantId):
        assert any(
            registered in exc.__mro__ for registered in app.exception_handlers
        ), exc


# ---------------------------------------------------------------------------
# Route level
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_summary_cache():
    """``explore._summary_cache`` is module-level and outlives a TestClient, so a
    hit from an earlier test would serve a 200 with ZERO store calls and quietly
    void every "which graph did we query" assertion below."""
    from infona_client.api.routes import explore

    explore._summary_cache.clear()
    yield
    explore._summary_cache.clear()


@pytest.fixture
def mock_neptune():
    client = AsyncMock(spec=NeptuneClient)
    client.health.return_value = True
    client.update.return_value = None
    client.query.return_value = {"results": {"bindings": []}}
    return client


@pytest.fixture
def app(mock_neptune):
    app = create_app()
    app.state.neptune_client = mock_neptune
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"X-API-Key": "test-key"}


def _seed_movie_in_store(tenant_id: str, kg_name: str = KG) -> str:
    """Write one Movie into the process GraphStore under ``tenant_id``/``kg_name``."""
    import asyncio

    from infona_client.graph.kg_writer import insert_facts
    from infona_client.graph.ontology_queries import entity_uri
    from infona_client.graph.store import get_graph_store

    uri = entity_uri("Movie", "m1")
    triples = [
        (uri, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", type_uri("Movie")),
        (uri, "http://www.w3.org/2000/01/rdf-schema#label", "The Matrix"),
        (uri, attr_uri("Movie", "title"), "The Matrix"),
    ]
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        insert_facts(
            None,
            f"{GRAPH_URI_PREFIX}{tenant_id}/kg/{kg_name}",
            triples,
            store=get_graph_store(),
        )
    )
    return uri


@pytest.fixture
def seeded_movie():
    """One Movie in the tenant's KG on the process GraphStore.

    The autouse ``_hermetic_graph_store`` fixture installs a fresh
    MemoryGraphStore per test, which is what the Explorer read paths resolve —
    so a route that is served from the store has something to return.
    """
    return _seed_movie_in_store(TENANT)


# A path-segment payload cannot contain "/" (the ASGI server percent-decodes the
# path before routing, so `%2F` becomes a separator and the route stops
# matching). It does not need to: `>` alone is the escape.
PATH_PAYLOAD = "Movie>"


@pytest.mark.parametrize("suffix", ["summary", "records"])
def test_explore_type_routes_422_before_touching_the_store(
    client, auth_headers, mock_neptune, suffix
):
    res = client.get(
        f"/graphs/{TENANT}/explore/kgs/{KG}/types/{PATH_PAYLOAD}/{suffix}",
        headers=auth_headers,
    )
    assert res.status_code == 422, res.text
    assert PATH_PAYLOAD in res.json()["detail"]
    mock_neptune.query.assert_not_called()


@pytest.mark.parametrize("suffix", ["summary", "records"])
def test_a_legitimate_type_name_is_unaffected(
    client, auth_headers, mock_neptune, seeded_movie, suffix
):
    """The guard must not over-reject: a normal name is served, not 422'd.

    Ported by ONTA-527 / P-A1a. Both ``/records`` and ``/summary`` read the
    process GraphStore now, so "served, not short-circuited" is proven by the
    seeded row (and that SPARQL is never touched) rather than
    ``mock_neptune.query.await_count > 0``.
    """
    res = client.get(
        f"/graphs/{TENANT}/explore/kgs/{KG}/types/Movie/{suffix}",
        headers=auth_headers,
    )
    assert res.status_code == 200, res.text
    if suffix == "records":
        assert [r["id"] for r in res.json()["rows"]] == [seeded_movie]
    else:
        body = res.json()
        assert body["name"] == "Movie"
        assert body["entity_count"] == 1
    mock_neptune.query.assert_not_called()


def test_ontology_write_route_fails_closed_before_the_update(
    client, auth_headers, mock_neptune
):
    """The severe half of ONTA-425: this one reaches ``client.update``.

    A 422 here is not cosmetic — an accepted payload would have been a
    statement-level injection on a SPARQL UPDATE, i.e. a cross-workspace write.
    """
    res = client.post(
        f"/graphs/{TENANT}/ontology/types/{PATH_PAYLOAD}/attributes",
        headers=auth_headers,
        json={"attributes": [{"name": "title", "datatype": "string"}]},
    )
    assert res.status_code == 422, res.text
    mock_neptune.update.assert_not_called()


def test_search_skips_one_corrupt_stored_name_instead_of_failing_the_listing(
    client, auth_headers, mock_neptune
):
    """The infona-oss#274 lesson, applied on arrival rather than as a follow-up.

    A corrupt ontology row does not need out-of-band DB access to exist:
    ``POST /graphs/{tenant}/triples`` writes arbitrary triples into the same
    tenant base graph the ontology lives in, and SPARQL literal escaping does not
    escape ``>``. One such row must not take down search for every good type.
    """
    good, corrupt = "Movie", "Movie> <injected"

    def rows(*names):
        # head.vars is load-bearing: the parser only reads declared variables.
        return {
            "head": {"vars": ["type", "label"]},
            "results": {
                "bindings": [
                    {"type": {"value": f"https://graph.infona.ai/types/{n}"},
                     "label": {"value": n}}
                    for n in names
                ]
            },
        }

    async def route(sparql, *a, **kw):
        if "#Class" in sparql:
            return rows(good, corrupt)
        return {
            "head": {"vars": ["n"]},
            "results": {"bindings": [{"n": {"value": "3"}}]},
        }

    mock_neptune.query.side_effect = route

    with structlog.testing.capture_logs() as logs:
        res = client.get(
            f"/graphs/{TENANT}/explore/search?kg={KG}&q=Movie&kind=type",
            headers=auth_headers,
        )

    assert res.status_code == 200, res.text
    names = [r["name"] for r in res.json()]
    assert good in names, "one corrupt row must not hide the healthy types"
    assert corrupt not in names
    assert any(e.get("event") == "type_name_invalid_skipped" for e in logs), (
        "the skip must stay observable, or the corruption becomes silent"
    )


# ---------------------------------------------------------------------------
# Fail-soft on every path that ENUMERATES stored names.
#
# The rule this section defends: a corrupt stored name costs you THAT name, and
# nothing else. Every case below was a real regression caught in review — the
# first draft of this change fail-softed only the Explorer's search and left the
# NL planner's ontology summary, the embedding chunks and the drift report to
# raise, which is infona-oss#274 all over again on hotter paths.
# ---------------------------------------------------------------------------

CORRUPT_LABEL = "Movie> <injected"
TYPES_NS = "https://graph.infona.ai/types/"
XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"


def _onto_rows(*labels):
    """`?type ?typeLabel ?attr ?attrLabel ?range` rows, one attribute per type."""
    rows = []
    for label in labels:
        rows.append({
            "type": {"value": f"{TYPES_NS}{label}"},
            "typeLabel": {"value": label},
            "attr": {"value": f"{TYPES_NS}{label}/attrs/title"},
            "attrLabel": {"value": "title"},
            "range": {"value": XSD_STRING},
        })
    return {
        "head": {"vars": ["type", "typeLabel", "attr", "attrLabel", "range"]},
        "results": {"bindings": rows},
    }


async def test_the_nl_ontology_summary_survives_one_corrupt_stored_name():
    """The hottest read path: every ``/ask`` builds this summary.

    The whole fetch sits under one ``except Exception: return
    ONTOLOGY_FETCH_ERROR``, so a raise does not degrade the bad type — it tells
    the planner the schema is UNKNOWN for the entire workspace, on every
    question.
    """
    from infona_client.nlp.pipeline import (
        ONTOLOGY_FETCH_ERROR,
        NLQueryPipeline,
        _ontology_cache,
    )

    _ontology_cache.clear()

    class _Neptune:
        async def query(self, sparql):
            if "?typeLabel" in sparql:
                return _onto_rows("Movie", CORRUPT_LABEL, "Director")
            # The ONTA-427 active-types probe: answer "populated" for every URI
            # it asks about, so the summary is assembled rather than short-
            # circuiting to ONTOLOGY_EMPTY (which would make this test pass for
            # the wrong reason).
            probed = re.findall(r"SELECT \(<([^>]+)> AS \?type\)", sparql)
            return {
                "head": {"vars": ["type"]},
                "results": {"bindings": [{"type": {"value": u}} for u in probed]},
            }

    summary = await NLQueryPipeline(_Neptune(), anthropic_key="dummy")._fetch_ontology(
        f"{GRAPH_URI_PREFIX}{TENANT}", f"{GRAPH_URI_PREFIX}{TENANT}/kg/{KG}"
    )
    _ontology_cache.clear()

    assert summary != ONTOLOGY_FETCH_ERROR
    assert "Movie" in summary and "Director" in summary
    assert "injected" not in summary


def test_ontology_embedding_chunks_survive_one_corrupt_stored_name():
    from infona_client.nlp.ontology_embeddings import _parse_ontology_bindings

    _, rows = __import__(
        "infona_client.graph.parser", fromlist=["parse_sparql_results"]
    ).parse_sparql_results(_onto_rows("Movie", CORRUPT_LABEL))
    parsed = _parse_ontology_bindings(rows)
    assert "Movie" in parsed
    assert CORRUPT_LABEL not in parsed


def test_a_corrupt_attribute_label_costs_only_that_attribute():
    """Attribute labels are stored literals too, and mint the same IRI."""
    from infona_client.graph.parser import parse_sparql_results
    from infona_client.nlp.ontology_embeddings import _parse_ontology_bindings

    raw = _onto_rows("Movie")
    raw["results"]["bindings"][0]["attrLabel"] = {"value": "title> <injected"}
    _, rows = parse_sparql_results(raw)
    parsed = _parse_ontology_bindings(rows)
    assert "Movie" in parsed, "the TYPE must survive its attribute being corrupt"
    assert parsed["Movie"]["attributes"] == []


def test_the_core_slot_membership_test_answers_false_rather_than_raising():
    """``_is_core_slot`` takes leaves SLICED off stored URIs.

    A URI outside the expected shape slices to ``""`` (a bare ``…/onto/``
    predicate does exactly this), and ``core_slots`` only holds well-formed
    attribute URIs, so False is the correct answer. Raising would fail a whole
    drift report over one malformed row.
    """
    from infona_client.api.routes.explore import _is_core_slot

    core = {"https://graph.infona.ai/types/Movie/attrs/title"}
    assert _is_core_slot("Movie", "title", core) is True
    assert _is_core_slot("", "title", core) is False
    assert _is_core_slot("Movie", "", core) is False
    assert _is_core_slot("Movie> <injected", "title", core) is False


def test_the_skip_helper_is_shared_not_re_implemented():
    """One fail-soft helper, imported by every enumeration site.

    Four copies of "is this name safe" is how the two ``kg_name`` patterns
    drifted apart before ONTA-414. Pinned structurally so the next enumeration
    site imports it instead of pasting it.
    """
    from infona_client.api.routes import explore
    from infona_client.graph.queries import skip_invalid_type_name
    from infona_client.nlp import ontology_embeddings, pipeline

    for module in (explore, pipeline, ontology_embeddings):
        assert module.skip_invalid_type_name is skip_invalid_type_name


# ---------------------------------------------------------------------------
# ONTA-422 at the route level: open access is the ONLY mode the tenant segment
# is caller-controlled in.
# ---------------------------------------------------------------------------


@pytest.fixture
def open_access(monkeypatch):
    from infona_client.auth import api_keys

    monkeypatch.setattr(api_keys, "_has_static_keys", lambda: False)
    monkeypatch.setattr(api_keys, "_external_verifier", None)


# No "." / ".." here: the HTTP client normalizes dot segments out of the path
# before the request is sent, so they never reach the route. They are covered
# against the builders above, which is where they would actually arrive from
# (a non-HTTP caller, or a client that does not normalize).
@pytest.mark.parametrize("payload", ["acme>", "acme> {} }; DROP ALL ;", "acme name"])
def test_open_access_rejects_a_crafted_workspace_at_the_door(
    open_access, client, mock_neptune, payload
):
    res = client.get(f"/graphs/{payload}/explore/kgs/{KG}/types/Movie/summary")
    assert res.status_code == 400, res.text
    mock_neptune.query.assert_not_called()
    mock_neptune.update.assert_not_called()


def test_open_access_still_serves_an_ordinary_workspace(
    open_access, client, mock_neptune
):
    """The guard must not cost a self-hosted install its normal operation."""
    # P-A1a: /summary is GraphStore-backed — seed the open-access workspace.
    _seed_movie_in_store("my_local_ws")
    res = client.get(f"/graphs/my_local_ws/explore/kgs/{KG}/types/Movie/summary")
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "Movie"
    mock_neptune.query.assert_not_called()


def test_with_auth_configured_the_path_segment_was_never_the_tenant(
    client, auth_headers, mock_neptune
):
    """Why this is ONTA-422's low-severity half, pinned rather than assumed.

    Auth-configured deployments never treat a crafted path segment as the
    tenant: a static key 403s when the path differs from the key's tenant; a
    verifier authorizes the path against a grant list. Matching path still
    resolves to the key's tenant graph (not a crafted segment).
    """
    # Foreign path → 403 (no silent reroute, no store read under a crafted id).
    foreign = client.get(
        "/graphs/whatever-the-caller-typed/explore/kgs/movies/types/Movie/summary",
        headers=auth_headers,
    )
    assert foreign.status_code == 403, foreign.text
    assert "whatever-the-caller-typed" in foreign.json()["detail"]

    # Matching path is served from the key's tenant GraphStore scope (P-A1a).
    movie_uri = _seed_movie_in_store(TENANT, "movies")
    res = client.get(
        f"/graphs/{TENANT}/explore/kgs/movies/types/Movie/summary",
        headers=auth_headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["name"] == "Movie"
    assert body["entity_count"] == 1
    # No SPARQL under either the real tenant or a crafted segment.
    mock_neptune.query.assert_not_called()
    # Seeded under TENANT only — a crafted id would 404 if the path were trusted.
    assert movie_uri.startswith("https://graph.infona.ai/entities/")
