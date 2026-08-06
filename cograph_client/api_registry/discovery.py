"""Project the registry executor onto the discovery rail (ONTA-194, phase 2).

``RegistryDiscoverySource`` adapts one chosen catalog entry to the OSS
``WebSourceProvider`` protocol so the existing web-discovery capability can
consult it exactly like any other source: same ``discover(...)`` contract, same
``DiscoverResult`` out, same per-row provenance keying, same cost seam. This is
the ONTA-193 "one shared core behind the existing seam" pattern — the registry
is one more source, not a fourth rail. When the unified ``RetrievalSource``
protocol lands, this shim retargets to it without touching the capability.

``build_registry_sources`` turns a :class:`RoutingDecision` into the list of
providers to splice (ahead of web) into the discovery ensemble.

Capability scope (ONTA-461 follow-on)
-------------------------------------
Each registry provider self-declares ``served_hosts`` (from the catalog
``base_url`` host) and ``registry_slug`` (the catalog slug). Its
:meth:`accepts` returns ``False`` only when **structured** plan/context fields
bind the sub-query to a *different* catalog or host — never via orchestrator
brand if-strings. Recognized keys:

* ``required_hosts`` — non-empty iterable of hosts the sub-query targets
* ``target_registry_ids`` / ``target_registry_slugs`` — non-empty iterable of
  registry slugs the sub-query targets
* ``source_constraint`` — preferred nested shape
  ``{hosts: [...], registry_ids: [...]}`` (same semantics as the flat keys)
* ``registry_ids`` containing ``__none__`` (from
  :data:`cograph_client.pipeline.source_scope.REGISTRY_NONE`) — exclusive
  "no catalog API": every registry provider returns ``False`` (sub-query named
  a source that matched no live catalog entry).

When those keys are absent/empty the provider accepts (don't over-skip).
Plan/execute populate constraints via
:func:`cograph_client.pipeline.source_scope.merge_provider_context` from each
provider's own metadata tokens — never orchestrator brand if-strings.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional
from urllib.parse import urlparse

from ..web_sources.base import DiscoverResult
from .catalog import ApiSourceCatalog
from .executor import RegistryApiSource
from .router import RoutingDecision
from .spec import ApiSourceSpec, AuthorityLevel

logger = logging.getLogger(__name__)


def _normalize_host(value: str) -> str:
    """Lowercase host, strip a leading ``www.``, drop a trailing dot."""
    h = (value or "").strip().lower().rstrip(".")
    if h.startswith("www."):
        h = h[4:]
    return h


def _host_from_base_url(base_url: str) -> str:
    try:
        return _normalize_host(urlparse(base_url or "").hostname or "")
    except Exception:  # noqa: BLE001 — defensive; never break provider construction
        return ""


def _as_token_set(raw: Any) -> frozenset[str]:
    """Coerce a context value into a frozenset of non-empty lowercased tokens.

    Accepts a str, an iterable of str, or anything truthy that stringifies.
    Empty / None → empty set (treated as "no constraint").
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        t = raw.strip().lower()
        return frozenset({t}) if t else frozenset()
    if isinstance(raw, (set, frozenset, list, tuple)):
        out: set[str] = set()
        for item in raw:
            if item is None:
                continue
            t = str(item).strip().lower()
            if t:
                out.add(t)
        return frozenset(out)
    t = str(raw).strip().lower()
    return frozenset({t}) if t else frozenset()


def _constraint_hosts(context: dict) -> frozenset[str]:
    """Hosts the sub-query is bound to, if any (normalized)."""
    hosts = _as_token_set(context.get("required_hosts"))
    sc = context.get("source_constraint")
    if isinstance(sc, dict):
        hosts = hosts | _as_token_set(sc.get("hosts"))
    # Normalize each token as a host (www strip) so "www.openrouter.ai" matches.
    return frozenset(_normalize_host(h) for h in hosts if _normalize_host(h))


def _constraint_registry_ids(context: dict) -> frozenset[str]:
    """Registry slugs the sub-query is bound to, if any."""
    ids = (
        _as_token_set(context.get("target_registry_ids"))
        | _as_token_set(context.get("target_registry_slugs"))
    )
    sc = context.get("source_constraint")
    if isinstance(sc, dict):
        ids = ids | _as_token_set(sc.get("registry_ids")) | _as_token_set(
            sc.get("registry_slugs")
        )
    return ids


class RegistryDiscoverySource:
    """A ``WebSourceProvider`` backed by one declarative catalog entry.

    Self-declares capability scope for the ONTA-461 ensemble skip:

    * ``registry_slug`` — catalog slug (e.g. ``openrouter_models``)
    * ``served_hosts`` — hosts this catalog entry serves (from ``base_url``)
    * :meth:`accepts` — returns ``False`` only under structured context
      constraints that exclude this entry (see module docstring)
    """

    def __init__(
        self,
        spec: ApiSourceSpec,
        *,
        endpoint: Optional[str] = None,
        bindings: Optional[dict[str, str]] = None,
        executor: Optional[RegistryApiSource] = None,
        tenant_id: str = "",
    ) -> None:
        self._spec = spec
        self._endpoint = endpoint
        self._bindings = dict(bindings or {})
        self._executor = executor or RegistryApiSource()
        # For a tenant_custom source whose auth uses a secret_ref, the executor
        # needs a per-tenant secret resolver (decrypts at call time). Built lazily
        # in discover() so a source that never runs never touches the store.
        self._tenant_id = tenant_id
        # WebSourceProvider surface. name carries the api:{slug} marker so the
        # run-level ingest source ("web:{name}:{query}") records the API used.
        self.name = f"api:{spec.slug}"
        self.title = spec.title or spec.slug
        self.is_paid = spec.is_paid
        self.cost_per_call = spec.cost_per_call
        # Declare records-per-paid-request so the cost estimator prices a
        # PAGINATING paid source across its pages (cost_per_call × ceil(rows /
        # page_size)) instead of billing one call for the whole run — otherwise a
        # paid registry source could slip under the auto-confirm gate.
        _ep = spec.endpoint(endpoint)
        _pg = _ep.pagination if _ep else None
        self.rows_per_call = _pg.page_size if (_pg and _pg.page_size > 0) else 0
        self.supports_urls = False
        self.url_only = False
        self.query_kinds = frozenset()
        # Pre-structured rows from a declarative field map — commit via
        # ingest_structured_rows (no soft multi-type re-extract). Same contract
        # as locate_scrape A1 rows after ONTA-272 / discovery quality.
        self.structured = True
        # ONTA-461 follow-on: provider self-knowledge for ensemble scope.
        # Derived from the catalog entry only — never a hardcoded brand list in
        # the orchestrator. openrouter_models → served_hosts={"openrouter.ai"}.
        self.registry_slug = (spec.slug or "").strip().lower()
        host = _host_from_base_url(spec.base_url)
        self.served_hosts: frozenset[str] = frozenset({host}) if host else frozenset()

    @property
    def is_source_of_truth(self) -> bool:
        return self._spec.authority_level is AuthorityLevel.source_of_truth

    def accepts(self, query: str, context: dict) -> bool:
        """Whether this registry catalog can answer *query* under *context*.

        Policy (ONTA-461 follow-on — provider self-knowledge is OK; orchestrator
        brand ifs are not):

        * No structured host/registry constraint in ``context`` → ``True``
          (ambiguous; don't over-skip). ``query`` alone never rejects.
        * ``required_hosts`` / ``source_constraint.hosts`` non-empty and
          disjoint from :attr:`served_hosts` → ``False``.
        * ``target_registry_ids`` / ``target_registry_slugs`` /
          ``source_constraint.registry_ids`` non-empty and this
          :attr:`registry_slug` not among them → ``False``.
        * Otherwise ``True``.

        Plan-time must populate those context keys for production ensemble
        skips to fire; empty context keeps every registry source in the
        ensemble (backward-compatible with WS3 FakeProvider tests).
        """
        ctx = context if isinstance(context, dict) else {}

        required_hosts = _constraint_hosts(ctx)
        if required_hosts and not (required_hosts & self.served_hosts):
            return False

        target_ids = _constraint_registry_ids(ctx)
        if target_ids:
            # Exclusive none: named source matched no registry catalog → all
            # catalog APIs skip (web/locate still run; they lack accepts).
            # Keep string literal in sync with source_scope.REGISTRY_NONE.
            if "__none__" in target_ids:
                return False
            if self.registry_slug not in target_ids:
                # Also accept the api:{slug} form callers may stamp from provider.name.
                if f"api:{self.registry_slug}" not in target_ids:
                    return False

        return True

    def _secret_resolver(self):
        """A per-tenant secret resolver iff this source's auth uses a secret_ref;
        else ``None`` (env-var auth needs no resolver). Built here so the store /
        cipher are only touched when a tenant_custom secret is actually needed."""
        if not self._spec.auth.secret_ref or not self._tenant_id:
            return None
        from .secret_store import make_secret_resolver

        return make_secret_resolver(self._tenant_id, self._spec.slug)

    async def discover(
        self,
        query: str,
        *,
        sample: bool,
        max_rows: int,
        hint_columns: Optional[list[str]],
        context: dict,
        urls: Optional[list[str]] = None,
    ) -> DiscoverResult:
        # The registry is a structured-query source; it does not do URL extraction.
        if urls:
            return DiscoverResult(rows=[], provenance={}, sources=[])
        res = await self._executor.execute(
            self._spec, self._bindings, endpoint_name=self._endpoint,
            max_rows=max_rows, sample=sample,
            secret_resolver=self._secret_resolver(),
        )
        if res.dormant:
            # No key -> behave as "nothing found here" so the ensemble falls back
            # to web, exactly like every dormant premium adapter.
            logger.info("api_registry source %s dormant: %s", self.name, res.error)
            return DiscoverResult(rows=[], provenance={}, sources=[])
        rows = [_enrich_provider_from_id(r) for r in (res.rows or [])]
        return DiscoverResult(
            rows=rows,
            provenance=res.provenance,
            sources=res.sources,
            is_partial=res.is_partial,
            estimated_total=res.estimated_total,
            error=res.error,
            calls=res.calls,
        )


def _enrich_provider_from_id(row: dict) -> dict:
    """If ``name`` looks like ``provider/slug`` and ``provider`` is empty, fill it.

    OpenRouter (and similar) catalogs use org/model ids; plans often request a
    ``provider`` attribute the JSON field map cannot split. Deterministic and
    lossless — never overwrites a non-empty provider cell.
    """
    if not isinstance(row, dict):
        return row
    name = str(row.get("name") or "").strip()
    if not name or "/" not in name:
        return row
    if str(row.get("provider") or "").strip():
        return row
    left = name.split("/", 1)[0].strip()
    if not left:
        return row
    out = dict(row)
    out["provider"] = left
    return out


def build_registry_sources(
    catalog: ApiSourceCatalog,
    decision: RoutingDecision,
    *,
    executor: Optional[RegistryApiSource] = None,
    tenant_id: str = "",
) -> list[RegistryDiscoverySource]:
    """Materialize the routing decision's picks into discovery providers.

    Skips picks whose slug is missing/disabled. Returns an empty list when the
    decision does not use an API (so the caller simply keeps today's web path).

    ``tenant_id`` is threaded to each source so a tenant_custom entry whose auth
    uses a ``secret_ref`` can build its per-tenant secret resolver (decrypt at call
    time). Env-var-keyed entries ignore it.
    """
    if not decision.uses_api:
        return []
    out: list[RegistryDiscoverySource] = []
    shared = executor or RegistryApiSource()
    for pick in decision.picks:
        spec = catalog.get(pick.slug)
        if spec is None or not spec.enabled:
            continue
        # Skip a dormant entry: splicing it in (and, in api_only mode, dropping
        # web) would yield an empty run instead of falling back to web. Same
        # dormancy contract as every premium adapter. An env-keyed entry is
        # dormant when its env var is unset; a secret_ref entry's presence is only
        # known after a store hit, so it is NOT pre-skipped here — the executor
        # surfaces it as dormant and discover() returns "nothing found", which the
        # ensemble already handles by falling back to web.
        auth = spec.auth
        if auth.requires_key and not auth.secret_ref and not os.environ.get(auth.key_env, "").strip():
            logger.info("api_registry: skipping dormant entry %s (env %s unset)", spec.slug, auth.key_env)
            continue
        out.append(
            RegistryDiscoverySource(
                spec, endpoint=pick.endpoint, bindings=pick.bindings,
                executor=shared, tenant_id=tenant_id,
            )
        )
    return out


__all__ = ["RegistryDiscoverySource", "build_registry_sources"]
