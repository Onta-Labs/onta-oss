import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cograph.functions.executor import FunctionExecutor
from cograph.models.function import FunctionRef, FunctionTier


@pytest.fixture
def executor():
    with patch("cograph.functions.executor.settings") as mock_settings:
        mock_settings.get_function_arns_map.return_value = {
            "calculate_distance": "arn:aws:lambda:us-east-1:123:function:calc-dist"
        }
        return FunctionExecutor()


@pytest.mark.asyncio
async def test_invoke_tier1(executor):
    mock_payload = {"distance_km": 5.2, "duration_minutes": 12.0}
    mock_response = {
        "Payload": MagicMock(read=MagicMock(return_value=json.dumps(mock_payload).encode()))
    }
    executor._lambda_client = MagicMock()
    executor._lambda_client.invoke.return_value = mock_response

    ref = FunctionRef(
        name="calculate_distance",
        entity_type="Place",
        tier=FunctionTier.PLATFORM,
    )
    result = await executor.invoke(ref, {"origin": "A", "destination": "B"})

    assert result.output["distance_km"] == 5.2
    assert result.function_name == "calculate_distance"
    assert result.duration_ms > 0


@pytest.mark.asyncio
async def test_invoke_tier1_missing_arn(executor):
    ref = FunctionRef(
        name="nonexistent_function",
        entity_type="Place",
        tier=FunctionTier.PLATFORM,
    )
    with pytest.raises(ValueError, match="No ARN found"):
        await executor.invoke(ref, {})


@pytest.mark.asyncio
async def test_invoke_tier2(executor):
    mock_response = MagicMock()
    mock_response.json.return_value = {"score": 85}
    mock_response.raise_for_status = MagicMock()

    executor._http_client = AsyncMock()
    executor._http_client.post.return_value = mock_response

    ref = FunctionRef(
        name="custom_scorer",
        entity_type="Place",
        endpoint_url="https://api.example.com/score",
        tier=FunctionTier.CUSTOM,
    )
    result = await executor.invoke(ref, {"lat": 40.7, "lng": -73.9})

    assert result.output["score"] == 85
    executor._http_client.post.assert_called_once_with(
        "https://api.example.com/score", json={"lat": 40.7, "lng": -73.9}
    )


@pytest.mark.asyncio
async def test_invoke_tier2_no_url(executor):
    ref = FunctionRef(
        name="broken",
        entity_type="Place",
        tier=FunctionTier.CUSTOM,
    )
    with pytest.raises(ValueError, match="endpoint URL"):
        await executor.invoke(ref, {})
