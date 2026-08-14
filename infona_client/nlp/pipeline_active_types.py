"""Bounded active-type probe. Never mark a THIS-KG populated type empty."""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Iterable

import httpx
import structlog

from infona_client.graph.iri import ENTITY_URI_PREFIX, IRI_BASE, TYPE_URI_PREFIX
from infona_client.graph.parser import parse_sparql_results, unbound_projection_vars
from infona_client.graph.queries import parse_kg_graph_uri, skip_invalid_type_name
from infona_client.graph.sparql_scope import (
    CrossTenantQueryError,
    confine_generated_query,
    tenant_of_graph,
)
from infona_client.models.query import NLResult
from infona_client.nlp.cypher_generate import (
    neo4j_ask_enabled,
    ontology_from_graph_store,
    records_to_bindings,
)
from infona_client.nlp.cypher_scope import (
    CrossTenantCypherError,
    CypherScopeError,
    confine_generated_cypher,
    scrub_cypher_error,
)
from infona_client.nlp.pipeline_helpers import (
    ACTIVE_TYPE_PROBE_CHUNK,
    ANSWER_ROW_CAP,
    MAX_ACTIVE_TYPE_PROBE_CONCURRENCY,
    MAX_ACTIVE_TYPE_PROBE_URIS,
    MAX_ENUM_DISCOVERY_CONCURRENCY,
    ONTOLOGY_CACHE_TTL,
    ONTOLOGY_EMPTY,
    ONTOLOGY_FETCH_ERROR,
    RDF_TYPE_URI,
    _GEO_WKT_URI,
    _active_types_cache,
    _active_types_cache_key,
    _alias_cache,
    _cypher_uses_forbidden_shapes,
    _drop_internal_predicate_rows,
    _is_interpolatable_iri,
    _missing_template_params,
    _neptune_safe_duration,
    _ontology_cache,
    _parse_iso_dt,
    _parse_point_wkt,
    _prefer_attr_name_over_rdfs_label,
    _sanitize_sparql_literal,
    _store_active_types,
)
from infona_client.nlp.pipeline_llm import (
    CEREBRAS_LENGTH_RECOVERY_TOKENS,
    OPENROUTER_BASE,
    OPENROUTER_QUERY_TIMEOUT_S,
    OPENROUTER_REASONING_MAX_TOKENS,
    EmptyLLMResponse,
    _is_reasoning_query_model,
    _openrouter_base,
    _parse_sparql_gen_json,
    _require_message_content,
    get_embedding_service,
)
from infona_client.nlp.prompts import (
    CYPHER_GENERATION_SYSTEM,
    SPARQL_GENERATION_SYSTEM,
    build_cypher_generation_prompt,
    build_generation_prompt,
)
from infona_client.nlp.validator import normalize_sparql, validate_sparql
from infona_client.nlp.token_usage import (
    STAGE_REPHRASE,
    TokenUsageLedger,
    attach_usage,
    pop_attached_usage,
    stage_for_attempt,
)
from infona_client.offline import assert_online_url
from infona_client.resolver.llm_router import model_chain
from infona_client.spatiotemporal.routing import (
    SPATIAL_INTENT_SCHEMA,
    SPATIAL_INTENT_SYSTEM,
    filter_by_type,
    format_spatial_answer,
    looks_spatial,
    parse_spatial_intent,
)

logger = structlog.stdlib.get_logger("infona.nlp.pipeline")


class PipelineActiveTypesMixin:
    # ── Active-type probe (ONTA-427) ──────────────────────────────────────── #
    # `active_types` decides which DECLARED types get the "[no instances]" mark
    # (ONTA-258). Getting it wrong in the FALSE-EMPTY direction (a populated type
    # marked empty) is the regression that matters: the model would then tell the
    # user a type has no data when it does. Everything below is written to make
    # the probe cheap WITHOUT ever risking that: a probe that cannot answer
    # confidently returns None and the caller falls back to the full scan.

    @staticmethod
    def _active_type_candidate_uris(type_names: Iterable[str]) -> list[str]:
        """Every type URI an instance of each declared name could plausibly carry.

        The pre-ONTA-427 scan matched instance types to declared types by NAME
        (``type_name_from_uri``), which is namespace-agnostic: an instance typed
        ``types/public/Person`` marked a tenant-declared ``Person`` active.
        Probing only the DECLARING layer's URI would silently turn such a type
        into a false "[no instances]", so we probe every layer namespace for the
        name (three URIs, deduped, order-preserving). That keeps the bounded
        probe's answer identical to the scan's while staying O(declared types).
        """
        from infona_client.graph.layers import Layer, layer_type_uri

        uris: list[str] = []
        seen: set[str] = set()
        for name in type_names:
            for layer in Layer:
                u = layer_type_uri(layer, name)
                if u not in seen:
                    seen.add(u)
                    uris.append(u)
        return uris

    @staticmethod
    def _active_type_probe_query(instance_graph: str, uris: list[str]) -> str:
        """One LIMIT-1 existence subselect per candidate type URI, UNIONed.

        READ-ONLY (a SELECT). Each subselect is a first-match seek on the
        (predicate, object) index, so the engine can stop at the first instance
        of that type instead of scanning every rdf:type triple in the graph.
        """
        blocks = " UNION ".join(
            f"{{ SELECT (<{u}> AS ?type) WHERE {{ ?s <{RDF_TYPE_URI}> <{u}> }} LIMIT 1 }}"
            for u in uris
        )
        return f"SELECT DISTINCT ?type FROM <{instance_graph}> WHERE {{ {blocks} }}"

    async def _probe_active_types(
        self, instance_graph: str, candidate_uris: list[str]
    ) -> set[str] | None:
        """Which of ``candidate_uris`` have at least one instance? (bounded)

        Returns the set of type NAMES found, or ``None`` when the probe could not
        be completed. ANY chunk failing invalidates the WHOLE result, because a
        partial answer would mark the missing chunk's types "[no instances]"
        (the exact ONTA-258 regression). ``None`` tells the caller to fall back to
        the unbounded scan, i.e. to the pre-ONTA-427 behavior, so a Neptune that
        dislikes this query shape degrades in cost, never in correctness.

        Chunks run concurrently under a semaphore, so the fan-out is bounded by
        MAX_ACTIVE_TYPE_PROBE_CONCURRENCY regardless of how many types the KG
        declares (the same treatment enum discovery gets, COG-58).
        """
        import asyncio

        from infona_client.graph.layers import type_name_from_uri

        chunks = [
            candidate_uris[i : i + ACTIVE_TYPE_PROBE_CHUNK]
            for i in range(0, len(candidate_uris), ACTIVE_TYPE_PROBE_CHUNK)
        ]
        sem = asyncio.Semaphore(MAX_ACTIVE_TYPE_PROBE_CONCURRENCY)

        async def _one(chunk: list[str]):
            async with sem:
                return await self.neptune.query(
                    self._active_type_probe_query(instance_graph, chunk)
                )

        # return_exceptions=True so a failing chunk cannot leave its siblings'
        # results unretrieved; the failure is then treated as a whole-probe
        # failure below, never as a partial answer.
        raws = await asyncio.gather(
            *[_one(chunk) for chunk in chunks], return_exceptions=True
        )
        if any(isinstance(r, BaseException) for r in raws):
            first = next(r for r in raws if isinstance(r, BaseException))
            logger.warning(
                "active_types_probe_failed",
                instance_graph=instance_graph,
                candidates=len(candidate_uris),
                error=str(first),
            )
            return None

        found: set[str] = set()
        for raw in raws:
            _, rows = parse_sparql_results(raw)
            for row in rows:
                name = type_name_from_uri(row.get("type", ""))
                if name:
                    found.add(name)
        return found

    async def _scan_instance_types(self, instance_graph: str) -> set[str]:
        """Every type NAME present in the instance graph (the UNBOUNDED scan).

        The pre-ONTA-427 probe verbatim. Still the right tool for the two jobs
        that genuinely need types the ontology never declared: the schema-missing
        instance fallback, and the over-cap case where one scan beats hundreds of
        seeks. READ-ONLY (a SELECT).
        """
        from infona_client.graph.layers import type_name_from_uri

        # Named for what it is, not `query`: the confinement drift guard in
        # tests/test_generated_sparql_scoping.py is deny-by-default and a
        # generically-named local would have to be allowlisted, which would then
        # wave through the next generated query that happened to reuse the name.
        type_scan_query = (
            f"SELECT DISTINCT ?type FROM <{instance_graph}> "
            f"WHERE {{ ?s <{RDF_TYPE_URI}> ?type }}"
        )
        _, rows = parse_sparql_results(await self.neptune.query(type_scan_query))
        out: set[str] = set()
        for row in rows:
            # type_name_from_uri understands tenant / public / enhanced
            # namespaces (longest-prefix-first), so a bare strip of the tenant
            # prefix would turn types/public/Person into "public/Person".
            name = type_name_from_uri(row.get("type", ""))
            if name:
                out.add(name)
        return out

    async def _resolve_active_types(
        self, instance_graph: str, declared_names=None
    ) -> tuple[set[str], set[str] | None]:
        """Active type names, plus the scan result when a scan was what produced them.

        The ONE place the ONTA-427 ladder lives, shared by the two callers that
        need it (:meth:`_active_types` for the semantic path, :meth:`_fetch_ontology`
        for the full one) so they cannot drift into asking the same question two
        different ways. Bounded LIMIT-1 probe when the declared names are known and
        there are few enough of them; the unbounded scan otherwise, or when the
        probe could not answer. The second element is the scan's own result (or
        None), so a caller that ALSO needs types the ontology never declared can
        reuse it instead of scanning twice.
        """
        names: set[str] | None = None
        if declared_names:
            candidate_uris = self._active_type_candidate_uris(declared_names)
            from infona_client.nlp import pipeline as _pl

            if candidate_uris and len(candidate_uris) <= _pl.MAX_ACTIVE_TYPE_PROBE_URIS:
                names = await self._probe_active_types(instance_graph, candidate_uris)
        scanned: set[str] | None = None
        if names is None:
            # Nothing declared, too many candidates to probe cheaply, or the
            # probe failed. Fall back to the pre-ONTA-427 scan.
            scanned = await self._scan_instance_types(instance_graph)
            names = scanned
        return names, scanned

    async def _active_types(
        self,
        instance_graph: str | None,
        ontology_graph: str = "",
        declared_names=None,
    ) -> set[str] | None:
        """Type names that actually carry instances in ``instance_graph``.

        Hoisted out of :meth:`_fetch_ontology` (ONTA-411) because the SEMANTIC
        retrieval path needs the same scope signal: the ontology store is
        tenant-wide, the instance graph is per-KG, and without this set a
        question retrieves a sibling KG's schema at max cosine similarity.

        ``declared_names`` keeps the probe on ONTA-427's bounded path. The
        semantic caller has no schema read to draw them from, so it passes the
        embedding store's own type names, which ARE the tenant's declared types;
        without them this would fall back to the unbounded per-ask scan that
        ONTA-427 removed.

        Returns ``None`` when there is nothing to scope: no instance graph, or
        the instance graph IS the ontology graph, in which case every declared
        type is in scope by definition. TTL-cached per instance graph AND
        candidate set (see :func:`_active_types_cache_key`); raises on
        a probe failure so each caller applies its own degradation policy
        (:meth:`_fetch_ontology` keeps reporting ONTOLOGY_FETCH_ERROR, while
        :meth:`ask` degrades to unscoped retrieval).

        An EMPTY result is deliberately NOT served from the cache. Downstream,
        `_fetch_ontology` treats "no declared type carries instances" as the
        fresh-ingest disambiguation branch and returns ONTOLOGY_EMPTY WITHOUT
        caching the summary, precisely so the next ask re-reads a KG that may
        have been populated in the meantime. Caching the empty probe would
        reinstate the stale answer that branch exists to avoid: a KG asked about
        while empty, then ingested by another worker, would keep answering
        "No ontology defined yet." for the rest of the TTL. Re-probing an empty
        graph is also the cheapest possible query.
        """
        if not instance_graph or instance_graph == ontology_graph:
            return None
        key = _active_types_cache_key(instance_graph, declared_names)
        cached = _active_types_cache.get(key)
        if cached and cached[0] and (time.time() - cached[1]) < ONTOLOGY_CACHE_TTL:
            return cached[0]
        names, _ = await self._resolve_active_types(instance_graph, declared_names)
        _store_active_types(key, names)
        return names
