import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from cograph_client.usage.recorder import get_usage_recorder

logger = structlog.stdlib.get_logger("cograph.api")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.monotonic()
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            method=request.method,
            path=request.url.path,
            client_ip=request.client.host if request.client else None,
        )

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            logger.exception("request_error", duration_ms=duration_ms)
            self._observe_usage(request, 500, duration_ms)
            raise

        duration_ms = round((time.monotonic() - start) * 1000, 1)
        logger.info(
            "request",
            status=response.status_code,
            duration_ms=duration_ms,
        )
        self._observe_usage(request, response.status_code, duration_ms)
        return response

    @staticmethod
    def _observe_usage(request: Request, status: int, duration_ms: float) -> None:
        # Per-tenant usage metering (dashboard "API usage" panel). Attribution
        # comes from the AUTHENTICATED tenant that get_tenant stashed on
        # request.state — absent (→ recorded as nothing) for 401s and for
        # 404/405s that never reached the auth dependency, so unauthenticated
        # traffic can't be attributed to a path-named tenant. observe() is
        # sync, in-memory, and never raises — see usage/recorder.py.
        get_usage_recorder().observe(
            path=request.url.path,
            method=request.method,
            status=status,
            duration_ms=duration_ms,
            api_key=request.headers.get("x-api-key"),
            tenant=getattr(request.state, "usage_tenant", None),
        )
