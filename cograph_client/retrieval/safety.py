"""Retrieval safety + citation module — the ONE place every rail's web fetch is
made SSRF-safe and every page is reduced to citable text (ONTA-193 §6).

The substrate concern here is item 6 of the retrieval-path convergence: a single
citation/safety module — ``is_fetchable_url`` + ``host_dns_blocked`` + the
HTML→text reduction — shared by **every** fetch on **every** rail (discovery,
enrichment, research), instead of the research harness being the only rail with a
DNS-level SSRF guard. Any rail that touches a page routes its URL through
:func:`is_fetchable_url` (a cheap string pre-filter) and :func:`host_dns_blocked`
(the resolve-and-check fetch-time guard) so a user/LLM-chosen URL can never point
the fetcher at loopback / link-local / private / cloud-metadata ranges.

This module was factored out of ``cograph_client.research.fetch`` (ONTA-166 /
ADR 0006) unchanged — the guards there were battle-tested by that harness's
hardening suite — so it becomes the substrate's shared safety primitive without
a behaviour change. ``cograph_client.research.fetch`` now re-exports these names
for published-package compatibility.

Boundary: OSS. Imports only stdlib. No ``from cograph.*`` and no proprietary
identifiers.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlparse

import structlog

logger = structlog.stdlib.get_logger("cograph.retrieval.safety")


# --- SSRF guard --------------------------------------------------------------- #
# Any rail may fetch URLs chosen by an LLM / discovery provider / user, so a page
# fetch is an SSRF surface. Refuse non-http(s) schemes and hosts that resolve to
# loopback / link-local / private / cloud-metadata ranges. Conservative by IP
# literal + obvious names; a deployment behind a locked-down egress proxy is the
# real defense, this is defense-in-depth.
_BLOCKED_HOST_RE = re.compile(
    r"^(localhost|.*\.local|.*\.internal|metadata\.google\.internal)$",
    re.IGNORECASE,
)


def _host_to_ip(host: str) -> Optional[ipaddress._BaseAddress]:
    """Parse a host into an IP across ENCODINGS, or None for a real hostname.

    An SSRF guard that only recognizes the canonical ``127.0.0.1`` literal is
    trivially bypassed: ``2130706433`` (decimal), ``0x7f000001`` (hex),
    ``0177.0.0.1`` (octal) and ``127.1`` (short) all resolve to loopback at
    connect time. We normalize every numeric IPv4 encoding here so the block
    decision sees the real address. Pure parsing, NO DNS — a genuine hostname
    returns None (its resolution is the egress proxy's job), which keeps this
    deterministic offline (tests/CI never hit the network)."""
    h = (host or "").rstrip(".")
    if not h:
        return None
    # 1. A plain literal (IPv4 or IPv6, incl. the [::1] form urlparse strips).
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    # 2. A bare 32-bit decimal integer host, e.g. "2130706433".
    if h.isdigit():
        try:
            return ipaddress.ip_address(int(h))
        except ValueError:
            return None
    # 3. Hex / octal / short dotted IPv4 forms — let the C numeric parser
    #    (inet_aton, NO DNS) canonicalize them; a hostname raises OSError here.
    try:
        return ipaddress.IPv4Address(socket.inet_aton(h))
    except (OSError, ValueError):
        return None


def _is_blocked_host(host: str) -> bool:
    if not host:
        return True
    if _BLOCKED_HOST_RE.match(host.rstrip(".")):
        return True
    ip = _host_to_ip(host)
    if ip is None:
        return False  # a real hostname; DNS resolution is the egress proxy's job
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_fetchable_url(url: str) -> bool:
    """True when ``url`` is an http(s) URL to a non-internal host.

    STRING-ONLY and NO DNS — deterministic offline (tests/CI never hit the
    network). It catches IP literals + obvious internal names; a real hostname
    that RESOLVES to an internal IP (a DNS-record SSRF) is caught separately at
    fetch time by :func:`host_dns_blocked`, so this stays a cheap pre-filter."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    return not _is_blocked_host(parsed.hostname)


# --- DNS-resolving SSRF guard (fetch-time) ------------------------------------ #
# The string guard above is name-blind: a public hostname whose A record points
# at 169.254.169.254 (cloud metadata) or a private range sails through it. Before
# a fetcher opens a socket it RESOLVES the host and re-checks every returned
# address against the same block rules. (This narrows, but does not fully close,
# DNS rebinding — a locked-down egress proxy remains the belt to this suspenders;
# resolve-time validation is the defense a proxy-less bare-OSS deployment
# otherwise lacked entirely.)
def _resolve_ips(host: str) -> list[str]:
    """Resolve a hostname to its A/AAAA addresses; ``[]`` on failure.

    Isolated at module scope so tests can stub it (keeping them offline). A name
    that doesn't resolve returns ``[]`` → not treated as blocked, since it simply
    can't connect anywhere."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    return list({str(info[4][0]) for info in infos})


def _host_dns_blocked(host: str) -> bool:
    """True when a real hostname RESOLVES to a blocked address.

    IP-literal hosts are already covered by :func:`_is_blocked_host`, so they
    short-circuit False here (no redundant lookup)."""
    if not host or _host_to_ip(host) is not None:
        return False
    for ip in _resolve_ips(host):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (
            addr.is_loopback
            or addr.is_link_local
            or addr.is_private
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return True
    return False


async def host_dns_blocked(host: str) -> bool:
    """Async wrapper — runs the blocking lookup off the event loop; never raises
    (a resolver hiccup must not sink the fetch, the connect will fail instead)."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _host_dns_blocked, host)
    except Exception:  # pragma: no cover - defensive
        return False


# --- HTML → text -------------------------------------------------------------- #
class _TextExtractor(HTMLParser):
    """Minimal readability: drop script/style/nav chrome, keep visible text and
    the ``<title>``. Not a full readability port — enough to feed an extractor;
    the premium render tier returns clean markdown for the hard pages."""

    # NB: do NOT skip <head> wholesale — <title> lives there. Its noisy children
    # (script/style) are skipped individually below.
    _SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        text = data.strip()
        if text:
            self._parts.append(text)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "\n".join(self._parts)).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """Reduce an HTML document to ``(title, text)``. Never raises."""
    try:
        parser = _TextExtractor()
        parser.feed(html or "")
        parser.close()
        return parser.title.strip(), parser.text()
    except Exception:  # pragma: no cover - parser is lenient, guard anyway
        # Last resort: strip tags with a regex so we still return *something*.
        stripped = re.sub(r"<[^>]+>", " ", html or "")
        return "", re.sub(r"\s+", " ", stripped).strip()


__all__ = [
    "host_dns_blocked",
    "html_to_text",
    "is_fetchable_url",
]
