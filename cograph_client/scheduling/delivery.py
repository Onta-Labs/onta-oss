"""Delivery sinks for a ``notify`` schedule (ONTA-235).

A ``notify`` schedule watches a value in a KG and, when it CHANGES since the last
fire, delivers a small JSON change payload OUT of the platform — to an
orchestrator webhook, a relations inbox integration, any HTTP endpoint the tenant
controls. This module is the DELIVERY SEAM: a swappable :class:`DeliverySink`
protocol (``register_delivery_sink`` mirroring ``register_page_fetcher`` /
``register_job_backend`` / ``register_secret_cipher``) and the OSS default —
a best-effort HTTP POST that routes every outbound request through the ONE shared
OSS SSRF guard (:mod:`cograph_client.retrieval.safety`).

Boundary split (docs/oss_proprietary_boundary.md §22):

- **OSS (here):** the sink protocol + registry, and the best-effort HTTP-POST
  sink. "Best-effort" = one attempt, SSRF-guarded, structured pass/fail result;
  no retries, no dead-letter queue, no HMAC signing, no rate limiting. This is
  the same graceful-degradation posture as every other OSS default (the static
  fetcher, the local AES cipher): it works out of the box for a self-hoster with
  zero cloud dependency.
- **Premium (NOT here, just the hook):** a RELIABLE delivery sink — retries with
  backoff + DLQ, HMAC request signing, per-tenant rate limiting — registers via
  :func:`register_delivery_sink` at app boot and transparently supersedes the OSS
  default. That code lives in the proprietary ``cograph/`` tree and this OSS
  module never imports it.

SSRF: the outbound POST target is a tenant-/LLM-chosen URL, so it is an SSRF
surface exactly like a page fetch. Every sink here MUST refuse a URL the shared
guard rejects (loopback / link-local / private / cloud-metadata / non-http(s)) —
a raw ``httpx.post`` to an unchecked URL would be both a boundary violation
(re-implementing the guard) and a live SSRF hole. We reuse
``is_fetchable_url`` (cheap string pre-filter) + ``host_dns_blocked`` (fetch-time
DNS re-check) verbatim; there is no second SSRF implementation.

Secrets: a delivery target may need a credential (a bearer token / signing key
for the tenant's endpoint). It is NEVER stored raw on the schedule row — the row
carries a ``secret_ref`` (an opaque ciphertext produced by the OSS
``SecretCipher`` seam) that the sink decrypts at delivery time only. A schedule
row never contains plaintext secret material.

Boundary: OSS. Imports only stdlib / ``cograph_client.*`` / ``httpx``. No
``from cograph.*`` and no proprietary identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx
import structlog

from cograph_client.retrieval.safety import host_dns_blocked, is_fetchable_url

logger = structlog.stdlib.get_logger("cograph.scheduling.delivery")

# Bounds the outbound POST so a slow/hostile endpoint can't wedge a runner tick.
_DEFAULT_TIMEOUT = 10.0
_UA = "Mozilla/5.0 (compatible; OntaNotifyBot/1.0; +https://onta.sh/bot)"


@dataclass
class DeliveryTarget:
    """Where + how a change notification is delivered.

    ``url`` is the outbound HTTP endpoint. ``secret_ref`` (optional) is an OPAQUE
    ciphertext from the OSS :class:`~cograph_client.api_registry.crypto.SecretCipher`
    seam — decrypted only at delivery time, never stored/transported in the clear.
    ``headers`` are static, non-secret headers; a decrypted secret is applied by
    the sink (as a bearer/authorization header) and never persisted here.
    """

    url: str
    secret_ref: Optional[str] = None
    headers: dict = field(default_factory=dict)

    @classmethod
    def from_params(cls, sink: dict | None) -> Optional["DeliveryTarget"]:
        """Build a target from a schedule's ``params['sink']`` dict, or ``None``.

        Tolerant: a missing/empty ``url`` yields ``None`` (the schedule simply has
        no reachable sink — dispatch logs and skips delivery rather than raising),
        so a malformed row can never sink a runner tick.
        """
        if not isinstance(sink, dict):
            return None
        url = str(sink.get("url") or "").strip()
        if not url:
            return None
        headers = sink.get("headers")
        return cls(
            url=url,
            secret_ref=(sink.get("secret_ref") or None),
            headers=headers if isinstance(headers, dict) else {},
        )


@dataclass
class DeliveryResult:
    """Structured outcome of ONE delivery attempt. Never raises out of a sink."""

    ok: bool
    status_code: Optional[int] = None
    error: Optional[str] = None
    # Whether the target URL was refused by the SSRF guard (vs a network error).
    blocked: bool = False


@runtime_checkable
class DeliverySink(Protocol):
    """A pluggable delivery sink.

    * ``name`` — stable id (``http`` for the OSS default; a premium reliable sink
      registers under its own name and supersedes it).
    * ``deliver(target, payload)`` — send ``payload`` (a JSON-serializable change
      notification) to ``target``. MUST NOT raise: a failure returns
      ``DeliveryResult(ok=False, error=...)`` so a runner tick never dies on a bad
      endpoint. MUST refuse an SSRF-blocked URL (``blocked=True``).
    """

    name: str

    async def deliver(
        self, target: DeliveryTarget, payload: dict
    ) -> DeliveryResult: ...


def _resolve_secret(secret_ref: Optional[str]) -> Optional[str]:
    """Decrypt a ``secret_ref`` via the OSS cipher seam, or ``None``.

    Best-effort + fail-closed: no ``secret_ref`` → ``None`` (no auth header). A
    ref present but no cipher configured, or a decrypt failure, logs and returns
    ``None`` (delivery proceeds unauthenticated rather than leaking the ciphertext
    or crashing the tick). NEVER logs the plaintext or the ciphertext.
    """
    if not secret_ref:
        return None
    try:
        from cograph_client.api_registry.crypto import get_secret_cipher

        cipher = get_secret_cipher()
        if cipher is None:
            logger.warning("delivery_secret_no_cipher")
            return None
        return cipher.decrypt(secret_ref)
    except Exception:  # noqa: BLE001 — a secret hiccup must not sink the tick
        logger.warning("delivery_secret_decrypt_failed")
        return None


class HttpPostSink:
    """OSS default sink: a single best-effort, SSRF-guarded HTTP POST.

    Best-effort = ONE attempt, no retries/DLQ/HMAC/rate-limit (those are the
    premium reliable sink registered over this seam). It:

    - refuses a URL the shared SSRF guard rejects (``is_fetchable_url`` +
      ``host_dns_blocked``) — the SAME guard every page fetch uses, no second
      implementation;
    - POSTs the JSON ``payload`` with a bounded timeout;
    - applies a decrypted ``secret_ref`` as a ``Authorization: Bearer`` header
      (never persisted, never logged);
    - never raises — returns a structured :class:`DeliveryResult`.
    """

    name = "http"

    def __init__(self, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    async def deliver(
        self, target: DeliveryTarget, payload: dict
    ) -> DeliveryResult:
        url = target.url
        # SSRF pre-filter (string-only, no DNS) — refuse non-http(s) + IP-literal
        # internal hosts before we ever open a socket.
        if not is_fetchable_url(url):
            logger.warning("delivery_url_blocked", reason="not_fetchable")
            return DeliveryResult(
                ok=False, blocked=True, error="blocked or non-http(s) URL"
            )
        # Fetch-time DNS re-check — a public hostname whose A record points at an
        # internal/metadata address is caught here (the string guard can't see it).
        if await host_dns_blocked(urlparse(url).hostname or ""):
            logger.warning("delivery_url_blocked", reason="dns_internal")
            return DeliveryResult(
                ok=False, blocked=True, error="host resolves to a blocked address"
            )

        headers = {
            "User-Agent": _UA,
            "Content-Type": "application/json",
            **{str(k): str(v) for k, v in (target.headers or {}).items()},
        }
        secret = _resolve_secret(target.secret_ref)
        if secret:
            headers["Authorization"] = f"Bearer {secret}"

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                # Do NOT follow redirects: a 302 could bounce us onto an internal
                # host past the guard (same reasoning as the static fetcher).
                follow_redirects=False,
            ) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except Exception as exc:  # network error, timeout, bad TLS, …
            logger.warning("delivery_post_failed", error=str(exc)[:200])
            return DeliveryResult(ok=False, error=str(exc)[:200])

        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.warning("delivery_post_non_2xx", status=resp.status_code)
        return DeliveryResult(ok=ok, status_code=resp.status_code)


# --- registry ---------------------------------------------------------------- #
# Module-level single-sink registry — same shape as register_page_fetcher /
# register_secret_cipher: a premium reliable sink registers at boot and supersedes
# the OSS default; a bare OSS deployment uses HttpPostSink.
_registered_sink: Optional[DeliverySink] = None
_default_sink: DeliverySink = HttpPostSink()


def register_delivery_sink(sink: Optional[DeliverySink]) -> None:
    """Register (or clear, with ``None``) the process delivery sink.

    The premium reliable-delivery binding (retries / DLQ / HMAC / rate-limit)
    calls this at startup; OSS deployments never do and fall back to the
    best-effort :class:`HttpPostSink`. Idempotent — last write wins.
    """
    global _registered_sink
    _registered_sink = sink
    logger.info(
        "delivery_sink_registered",
        sink=getattr(sink, "name", None) if sink is not None else None,
    )


def get_delivery_sink() -> DeliverySink:
    """The registered sink (premium reliable), else the OSS best-effort default."""
    return _registered_sink if _registered_sink is not None else _default_sink


def reset_delivery_sink() -> None:
    """Test helper — clear the registered sink so the OSS default is used again."""
    global _registered_sink
    _registered_sink = None


__all__ = [
    "DeliveryResult",
    "DeliverySink",
    "DeliveryTarget",
    "HttpPostSink",
    "get_delivery_sink",
    "register_delivery_sink",
    "reset_delivery_sink",
]
