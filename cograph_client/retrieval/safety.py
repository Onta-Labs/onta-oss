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
    """Minimal readability that PRESERVES the structural cues an extractor needs.

    Beyond dropping script/style/nav chrome and keeping the ``<title>``, this
    keeps the row/column shape of ``<table>``\\ s and the label/value pairing of
    ``<dl>``\\ s — the very carriers of the DECLARED STRUCTURED attributes
    (pricing, latency, NPI/taxonomy, …) a naive text dump destroys (ONTA-193 P4
    depth RCA). A flat reducer turns a pricing table into an undifferentiated
    vertical stream — ``Model / Input / Output / Context / gpt-4o / $2.50 /
    $10.00 / 128k / …`` — with the row→column association GONE, so the extractor
    can no longer tell which price belongs to which model and the declared fields
    never land (name/description/url do, because those need no association). We
    instead emit each table ROW as one line with cells joined by ``" | "`` —
    ``gpt-4o | $2.50 | $10.00 | 128k`` — so a record stays coherent, and a
    ``<dl>`` emits ``term: definition`` pairs. Empty cells are kept in place so a
    missing value is a visible gap (``a |  | c``), never silently realigned into a
    neighbour's column — the anti-fabrication contract (unknown → gap, never an
    invented value): this reducer only ever REFORMATS text already on the page, it
    can add no value that was not there. Adjacent display:block elements inside a
    cell are space-broken (see ``_BLOCK``) so they can't glue into a token the page
    never rendered (``<dt>2</dt><dd>50</dd>`` → ``2 50``, not ``250``); inline runs
    (``$<b>2.50</b>`` → ``$2.50``) stay joined, matching the browser. Everything
    else is the prior
    line-per-text-node flow. Not a full readability port — enough to feed an
    extractor; the premium render tier returns clean markdown for the hard pages,
    and ITS rendered HTML flows back through here too, so this reshaping is what
    gives depth on every successfully-fetched page regardless of fetch tier.

    Robustness note: ``</td>``/``</th>``/``</tr>``/``</dt>``/``</dd>`` are OPTIONAL
    end tags (routinely omitted by real markup) and ``HTMLParser`` does not insert
    the implied closes, so boundaries are keyed off the always-present START tags
    plus the non-omittable ``</table>``/``</dl>``, and every open buffer is flushed
    at EOF (:meth:`text`) — a page that ends mid-cell (truncated fetch) or omits
    its closes still yields ALL its text, never less than the old flat dump.
    """

    # NB: do NOT skip <head> wholesale — <title> lives there. Its noisy children
    # (script/style) are skipped individually below.
    _SKIP = {"script", "style", "noscript", "template", "svg"}
    # Block-level tags whose boundary inside a buffered cell/term/value marks a
    # VISUAL break: a space is inserted so two adjacent block texts can't GLUE
    # into a token that never appeared on the rendered page
    # (``<div>12</div><div>8k</div>`` → ``12 8k``, NOT ``128k`` — the
    # anti-fabrication contract; a reducer must add no value the page lacked).
    # Excludes the table/dl structural tags, whose spacing is handled explicitly.
    _BLOCK = {
        "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "aside", "nav", "blockquote",
        "pre", "hr", "figure", "figcaption", "address", "main", "caption",
        "thead", "tbody", "tfoot", "dl",
        # display:block elements that can nest INSIDE a <td>/<th> cell (where <dl>
        # structure is suppressed) — must space-break too, or their adjacent block
        # texts GLUE into a fabricated token (``<dt>2</dt><dd>50</dd>`` -> ``250``,
        # ``<form>12</form><form>8k</form>`` -> ``128k``). Inline tags (span/b/i/a/
        # em/strong) stay OUT so their run correctly joins (``$<b>2.50</b>`` ->
        # ``$2.50``) — matching browser rendering. (A residual exotic case — a bare
        # <dt> directly in a <table> with no <td>, gluing across a <tr> — is a low
        # non-blocking follow-up.)
        "dt", "dd", "form", "fieldset", "details", "summary", "dialog",
        "hgroup", "menu", "legend",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.title = ""
        # --- Structured-context state -------------------------------------- #
        # HTML lets <td>/<th>/<tr>/<dt>/<dd> END tags be OMITTED, and HTMLParser
        # (a raw SAX parser) does NOT insert the implied closes — so buffering
        # text until an end tag would silently DROP it on the very pages (real
        # CMS/hand-written markup) this targets. We instead key boundaries off the
        # always-present START tags and the NON-omittable ``</table>`` / ``</dl>``
        # (whose end tags are required), flushing a cell/row/pair on the NEXT
        # boundary or at EOF. ``_table_depth`` / ``_dl_depth`` are therefore
        # reliable nesting counters; only the OUTERMOST table/dl is given
        # structure (nested ones flatten into the enclosing buffer, space-joined).
        self._table_depth = 0
        self._cells: Optional[list[str]] = None  # finalized cells of current row
        self._cur: Optional[list[str]] = None     # fragments of the open cell
        self._dl_depth = 0
        self._dl_sink: Optional[str] = None        # "term" | "value" | None
        self._term: list[str] = []
        self._value: list[str] = []
        self._last_term = ""
        self._term_paired = False  # did _last_term already emit in a pair?

    # -- active text sink -------------------------------------------------- #
    def _active(self) -> Optional[list[str]]:
        """The buffer raw text is currently routed into (cell > dl term/value),
        or ``None`` when text flows straight to output as its own line."""
        if self._cur is not None:
            return self._cur
        if self._dl_sink == "term":
            return self._term
        if self._dl_sink == "value":
            return self._value
        return None

    # -- table flush helpers ----------------------------------------------- #
    def _finalize_cell(self) -> None:
        if self._cur is not None:
            if self._cells is None:
                self._cells = []
            self._cells.append(self._collapse(self._cur))  # empties kept → gap
            self._cur = None

    def _flush_row(self) -> None:
        self._finalize_cell()
        if self._cells and any(self._cells):
            self._parts.append(" | ".join(self._cells))
        self._cells = None

    # -- definition-list flush helpers ------------------------------------- #
    def _emit_pending_value(self) -> None:
        """Emit the buffered ``term: value`` pair (keeps ``_last_term`` so a
        sibling <dd> under the same <dt> reuses it). An EMPTY value does not emit
        and does not mark the term paired — the term is preserved for a later <dd>
        or the final flush, so a ``<dt>`` with an empty ``<dd>`` never loses it."""
        if self._dl_sink == "value":
            val = self._collapse(self._value)
            if val:
                self._parts.append(
                    f"{self._last_term}: {val}" if self._last_term else val
                )
                self._term_paired = True
            self._value = []
            self._dl_sink = None

    def _flush_dl(self) -> None:
        """Close out a definition list: emit a dangling value, then any term that
        never paired (a <dt> with no/empty <dd> — keep its text so nothing is
        lost), then reset."""
        self._emit_pending_value()
        if self._dl_sink == "term":  # a term still open (no <dd> seen at all)
            self._last_term = self._collapse(self._term)
            self._dl_sink = None
        if self._last_term and not self._term_paired:
            self._parts.append(self._last_term)
        self._last_term = ""
        self._term_paired = False

    def _flush_all(self) -> None:
        """Flush every open buffer — called at EOF so text is NEVER dropped even
        when a page ends mid-table / mid-list (truncated fetch, omitted closes)."""
        self._flush_row()
        self._flush_dl()

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
            return
        active = self._active()
        # A block-level boundary inside a buffer becomes a space (anti-glue).
        if active is not None and tag in self._BLOCK:
            active.append(" ")
        if tag == "table":
            if active is not None:
                active.append(" ")  # nested table inside a cell/value: break
            self._table_depth += 1
        elif tag == "tr":
            if self._table_depth == 1:
                self._flush_row()      # implied-close the previous row
                self._cells = []
            elif active is not None:
                active.append(" ")     # nested-table row: break inside the buffer
        elif tag in ("td", "th"):
            if self._table_depth == 1:
                self._finalize_cell()  # implied-close the previous cell
                if self._cells is None:
                    self._cells = []
                self._cur = []
            elif active is not None:
                active.append(" ")     # nested-table cell: break inside the buffer
        elif tag == "dl":
            self._dl_depth += 1
        elif tag == "dt":
            # Suppress <dl> structure while inside a table cell (text stays cell
            # content); its block space was already inserted above.
            if self._dl_depth >= 1 and self._cur is None:
                self._flush_dl()       # implied-close the previous pair
                self._dl_sink = "term"
                self._term = []
        elif tag == "dd":
            if self._dl_depth >= 1 and self._cur is None:
                if self._dl_sink == "value":
                    self._emit_pending_value()          # sibling <dd>: flush, keep term
                elif self._dl_sink == "term":
                    self._last_term = self._collapse(self._term)
                self._dl_sink = "value"
                self._value = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
            return
        active = self._active()
        if active is not None and tag in self._BLOCK:
            active.append(" ")
        if tag == "table":
            if self._table_depth == 1:
                self._flush_row()
            if self._table_depth > 0:
                self._table_depth -= 1
        elif tag == "tr":
            if self._table_depth == 1:
                self._flush_row()
        elif tag in ("td", "th"):
            if self._table_depth == 1:
                self._finalize_cell()
        elif tag == "dl":
            if self._dl_depth == 1:
                self._flush_dl()
            if self._dl_depth > 0:
                self._dl_depth -= 1
        elif tag == "dt":
            if self._dl_sink == "term" and self._cur is None:
                self._last_term = self._collapse(self._term)
                self._dl_sink = None   # term captured; await its <dd>
        elif tag == "dd":
            if self._cur is None:
                self._emit_pending_value()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        # Structured contexts buffer RAW data (whitespace normalized on flush) so
        # a cell split across inline nodes — ``$<b>2.50</b>`` — rejoins to
        # ``$2.50``; block boundaries above inject the separating space.
        active = self._active()
        if active is not None:
            active.append(data)
            return
        text = data.strip()
        if text:
            self._parts.append(text)

    @staticmethod
    def _collapse(parts: list[str]) -> str:
        """Join a buffered cell/term/value and collapse internal whitespace."""
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    def text(self) -> str:
        self._flush_all()  # never leave a buffer un-emitted at EOF
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
