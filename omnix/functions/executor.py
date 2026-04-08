import json
import time

import boto3
import httpx

from omnix.config import settings
from omnix.models.function import FunctionRef, FunctionResult, FunctionTier


class FunctionExecutor:
    def __init__(self):
        self._lambda_client = None
        self._http_client = httpx.AsyncClient(timeout=30.0)
        self._arn_map = settings.get_function_arns_map()

    @property
    def lambda_client(self):
        if self._lambda_client is None:
            self._lambda_client = boto3.client("lambda")
        return self._lambda_client

    async def invoke(self, ref: FunctionRef, payload: dict) -> FunctionResult:
        start = time.monotonic()
        if ref.tier == FunctionTier.PLATFORM:
            output = await self._invoke_tier1(ref.name, payload)
        else:
            output = await self._invoke_tier2(ref.endpoint_url, payload)
        duration = (time.monotonic() - start) * 1000
        return FunctionResult(output=output, duration_ms=duration, function_name=ref.name)

    async def _invoke_tier1(self, function_name: str, payload: dict) -> dict:
        arn = self._arn_map.get(function_name)
        if not arn:
            raise ValueError(f"No ARN found for platform function '{function_name}'")
        response = self.lambda_client.invoke(
            FunctionName=arn,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )
        response_payload = response["Payload"].read()
        return json.loads(response_payload)

    async def _invoke_tier2(self, endpoint_url: str, payload: dict) -> dict:
        if not endpoint_url:
            raise ValueError("Tier 2 function requires an endpoint URL")
        response = await self._http_client.post(endpoint_url, json=payload)
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self._http_client.aclose()
