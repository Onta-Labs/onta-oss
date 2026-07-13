"""Product-analytics sink seam (ONTA-323).

A thin, process-wide seam for emitting **product/app-observability events**
(business events + exceptions) out of the backend, mirroring the established
OSS plugin pattern (:func:`register_delivery_sink`, :func:`register_geocoder`,
:func:`register_external_verifier`, …). The OSS default is a **no-op**, so the
package is analytics-free and importable standalone with **zero third-party
dependency** — the whole point of the OSS/proprietary boundary. OSS never phones
home to any SaaS; the seam is deliberately vendor-neutral and names no provider.

Boundary split (docs/oss_proprietary_boundary.md, ONTA-323):

- **OSS (here):** the :class:`AnalyticsSink` protocol + registry, the no-op
  default, and the fire-and-forget :func:`emit` / :func:`flush_analytics`
  helpers the event call sites use. No third-party analytics import anywhere.
- **Premium (NOT here, just the hook):** a real sink that ships events to a
  hosted analytics provider registers via :func:`register_analytics_sink` at app
  boot (through the ``OMNIX_ANALYTICS_PLUGIN`` env hook) and transparently
  supersedes the no-op default. That code lives in the proprietary ``cograph/``
  tree and this OSS module never imports it.

Discipline — **analytics must never break or slow a request** (the same contract
as :meth:`cograph_client.usage.recorder.UsageRecorder.observe`): :func:`emit`
and :func:`flush_analytics` are strictly best-effort. They swallow every error
(logging it at ``debug``) and hand the sink a fire-and-forget call — the sink is
responsible for its own non-blocking delivery (a real sink batches on a
background thread). A misbehaving sink can never surface as a 500.

Boundary: OSS. Imports only stdlib / ``cograph_client.*``. No ``from cograph.*``
and no proprietary identifiers.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, runtime_checkable

import structlog

logger = structlog.stdlib.get_logger("cograph.analytics")


@runtime_checkable
class AnalyticsSink(Protocol):
    """A pluggable product-analytics sink.

    The proprietary sink implements this exact shape; the OSS default
    (:class:`NoOpSink`) does nothing. Implementations MUST NOT block the caller
    (buffer/batch internally) and SHOULD NOT raise — :func:`emit` swallows
    anything that escapes anyway, but a well-behaved sink degrades quietly.

    * ``capture`` — record one event for ``distinct_id`` (the identified user, or
      ``None`` for an anonymous/unattributed event) with a flat ``properties``
      mapping.
    * ``flush`` — best-effort drain of any buffered events (called on app
      shutdown).
    """

    def capture(
        self,
        *,
        event: str,
        distinct_id: Optional[str],
        properties: Mapping[str, Any],
    ) -> None: ...

    def flush(self) -> None: ...


class NoOpSink:
    """OSS default sink: drops every event. Keeps OSS analytics-free.

    ``emit()`` routes through this until a real sink is registered, so a bare
    OSS deployment (or CI/dev with no plugin configured) emits nothing and pulls
    in no analytics dependency.
    """

    def capture(
        self,
        *,
        event: str,
        distinct_id: Optional[str],
        properties: Mapping[str, Any],
    ) -> None:
        return None

    def flush(self) -> None:
        return None


# --- registry --------------------------------------------------------------- #
# Module-level single-sink registry — same shape as register_delivery_sink /
# register_page_fetcher / register_secret_cipher: the premium sink registers at
# boot and supersedes the OSS default; a bare OSS deployment uses NoOpSink and
# emits nothing.
_registered_sink: Optional[AnalyticsSink] = None
_default_sink: AnalyticsSink = NoOpSink()


def register_analytics_sink(sink: Optional[AnalyticsSink]) -> None:
    """Register (or clear, with ``None``) the process analytics sink.

    The premium analytics binding calls this at startup (via the
    ``OMNIX_ANALYTICS_PLUGIN`` hook); OSS deployments never do and fall back to
    the no-op :class:`NoOpSink`. Idempotent — last write wins.
    """
    global _registered_sink
    _registered_sink = sink
    logger.info(
        "analytics_sink_registered",
        sink=getattr(sink, "name", type(sink).__name__) if sink is not None else None,
    )


def get_analytics_sink() -> AnalyticsSink:
    """The registered sink (premium), else the OSS no-op default."""
    return _registered_sink if _registered_sink is not None else _default_sink


def reset_analytics_sink() -> None:
    """Test helper — clear the registered sink so the no-op default is used."""
    global _registered_sink
    _registered_sink = None


def emit(
    event: str,
    *,
    distinct_id: Optional[str] = None,
    **properties: Any,
) -> None:
    """Emit one product-analytics event. No-op unless a sink is registered.

    Fire-and-forget: never raises and never blocks the caller (same discipline
    as ``UsageRecorder.observe``). Any error from the sink is swallowed and
    logged at ``debug`` — analytics must never break or slow a request. The sink
    owns non-blocking delivery.
    """
    try:
        get_analytics_sink().capture(
            event=event,
            distinct_id=distinct_id,
            properties=properties,
        )
    except Exception:  # noqa: BLE001 — analytics must never break a request
        # NB: structlog reserves ``event`` for the log message, so the emitted
        # event name is logged under ``event_name`` to avoid the kwarg collision.
        logger.debug("analytics_emit_failed", event_name=event, exc_info=True)


def flush_analytics() -> None:
    """Best-effort drain of any buffered events (app shutdown). Never raises."""
    try:
        get_analytics_sink().flush()
    except Exception:  # noqa: BLE001 — shutdown flush is best-effort
        logger.debug("analytics_flush_failed", exc_info=True)


def distinct_id_for(
    subject: Optional[str], tenant: Optional[str]
) -> Optional[str]:
    """Resolve an analytics ``distinct_id`` from an auth subject + tenant.

    Prefers the identified user (the Clerk user id ``get_tenant`` resolved onto
    :class:`~cograph_client.auth.api_keys.TenantContext.subject`) so frontend and
    backend land on ONE person. Falls back to a stable ``system:<tenant>`` id for
    background / non-request events that carry only a tenant. Returns ``None``
    when neither is known (unauthenticated traffic) — never attributing to a
    caller-controlled path tenant, the same rule the usage recorder follows for
    401/403.
    """
    if subject:
        return subject
    if tenant:
        return f"system:{tenant}"
    return None


__all__ = [
    "AnalyticsSink",
    "NoOpSink",
    "distinct_id_for",
    "emit",
    "flush_analytics",
    "get_analytics_sink",
    "register_analytics_sink",
    "reset_analytics_sink",
]
