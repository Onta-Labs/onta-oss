import asyncio
import re
import ssl
import time

import httpx
import structlog

logger = structlog.stdlib.get_logger("cograph.neptune")

# Cap on how much of the endpoint's error body we surface, so a runaway HTML
# error page can't blow up the retry prompt / logs.
_MAX_ERROR_BODY_CHARS = 600
# Scrub anything endpoint-shaped out of the diagnostic before it reaches the
# retry prompt or logs — the Neptune host must NEVER leak into user-facing text.
# Covers three shapes seen in real 4xx/5xx bodies: a scheme URL, a bare
# host:port (e.g. an Envoy/ALB upstream error `…neptune.amazonaws.com:8182` with
# no scheme), and an AWS/Neptune hostname with no port.
_SCRUB_RES = (
    re.compile(r"https?://\S+"),
    re.compile(r"\b(?:[\w-]+\.)+[\w-]+:\d+\b"),
    re.compile(r"\b(?:[\w-]+\.)+(?:amazonaws\.com|neptune\.[\w.-]+)\b"),
)


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
    for _scrub in _SCRUB_RES:
        detail = _scrub.sub("[endpoint]", detail)
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


# Transient TRANSPORT failures worth retrying on the read path. The one that bit
# the nightly QC audit is RemoteProtocolError: Neptune drops idle keep-alive
# connections, httpx then reuses a dead one and raises "Server disconnected
# without sending a response" on the very next request — one such drop was
# crashing an entire ~10-min audit sweep. We retry only FAST-FAILING transport
# errors (a dropped keep-alive / refused / reset connection surfaces
# immediately), so the worst-case added latency is just the backoff. The
# TIMEOUT-class errors (ConnectTimeout / PoolTimeout / ReadTimeout / WriteTimeout)
# are deliberately EXCLUDED: with the flat 120s client timeout, retrying them
# would stack multiple 120s stalls onto a single live query, and a read timeout
# on a genuinely slow query must not be blindly re-issued and amplify endpoint
# load. HTTP status errors are likewise never retried here (a malformed LLM query
# returns a deterministic 4xx, raised immediately for the NL loop to self-correct)
# and writes (`update`) stay off this path to keep at-most-once mutation semantics.
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.RemoteProtocolError,  # dropped keep-alive: "Server disconnected …"
    httpx.ConnectError,         # connection refused / reset (fast-failing)
    httpx.ReadError,            # socket read failure mid-response (fast-failing)
    httpx.WriteError,           # socket write failure (fast-failing)
)
_MAX_TRANSPORT_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.5


class NeptuneClient:
    """SPARQL client for Neptune, Fuseki, or any SPARQL 1.1 endpoint."""

    def __init__(
        self,
        endpoint: str,
        backend: str = "neptune",
        auth: tuple[str, str] | None = None,
    ):
        """``auth`` is an optional (username, password) HTTP Basic credential.
        Neptune authorizes via IAM/network and needs none, so it defaults off;
        it exists for auth-protected SPARQL endpoints such as a Fuseki store
        whose update endpoint is guarded (e.g. the QC disposable-store sidecar,
        which ships with an admin password). httpx sends the credential as an
        ``Authorization: Basic`` header; it is never logged here."""
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
            auth=auth,
        )

    async def _post_with_retry(
        self, path: str, *, data: dict, headers: dict | None = None
    ) -> httpx.Response:
        """POST to the endpoint, retrying only transient TRANSPORT failures.

        Bounded retry (``_MAX_TRANSPORT_ATTEMPTS``) on the connection-class errors
        in ``_RETRYABLE_TRANSPORT_ERRORS`` — chiefly a dropped keep-alive
        connection — so a single stale-connection blip does not abort a
        long-running read caller (e.g. the nightly QC audit). Each retry acquires
        a fresh connection from the pool. HTTP *status* handling stays with the
        caller: this only re-issues the request on a transport exception, and the
        successful ``Response`` (including 4xx/5xx) is returned unchanged for the
        caller to interpret. Used by reads only; writes are not routed here.
        """
        last_exc: BaseException | None = None
        for attempt in range(1, _MAX_TRANSPORT_ATTEMPTS + 1):
            try:
                return await self._client.post(path, data=data, headers=headers)
            except _RETRYABLE_TRANSPORT_ERRORS as exc:
                last_exc = exc
                if attempt >= _MAX_TRANSPORT_ATTEMPTS:
                    break
                logger.warning(
                    "sparql_transport_retry",
                    attempt=attempt,
                    max_attempts=_MAX_TRANSPORT_ATTEMPTS,
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(_RETRY_BACKOFF_S * attempt)
        assert last_exc is not None  # loop only exits via return or a caught exc
        raise last_exc

    async def query(self, sparql: str) -> dict:
        start = time.monotonic()
        response = await self._post_with_retry(
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
        response = await self._post_with_retry(
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
        response = await self._post_with_retry(
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
