"""Route-level tests for POST /graphs/{tenant}/ask (persona-eval RCA, ONTA-240).

The /ask contract is ALWAYS an NLResult — a transient provider failure that
somehow escapes the pipeline's internal retry/degrade must never surface as a
bare HTTP 500 with no error body (the persona-eval Cluster 4 symptom). The
route-level handler catches it, logs at the boundary, and returns a graceful
200 NLResult.
"""
from unittest.mock import AsyncMock, patch

from cograph_client.models.query import NLResult

TENANT = "test-tenant"  # conftest's static-key tenant


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
