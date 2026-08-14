"""Adapter lookup chain + binding-attribute fetch for EnrichmentExecutor."""

from __future__ import annotations

import asyncio
from typing import Optional

from infona_client.enrichment.executor_helpers import _host, _validate_entity_uris
from infona_client.enrichment.executor_select import _extract_bind_attrs
from infona_client.enrichment.executor_tally import _ProviderTally
from infona_client.enrichment.extraction import coerce_url_attribute_value
from infona_client.enrichment.models import EnrichJob, Verdict
from infona_client.pipeline.manifest import RunManifest
from infona_client.retrieval.cost import source_cost


class EnrichmentLookupMixin:
    """Wikidata / chain lookup, cache, and binding-source attribute fetch."""

    async def _lookup(
        self,
        entity_label: str,
        attribute: str,
        job: EnrichJob,
        cache_hit_inc: bool,
        strategy_version: str = "v1",
    ) -> list[Verdict]:
        source = self._wikidata.name
        cached = await self._cache.get(
            entity_label, attribute, source, job.type_name, strategy_version
        )
        if cached is not None:
            if cache_hit_inc:
                job.progress.cache_hits += 1
            return cached
        # Thread optional custom instructions into the lookup context (empty
        # when none), mirroring _lookup_chain. Wikidata ignores it harmlessly.
        ctx = {"instructions": job.instructions} if job.instructions else {}
        # URL-targeted enrichment: hand any user-supplied pages to the adapter so
        # a URL-aware premium adapter (e.g. Firecrawl) reads values FROM them.
        # Wikidata ignores it harmlessly. Empty by default → unchanged call shape.
        if job.source_urls:
            ctx["target_urls"] = list(job.source_urls)
        # Entity TYPE gating: hand the job's (canonical) type label to the adapter
        # so a type-aware adapter can self-exclude on entities it can't serve
        # (e.g. Google Places skipping a Person/Book). Free adapters ignore it
        # harmlessly. Only set when present so the call shape is unchanged when
        # absent (mirrors _lookup_chain).
        if job.type_name:
            ctx["entity_type"] = job.type_name
        # Tenant scope: a tenant_custom registry adapter needs the tenant to build
        # its per-tenant secret resolver (decrypt a secret_ref at call time). Free
        # adapters ignore it harmlessly.
        if job.tenant_id:
            ctx["tenant_id"] = job.tenant_id
        verdicts = await self._wikidata.lookup(entity_label, attribute, ctx)
        await self._cache.put(
            entity_label, attribute, source, verdicts, job.type_name, strategy_version
        )
        return verdicts

    async def _load_binding_attrs(
        self,
        graph_uri: str,
        entity_uris: list[str],
        type_name: str,
        leaves,
        *,
        tenant_id: str = "",
        kg_name: str = "",
    ) -> dict[str, dict[str, str]]:
        """Fetch specific attribute LEAVES for the given entity URIs so an
        ``attribute:<attr>`` enrich_from recipe can bind a request param FROM
        another of the entity's own attributes (e.g. a resolved ``bls_series_id``
        feeding a FRED price lookup — ONTA-194 phase 3).

        Returns ``{entity_uri: {leaf: value}}`` for exactly the passed URIs and
        leaves. GraphStore only (ONTA-527). A residual SPARQL hop used to raise
        and fail-open to ``{}``, so ClinicalTrials.gov (and every other
        ``attribute:<id>`` adapter) saw empty bindings and returned no_match
        without ever calling the API. An empty map is a real miss (no such
        props), not a query-language error. A store outage logs at error and
        returns ``{}``.
        """
        del graph_uri, type_name  # store-keyed by tenant/kg + entity id
        leaf_list = [str(x) for x in (leaves or []) if x]
        uris = _validate_entity_uris([u for u in (entity_uris or []) if u])
        if not leaf_list or not uris:
            return {}
        if not tenant_id or not kg_name:
            _host().logger.error(
                "enrich_bind_attrs_missing_scope",
                tenant_id=tenant_id,
                kg_name=kg_name,
            )
            return {}
        return await self._load_binding_attrs_via_store(
            uris, leaf_list, tenant_id=tenant_id, kg_name=kg_name
        )

    async def _load_binding_attrs_via_store(
        self,
        entity_uris: list[str],
        leaf_list: list[str],
        *,
        tenant_id: str,
        kg_name: str,
    ) -> dict[str, dict[str, str]]:
        """Read binding leaves from GraphStore entity_detail props. ``{}`` if
        the store is unavailable or none of the URIs resolve."""
        if not tenant_id or not kg_name:
            _host().logger.error(
                "enrich_bind_attrs_missing_scope",
                tenant_id=tenant_id,
                kg_name=kg_name,
            )
            return {}
        from infona_client.graph.scope import GraphScope
        from infona_client.graph.store import GraphConfigError, get_optional_graph_store

        try:
            store = get_optional_graph_store()
            session = store.session(GraphScope.for_instance(tenant_id, kg_name))
        except GraphConfigError:
            _host().logger.error(
                "enrich_bind_attrs_no_store",
                tenant_id=tenant_id,
                kg_name=kg_name,
            )
            return {}
        except Exception:  # noqa: BLE001
            _host().logger.exception(
                "enrich_bind_attrs_store_session_failed",
                tenant_id=tenant_id,
                kg_name=kg_name,
            )
            return {}

        out: dict[str, dict[str, str]] = {}
        for eid in entity_uris:
            try:
                detail_rows = await session.execute_template(
                    "entity_detail", {"id": eid}
                )
            except Exception:  # noqa: BLE001
                continue
            if not detail_rows:
                continue
            detail = (
                detail_rows[0].to_dict()
                if hasattr(detail_rows[0], "to_dict")
                else dict(detail_rows[0])
            )
            raw_props = detail.get("props") or {}
            if not isinstance(raw_props, dict):
                continue
            bound = _extract_bind_attrs(raw_props, leaf_list, uri=eid)
            if bound:
                out[eid] = bound
        return out

    async def _lookup_chain(
        self,
        entity_label: str,
        attribute: str,
        chain: list[str],
        job: EnrichJob,
        missing: set[str],
        confidence_min: float,
        strategy_version: str = "v1",
        tally: Optional["_ProviderTally"] = None,
        manifest: Optional[RunManifest] = None,
        entity_attrs: Optional[dict] = None,
    ) -> list[Verdict]:
        """Walk an adapter chain, returning verdicts from the first adapter
        that yields one with confidence >= confidence_min.

        - "cache" entries in the chain are skipped (cache is a layer wrapped
          around each adapter call, not an adapter itself).
        - Unregistered adapter names are skipped with a one-shot warning per
          job, never fail the job.
        - ``manifest`` (A9 cost envelope, ONTA-282): when supplied, the cost of
          every PAID adapter call actually ISSUED here (the non-cache branch) is
          fed into ``manifest.add_spend`` so the per-run ceiling check in
          ``process_entity`` can halt the run before it overspends. Cache hits are
          free and add nothing; a free adapter adds $0.
        """
        cache_hit_counted = False
        for name in chain:
            if name == "cache":
                # Cache is a layer, not an adapter.
                continue
            adapter = _host().get_adapter(name)
            if adapter is None:
                if name not in missing:
                    missing.add(name)
                    _host().logger.warning(
                        "enrichment_adapter_missing",
                        adapter=name,
                        job_id=job.id,
                        tier=job.tier.value if hasattr(job.tier, "value") else str(job.tier),
                    )
                    if tally is not None:
                        tally.record_missing(name)
                continue
            # Per-attempt outcome for the provider log: "match" | "no_match" |
            # "timeout" | "error", with the cache flag tracked separately.
            from_cache = False
            err_outcome: Optional[str] = None
            err_msg: Optional[str] = None
            cached = await self._cache.get(
                entity_label, attribute, adapter.name, job.type_name, strategy_version
            )
            if cached is not None:
                if not cache_hit_counted:
                    job.progress.cache_hits += 1
                    cache_hit_counted = True
                verdicts = cached
                from_cache = True
            else:
                # Optional custom instructions ride in the adapter lookup
                # context dict. Adapters that don't use it (wikidata) ignore it
                # harmlessly; agentic/premium adapters can read it. Empty when no
                # instructions so the call shape is unchanged in the common case.
                ctx = {"instructions": job.instructions} if job.instructions else {}
                # URL-targeted enrichment: hand any user-supplied pages to the
                # adapter via ``target_urls`` so a URL-aware premium adapter
                # (e.g. Firecrawl) reads values FROM them. Free adapters ignore
                # it harmlessly; empty by default → unchanged call shape.
                if job.source_urls:
                    ctx["target_urls"] = list(job.source_urls)
                # Entity TYPE gating: hand the job's (canonical) type label to the
                # adapter via ``entity_type`` so a type-aware adapter can
                # self-exclude on entities it can't serve (e.g. Google Places
                # skipping a Person/Book). Free adapters ignore it harmlessly;
                # only set when present → unchanged call shape when absent.
                if job.type_name:
                    ctx["entity_type"] = job.type_name
                # Tenant scope for a tenant_custom registry adapter's per-tenant
                # secret resolver (decrypt a secret_ref at call time). Free
                # adapters ignore it harmlessly.
                if job.tenant_id:
                    ctx["tenant_id"] = job.tenant_id
                # Binding-source attributes (attribute:<attr> enrich_from): the
                # entity's own attribute values a registry adapter binds a request
                # param FROM (e.g. a resolved bls_series_id feeding a price
                # lookup). Pre-loaded per entity above; only set when non-empty so
                # the call shape is unchanged for every other adapter.
                if entity_attrs:
                    ctx["entity_attributes"] = entity_attrs
                # Bound every adapter call so one stalled lookup (e.g. a
                # hung network call whose own client lacks a total-operation
                # timeout) can never strand the whole job (COG-112).
                # Per-adapter override: slow agentic providers (Parallel Task
                # API) declare ``lookup_timeout_s`` so the global 30s default
                # does not kill a still-running research task and silently
                # fall through to the next chain source.
                timeout_s = _host().ADAPTER_LOOKUP_TIMEOUT_S
                adapter_timeout = getattr(adapter, "lookup_timeout_s", None)
                if adapter_timeout is not None:
                    try:
                        candidate = float(adapter_timeout)
                        if candidate > 0:
                            timeout_s = candidate
                    except (TypeError, ValueError):
                        pass
                try:
                    verdicts = await asyncio.wait_for(
                        adapter.lookup(entity_label, attribute, ctx),
                        timeout=timeout_s,
                    )
                except asyncio.TimeoutError:
                    _host().logger.warning(
                        "enrichment_adapter_timeout",
                        adapter=name,
                        job_id=job.id,
                        timeout_s=timeout_s,
                        entity=entity_label,
                        attribute=attribute,
                    )
                    verdicts = []
                    err_outcome = "timeout"
                    err_msg = f"timed out after {timeout_s:.0f}s"
                except Exception as exc:  # noqa: BLE001
                    _host().logger.warning(
                        "enrichment_adapter_error",
                        adapter=name,
                        job_id=job.id,
                        error=str(exc),
                    )
                    verdicts = []
                    err_outcome = "error"
                    err_msg = str(exc)
                await self._cache.put(
                    entity_label,
                    attribute,
                    adapter.name,
                    verdicts,
                    job.type_name,
                    strategy_version,
                )
                # A9 cost envelope (ONTA-282): a PAID adapter call was actually
                # issued (this is the non-cache branch) — feed its cost into the
                # run manifest's spend-to-date so the per-run ceiling check can
                # halt the run before it overspends. Cost is incurred whether the
                # call matched, no-matched, timed out, or errored (the paid request
                # went out either way). source_cost reads the adapter's cost
                # defensively (free default), so a free adapter adds $0.
                if manifest is not None:
                    _is_paid, _cost_per_call = source_cost(adapter)
                    if _cost_per_call > 0.0:
                        manifest.add_spend(_cost_per_call)
            # URL-valued attributes (website, *_url, datatype uri): the answer is
            # a URL, and a single-pass extractor run over page text otherwise
            # lifts page chrome ("Skip to content", "Platform") or the entity
            # name as the value. Coerce to a URL — keeping an already-URL value
            # (e.g. Wikidata's official site) and only falling back to the
            # resolved source_url citation when the value isn't a URL (ONTA-157).
            # Applied here, the one shared post-adapter seam, so it covers every
            # provider (and re-coerces stale cached verdicts on read).
            verdicts = [coerce_url_attribute_value(attribute, v) for v in verdicts]
            sufficient = any(v.confidence >= confidence_min for v in verdicts)
            if tally is not None:
                outcome = (
                    err_outcome
                    if err_outcome is not None
                    else ("match" if sufficient else "no_match")
                )
                tally.record_attempt(
                    adapter.name,
                    cache_hit=from_cache,
                    outcome=outcome,
                    error_msg=err_msg,
                )
            # Stop at first sufficient-confidence verdict.
            if sufficient:
                return verdicts
        # No adapter yielded a sufficiently-confident verdict; return last (may
        # be empty). For simplicity return [] so caller treats as no_match.
        return []

    def _pick_best(
        self, verdicts: list[Verdict], confidence_min: float
    ) -> Optional[Verdict]:
        eligible = [v for v in verdicts if v.confidence >= confidence_min]
        if not eligible:
            return None
        return max(eligible, key=lambda v: v.confidence)
