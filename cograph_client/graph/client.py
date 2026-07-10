import re
import ssl
import time

import httpx
import structlog

logger = structlog.stdlib.get_logger("cograph.neptune")

# Cap on how much of the endpoint's error body we surface, so a runaway HTML
# error page can't blow up the retry prompt / logs.
_MAX_ERROR_BODY_CHARS = 600
# Scrub anything URL-shaped out of the diagnostic before it reaches the retry
# prompt or logs — the Neptune host must never leak into user-facing text.
_URL_RE = re.compile(r"https?://\S+")


class SparqlQueryError(RuntimeError):
    """A SPARQL request failed at the endpoint (HTTP 4xx/5xx).

    Unlike ``httpx.HTTPStatusError`` (whose message is a generic
    ``"Client error '400 Bad Request' for url '<host>/sparql'"`` that both
    discards the endpoint's diagnostic body AND leaks the host), this carries
    the endpoint's *parse error* — e.g. Neptune's ``MalformedQueryException``
    naming the offending token — with the host scrubbed out. The NL→SPARQL
    retry loop feeds ``str(err)`` back to the generator so a malformed query can
    self-correct instead of retrying blind.
    """

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"SPARQL endpoint returned {status_code}: {detail}")


def _extract_error_detail(response: httpx.Response) -> str:
    """Pull a host-scrubbed, length-capped diagnostic out of an error response.

    Prefers the structured Neptune fields (``detailedMessage`` / ``message`` /
    ``code``) when the body is JSON, else falls back to the raw text. Never
    includes the endpoint URL/host.
    """
    detail = ""
    try:
        data = response.json()
        if isinstance(data, dict):
            msg = str(data.get("detailedMessage") or data.get("message") or "").strip()
            code = str(data.get("code") or "").strip()
            if msg and code and code not in msg:
                detail = f"{code}: {msg}"
            else:
                detail = msg or code
    except Exception:
        detail = ""
    if not detail:
        detail = (response.text or "").strip()
    detail = _URL_RE.sub("[endpoint]", detail)
    if len(detail) > _MAX_ERROR_BODY_CHARS:
        detail = detail[:_MAX_ERROR_BODY_CHARS] + "…(truncated)"
    return detail or f"{response.status_code} {response.reason_phrase}"


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
        if response.is_error:
            # Capture Neptune's parse-error body (the offending token in a
            # MalformedQueryException) so the NL→SPARQL retry can self-correct,
            # instead of discarding it via raise_for_status() and retrying blind.
            detail = _extract_error_detail(response)
            logger.warning("sparql_query_error", status=response.status_code, duration_ms=duration_ms, detail=detail)
            raise SparqlQueryError(response.status_code, detail)
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
