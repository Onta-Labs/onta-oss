"""Post-generation SPARQL URI/shape repairs (residual helpers; /ask is Cypher)."""
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


class PipelineSparqlFixMixin:
    @staticmethod
    def _fix_attribute_uris(sparql: str, ontology_summary: str) -> str:
        """Fix incorrect URIs in generated SPARQL using the ontology as ground truth.

        This is the post-processing safety net (Fix B). It catches URI mistakes
        the LLM makes despite the prompt telling it to copy-paste exact URIs.

        Strategy:
        1. Extract ALL valid URIs from the ontology summary (attributes + relationships)
        2. Find ALL graph.infona.ai URIs in the SPARQL
        3. For each URI not in the valid set, fuzzy-match against valid URIs
        4. Replace with the best match if similarity is high enough

        Common mistakes this catches:
        - <https://graph.infona.ai/bedrooms> → <https://graph.infona.ai/types/Property/attrs/bedrooms>
        - <https://graph.infona.ai/onto/bedrooms> → <https://graph.infona.ai/types/Property/attrs/bedrooms>
        - <https://graph.infona.ai/types/Property/attrs/property_type> → .../attrs/home_type
        - <https://graph.infona.ai/Property> → <https://graph.infona.ai/types/Property>
        """
        import re
        from difflib import SequenceMatcher

        # Step 1: Build the set of ALL valid URIs from the ontology
        valid_uris: dict[str, str] = {}  # name → full URI

        # Attribute URIs: "attr_name (type) — URI: <https://graph.infona.ai/types/Type/attrs/attr_name>"
        for match in re.finditer(rf"URI: <({re.escape(IRI_BASE)}/types/(\w+)/attrs/(\w+))>", ontology_summary):
            full_uri = match.group(1)
            attr_name = match.group(3)
            valid_uris[attr_name] = full_uri
            # Also index by type/attr for disambiguation
            valid_uris[f"{match.group(2)}/{attr_name}"] = full_uri

        # Relationship URIs: "predicate URI: <https://graph.infona.ai/onto/pred_name>"
        for match in re.finditer(rf"predicate URI: <({re.escape(IRI_BASE)}/onto/(\w+))>", ontology_summary):
            full_uri = match.group(1)
            pred_name = match.group(2)
            valid_uris[pred_name] = full_uri

        # Type URIs: "Type: TypeName — URI: <https://graph.infona.ai/types/TypeName>"
        for match in re.finditer(rf"URI: <({re.escape(IRI_BASE)}/types/(\w+))>", ontology_summary):
            full_uri = match.group(1)
            type_name = match.group(2)
            if "/attrs/" not in full_uri:  # don't overwrite attr URIs
                valid_uris[type_name] = full_uri

        valid_uri_set = set(valid_uris.values())

        # Step 2: Find and fix all graph.infona.ai URIs in the SPARQL
        def _fix_uri(m: re.Match) -> str:
            uri = m.group(1)

            # Already valid? Keep it.
            if uri in valid_uri_set:
                return m.group(0)

            # Skip known system URIs. attr_meta/ is load-bearing here (ONTA-262):
            # the freshness prompt teaches the planner to CONSTRUCT
            # attr_meta/<Type>/<attr>/verified_at from the type + attribute names
            # — deliberately absent from the ontology summary — so the fuzzy
            # repair below must never "fix" it into some unrelated declared
            # attribute (measured: it cross-wired to a legacy `fax_verified_at`
            # at ratio 0.846 before this skip).
            if any(
                uri.startswith(f"{IRI_BASE}/{p}")
                for p in ("graphs/", "entities/", "functions/", "kgs/", "attr_meta/")
            ):
                return m.group(0)

            # Extract the "name" part from the URI for matching
            # e.g., "https://graph.infona.ai/bedrooms" → "bedrooms"
            # e.g., "https://graph.infona.ai/onto/listed_by" → "listed_by"
            # e.g., "https://graph.infona.ai/types/Property/attrs/property_type" → "property_type"
            parts = uri.replace(f"{IRI_BASE}/", "").rstrip("/").split("/")
            name = parts[-1] if parts else ""

            if not name:
                return m.group(0)

            # Direct name match
            if name in valid_uris:
                return f"<{valid_uris[name]}>"

            # Fuzzy match against all valid URI names
            best_match = None
            best_ratio = 0.0
            for vname, vuri in valid_uris.items():
                # Compare the short name part only
                vshort = vname.split("/")[-1]
                ratio = SequenceMatcher(None, name, vshort).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = vuri

            if best_ratio >= 0.75 and best_match:
                return f"<{best_match}>"

            return m.group(0)

        return re.sub(rf"<({re.escape(IRI_BASE)}/[^>]+)>", _fix_uri, sparql)

    @staticmethod
    def _fix_common_sparql_issues(sparql: str, ontology_summary: str, alias_map: dict[str, str] | None = None) -> str:
        """Fix common SPARQL generation mistakes that the LLM makes.

        1. Replace `a` shorthand with full rdf:type URI
        2. Replace cross-type attribute URIs (e.g., Person/attrs/name used on a Movie)
           with rdfs:label
        3. Replace overview/description attributes used as display names with rdfs:label
        """
        import re

        RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
        RDFS_LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"

        # Fix 1: Replace `a` shorthand (only when used as predicate position)
        # Match "?var a <..." or "?var rdf:type <..."
        sparql = re.sub(
            rf'(\?\w+)\s+a\s+(<{re.escape(IRI_BASE)}/)',
            rf'\1 {RDF_TYPE} \2',
            sparql,
        )
        sparql = re.sub(
            r'(\?\w+)\s+rdf:type\s+',
            rf'\1 {RDF_TYPE} ',
            sparql,
        )

        # Fix 2: Replace overview used ONLY when it's the sole "name" variable selected
        # and the entity type has no name attribute. This is conservative to avoid
        # breaking legitimate description/narrative queries.
        # Only replace Movie/attrs/overview when used in a "name-like" position
        overview_pattern = rf'<{re.escape(IRI_BASE)}/types/Movie/attrs/overview>'
        if re.search(overview_pattern, sparql):
            # Check if the query is trying to get movie names (not filtering by overview content)
            # Heuristic: if overview appears in SELECT projection but not in FILTER
            select_part = sparql.split('WHERE')[0] if 'WHERE' in sparql else ''
            filter_uses_overview = 'overview' in sparql.split('FILTER')[1] if 'FILTER' in sparql else False
            if not filter_uses_overview:
                sparql = re.sub(overview_pattern, RDFS_LABEL[1:-1], sparql)

        # Fix 4: Rewrite type-assertion predicates to subclass-closure paths so a
        # query over a parent type returns subtype instances (ADR rule 2).
        # Deterministic, idempotent, no ontology lookup needed.
        from infona_client.graph.ontology_queries import rewrite_type_predicate_to_closure
        sparql = rewrite_type_predicate_to_closure(sparql)

        # Fix 4a: Person asks must bind Contact|Staff attribute predicates
        # (INF-599). Instance triples stay on the asserted leaf
        # (`types/Contact/attrs/first_name`); `types/Person/attrs/first_name`
        # alone is empty. Ontology summary supplies the subclass map.
        from infona_client.graph.ontology_queries import (
            rewrite_parent_attr_to_subclass_predicates,
        )
        from infona_client.graph.rdfs_helpers import extract_subclass_map_from_ontology

        sparql = rewrite_parent_attr_to_subclass_predicates(
            sparql, extract_subclass_map_from_ontology(ontology_summary)
        )

        # Fix 4b: follow sameAs so a query pinning a MERGED-away entity IRI
        # (ONTA-274 / -278) resolves the canonical's facts under either alias.
        # Same deterministic-property-path shape as the closure rewrite above.
        from infona_client.graph.ontology_queries import rewrite_entity_ref_to_sameas_closure
        sparql = rewrite_entity_ref_to_sameas_closure(sparql)

        # Fix 5: resolve attribute aliases (ADR 0002 §7) — a renamed attribute
        # keeps answering through its alias until backfill retires it. A None
        # or empty map (the default) leaves the query untouched.
        if alias_map:
            from infona_client.graph.aliases import rewrite_query_attrs
            sparql = rewrite_query_attrs(sparql, alias_map)

        # Fix 6: normalize freshness-window duration literals to the Neptune-valid
        # datatype. The recency pattern the prompt teaches is
        # `NOW() - "PnD"^^xsd:dayTimeDuration`, which is valid SPARQL 1.1 (and works
        # on spec engines like pyoxigraph) — but Neptune does NOT implement
        # `xsd:dayTimeDuration` arithmetic: `NOW() - "P7D"^^xsd:dayTimeDuration`
        # yields an ERROR/unbound rather than a dateTime, so a comparison against it
        # is an error and the FILTER silently drops EVERY row (and in aggregate /
        # property-path shapes escalates to a hard 400/500). The identical `xsd:duration`
        # subtraction DOES evaluate on Neptune and on pyoxigraph, so rewriting the
        # datatype makes the recency filter work on the deployed backend while staying
        # correct on the spec engine. Idempotent; touches only the duration datatype IRI.
        sparql = _neptune_safe_duration(sparql)

        # Fix 7: prefer types/<T>/attrs/name over rdfs:label for display names when
        # the query already types the subject as <T>. Path-B / CSV-ingested KGs often
        # mint rdfs:label as a slug or numeric id while attrs/name holds the human
        # string — ranking queries then return "eventName: 5" with the right numeric
        # extreme (Eval-MH freeze flaky projection fails). Only rewrites when
        # attrs/name is not already used for that type in the query.
        sparql = _prefer_attr_name_over_rdfs_label(sparql, ontology_summary)

        return sparql

    @staticmethod
    def _ensure_order_by(sparql: str) -> str:
        """Add a deterministic ORDER BY to a plain SELECT so truncation is stable.

        Result rows come back in arbitrary Neptune order, so slicing to a row
        cap (``bindings[:cap]``) cut an essentially random subset — two runs of
        the same question could truncate to different rows. Adding a stable
        ORDER BY over the projected variables makes the cut deterministic
        (same rows every run) and groups like-with-like (e.g. by type then
        label) so a truncated page reads coherently.

        Conservative — leaves the query untouched when ordering would be wrong
        or risky:
        - already has ORDER BY (respect the LLM's / template's intent),
        - is an aggregate (GROUP BY / HAVING) — ordering by raw projected vars
          would be invalid,
        - isn't a SELECT, is a SELECT * (no named vars to order by), or has an
          existing LIMIT/OFFSET (assume intentional shape).
        Ordering is best-effort: any parse hiccup returns the original query.
        """
        import re

        try:
            upper = sparql.upper()
            if "SELECT" not in upper:
                return sparql
            if "ORDER BY" in upper or "GROUP BY" in upper or "HAVING" in upper:
                return sparql
            if "LIMIT" in upper or "OFFSET" in upper:
                return sparql

            # Extract the projected variables from the SELECT clause. Bail on
            # SELECT * (nothing named to order by) or aggregate projections.
            m = re.search(r"SELECT\s+(DISTINCT\s+|REDUCED\s+)?(.*?)\s+WHERE", sparql, re.IGNORECASE | re.DOTALL)
            if not m:
                return sparql
            proj = m.group(2)
            if "*" in proj or "(" in proj:  # SELECT * or has an expression/aggregate/alias
                return sparql
            proj_vars = re.findall(r"\?(\w+)", proj)
            if not proj_vars:
                return sparql

            order_expr = " ".join(f"?{v}" for v in proj_vars)
            # Append ORDER BY at the very end (after the closing WHERE brace and
            # any solution modifiers we already screened out above).
            return f"{sparql.rstrip().rstrip('.')}\nORDER BY {order_expr}"
        except Exception:
            return sparql

    async def _fetch_alias_map(self, graph_uri: str) -> dict[str, str]:
        """Cached attribute-alias map for the tenant ontology graph (ADR 0002 §7).

        Failures degrade to an empty map — alias resolution never blocks /ask.
        """
        cached = _alias_cache.get(graph_uri)
        if cached and (time.time() - cached[1]) < ONTOLOGY_CACHE_TTL:
            return cached[0]
        from infona_client.graph.aliases import fetch_alias_map
        try:
            alias_map = await fetch_alias_map(self.neptune, graph_uri)
        except Exception:
            alias_map = {}
        _alias_cache[graph_uri] = (alias_map, time.time())
        return alias_map

    @staticmethod
    def invalidate_cache(graph_uri: str) -> None:
        """Call after ingestion to clear the cached ontology for a graph."""
        _ontology_cache.pop(graph_uri, None)
        # Also clear any KG-specific cache entries
        keys_to_remove = [k for k in _ontology_cache if k.startswith(graph_uri)]
        for k in keys_to_remove:
            _ontology_cache.pop(k, None)
        # Alias map is keyed by the ontology graph URI alone
        _alias_cache.pop(graph_uri, None)
        # Active-type sets are keyed by the INSTANCE graph, whose URI extends the
        # tenant graph URI (.../graphs/<tenant>/kg/<name>), so the same prefix
        # sweep drops every KG's entry for this tenant. Stale entries here would
        # keep demoting types that an ingest just populated (ONTA-411).
        for k in [k for k in _active_types_cache if k.startswith(graph_uri)]:
            _active_types_cache.pop(k, None)
        # Invalidate embeddings
        from infona_client.nlp import pipeline as _pl

        svc = _pl.get_embedding_service()
        if svc:
            svc.invalidate(graph_uri)
