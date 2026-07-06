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
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from ..web_sources.base import DiscoverResult
from .catalog import ApiSourceCatalog
from .executor import RegistryApiSource
from .router import RoutingDecision
from .spec import ApiSourceSpec, AuthorityLevel

logger = logging.getLogger(__name__)


class RegistryDiscoverySource:
    """A ``WebSourceProvider`` backed by one declarative catalog entry."""

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

    @property
    def is_source_of_truth(self) -> bool:
        return self._spec.authority_level is AuthorityLevel.source_of_truth

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
        return DiscoverResult(
            rows=res.rows,
            provenance=res.provenance,
            sources=res.sources,
            is_partial=res.is_partial,
            estimated_total=res.estimated_total,
            error=res.error,
        )


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
