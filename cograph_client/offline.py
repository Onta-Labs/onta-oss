"""Fail-closed outbound network guard for private / sensitive OSS dogfood.

When ``OMNIX_OFFLINE=1`` or ``COGRAPH_OFFLINE=1``, every call to
:func:`assert_online_url` / :func:`assert_online_host` raises
:class:`OfflineModeError` unless the host is on the allowlist.

**Default is OFF** — normal OSS users see no behavior change until they opt in.

Default allowlist (loopback only)::

    localhost, 127.0.0.1, ::1, [::1]

Extend with a comma-separated override::

    OMNIX_OFFLINE_ALLOW_HOSTS=ollama.local,my-vllm.internal

Typical private-deploy pattern::

    OMNIX_OFFLINE=1
    OMNIX_LLM_BASE_URL=http://127.0.0.1:11434/v1
    OMNIX_EMBED_BASE_URL=http://127.0.0.1:11434/v1

Wired at the main outbound entrypoints (LLM router, embed client, Wikidata
adapter, page-fetch ladder, query-pipeline LLM posts, Anthropic SDK
``messages.create`` call sites in extract / CSV schema / ontology resolve).
Local graph backends (Fuseki / Neptune on loopback) stay reachable under the
default allowlist.
"""

from __future__ import annotations

import os
from typing import Iterable
from urllib.parse import urlparse

# Default loopback hosts — keep private data on-box. Override with
# OMNIX_OFFLINE_ALLOW_HOSTS for a self-hosted stack on a named host.
_DEFAULT_ALLOW_HOSTS: frozenset[str] = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "[::1]",
    }
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class OfflineModeError(RuntimeError):
    """Raised when offline mode blocks an outbound request to a non-allowlisted host.

    Callers should surface the message as-is — it names the blocked host and how
    to either allow a local endpoint or disable offline mode.
    """


def offline_enabled() -> bool:
    """True when the operator opted into fail-closed offline mode.

    Either ``OMNIX_OFFLINE`` or ``COGRAPH_OFFLINE`` (legacy alias) set to a
    truthy value enables the guard. Unset / empty / ``0`` / ``false`` → off.
    """
    for key in ("OMNIX_OFFLINE", "COGRAPH_OFFLINE"):
        raw = (os.environ.get(key) or "").strip().lower()
        if raw in _TRUTHY:
            return True
    return False


def offline_allow_hosts() -> frozenset[str]:
    """Effective allowlist: defaults ∪ ``OMNIX_OFFLINE_ALLOW_HOSTS`` (comma-sep).

    Host comparison is case-insensitive; bracketed IPv6 forms are normalized.
    """
    extra_raw = os.environ.get("OMNIX_OFFLINE_ALLOW_HOSTS") or ""
    extra = {
        _normalize_host(h)
        for h in extra_raw.split(",")
        if h.strip()
    }
    return frozenset(_DEFAULT_ALLOW_HOSTS | extra)


def _normalize_host(host: str) -> str:
    h = (host or "").strip().lower().rstrip(".")
    # urlparse may leave brackets on IPv6 literals; also accept bare ::1.
    if h.startswith("[") and h.endswith("]"):
        return h
    if ":" in h and not h.startswith("["):
        # bare IPv6 → bracketed form for set membership with [::1]
        if h == "::1":
            return h  # keep bare; allowlist has both
    return h


def host_allowed_offline(host: str) -> bool:
    """True if ``host`` is on the offline allowlist (or host is empty → deny)."""
    if not host:
        return False
    n = _normalize_host(host)
    allowed = offline_allow_hosts()
    if n in allowed:
        return True
    # Bracketed vs bare IPv6
    if n.startswith("[") and n.endswith("]") and n[1:-1] in allowed:
        return True
    if f"[{n}]" in allowed:
        return True
    return False


def _purpose_hint(purpose: str, host: str) -> str:
    """Extra guidance when the blocked call looks like an LLM / embed path."""
    p = (purpose or "").lower()
    h = (host or "").lower()
    llmish = (
        "llm" in p
        or "embed" in p
        or "chat" in p
        or "openrouter" in h
        or "cerebras" in h
    )
    if not llmish:
        return (
            "Unset OMNIX_OFFLINE / COGRAPH_OFFLINE to allow external network, "
            "or add the host to OMNIX_OFFLINE_ALLOW_HOSTS."
        )
    return (
        "Unset OMNIX_OFFLINE / COGRAPH_OFFLINE to use cloud providers, "
        "or set OMNIX_LLM_BASE_URL (and optionally OMNIX_EMBED_BASE_URL) to a "
        "local OpenAI-compatible endpoint on an allowlisted host "
        "(default allowlist: localhost, 127.0.0.1, ::1). "
        "Extend the allowlist with OMNIX_OFFLINE_ALLOW_HOSTS=host1,host2."
    )


def assert_online_host(host: str, *, purpose: str = "outbound HTTP") -> None:
    """Raise :class:`OfflineModeError` when offline mode blocks ``host``.

    No-op when offline mode is off, or when ``host`` is allowlisted.
    """
    if not offline_enabled():
        return
    if host_allowed_offline(host):
        return
    display = host or "(empty host)"
    allowed = ", ".join(sorted(offline_allow_hosts()))
    raise OfflineModeError(
        f"Offline mode is enabled (OMNIX_OFFLINE=1 / COGRAPH_OFFLINE=1) and "
        f"blocks {purpose} to '{display}'. Allowed hosts: {allowed}. "
        f"{_purpose_hint(purpose, display)}"
    )


def assert_online_url(url: str, *, purpose: str = "outbound HTTP") -> None:
    """Parse ``url`` and enforce :func:`assert_online_host` on its hostname.

    Empty / unparseable URLs are treated as blocked when offline (fail closed).
    """
    if not offline_enabled():
        return
    try:
        host = urlparse(url).hostname or ""
    except Exception:  # noqa: BLE001 — fail closed on garbage
        host = ""
    assert_online_host(host, purpose=purpose)


def filter_urls_online(urls: Iterable[str]) -> list[str]:
    """Return only URLs whose hosts are allowed under the current offline policy.

    When offline mode is off, returns the input list unchanged. Useful for
    bulk discovery fan-out that should silently drop external targets rather
    than raise per-URL.
    """
    if not offline_enabled():
        return list(urls)
    out: list[str] = []
    for u in urls:
        try:
            host = urlparse(u).hostname or ""
        except Exception:  # noqa: BLE001
            continue
        if host_allowed_offline(host):
            out.append(u)
    return out
