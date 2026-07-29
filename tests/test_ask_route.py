"""Route-level tests for POST /graphs/{tenant}/ask (persona-eval RCA, ONTA-240).

The /ask contract is ALWAYS an NLResult — a transient provider failure that
somehow escapes the pipeline's internal retry/degrade must never surface as a
bare HTTP 500 with no error body (the persona-eval Cluster 4 symptom). The
route-level handler catches it, logs at the boundary, and returns a graceful
200 NLResult.

ONTA-413 (below): the ONE exception to "always an NLResult" is a kg_name that
names no KG at all. That is a missing RESOURCE, not an unanswerable question, so
it is a 404 the SDK raises on and the MCP server renders as a tool error.

ONTA-414 (below): kg_name is interpolated into a graph IRI inside generated
SPARQL, so a name carrying ">" could close the IRI early and inject a second
FROM naming another tenant's graph. It is now pattern-validated.
"""
import pytest
from unittest.mock import AsyncMock, patch

from cograph_client.graph.kg_status import invalidate_kg_status
from cograph_client.models.query import NLResult

TENANT = "test-tenant"  # conftest's static-key tenant


@pytest.fixture(autouse=True)
def _clear_kg_status_cache():
    """The KG-status probe caches POSITIVE verdicts; keep tests independent."""
    invalidate_kg_status(TENANT)
    yield
    invalidate_kg_status(TENANT)


def _select(var: str, values: list[str]) -> dict:
    return {
        "head": {"vars": [var]},
        "results": {
            "bindings": [{var: {"type": "literal", "value": v}} for v in values]
        },
    }


def _wire_kg(
    mock_neptune, *, registered: bool, has_data: bool, base_instances=False, others=()
):
    """Make the shared KG-status probe report a specific state.

    The probe fires two ASKs on the hot path (the registration record in the
    tenant base graph, and "does the KG graph hold a triple"), a third only when
    the KG graph is empty ("does the tenant BASE graph hold instance data", the
    union-aware check), and one SELECT for the tenant's real KG names on the
    missing path.
    """

    async def fake_ask(sparql: str) -> bool:
        if "/kg_name>" in sparql:
            return registered
        if "rdf-syntax-ns#type" in sparql:
            return base_instances
        return has_data

    mock_neptune.ask.side_effect = fake_ask
    mock_neptune.query.return_value = _select("name", list(others))


def test_ask_unhandled_error_returns_graceful_result_not_500(client, auth_headers):
    with patch(
        "cograph_client.api.routes.ask.NLQueryPipeline.ask",
        new_callable=AsyncMock,
    ) as mock_ask:
        mock_ask.side_effect = RuntimeError("provider exploded outside retry loop")
        res = client.post(
            f"/graphs/{TENANT}/ask",
            json={"question": "list all attributes"},
            headers=auth_headers,
        )

    assert res.status_code == 200  # NOT a bare 500
    body = res.json()
    assert "Could not answer" in body["answer"]
    # Shape is a valid NLResult
    NLResult(**body)


def test_ask_happy_path_passes_through(client, auth_headers):
    ok = NLResult(answer="42", sparql="SELECT ...", explanation="e")
    with patch(
        "cograph_client.api.routes.ask.NLQueryPipeline.ask",
        new_callable=AsyncMock,
    ) as mock_ask:
        mock_ask.return_value = ok
        res = client.post(
            f"/graphs/{TENANT}/ask",
            json={"question": "what is the answer"},
            headers=auth_headers,
        )

    assert res.status_code == 200
    assert res.json()["answer"] == "42"


def test_ask_requires_auth(client):
    res = client.post(f"/graphs/{TENANT}/ask", json={"question": "hi"})
    assert res.status_code == 401


# --------------------------------------------------------------------------- #
# ONTA-413: the three states a zero-row answer used to collapse into
# --------------------------------------------------------------------------- #


def test_ask_missing_kg_returns_404_naming_the_available_kgs(
    client, auth_headers, mock_neptune
):
    """(a) The KG does not exist: an explicit 404, not "No results found."."""
    _wire_kg(mock_neptune, registered=False, has_data=False, others=["imdb", "events"])

    with patch(
        "cograph_client.api.routes.ask.NLQueryPipeline.ask",
        new_callable=AsyncMock,
    ) as mock_ask:
        res = client.post(
            f"/graphs/{TENANT}/ask",
            json={"question": "how many movies", "kg_name": "no-such-kg"},
            headers=auth_headers,
        )

    assert res.status_code == 404
    detail = res.json()["detail"]
    assert detail["error"] == "kg_not_found"
    assert detail["kg_name"] == "no-such-kg"
    # The available names ride along so an agent can self-correct in one hop.
    assert detail["available_kgs"] == ["imdb", "events"]
    assert "imdb" in detail["message"]
    # No LLM generation was wasted on a graph that does not exist.
    mock_ask.assert_not_called()


def test_ask_empty_kg_says_so_explicitly_and_skips_generation(
    client, auth_headers, mock_neptune
):
    """(b) The KG is registered but holds nothing: an honest 200 NLResult."""
    _wire_kg(mock_neptune, registered=True, has_data=False)

    with patch(
        "cograph_client.api.routes.ask.NLQueryPipeline.ask",
        new_callable=AsyncMock,
    ) as mock_ask:
        res = client.post(
            f"/graphs/{TENANT}/ask",
            json={"question": "how many widgets", "kg_name": "widgets"},
            headers=auth_headers,
        )

    assert res.status_code == 200
    body = res.json()
    NLResult(**body)  # still in contract
    assert "contains no data" in body["answer"]
    assert "widgets" in body["answer"]
    # Explicitly NOT the old indistinguishable sentinel.
    assert body["answer"] != "No results found."
    assert body["sparql"] == ""
    mock_ask.assert_not_called()


def test_ask_populated_kg_still_answers_normally(client, auth_headers, mock_neptune):
    """(c) The KG has data: unchanged. A zero-row answer stays a normal answer."""
    _wire_kg(mock_neptune, registered=True, has_data=True)
    ok = NLResult(answer="No results found.", sparql="SELECT ...", explanation="e")

    with patch(
        "cograph_client.api.routes.ask.NLQueryPipeline.ask",
        new_callable=AsyncMock,
    ) as mock_ask:
        mock_ask.return_value = ok
        res = client.post(
            f"/graphs/{TENANT}/ask",
            json={"question": "how many sprockets", "kg_name": "widgets"},
            headers=auth_headers,
        )

    assert res.status_code == 200
    assert res.json()["answer"] == "No results found."
    mock_ask.assert_called_once()


def test_ask_unregistered_but_populated_kg_is_not_reported_missing(
    client, auth_headers, mock_neptune
):
    """A legacy graph with data but no registration record must still answer.

    Registration was only folded into the shared write path later, so some KGs
    hold data with no ``kg_name`` record. Refusing to answer those would be a
    worse regression than the bug being fixed: "missing" requires BOTH signals.
    """
    _wire_kg(mock_neptune, registered=False, has_data=True)
    ok = NLResult(answer="42", sparql="SELECT ...", explanation="e")

    with patch(
        "cograph_client.api.routes.ask.NLQueryPipeline.ask",
        new_callable=AsyncMock,
    ) as mock_ask:
        mock_ask.return_value = ok
        res = client.post(
            f"/graphs/{TENANT}/ask",
            json={"question": "how many", "kg_name": "legacy"},
            headers=auth_headers,
        )

    assert res.status_code == 200
    assert res.json()["answer"] == "42"


def test_ask_empty_kg_still_answers_when_data_lives_in_the_base_graph(
    client, auth_headers, mock_neptune
):
    """The dataset is a UNION, so an empty per-KG graph is not an empty dataset.

    /ask threads layer_stack_for(tenant).visible_graph_uris() into the pipeline,
    that stack ALWAYS includes the tenant base graph, and add_layer_from_clauses
    splices it in as an extra FROM. A workspace that ingested without a kg_name
    keeps its instances there (api/routes/ingest.py), so short-circuiting on the
    per-KG ASK alone would turn an answer main gives today into a confident
    "nothing to query" refusal.
    """
    _wire_kg(mock_neptune, registered=True, has_data=False, base_instances=True)
    ok = NLResult(answer="42", sparql="SELECT ...", explanation="e")

    with patch(
        "cograph_client.api.routes.ask.NLQueryPipeline.ask",
        new_callable=AsyncMock,
    ) as mock_ask:
        mock_ask.return_value = ok
        res = client.post(
            f"/graphs/{TENANT}/ask",
            json={"question": "how many", "kg_name": "fresh"},
            headers=auth_headers,
        )

    assert res.status_code == 200
    assert res.json()["answer"] == "42"
    mock_ask.assert_called_once()


def test_ask_missing_kg_still_answers_when_data_lives_in_the_base_graph(
    client, auth_headers, mock_neptune
):
    """Same do-no-harm rule on the 404 path: main would have answered this."""
    _wire_kg(mock_neptune, registered=False, has_data=False, base_instances=True)
    ok = NLResult(answer="42", sparql="SELECT ...", explanation="e")

    with patch(
        "cograph_client.api.routes.ask.NLQueryPipeline.ask",
        new_callable=AsyncMock,
    ) as mock_ask:
        mock_ask.return_value = ok
        res = client.post(
            f"/graphs/{TENANT}/ask",
            json={"question": "how many", "kg_name": "typo"},
            headers=auth_headers,
        )

    assert res.status_code == 200
    assert res.json()["answer"] == "42"


def test_ask_probe_failure_degrades_to_answering(client, auth_headers, mock_neptune):
    """A backend hiccup during the probe must never invent "graph missing"."""
    mock_neptune.ask.side_effect = RuntimeError("neptune throttled")
    ok = NLResult(answer="42", sparql="SELECT ...", explanation="e")

    with patch(
        "cograph_client.api.routes.ask.NLQueryPipeline.ask",
        new_callable=AsyncMock,
    ) as mock_ask:
        mock_ask.return_value = ok
        res = client.post(
            f"/graphs/{TENANT}/ask",
            json={"question": "how many", "kg_name": "widgets"},
            headers=auth_headers,
        )

    assert res.status_code == 200
    assert res.json()["answer"] == "42"


def test_ask_without_kg_name_never_probes(client, auth_headers, mock_neptune):
    """No kg_name means the tenant base graph: no KG to check, no extra query."""
    mock_neptune.ask.side_effect = AssertionError("must not probe without a kg_name")
    ok = NLResult(answer="42", sparql="SELECT ...", explanation="e")

    with patch(
        "cograph_client.api.routes.ask.NLQueryPipeline.ask",
        new_callable=AsyncMock,
    ) as mock_ask:
        mock_ask.return_value = ok
        res = client.post(
            f"/graphs/{TENANT}/ask",
            json={"question": "how many"},
            headers=auth_headers,
        )

    assert res.status_code == 200


# --------------------------------------------------------------------------- #
# ONTA-414: kg_name can never break out of the graph IRI
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_name",
    [
        # The tenant-isolation break: ">" closes <...> so a second FROM naming
        # another tenant's graph can be appended.
        "widgets> FROM <https://cograph.tech/graphs/other-tenant",
        "widgets ",
        "a/b",
        'x"y',
        "kg\nname",
    ],
)
def test_ask_malformed_kg_name_is_422(client, auth_headers, mock_neptune, bad_name):
    mock_neptune.ask.side_effect = AssertionError("must reject before querying")

    with patch(
        "cograph_client.api.routes.ask.NLQueryPipeline.ask",
        new_callable=AsyncMock,
    ) as mock_ask:
        res = client.post(
            f"/graphs/{TENANT}/ask",
            json={"question": "how many", "kg_name": bad_name},
            headers=auth_headers,
        )

    assert res.status_code == 422
    mock_ask.assert_not_called()
    # Nothing derived from the hostile name ever reached a SPARQL string.
    for call in mock_neptune.query.call_args_list:
        assert "other-tenant" not in str(call)


@pytest.mark.parametrize(
    "bad_segment",
    [
        "kg%3E",  # ">", the IRI-breakout character
        "kg%20name",  # space
        "kg%0A",  # trailing newline (the "$" vs "\Z" gap)
    ],
)
def test_path_param_kg_name_is_422_via_the_app_level_handler(
    client, auth_headers, bad_segment
):
    """Covers the handler itself, NOT a pydantic pattern.

    Every other 422 in this file is satisfied by the request MODEL, so deleting
    `app.add_exception_handler(InvalidKGName, ...)` would leave them all green
    while turning this into an unhandled 500 in production. This route takes
    kg_name as a PATH parameter, so nothing validates it before it reaches
    `kg_graph_uri` and only the handler stands between the caller and a 500.
    """
    res = client.get(
        f"/graphs/{TENANT}/explore/kgs/{bad_segment}/types/Person/summary",
        headers=auth_headers,
    )
    assert res.status_code == 422, res.text
    assert "kg_name" in res.json()["detail"]


def test_ask_empty_kg_name_still_means_tenant_graph(client, auth_headers):
    """`""` is a legal "no KG selected" value clients already send."""
    ok = NLResult(answer="42", sparql="SELECT ...", explanation="e")
    with patch(
        "cograph_client.api.routes.ask.NLQueryPipeline.ask",
        new_callable=AsyncMock,
    ) as mock_ask:
        mock_ask.return_value = ok
        res = client.post(
            f"/graphs/{TENANT}/ask",
            json={"question": "how many", "kg_name": ""},
            headers=auth_headers,
        )

    assert res.status_code == 200
    assert res.json()["answer"] == "42"
