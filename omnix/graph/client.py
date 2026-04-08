import ssl
import time

import httpx
import structlog

logger = structlog.stdlib.get_logger("omnix.neptune")


def _build_ssl_context(endpoint: str) -> ssl.SSLContext | bool:
    """Build SSL context for Neptune connections.

    Neptune serverless always requires HTTPS with TLS. The AWS-managed
    certificates are signed by the Amazon Root CA, which is in the
    standard CA bundle on most systems. We use the default CA bundle
    and fall back to unverified if explicitly HTTP (local dev).
    """
    if endpoint.startswith("http://"):
        return False  # local dev, no SSL
    ctx = ssl.create_default_context()
    return ctx


# Backend-specific endpoint paths
BACKENDS = {
    "neptune": {
        "query": "/sparql",
        "update": "/sparql",
        "health": "/status",
        "update_param": "update",
    },
    "fuseki": {
        "query": "/ds/query",
        "update": "/ds/update",
        "health": "/$/ping",
        "update_param": "update",
    },
}


class NeptuneClient:
    """SPARQL client for Neptune, Fuseki, or any SPARQL 1.1 endpoint."""

    def __init__(self, endpoint: str, backend: str = "neptune"):
        self.endpoint = endpoint.rstrip("/")
        self.backend = backend
        paths = BACKENDS.get(backend, BACKENDS["neptune"])
        self._query_path = paths["query"]
        self._update_path = paths["update"]
        self._health_path = paths["health"]
        self._update_param = paths["update_param"]
        ssl_context = _build_ssl_context(self.endpoint)
        self._client = httpx.AsyncClient(
            base_url=self.endpoint,
            timeout=120.0,
            verify=ssl_context if ssl_context else False,
        )

    async def query(self, sparql: str) -> dict:
        start = time.monotonic()
        response = await self._client.post(
            self._query_path,
            data={"query": sparql},
            headers={"Accept": "application/sparql-results+json"},
        )
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        response.raise_for_status()
        logger.info("sparql_query", duration_ms=duration_ms, status=response.status_code)
        return response.json()

    async def update(self, sparql: str) -> None:
        start = time.monotonic()
        response = await self._client.post(
            self._update_path,
            data={self._update_param: sparql},
        )
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        response.raise_for_status()
        logger.info("sparql_update", duration_ms=duration_ms, status=response.status_code)

    async def ask(self, sparql: str) -> bool:
        """Execute a SPARQL ASK query and return the boolean result."""
        start = time.monotonic()
        response = await self._client.post(
            self._query_path,
            data={"query": sparql},
            headers={"Accept": "application/sparql-results+json"},
        )
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        response.raise_for_status()
        logger.info("sparql_ask", duration_ms=duration_ms, status=response.status_code)
        return response.json().get("boolean", False)

    async def batch_exists(self, sparql: str) -> set[str]:
        """Execute a SPARQL SELECT for batch existence check. Returns set of URIs that exist."""
        start = time.monotonic()
        response = await self._client.post(
            self._query_path,
            data={"query": sparql},
            headers={"Accept": "application/sparql-results+json"},
        )
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        response.raise_for_status()
        logger.info("sparql_batch_exists", duration_ms=duration_ms, status=response.status_code)
        data = response.json()
        results = data.get("results", {}).get("bindings", [])
        return {row["entity"]["value"] for row in results if "entity" in row}

    async def health(self) -> bool:
        try:
            response = await self._client.get(self._health_path)
            return response.status_code == 200
        except httpx.ConnectError as e:
            logger.warning("neptune_health_connect_error", error=str(e), endpoint=self.endpoint)
            return False
        except ssl.SSLError as e:
            logger.warning("neptune_health_ssl_error", error=str(e), endpoint=self.endpoint)
            return False
        except Exception as e:
            logger.warning("neptune_health_failed", error=str(e), error_type=type(e).__name__, endpoint=self.endpoint)
            return False

    async def close(self):
        await self._client.aclose()
