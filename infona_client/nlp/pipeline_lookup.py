"""Zero-row name-lookup broadening against a type's supertype."""
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


class PipelineLookupMixin:
    # ------------------------------------------------ name-lookup broadening
    # Match a `types/<Leaf>` URI in rdf:type OBJECT position, whether the
    # predicate is a bare rdf:type or the subclass-closure path the pipeline
    # rewrites it to (`<…#type>/<…#subClassOf>*`).
    _TYPE_OBJECT_RE = re.compile(
        r"#type>(?:\s*/\s*<[^>]*#subClassOf>\*)?\s*"
        rf"<({re.escape(IRI_BASE)}/types/[^>]+)>"
    )
    # Capture the variable a case-insensitive substring FILTER targets:
    # FILTER(CONTAINS(LCASE(?V), …)) — allowing an optional STR() coercion around
    # the variable. The generation prompt teaches this exact shape for BOTH name
    # matching AND arbitrary string-attribute filters (tags, status, …), so a bare
    # `CONTAINS(LCASE` match would over-trigger broadening; we additionally require
    # ?V to be a display-NAME variable (see :meth:`_targets_label_name_var`).
    _CONTAINS_VAR_RE = re.compile(
        r"CONTAINS\s*\(\s*LCASE\s*\(\s*(?:STR\s*\(\s*)?\?(\w+)", re.IGNORECASE
    )
    _RDFS_LABEL_URI = "http://www.w3.org/2000/01/rdf-schema#label"

    @classmethod
    def _targets_label_name_var(cls, sparql: str) -> bool:
        """True iff a ``CONTAINS(LCASE(?V))`` filter in the query targets a DISPLAY
        NAME variable — ``?V`` bound in the SAME query as the object of an
        ``rdfs:label`` triple or a ``types/<T>/attrs/{name,label}`` attribute
        triple.

        A ``CONTAINS`` over an arbitrary string attribute (``tags``, ``status``, …)
        returns ``False``. Without this gate, broadening would widen a
        legitimately type-constrained attribute query (e.g. a ``MortgageComplaint``
        filtered on ``attrs/tags`` that returns zero rows) up to the supertype and
        surface a sibling-subtype row — turning an honest "no results" into a
        confidently wrong-TYPE answer.
        """
        for m in cls._CONTAINS_VAR_RE.finditer(sparql):
            v = re.escape(m.group(1))
            if re.search(rf"<{re.escape(cls._RDFS_LABEL_URI)}>\s*\?{v}\b", sparql):
                return True
            if re.search(
                rf"<{re.escape(IRI_BASE)}/types/[^>]+/attrs/(?:name|label)>\s*\?{v}\b",
                sparql,
                re.IGNORECASE,
            ):
                return True
        return False

    async def _broaden_name_lookup(
        self,
        sparql: str,
        graph_uri: str,
        data_graph: str | None = None,
        layer_graph_uris: list[str] | None = None,
    ) -> tuple[str, dict] | None:
        """Retry a zero-row NAME lookup against the type's SUPERTYPE.

        A lookup by name that the generator bound to ONE specific subtype (e.g. a
        person queried as ``OrthopedicSurgeon`` who is actually a
        ``BreastOncologist``) returns zero rows, even though the shared supertype
        (``Physician``) spans every subtype. When the executed query is (a) a
        DISPLAY-NAME lookup — a ``FILTER(CONTAINS(LCASE(?V)))`` whose ``?V`` is
        bound as an ``rdfs:label`` / name attribute (NOT an arbitrary string
        attribute like ``tags``; see :meth:`_targets_label_name_var`) — and (b)
        constrains ``rdf:type`` to EXACTLY ONE ``types/<Sub>`` that HAS a
        supertype, re-issue it with that subtype swapped for its top-most
        ancestor. The subclass-closure rewrite (``rdf:type/subClassOf*``) already
        applied to the query then makes the ancestor match every sibling subtype.

        Returns ``(broadened_sparql, raw_result)`` from the re-query, or ``None``
        when the query is not a single-subtype NAME lookup, the type has no
        supertype, or anything errors — best-effort. The one exception it lets
        through is :class:`CrossTenantQueryError` (ONTA-424): swallowing a
        confinement failure into ``None`` would turn a security event into a
        silent "no broadening happened", so it propagates to ``ask()``, which
        re-raises it.
        """
        try:
            if not self._targets_label_name_var(sparql):
                return None
            type_uris = set(self._TYPE_OBJECT_RE.findall(sparql))
            if len(type_uris) != 1:
                return None
            sub_uri = next(iter(type_uris))
            sub_name = sub_uri.rsplit("/", 1)[-1]

            parent_of = await self._fetch_parent_map(graph_uri)
            if sub_name not in parent_of:
                return None
            # Walk to the top-most ancestor so the broadened query spans the whole
            # hierarchy, not just one level up. Guard against cyclic subClassOf.
            ancestor = sub_name
            seen = {ancestor}
            while parent_of.get(ancestor) and parent_of[ancestor] not in seen:
                ancestor = parent_of[ancestor]
                seen.add(ancestor)
            if ancestor == sub_name:
                return None

            from infona_client.graph.ontology_queries import type_uri as _type_uri
            super_uri = _type_uri(ancestor)
            # Replace ONLY the exact bracketed type-object, never a raw substring:
            # a bare `sparql.replace(sub_uri, super_uri)` would corrupt every URI
            # that shares the prefix — `types/Cat/attrs/breed` → the non-existent
            # `types/Animal/attrs/breed`, and sibling `types/CatFood` →
            # `types/AnimalFood` — breaking a "show details" query that projects
            # type-specific attribute URIs.
            broadened = sparql.replace(f"<{sub_uri}>", f"<{super_uri}>")
            if broadened == sparql:
                return None
            # ONTA-424: re-confine rather than inherit the caller's verdict. The
            # substitution above only rewrites a `types/` URI today, so the
            # dataset cannot change, but the guard belongs at the store call and
            # not at whichever transform happens to precede it.
            broadened = self._confine_generated(
                broadened, data_graph or graph_uri, layer_graph_uris
            )
            raw = await self.neptune.query(broadened)
            return broadened, raw
        except CrossTenantQueryError:
            raise
        except Exception:
            logger.debug("name_lookup_broaden_failed", exc_info=True)
            return None

    async def _fetch_parent_map(self, graph_uri: str) -> dict[str, str]:
        """child_name -> parent_name from the graph's ``rdfs:subClassOf`` edges.

        Best-effort; used only on the zero-row broadening path. Keys/values are
        the type NAMES (last URI path segment), so callers walk the hierarchy by
        name.
        """
        from infona_client.graph.ontology_queries import parent_map_query
        TYPES = TYPE_URI_PREFIX
        raw = await self.neptune.query(parent_map_query(graph_uri))
        _, bindings = parse_sparql_results(raw)
        parent_of: dict[str, str] = {}
        for row in bindings:
            child = row.get("child", "")
            parent = row.get("parent", "")
            if child.startswith(TYPES) and parent.startswith(TYPES):
                parent_of[child[len(TYPES):]] = parent[len(TYPES):]
        return parent_of
