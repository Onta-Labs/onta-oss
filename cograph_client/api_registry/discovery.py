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
    ) -> None:
        self._spec = spec
        self._endpoint = endpoint
        self._bindings = dict(bindings or {})
        self._executor = executor or RegistryApiSource()
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
) -> list[RegistryDiscoverySource]:
    """Materialize the routing decision's picks into discovery providers.

    Skips picks whose slug is missing/disabled. Returns an empty list when the
    decision does not use an API (so the caller simply keeps today's web path).
    """
    if not decision.uses_api:
        return []
    out: list[RegistryDiscoverySource] = []
    shared = executor or RegistryApiSource()
    for pick in decision.picks:
        spec = catalog.get(pick.slug)
        if spec is None or not spec.enabled:
            continue
        # Skip a key-gated entry whose key is absent: it's dormant, so splicing it
        # in (and, in api_only mode, dropping web) would yield an empty run instead
        # of falling back to web. Same dormancy contract as every premium adapter.
        auth = spec.auth
        if auth.requires_key and not os.environ.get(auth.key_env, "").strip():
            logger.info("api_registry: skipping dormant entry %s (env %s unset)", spec.slug, auth.key_env)
            continue
        out.append(
            RegistryDiscoverySource(
                spec, endpoint=pick.endpoint, bindings=pick.bindings, executor=shared,
            )
        )
    return out


__all__ = ["RegistryDiscoverySource", "build_registry_sources"]
