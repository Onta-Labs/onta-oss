"""CUSTOM functions may target a Lambda ARN in the same endpoint_url slot.

HTTPS URLs keep working. Invalid / empty / ARN-ish junk is 422. The executor
invokes a Lambda ARN via boto3 and still HTTP-POSTs a https URL. All mocked.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from infona_client.auth.api_keys import TenantContext
from infona_client.functions.executor import FunctionExecutor
from infona_client.models.function import FunctionRef, FunctionRegister, FunctionTier

VALID_ARN = "arn:aws:lambda:us-east-1:123456789012:function:score-place"  # boundary-ok: AWS example account 123456789012, not a real account
VALID_ARN_QUALIFIER = "arn:aws:lambda:us-east-1:123456789012:function:score-place:prod"  # boundary-ok: AWS example account 123456789012, not a real account
VALID_ARN_VERSION = "arn:aws:lambda:eu-west-1:123456789012:function:score-place:7"  # boundary-ok: AWS example account 123456789012, not a real account
VALID_ARN_LATEST = "arn:aws:lambda:us-west-2:123456789012:function:score-place:$LATEST"  # boundary-ok: AWS example account 123456789012, not a real account
VALID_HTTPS = "https://api.example.com/score"

_REGISTER_BODY = {
    "name": "score",
    "entity_type": "Place",
}


def _register(**overrides) -> FunctionRegister:
    payload = {**_REGISTER_BODY, "endpoint_url": VALID_HTTPS, **overrides}
    return FunctionRegister(**payload)


# ---------------------------------------------------------------------------
# FunctionRegister / route validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arn",
    [VALID_ARN, VALID_ARN_QUALIFIER, VALID_ARN_VERSION, VALID_ARN_LATEST],
)
def test_function_register_accepts_lambda_arn(arn):
    body = _register(endpoint_url=arn)
    assert body.endpoint_url == arn


def test_function_register_accepts_https():
    body = _register(endpoint_url=VALID_HTTPS)
    assert body.endpoint_url == VALID_HTTPS


@pytest.mark.parametrize(
    "junk",
    [
        "",
        "   ",
        "arn:aws:s3:::bucket",
        "arn:aws:lambda:bad",
        "arn:aws:lambda:us-east-1:123456789012:layer:foo:1",  # boundary-ok: AWS example account 123456789012, not a real account
        "not-a-url",
        "http://api.example.com/score",
    ],
)
def test_function_register_rejects_invalid_endpoint(junk):
    with pytest.raises(ValidationError):
        _register(endpoint_url=junk)


def _functions_client() -> TestClient:
    from infona_client.api.deps import get_neptune_client
    from infona_client.api.routes import functions as functions_routes
    from infona_client.auth import api_keys

    class FakeNeptune:
        async def update(self, sparql: str):
            return None

        async def query(self, sparql: str):  # pragma: no cover
            return {"head": {"vars": []}, "results": {"bindings": []}}

    app = FastAPI()
    app.include_router(functions_routes.router)
    app.dependency_overrides[get_neptune_client] = lambda: FakeNeptune()
    app.dependency_overrides[api_keys.get_tenant] = lambda: TenantContext(
        tenant_id="t1", api_key="k", is_operator=False
    )
    return TestClient(app)


def test_route_accepts_lambda_arn():
    resp = _functions_client().post(
        "/graphs/t1/functions",
        json={**_REGISTER_BODY, "endpoint_url": VALID_ARN},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["registered"] == "score"


def test_route_accepts_https():
    resp = _functions_client().post(
        "/graphs/t1/functions",
        json={**_REGISTER_BODY, "endpoint_url": VALID_HTTPS},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.parametrize(
    "junk",
    ["", "arn:aws:s3:::bucket", "arn:aws:lambda:bad"],
)
def test_route_rejects_invalid_endpoint_with_422(junk):
    resp = _functions_client().post(
        "/graphs/t1/functions",
        json={**_REGISTER_BODY, "endpoint_url": junk},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Executor routing
# ---------------------------------------------------------------------------


@pytest.fixture
def executor():
    with patch("infona_client.functions.executor.settings") as mock_settings:
        mock_settings.get_function_arns_map.return_value = {}
        return FunctionExecutor()


@pytest.mark.asyncio
async def test_executor_invokes_lambda_arn_not_httpx(executor):
    mock_payload = {"score": 91}
    executor._lambda_client = MagicMock()
    executor._lambda_client.invoke.return_value = {
        "Payload": MagicMock(read=MagicMock(return_value=json.dumps(mock_payload).encode()))
    }
    executor._http_client = AsyncMock()

    ref = FunctionRef(
        name="score",
        entity_type="Place",
        endpoint_url=VALID_ARN,
        tier=FunctionTier.CUSTOM,
    )
    payload = {"lat": 40.7, "lng": -73.9}
    result = await executor.invoke(ref, payload)

    assert result.output == mock_payload
    executor._lambda_client.invoke.assert_called_once_with(
        FunctionName=VALID_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    executor._http_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_executor_http_posts_https_url(executor):
    mock_response = MagicMock()
    mock_response.json.return_value = {"score": 85}
    mock_response.raise_for_status = MagicMock()
    executor._http_client = AsyncMock()
    executor._http_client.post.return_value = mock_response
    executor._lambda_client = MagicMock()

    ref = FunctionRef(
        name="score",
        entity_type="Place",
        endpoint_url=VALID_HTTPS,
        tier=FunctionTier.CUSTOM,
    )
    payload = {"lat": 40.7, "lng": -73.9}
    result = await executor.invoke(ref, payload)

    assert result.output["score"] == 85
    executor._http_client.post.assert_called_once_with(
        VALID_HTTPS, json=payload, headers=None
    )
    executor._lambda_client.invoke.assert_not_called()
