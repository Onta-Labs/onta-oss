"""The one generic executor that runs any declarative ``ApiSourceSpec``.

``RegistryApiSource`` interprets a catalog entry with **zero per-API Python**:

    build request  →  inject auth from the named env var  →  SSRF-guard the fetch
    →  paginate via the declarative engine  →  extract ``result_path``  →  map
    fields  →  coerce rows  →  stamp ``api:{slug}`` + request-URL provenance
    →  respect the ``Budget`` / max_rows / max_pages.

It is **seam-agnostic**: it returns a plain :class:`ApiCallResult` (rows +
row→URL provenance + cost + partiality), so phases 2–3 can wrap it behind the
``WebSourceProvider`` / ``SourceAdapter`` shims (or ONTA-193's future
``RetrievalSource``) over this one shared core — never a fourth rail.

Safety: every fetched URL — the first page, every synthesized pagination URL,
and every redirect hop — goes through ``is_fetchable_url`` + ``host_dns_blocked``
(reused verbatim from the research fetch ladder), because the web-source probe
path does *not* apply those guards today. The executor never raises; failures
surface as ``ApiCallResult.error``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx

from ..research.fetch import host_dns_blocked, is_fetchable_url
from ..research.types import Budget, redact_url
from .jsonpath import extract_records, map_record
from .paginate import PageState, declared_total, first_page, next_page
from .spec import (
    ApiSourceSpec,
    AuthMode,
    EndpointSpec,
    ParamLocation,
    ParamSpec,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 20.0
_MAX_BYTES = 2_000_000
_MAX_REDIRECTS = 5
_UA = "Mozilla/5.0 (compatible; OntaApiRegistry/1.0; +https://onta.sh/bot)"


# --------------------------------------------------------------------------- #
# Result type (the rail boundary projects this into DiscoverResult / Verdict)
# --------------------------------------------------------------------------- #
@dataclass
class ApiCallResult:
    slug: str
    rows: list[dict[str, str]] = field(default_factory=list)
    provenance: dict[str, str] = field(default_factory=dict)   # row-key -> source URL
    sources: list[str] = field(default_factory=list)           # distinct page URLs consulted
    source: str = ""                                           # "api:{slug}"
    cost: float = 0.0
    pages_fetched: int = 0
    is_partial: bool = False
    estimated_total: Optional[int] = None
    dormant: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "rows": [dict(r) for r in self.rows],
            "provenance": dict(self.provenance),
            "sources": list(self.sources),
            "source": self.source,
            "cost": self.cost,
            "pages_fetched": self.pages_fetched,
            "is_partial": self.is_partial,
            "estimated_total": self.estimated_total,
            "dormant": self.dormant,
            "error": self.error,
        }


@dataclass
class _FetchOutcome:
    url: str
    payload: Any = None
    ok: bool = False
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #
class RegistryApiSource:
    """Runs declarative specs. Stateless across calls; safe to reuse."""

    def __init__(
        self,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        transport: Optional[httpx.BaseTransport] = None,
        max_bytes: int = _MAX_BYTES,
    ) -> None:
        self._timeout = timeout
        # A test injects httpx.MockTransport(handler); prod leaves it None.
        self._transport = transport
        self._max_bytes = max_bytes

    async def execute(
        self,
        spec: ApiSourceSpec,
        bindings: Optional[dict[str, str]] = None,
        *,
        endpoint_name: Optional[str] = None,
        max_rows: int = 50,
        sample: bool = False,
        budget: Optional[Budget] = None,
    ) -> ApiCallResult:
        bindings = {k: str(v) for k, v in (bindings or {}).items() if v is not None}
        result = ApiCallResult(slug=spec.slug, source=f"api:{spec.slug}")

        if not spec.enabled:
            result.error = "disabled"
            return result

        ep = spec.endpoint(endpoint_name)
        if ep is None:
            result.error = f"no endpoint {endpoint_name!r}" if endpoint_name else "no endpoints"
            return result

        # Auth: resolve the secret by env-var name. Missing key => dormant.
        headers, auth_query, auth_err = self._resolve_auth(spec)
        if auth_err is not None:
            result.dormant = True
            result.error = auth_err
            return result

        # Build the base path + static/bound query params.
        try:
            path, base_query, missing = self._build_request(ep, bindings)
        except _RequestError as exc:
            result.error = str(exc)
            return result
        if missing:
            result.error = f"missing required params: {', '.join(missing)}"
            return result

        base_query.update(auth_query)

        # Budget: reuse the research Budget so fetches are counted the same way
        # everywhere. Size the default to the declared page budget.
        pg = ep.pagination
        if budget is None:
            budget = Budget(max_fetches=max(1, pg.max_pages) + 1).start()
        else:
            budget.start()

        rows: list[dict[str, str]] = []
        seen: set[tuple[tuple[str, str], ...]] = set()
        sources: list[str] = []
        estimated_total: Optional[int] = None
        page_max = 1 if sample else max(1, pg.max_pages)

        state: Optional[PageState] = first_page(pg)
        while state is not None:
            if not budget.can_fetch():
                result.is_partial = True
                break

            url = self._page_url(spec.base_url, path, base_query, state)
            outcome = await self._fetch_json(url, headers)
            budget.note_fetch(1)
            result.pages_fetched += 1
            if url not in sources:
                sources.append(url)

            if not outcome.ok:
                # First page failing is a hard error; a later page failing just
                # truncates what we already have.
                if result.pages_fetched == 1:
                    result.error = outcome.error
                    return _finalize(result, rows, sources, estimated_total, spec)
                result.is_partial = True
                break

            if estimated_total is None:
                estimated_total = declared_total(pg, outcome.payload)

            records = extract_records(outcome.payload, ep.result_path)
            page_rows = 0
            for rec in records:
                mapped = map_record(rec, ep.field_mappings)
                if not mapped:
                    continue
                key = tuple(sorted(mapped.items()))
                if key in seen:
                    continue
                seen.add(key)
                prov_key = str(rec.get("name", len(rows)))
                result.provenance.setdefault(prov_key, url)
                rows.append(mapped)
                page_rows += 1
                if max_rows > 0 and len(rows) >= max_rows:
                    break

            if max_rows > 0 and len(rows) >= max_rows:
                # There may be more upstream than we pulled.
                if estimated_total is None or estimated_total > len(rows):
                    result.is_partial = True
                break
            if sample or result.pages_fetched >= page_max:
                if len(records) > 0 and (estimated_total is None or estimated_total > len(rows)):
                    result.is_partial = True
                break

            state = next_page(
                pg,
                state,
                outcome.payload,
                rows_on_page=page_rows,
                rows_so_far=len(rows),
                max_rows=max_rows,
            )

        result.cost = round(spec.cost_per_call * result.pages_fetched, 6)
        return _finalize(result, rows, sources, estimated_total, spec)

    # -- request building --------------------------------------------------- #
    def _resolve_auth(
        self, spec: ApiSourceSpec
    ) -> tuple[dict[str, str], dict[str, str], Optional[str]]:
        """Return (headers, query-additions, dormancy-error-or-None)."""
        headers: dict[str, str] = {"User-Agent": _UA, "Accept": "application/json, */*"}
        auth = spec.auth
        if auth.mode is AuthMode.none:
            return headers, {}, None
        secret = os.environ.get(auth.key_env, "").strip()
        if not secret:
            return headers, {}, f"dormant: env {auth.key_env} not set"
        if auth.mode is AuthMode.api_key_header:
            headers[auth.header_name] = secret
            return headers, {}, None
        if auth.mode is AuthMode.bearer:
            headers["Authorization"] = f"Bearer {secret}"
            return headers, {}, None
        if auth.mode is AuthMode.api_key_query:
            return headers, {auth.query_key: secret}, None
        return headers, {}, f"unsupported auth mode {auth.mode.value}"

    def _build_request(
        self, ep: EndpointSpec, bindings: dict[str, str]
    ) -> tuple[str, dict[str, str], list[str]]:
        """Fill path placeholders + assemble the static/bound query. Returns
        (path, query, missing_required)."""
        path = ep.path
        query: dict[str, str] = {str(k): str(v) for k, v in ep.query.items()}
        missing: list[str] = []
        for p in ep.params:
            value = self._param_value(p, bindings, missing)
            if value is None:
                continue
            if p.location is ParamLocation.path:
                placeholder = "{" + p.target + "}"
                if placeholder not in path:
                    raise _RequestError(f"path param {p.name!r} has no placeholder {placeholder}")
                path = path.replace(placeholder, _safe_path_segment(value))
            else:
                query[p.target] = value
        # Any unfilled placeholder is a hard error (should be caught in validation).
        if "{" in path and "}" in path:
            raise _RequestError(f"unfilled path placeholder in {path!r}")
        return path, query, missing

    @staticmethod
    def _param_value(
        p: ParamSpec, bindings: dict[str, str], missing: list[str]
    ) -> Optional[str]:
        if p.name in bindings and bindings[p.name] != "":
            return bindings[p.name]
        if p.default is not None:
            return p.default
        if p.required:
            missing.append(p.name)
        return None

    def _page_url(
        self, base_url: str, path: str, base_query: dict[str, str], state: PageState
    ) -> str:
        if state.url:  # next_link style: the body handed us an absolute URL
            return state.url
        merged = dict(base_query)
        merged.update(state.query)
        root = base_url.rstrip("/") + "/" + path.lstrip("/")
        query_str = _encode_query(merged)
        return f"{root}?{query_str}" if query_str else root

    # -- SSRF-guarded fetch (manual redirects, byte-capped, never raises) ---- #
    async def _fetch_json(self, url: str, headers: dict[str, str]) -> _FetchOutcome:
        if not is_fetchable_url(url):
            return _FetchOutcome(url=url, error="blocked or non-http(s) URL")
        host = urlparse(url).hostname or ""
        if await host_dns_blocked(host):
            return _FetchOutcome(url=url, error="host resolves to a blocked address")

        current = url
        try:
            async with self._new_client() as client:
                for _hop in range(_MAX_REDIRECTS + 1):
                    body, status, is_redirect, location = await self._get(client, current, headers)
                    if is_redirect:
                        nxt = urljoin(current, location or "")
                        if not is_fetchable_url(nxt):
                            return _FetchOutcome(url=current, error="redirect to blocked URL")
                        if await host_dns_blocked(urlparse(nxt).hostname or ""):
                            return _FetchOutcome(url=current, error="redirect resolves to blocked address")
                        current = nxt
                        continue
                    if status >= 400:
                        return _FetchOutcome(url=current, error=f"HTTP {status}")
                    payload = _parse_json(body)
                    if payload is None:
                        return _FetchOutcome(url=current, error="response was not valid JSON")
                    return _FetchOutcome(url=current, payload=payload, ok=True)
            return _FetchOutcome(url=current, error="too many redirects")
        except Exception as exc:  # never raise out of the executor
            logger.debug("api_registry fetch failed url=%s err=%s", redact_url(current), exc)
            return _FetchOutcome(url=current, error=str(exc)[:200])

    def _new_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "follow_redirects": False,  # we follow manually so we can re-check each hop
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def _get(
        self, client: httpx.AsyncClient, url: str, headers: dict[str, str]
    ) -> tuple[bytes, int, bool, Optional[str]]:
        async with client.stream("GET", url, headers=headers) as resp:
            if resp.is_redirect:
                return b"", resp.status_code, True, resp.headers.get("location")
            total = 0
            chunks: list[bytes] = []
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= self._max_bytes:
                    break
            return b"".join(chunks), resp.status_code, False, None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class _RequestError(Exception):
    pass


def _finalize(
    result: ApiCallResult,
    rows: list[dict[str, str]],
    sources: list[str],
    estimated_total: Optional[int],
    spec: ApiSourceSpec,
) -> ApiCallResult:
    result.rows = rows
    result.sources = sources
    if estimated_total is not None:
        result.estimated_total = estimated_total
    return result


def _parse_json(body: bytes) -> Any:
    import json

    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None


def _encode_query(params: dict[str, str]) -> str:
    from urllib.parse import urlencode

    # Preserve insertion order; skip empty values.
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    return urlencode(clean, doseq=False)


def _safe_path_segment(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")


__all__ = ["RegistryApiSource", "ApiCallResult"]
