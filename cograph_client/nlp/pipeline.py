import json
import os
import re
import time

import anthropic
import httpx
import structlog

from cograph_client.graph.client import NeptuneClient
from cograph_client.graph.parser import parse_sparql_results, unbound_projection_vars
from cograph_client.graph.queries import parse_kg_graph_uri
from cograph_client.models.query import NLResult
from cograph_client.nlp.prompts import SPARQL_GENERATION_SYSTEM, build_generation_prompt
from cograph_client.nlp.validator import normalize_sparql, validate_sparql
from cograph_client.resolver.llm_router import model_chain
from cograph_client.spatiotemporal.routing import (
    SPATIAL_INTENT_SCHEMA,
    SPATIAL_INTENT_SYSTEM,
    filter_by_type,
    format_spatial_answer,
    looks_spatial,
    parse_spatial_intent,
)

logger = structlog.stdlib.get_logger("cograph.nlp.pipeline")

# In-memory ontology cache: {graph_uri: (summary_str, timestamp)}
_ontology_cache: dict[str, tuple[str, float]] = {}
ONTOLOGY_CACHE_TTL = 60  # seconds

# Distinct markers so a TRANSIENT fetch failure is never mistaken for a genuinely
# empty graph (ONTA-248 A2: "errors masquerade as facts"). The old error text
# ("Graph may be empty.") let the LLM authoritatively state the graph was empty on
# a mere throttle/timeout. These strings are surfaced to the SPARQL-generation LLM;
# the error marker explicitly forbids asserting absence.
ONTOLOGY_FETCH_ERROR = (
    "Could not fetch the ontology for this graph (a transient backend error, e.g. "
    "a timeout or throttle). This does NOT mean the graph is empty or that any "
    "type is absent — the schema is simply UNKNOWN right now. Do not claim any "
    "type or attribute does not exist; suggest retrying."
)
ONTOLOGY_EMPTY = "No ontology defined yet."

# Cap on concurrent enum-discovery SPARQL queries (COG-58). Enum discovery
# fires one COUNT(DISTINCT) per attribute + per relationship; an unbounded
# asyncio.gather meant a wide table (hundreds of columns → hundreds of
# attributes) launched O(columns) simultaneous queries, throttling serverless
# Neptune (1–2.5 NCU). The semaphore keeps the round-trip count bounded
# regardless of column count, trading a little latency for stability.
MAX_ENUM_DISCOVERY_CONCURRENCY = int(
    os.environ.get("OMNIX_ENUM_DISCOVERY_CONCURRENCY", "8")
)

# Attribute-alias map cache (ADR 0002 §7): {graph_uri: (old->new map, timestamp)}
_alias_cache: dict[str, tuple[dict[str, str], float]] = {}

# Query generation provider config
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_QUERY_MODEL = os.environ.get("OMNIX_QUERY_MODEL", "llama3.1-8b")
DEFAULT_QUERY_PROVIDER = os.environ.get("OMNIX_QUERY_PROVIDER", "cerebras")  # cerebras, openrouter, or anthropic

# Max rows rendered in the plain-text answer before truncating. The old
# hard-coded 20 silently dropped most of a wide "list all ..." result; raise it
# and make it tunable. Truncation is now stated prominently (not buried) AND
# the slice is deterministic because generated SELECTs get a stable ORDER BY.
ANSWER_ROW_CAP = int(os.environ.get("OMNIX_ANSWER_ROW_CAP", "100"))

# Embedding service singleton
_embedding_service = None


def get_embedding_service():
    """Lazy-init singleton for the ontology embedding service."""
    global _embedding_service
    if _embedding_service is None:
        from cograph_client.config import settings
        if settings.openrouter_api_key:
            from cograph_client.nlp.ontology_embeddings import OntologyEmbeddingService
            _embedding_service = OntologyEmbeddingService(
                openrouter_api_key=settings.openrouter_api_key,
                s3_bucket=settings.embeddings_s3_bucket,
                s3_prefix=settings.embeddings_s3_prefix,
            )
    return _embedding_service


# Spatial fast-path helpers (ONTA-157 Phase 2). Module-level + pure so they're
# trivially testable; the orchestration that uses them lives on NLQueryPipeline.
_GEO_WKT_URI = "http://www.opengis.net/ont/geosparql#wktLiteral"
_POINT_RE = re.compile(
    r"POINT\s*\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", re.IGNORECASE
)


def _parse_iso_dt(s):
    """ISO-8601 string → tz-aware (UTC-assumed) datetime, or None. Mirrors the
    extractor so a query bound and an indexed validity compare without raising."""
    if not s or not isinstance(s, str):
        return None
    from datetime import datetime, timezone

    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_point_wkt(wkt: str):
    """``"POINT(lon lat)"`` → (lon, lat) in WGS84 range, else None."""
    if not isinstance(wkt, str):
        return None
    m = _POINT_RE.search(wkt)
    if not m:
        return None
    try:
        lon, lat = float(m.group(1)), float(m.group(2))
    except ValueError:
        return None
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        return None
    return lon, lat


def _sanitize_sparql_literal(text: str) -> str:
    """Strip characters that could break out of a SPARQL string literal, and cap
    length — the anchor description comes from the LLM and is interpolated into a
    FILTER(CONTAINS(...)) literal."""
    return re.sub(r'["\\\n\r\t]', " ", text).strip().lower()[:80]


# Neptune does not implement `xsd:dayTimeDuration` (nor `xsd:yearMonthDuration`)
# arithmetic on `xsd:dateTime`: `NOW() - "P7D"^^xsd:dayTimeDuration` evaluates to an
# ERROR/unbound (not a dateTime), so a recency FILTER against it silently drops every
# row — and in aggregate/property-path query shapes escalates to a hard 400/500. The
# equivalent `xsd:duration` subtraction DOES evaluate on Neptune (verified on the
# deployed cluster) and is also accepted by spec engines like pyoxigraph, so it is the
# common-denominator datatype for a "last N days" window. This rewrites the datatype IRI
# of a duration literal (bare `xsd:` prefix or the full XMLSchema# IRI, in angle brackets
# or not) to `duration`. Idempotent — a literal already typed `duration` is untouched.
_DURATION_DATATYPE_RE = re.compile(
    r"(\^\^)"                                                    # the datatype marker
    r"(<?)"                                                      # optional opening angle bracket
    r"(xsd:|http://www\.w3\.org/2001/XMLSchema#)"                # bare prefix OR full namespace
    r"(?:dayTimeDuration|yearMonthDuration)"                     # the Neptune-unsupported subtypes
    r"(>?)",                                                     # optional closing angle bracket
    re.IGNORECASE,
)


def _neptune_safe_duration(sparql: str) -> str:
    """Rewrite `xsd:dayTimeDuration`/`xsd:yearMonthDuration` duration literals to
    `xsd:duration` so a NOW()-relative recency FILTER is valid on Neptune.

    Preserves the exact surface form the LLM emitted (bare `xsd:` prefix vs full
    XMLSchema# IRI, and whether it was wrapped in angle brackets), rewriting only the
    local name. Idempotent and safe on any query — it matches only a duration-subtype
    datatype IRI, which appears nowhere else.
    """
    def _sub(m: re.Match) -> str:
        marker, open_b, namespace, close_b = m.groups()
        # `namespace` is exactly the prefix/IRI the LLM used (`xsd:` or the full
        # XMLSchema# IRI); reuse it verbatim so only the local name changes.
        return f"{marker}{open_b}{namespace}duration{close_b}"

    return _DURATION_DATATYPE_RE.sub(_sub, sparql)


_ENTITY_URI_PREFIX = "https://cograph.tech/entities/"


def _row_has_entity_object(row: dict) -> bool:
    """True if any value in the row is an entity IRI (``…/entities/…``).

    A describe-shape row (``?p ?o``) whose object is an entity IRI means ``?p`` is
    a RELATIONSHIP edge, not a literal-valued housekeeping marker — so the
    predicate filter must apply its ``is_relationship`` exemption for that row and
    NOT hide a real relationship that happens to share a housekeeping leaf name.
    """
    return any(
        isinstance(v, str) and v.startswith(_ENTITY_URI_PREFIX) for v in row.values()
    )


def _drop_internal_predicate_rows(bindings: list[dict]) -> list[dict]:
    """Drop result rows that describe an INTERNAL/housekeeping predicate.

    The NL ``ask`` path renders every binding verbatim, so a ``SELECT ?p ?o``
    "describe this entity" or a ``SELECT DISTINCT ?p`` query leaks entity-
    resolution internals (``er/blockKey``, ``er/erSignal_*``), ingest housekeeping
    (``onto/batch_id``, …) and normalization bookkeeping (``onto/norm/*``) straight
    into the answer text. This is the render-time twin of the Explorer's panel
    filter: a row is dropped when ANY of its values is an internal predicate URI
    per the shared :func:`is_internal_predicate`.

    Real relationships on ``…/onto/<leaf>`` are PRESERVED (the shared helper
    returns False for them). When a row's object is an entity IRI the predicate is
    treated as a relationship (``is_relationship=True``) so a legitimate edge that
    shares a housekeeping leaf name (e.g. an ``…/onto/source`` edge pointing at an
    Organization) is not hidden. Rows carrying no predicate-shaped value (ordinary
    attribute projections like ``?name ?latency``) are untouched — nothing in them
    matches an internal predicate URI, so they always pass through.
    """
    from cograph_client.graph.predicates import is_internal_predicate

    def _is_uri(v) -> bool:
        return isinstance(v, str) and v.startswith(("http://", "https://"))

    kept: list[dict] = []
    for row in bindings:
        is_rel = _row_has_entity_object(row)
        # Only URI-shaped values can be a predicate; a literal / empty attribute
        # value must never trigger the drop (is_internal_predicate("") is True).
        if any(
            _is_uri(v) and is_internal_predicate(v, is_relationship=is_rel)
            for v in row.values()
        ):
            continue
        kept.append(row)
    return kept


class NLQueryPipeline:
    def __init__(self, neptune: NeptuneClient, anthropic_key: str):
        self.neptune = neptune
        self.anthropic = anthropic.AsyncAnthropic(api_key=anthropic_key)
        from cograph_client.config import settings
        self._openrouter_key = settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._cerebras_key = os.environ.get("CEREBRAS_API_KEY", getattr(settings, "cerebras_api_key", ""))
        self._query_model = DEFAULT_QUERY_MODEL
        self._query_provider = DEFAULT_QUERY_PROVIDER
        # Attribute aliases (ADR 0002 §7): resolve renamed attribute IRIs in
        # generated SPARQL. Default OFF so the default Neptune call pattern
        # stays byte-identical (same gating pattern as COGRAPH_ER_ENABLED).
        self._aliases_enabled = os.environ.get("COGRAPH_ALIASES_ENABLED", "0") == "1"
        # Spatio-temporal read routing (ONTA-157 Phase 2 → ONTA-249): a
        # geo/proximity question is answered directly from the secondary index (no
        # Neptune round-trip). Now a SUPPORTED path and ENABLED BY DEFAULT (ONTA-249):
        # the radius/bbox engine is fully built and, with the free-text geocoder
        # seam, a bare place-name anchor resolves — so "within N km of PLACE" works
        # end-to-end. It is defensively gated: the fast path returns None (falls
        # through to SPARQL unchanged) whenever the question isn't spatial, the KG
        # can't be scoped, the intent doesn't parse, or the anchor can't be
        # resolved — so enabling it cannot regress a non-spatial query. Set
        # COGRAPH_SPATIAL_ROUTING_ENABLED=0 to force it off (e.g. byte-stable evals).
        self._spatial_routing_enabled = (
            os.environ.get("COGRAPH_SPATIAL_ROUTING_ENABLED", "1") != "0"
        )

    async def ask(self, question: str, graph_uri: str, instance_graph: str | None = None, exclude_questions: list[str] | None = None, layer_graph_uris: list[str] | None = None) -> NLResult:
        """Answer a natural-language question over the graph.

        layer_graph_uris (ADR 0002 §1, COG-37, opt-in): a LayerStack's
        visible_graph_uris(). Generated queries are graph-scoped (FROM the
        data graph), so without this the subclass-closure path can't see
        subClassOf edges living in other layer graphs; when provided, each
        generated query gains FROM clauses for every visible layer. When
        None (the default), behavior is exactly as before.
        """
        timing: dict[str, float] = {}
        timing["model"] = f"{self._query_provider}:{self._query_model}"
        # Ontology is always fetched from the base tenant graph
        # Instance data may be in a different graph (KG-specific)
        data_graph = instance_graph or graph_uri

        t0 = time.time()
        # Try semantic retrieval first, fall back to full ontology
        ontology = None
        embedding_svc = get_embedding_service()
        if embedding_svc:
            try:
                from cograph_client.config import settings
                ontology = await embedding_svc.retrieve(graph_uri, question, top_k=settings.embeddings_top_k)
                if ontology:
                    timing["ontology_source"] = "semantic"
            except Exception:
                pass
        if ontology is None:
            ontology = await self._fetch_ontology(graph_uri, data_graph)
            timing["ontology_source"] = "full"
        timing["ontology_fetch_ms"] = round((time.time() - t0) * 1000, 1)

        # Spatio-temporal fast path (ONTA-157 Phase 2, gated). For a geo/proximity
        # question, answer directly from the secondary index — no SPARQL, no Neptune
        # round-trip. Returns None (and we fall through to the normal path unchanged)
        # whenever routing is off, the question isn't spatial, the KG can't be
        # scoped, the intent doesn't parse, or the anchor can't be resolved.
        if self._spatial_routing_enabled and looks_spatial(question):
            spatial = await self._try_spatial_fast_path(
                question, ontology, data_graph, timing, t0
            )
            if spatial is not None:
                return spatial

        # Attribute-alias map (ADR 0002 §7). Only fetched when the feature is
        # enabled; an empty map (no aliases registered) leaves every query
        # untouched, so zero aliases => zero behavior change.
        alias_map: dict[str, str] = {}
        if self._aliases_enabled:
            alias_map = await self._fetch_alias_map(graph_uri)

        # Retrieve few-shot examples from the example bank
        examples_text = ""
        try:
            from cograph_client.nlp.example_bank import get_example_bank, format_examples_for_prompt
            bank = get_example_bank()
            if bank and bank._examples:
                # Extract kg_name from data_graph URI for cross-dataset preference
                kg_name = data_graph.split("/kg/")[-1] if "/kg/" in data_graph else ""
                examples = await bank.retrieve(
                    question=question,
                    ontology_context=ontology,
                    exclude_questions=exclude_questions or [],
                    kg_name=kg_name,
                    top_k=3,
                )
                if examples:
                    examples_text = format_examples_for_prompt(examples)
                    timing["examples_retrieved"] = len(examples)
        except Exception:
            pass

        max_attempts = 3
        last_error = ""
        sparql = ""
        explanation = ""
        functions_needed: list[str] = []

        for attempt in range(max_attempts):
            # The ENTIRE attempt — SPARQL generation, post-processing,
            # validation, execution, and formatting — runs inside one
            # try/except so a transient failure at ANY stage retries instead of
            # escaping the loop. Generation in particular calls a provider whose
            # `raise_for_status()` / `json.loads` are unguarded: a provider 5xx,
            # timeout, or malformed-JSON response used to fly straight past all
            # three attempts and out of `ask()` as a bare 500 (no error body).
            # Now it's caught here, recorded as `last_error`, and retried; after
            # max_attempts it falls through to the graceful NLResult below.
            try:
                t1 = time.time()
                if attempt == 0:
                    llm_response = await self._generate_sparql(question, ontology, data_graph, examples_text=examples_text)
                else:
                    llm_response = await self._generate_sparql(
                        question, ontology, data_graph,
                        error_feedback=f"The previous query failed with: {last_error}\nQuery was: {sparql}\nPlease fix the SPARQL syntax and try again.",
                    )
                timing[f"sparql_gen_ms{f'_retry{attempt}' if attempt > 0 else ''}"] = round((time.time() - t1) * 1000, 1)

                sparql = normalize_sparql(llm_response.get("sparql", ""))
                # Fix bare attribute URIs using ontology context
                sparql = self._fix_attribute_uris(sparql, ontology)
                # Fix cross-type attribute misuse and rdf:type shorthand
                sparql = self._fix_common_sparql_issues(sparql, ontology, alias_map)
                # Deterministic ordering so truncation cuts cleanly and results
                # are stable across runs (COG / persona-eval: unordered results
                # made the [:cap] slice arbitrary).
                sparql = self._ensure_order_by(sparql)
                if layer_graph_uris:
                    # Layer-aware closure (COG-37): widen the graph scope so the
                    # subClassOf* walk sees edges in every visible layer graph.
                    from cograph_client.graph.ontology_queries import add_layer_from_clauses
                    sparql = add_layer_from_clauses(sparql, layer_graph_uris)
                explanation = llm_response.get("explanation", "")
                functions_needed = llm_response.get("functions_needed", [])

                is_valid, error = validate_sparql(sparql)
                if not is_valid:
                    last_error = error
                    continue

                t2 = time.time()
                raw = await self.neptune.query(sparql)
                timing[f"neptune_exec_ms{f'_retry{attempt}' if attempt > 0 else ''}"] = round((time.time() - t2) * 1000, 1)
                variables, bindings = parse_sparql_results(raw)
                # Projected vars that bound in ZERO rows (e.g. an OPTIONAL
                # attribute absent from every matching entity, or a drifted
                # attribute URI). Reported honestly instead of silently omitted,
                # so the answer signals "column missing" vs "column empty".
                missing_vars = unbound_projection_vars(variables, bindings)
                if missing_vars:
                    timing["unbound_projection_vars"] = ", ".join(missing_vars)
                    logger.info("unbound_projection_vars", vars=missing_vars, question=question)
                answer = await self._format_answer(bindings, explanation, missing_vars=missing_vars)
                t_reph = time.time()
                narrative_answer = await self._rephrase_via_openrouter(question, bindings)
                timing["rephrase_ms"] = round((time.time() - t_reph) * 1000, 1)
                timing["total_ms"] = round((time.time() - t0) * 1000, 1)
                timing["attempts"] = attempt + 1
                return NLResult(
                    answer=answer,
                    sparql=sparql,
                    explanation=explanation,
                    ontology=ontology,
                    narrative_answer=narrative_answer,
                    functions_invoked=functions_needed,
                    timing=timing,
                )
            except Exception as e:
                last_error = str(e)
                logger.warning("ask_attempt_failed", attempt=attempt, error=last_error, question=question)
                continue

        timing["total_ms"] = round((time.time() - t0) * 1000, 1)
        timing["attempts"] = max_attempts
        return NLResult(
            answer=f"Could not answer after {max_attempts} attempts. Last error: {last_error}",
            sparql=sparql,
            explanation=explanation,
            ontology=ontology,
            timing=timing,
        )

    # ------------------------------------------------------------- spatial path
    async def _try_spatial_fast_path(
        self,
        question: str,
        ontology: str,
        data_graph: str,
        timing: dict,
        t0: float,
    ) -> NLResult | None:
        """Answer a geo/proximity question directly from the spatio-temporal index.

        Returns an ``NLResult`` on success, or ``None`` to fall through to the
        normal SPARQL path — when the graph isn't a per-KG instance graph, the LLM
        doesn't return a servable spatial intent, the anchor can't be resolved, or
        anything errors. Never raises into :meth:`ask` (best-effort fast path).
        """
        scope = parse_kg_graph_uri(data_graph)
        if scope is None:
            return None  # index rows are scoped per (tenant, kg); can't route otherwise
        tenant_id, kg_name = scope
        try:
            ts = time.time()
            raw = await self._detect_spatial_intent(question, ontology)
            intent = parse_spatial_intent(raw) if raw else None
            timing["spatial_intent_ms"] = round((time.time() - ts) * 1000, 1)
            if intent is None:
                return None

            from cograph_client.spatiotemporal.registry import get_spatiotemporal_index

            index = get_spatiotemporal_index()

            # Temporal predicate: a single instant (as_of) wins over a window.
            as_of = _parse_iso_dt(intent.as_of)
            window = None
            if as_of is None and (intent.time_from or intent.time_to):
                window = (_parse_iso_dt(intent.time_from), _parse_iso_dt(intent.time_to))

            tq = time.time()
            if intent.kind == "radius":
                coords = await self._resolve_anchor_coords(intent.anchor, data_graph)
                if coords is None:
                    return None  # "near X" but X didn't resolve → fall through
                lon, lat = coords
                hits = await index.query_radius(
                    tenant_id, lon, lat, intent.radius_m,
                    kg_name=kg_name, time_window=window, as_of=as_of,
                )
            else:  # bbox
                min_lon, min_lat, max_lon, max_lat = intent.bbox
                hits = await index.query_bbox(
                    tenant_id, min_lon, min_lat, max_lon, max_lat,
                    kg_name=kg_name, time_window=window, as_of=as_of,
                )
            timing["spatial_index_ms"] = round((time.time() - tq) * 1000, 1)

            hits = filter_by_type(hits, intent.target_type)
            answer = format_spatial_answer(hits, intent)
            timing["spatial_routed"] = "true"
            timing["total_ms"] = round((time.time() - t0) * 1000, 1)
            return NLResult(
                answer=answer,
                sparql="",
                explanation="Answered from the spatio-temporal index (no SPARQL).",
                ontology=ontology,
                narrative_answer=answer,
                functions_invoked=[],
                timing=timing,
            )
        except Exception:
            logger.warning("spatial_fast_path_failed", exc_info=True)
            return None

    async def _detect_spatial_intent(self, question: str, ontology: str) -> dict | None:
        """LLM classify: is this a servable spatial lookup, and with what params?
        Returns the raw JSON dict (caller parses) or None on error."""
        user = (
            f"Question: {question}\n\n"
            f"Knowledge-graph types/attributes (for the target type, if any):\n"
            f"{ontology[:2000]}"
        )
        try:
            return await self._structured_llm(
                SPATIAL_INTENT_SYSTEM, user, "spatial_intent", SPATIAL_INTENT_SCHEMA
            )
        except Exception:
            logger.warning("spatial_intent_detect_failed", exc_info=True)
            return None

    async def _resolve_anchor_coords(self, anchor, data_graph: str):
        """Resolve a radius anchor to ``(lon, lat)``.

        Resolution ladder (first hit wins):
          1. explicit coordinates on the intent;
          2. a KG entity whose label matches ``entity_description`` AND carries a
             ``geo:wktLiteral`` (one scoped Neptune lookup) — preferred, since it
             pins the anchor to the tenant's own data;
          3. the free-text GEOCODER seam (ONTA-249): turn a bare place name
             ("Irvine") into coords via the registered geocoder — the OSS default
             is a deterministic offline gazetteer; a premium geocoder registers
             over it. This is what lets a place name resolve when no KG entity for
             it exists.

        Returns ``None`` when nothing resolves — the caller then falls through to
        the normal SPARQL path (byte-stable pre-existing behavior)."""
        if anchor is None:
            return None
        if anchor.has_coords():
            return (anchor.lon, anchor.lat)
        if not anchor.entity_description:
            return None
        # 2. KG-entity geometry (preferred — anchored to the tenant's own data).
        via_kg = await self._resolve_anchor_via_neptune(
            anchor.entity_description, data_graph
        )
        if via_kg is not None:
            return via_kg
        # 3. Free-text geocoder seam.
        return await self._geocode_anchor(anchor.entity_description)

    async def _geocode_anchor(self, description: str):
        """Resolve a free-text place name to ``(lon, lat)`` via the geocoder seam.

        Best-effort: returns ``None`` (never raises) when the place is unknown or
        the geocoder errors, so the caller falls through to the SPARQL path."""
        if not description or not description.strip():
            return None
        try:
            from cograph_client.spatiotemporal.geocoder import get_geocoder

            coords = await get_geocoder().geocode(description)
        except Exception:
            logger.warning("geocode_anchor_failed", exc_info=True)
            return None
        if (
            isinstance(coords, tuple)
            and len(coords) == 2
            and all(isinstance(c, (int, float)) for c in coords)
        ):
            lon, lat = float(coords[0]), float(coords[1])
            if -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
                return (lon, lat)
        return None

    async def _resolve_anchor_via_neptune(self, description: str, data_graph: str):
        """Find a KG entity whose label/text contains ``description`` AND that
        carries a ``geo:wktLiteral``; return that point's ``(lon, lat)`` or None.

        One scoped SELECT, LIMIT 1. The description is sanitized before it is
        interpolated into the FILTER literal."""
        desc = _sanitize_sparql_literal(description)
        for article in ("the ", "a ", "an "):
            if desc.startswith(article):
                desc = desc[len(article):]
        if not desc:
            return None
        q = (
            f"SELECT ?wkt FROM <{data_graph}> WHERE {{ "
            f"?e ?lp ?lbl . "
            f'FILTER(isLiteral(?lbl) && CONTAINS(LCASE(STR(?lbl)), "{desc}")) '
            f"?e ?gp ?wkt . "
            f"FILTER(datatype(?wkt) = <{_GEO_WKT_URI}>) "
            f"}} LIMIT 1"
        )
        try:
            raw = await self.neptune.query(q)
            _, rows = parse_sparql_results(raw)
        except Exception:
            logger.warning("anchor_resolve_failed", exc_info=True)
            return None
        if not rows:
            return None
        return _parse_point_wkt(rows[0].get("wkt", ""))

    async def _structured_llm(
        self, system: str, user: str, schema_name: str, schema: dict
    ) -> dict:
        """Provider-agnostic structured-JSON call for non-SPARQL classifiers (e.g.
        spatial-intent detection). Mirrors :meth:`_generate_sparql`'s provider
        selection but is a SEPARATE method on purpose — the SPARQL generators stay
        byte-identical so evals are unaffected."""
        if self._query_provider == "cerebras" and self._cerebras_key:
            endpoint = "https://api.cerebras.ai/v1/chat/completions"
            key, model = self._cerebras_key, self._query_model
        elif self._openrouter_key:
            endpoint = f"{OPENROUTER_BASE}/chat/completions"
            key, model = self._openrouter_key, self._query_model
        else:
            return await self._structured_via_anthropic(system, user, schema)
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                    },
                },
            )
            res.raise_for_status()
            text = res.json()["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = "\n".join(
                    l for l in text.split("\n") if not l.strip().startswith("```")
                )
            return json.loads(text)

    async def _structured_via_anthropic(self, system: str, user: str, schema: dict) -> dict:
        message = await self.anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        return json.loads(message.content[0].text)

    async def select_entity_uris(
        self,
        description: str,
        type_name: str,
        graph_uri: str,
        instance_graph: str | None = None,
        limit: int | None = None,
    ) -> list[str]:
        """Resolve an NL subset description to the IRIs of ``type_name`` entities.

        Turns a ranked/specific subset — e.g. "the 5 brokers with the most
        property listings" — into the concrete entity IRIs it names, so a caller
        (the agent's enrich planner) can enrich exactly those via ``entity_uris``
        instead of the whole type. Reuses the SAME NL→SPARQL generation +
        validation as :meth:`ask` (one query engine, no divergence); it only
        constrains the projection to the entity IRI (``?uri``) and extracts it.

        Returns a deduped, order-preserving list capped at ``limit``. Returns
        ``[]`` on any failure (unparseable/invalid SPARQL, Neptune error, or no
        IRI column) — never raises; the caller decides how to handle "couldn't
        resolve".
        """
        data_graph = instance_graph or graph_uri
        try:
            ontology = await self._fetch_ontology(graph_uri, data_graph)
        except Exception:
            logger.warning("select_entity_uris_ontology_failed", exc_info=True)
            return []
        cap = f" Return at most {int(limit)} rows." if limit else ""
        question = (
            f"Return ONLY the IRI of each {type_name} entity in this set: "
            f"{description}. The SELECT must project a single column named ?uri, "
            f"bound by `?uri a` the {type_name} class. Apply any ranking/ordering "
            f"and limit the set describes, but keep ?uri in the SELECT — do NOT "
            f"aggregate it away or replace it with a label.{cap}"
        )
        try:
            resp = await self._generate_sparql(question, ontology, data_graph)
            sparql = normalize_sparql(resp.get("sparql", ""))
            sparql = self._fix_attribute_uris(sparql, ontology)
            sparql = self._fix_common_sparql_issues(sparql, ontology)
            is_valid, error = validate_sparql(sparql)
            if not is_valid:
                logger.warning("select_entity_uris_invalid_sparql", error=error)
                return []
            raw = await self.neptune.query(sparql)
            _, bindings = parse_sparql_results(raw)
        except Exception:
            logger.warning("select_entity_uris_failed", exc_info=True)
            return []
        return self._entity_uris_from_bindings(bindings, limit)

    @staticmethod
    def _entity_uris_from_bindings(
        bindings: list[dict], limit: int | None = None
    ) -> list[str]:
        """Pull entity IRIs out of result bindings, order-preserving and deduped.

        Prefers the ``?uri`` column the resolver prompt asks for; if a row lacks
        it, falls back to the first http(s)-IRI value in that row. Caps at
        ``limit`` when given.
        """
        out: list[str] = []
        seen: set[str] = set()

        def _is_iri(v: object) -> bool:
            return isinstance(v, str) and v.startswith(("http://", "https://"))

        for row in bindings:
            val = row.get("uri")
            if not _is_iri(val):
                val = next((v for v in row.values() if _is_iri(v)), None)
            if val and val not in seen:
                seen.add(val)
                out.append(val)
                if limit and len(out) >= int(limit):
                    break
        return out

    async def _fetch_ontology(self, graph_uri: str, instance_graph: str | None = None) -> str:
        # Cache key includes instance graph so different KGs get filtered ontologies
        cache_key = f"{graph_uri}|{instance_graph or ''}"
        cached = _ontology_cache.get(cache_key)
        if cached and (time.time() - cached[1]) < ONTOLOGY_CACHE_TTL:
            return cached[0]

        from cograph_client.graph.ontology_queries import get_full_ontology_query, type_uri, attr_uri
        TYPE_URI_PREFIX = "https://cograph.tech/types/"
        try:
            # If querying a specific KG, find which types actually have instances
            active_types: set[str] | None = None
            if instance_graph and instance_graph != graph_uri:
                type_query = (
                    f"SELECT DISTINCT ?type FROM <{instance_graph}> "
                    f"WHERE {{ ?s <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> ?type }}"
                )
                type_raw = await self.neptune.query(type_query)
                _, type_bindings = parse_sparql_results(type_raw)
                active_types = set()
                for row in type_bindings:
                    t = row.get("type", "")
                    if t.startswith(TYPE_URI_PREFIX):
                        active_types.add(t[len(TYPE_URI_PREFIX):])

            raw = await self.neptune.query(get_full_ontology_query(graph_uri))
            _, bindings = parse_sparql_results(raw)

            types: dict[str, dict] = {}
            for row in bindings:
                tl = row.get("typeLabel", "")
                if not tl:
                    continue
                # NOTE: we no longer drop a declared type that is absent from
                # `active_types` here (ONTA-258). Every declared type is parsed
                # in; types with no instances in the queried KG are annotated
                # "[no instances]" during summary assembly below instead of being
                # hidden. See the empty-type handling after this loop.
                if tl not in types:
                    types[tl] = {"attributes": [], "relationships": [], "functions": set()}
                if row.get("attrLabel"):
                    attr_name = row["attrLabel"]
                    range_str = row.get("range", "")
                    if range_str.startswith(TYPE_URI_PREFIX):
                        target_type = range_str[len(TYPE_URI_PREFIX):]
                        # Relationship predicates use onto/ namespace in instance data
                        onto_uri = f"https://cograph.tech/onto/{attr_name}"
                        entry = f"{attr_name} → {target_type} — predicate URI: <{onto_uri}>"
                        if entry not in types[tl]["relationships"]:
                            types[tl]["relationships"].append(entry)
                    else:
                        dtype = range_str.split("#")[-1] if "#" in range_str else "string"
                        entry = f"{attr_name} ({dtype}) — URI: <{attr_uri(tl, attr_name)}>"
                        if entry not in types[tl]["attributes"]:
                            types[tl]["attributes"].append(entry)
                if row.get("funcName"):
                    types[tl]["functions"].add(row["funcName"])

            # A DECLARED type with no correctly-typed instances in the queried KG
            # is KEPT and annotated "[no instances]" — NOT dropped (ONTA-258).
            # This mirrors the ONTA-248 treatment of declared-but-empty
            # attributes/relationships further down. Hiding a declared type made
            # it indistinguishable from a nonexistent one, so the SPARQL-
            # generating LLM asserted "that type doesn't exist" (or silently
            # queried the closest wrong type) instead of returning an honest
            # zero-row answer. `active_types` still scopes which types carry
            # instance data — it no longer decides a declared type's VISIBILITY.
            empty_types: set[str] = (
                {tl for tl in types if tl not in active_types}
                if active_types is not None else set()
            )
            # Declared types that actually carry instances in this KG. When this
            # is zero we fall through to the SAME instance-graph fallback /
            # ONTOLOGY_EMPTY handling as before (ONTA-248): a schema that shares
            # NO type with the instance data is the "schema missing" case, and a
            # summary of only [no instances] types would be worse than the
            # instance-derived fallback.
            active_matched = len(types) - len(empty_types)

            if active_matched == 0:
                # No DECLARED type carries instances in this KG (the schema query
                # returned nothing, or nothing that overlaps the instance data).
                # When querying a SPECIFIC KG (distinct instance graph), that can
                # mean two very different things which look identical here:
                #  (a) instances exist but the base-graph schema hasn't been
                #      written yet (fresh ingest, schema-write lagging) — a basic
                #      "list all X" ask SHOULD still work, so fall back to the
                #      types present in the instance data and emit a distinct
                #      diagnostic instead of the misleading "No ontology" text.
                #  (b) the KG is genuinely empty — keep the original message.
                # We already know `active_types` (the instance-graph types)
                # from the probe above, so disambiguating adds NO extra query in
                # the empty case. Only attempt this for a distinct instance
                # graph; a bare tenant/ontology graph with no schema genuinely
                # has no ontology.
                if instance_graph and instance_graph != graph_uri and active_types:
                    fallback = await self._instance_graph_ontology_fallback(
                        graph_uri, instance_graph, active_types
                    )
                    if fallback is not None:
                        summary, has_instances = fallback
                        if has_instances:
                            logger.info(
                                "ontology_schema_missing_instances_present",
                                graph_uri=graph_uri,
                                instance_graph=instance_graph,
                                instance_types=len(active_types),
                            )
                            _ontology_cache[cache_key] = (summary, time.time())
                            return summary
                return ONTOLOGY_EMPTY

            # Discover enumerated values for low-cardinality string attributes.
            # Runs cardinality checks concurrently (asyncio.gather) instead of
            # serially, cutting ontology fetch from ~7s to ~500ms. Concurrency
            # is bounded by a semaphore (COG-58) so a wide table with hundreds
            # of attributes can't launch hundreds of simultaneous queries
            # against serverless Neptune — the count stays capped regardless of
            # column count.
            import asyncio
            MAX_ENUM_CARDINALITY = 25
            _enum_sem = asyncio.Semaphore(MAX_ENUM_DISCOVERY_CONCURRENCY)

            async def _gather_bounded(coros: list) -> list:
                """asyncio.gather, but each coroutine acquires the shared enum
                semaphore first so at most MAX_ENUM_DISCOVERY_CONCURRENCY run at
                once. Preserves return_exceptions semantics for callers."""
                async def _run(coro):
                    async with _enum_sem:
                        return await coro
                return await asyncio.gather(
                    *[_run(c) for c in coros], return_exceptions=True
                )
            enum_values: dict[str, dict[str, list[str]]] = {}
            enum_counts: dict[str, dict[str, int]] = {}
            empty_rels: set[tuple[str, str]] = set()
            if instance_graph:
                # Collect all attribute and relationship URIs for cardinality checks
                all_attrs: list[tuple[str, str, str]] = []  # (type_name, attr_name, uri)
                string_attrs: list[tuple[str, str, str]] = []  # string attrs only (for enum values)
                rel_uris: list[tuple[str, str, str]] = []  # (type_name, rel_name, onto_uri)
                for type_name, info in types.items():
                    # Empty declared types have zero instances by definition, so
                    # every cardinality COUNT would return 0 — skip the probes
                    # (no extra Neptune round-trips) and render their declared
                    # schema plainly under the type-level [no instances] mark.
                    if type_name in empty_types:
                        continue
                    for attr_entry in info["attributes"]:
                        a_name = attr_entry.split(" (")[0]
                        all_attrs.append((type_name, a_name, attr_uri(type_name, a_name)))
                        if "(string)" in attr_entry:
                            string_attrs.append((type_name, a_name, attr_uri(type_name, a_name)))
                    for rel_entry in info["relationships"]:
                        r_name = rel_entry.split(" →")[0].strip()
                        onto_uri = f"https://cograph.tech/onto/{r_name}"
                        rel_uris.append((type_name, r_name, onto_uri))

                # Define cardinality check function ONCE (used for both attrs and rels)
                async def _count_predicate(tn: str, an: str, uri: str) -> tuple[str, str, int]:
                    q = (
                        f"SELECT (COUNT(DISTINCT ?val) AS ?cnt) FROM <{instance_graph}> "
                        f"WHERE {{ ?s <{uri}> ?val }}"
                    )
                    raw = await self.neptune.query(q)
                    _, bindings = parse_sparql_results(raw)
                    cnt = int(bindings[0].get("cnt", 0)) if bindings else 0
                    return tn, an, cnt

                # Phase 1: Concurrent cardinality checks for ALL attributes
                if all_attrs:
                    try:
                        count_results = await _gather_bounded(
                            [_count_predicate(tn, an, uri) for tn, an, uri in all_attrs]
                        )

                        low_card_attrs: list[tuple[str, str, str]] = []
                        exceptions = sum(1 for r in count_results if isinstance(r, Exception))
                        if exceptions:
                            logger.warning("cardinality_check_exceptions", count=exceptions, total=len(count_results))
                        for result in count_results:
                            if isinstance(result, Exception):
                                continue
                            tn, an, cnt = result
                            enum_counts.setdefault(tn, {})[an] = cnt
                            if 0 < cnt <= MAX_ENUM_CARDINALITY:
                                low_card_attrs.append((tn, an, attr_uri(tn, an)))

                        # Phase 2: Concurrent value fetches for low-cardinality attrs
                        async def _fetch_vals(tn: str, an: str, uri: str) -> tuple[str, str, list[str]]:
                            q = (
                                f"SELECT DISTINCT ?val FROM <{instance_graph}> "
                                f"WHERE {{ ?s <{uri}> ?val }} LIMIT {MAX_ENUM_CARDINALITY}"
                            )
                            raw = await self.neptune.query(q)
                            _, bindings = parse_sparql_results(raw)
                            return tn, an, [r["val"] for r in bindings if r.get("val")]

                        if low_card_attrs:
                            val_results = await _gather_bounded(
                                [_fetch_vals(tn, an, uri) for tn, an, uri in low_card_attrs]
                            )
                            for result in val_results:
                                if isinstance(result, Exception):
                                    continue
                                tn, an, vals = result
                                if vals:
                                    enum_values.setdefault(tn, {})[an] = sorted(vals)
                    except Exception:
                        logger.warning("cardinality_attr_check_failed", exc_info=True)

                # Phase 3: Check relationship cardinality to annotate empty ones.
                # A CONFIRMED-empty relationship is annotated "[no instances]" but
                # NEVER removed (ONTA-248 determinism): a DECLARED relationship is
                # part of the schema, and dropping it on a cnt==0 — which a
                # transient throttle produces exactly like a genuinely-empty edge —
                # made a relationship appear then vanish across identical calls.
                empty_rels: set[tuple[str, str]] = set()  # (type_name, rel_name)
                if rel_uris:
                    try:
                        rel_counts = await _gather_bounded(
                            [_count_predicate(tn, rn, uri) for tn, rn, uri in rel_uris]
                        )
                        for result in rel_counts:
                            if isinstance(result, Exception):
                                continue
                            tn, rn, cnt = result
                            if cnt == 0:
                                empty_rels.add((tn, rn))
                    except Exception:
                        logger.warning("cardinality_rel_check_failed", exc_info=True)

            lines = []
            for type_name, info in types.items():
                # DECLARED-but-empty type: annotate at the type level (ONTA-258)
                # so the LLM writes a valid zero-row query with an honest
                # "declared but no instances" explanation instead of claiming the
                # type is absent or substituting a different type.
                empty_suffix = " [no instances]" if type_name in empty_types else ""
                lines.append(f"Type: {type_name} — URI: <{type_uri(type_name)}>{empty_suffix}")
                if info["attributes"]:
                    annotated = []
                    for attr_entry in sorted(info["attributes"]):
                        a_name = attr_entry.split(" (")[0]
                        if type_name in enum_values and a_name in enum_values[type_name]:
                            # Low-cardinality: show actual values
                            vals = enum_values[type_name][a_name]
                            val_str = ", ".join(f'"{v}"' for v in vals[:10])
                            if len(vals) > 10:
                                val_str += f", ... ({len(vals)} total)"
                            annotated.append(f"{attr_entry} [values: {val_str}]")
                        elif type_name in enum_counts and a_name in enum_counts[type_name]:
                            cnt = enum_counts[type_name][a_name]
                            if cnt == 0:
                                # DECLARED attribute with zero instances. Keep it
                                # (do NOT drop) — dropping made the schema the LLM
                                # sees NON-DETERMINISTIC (ONTA-248): a transient
                                # Neptune throttle returns an empty COUNT result
                                # (cnt=0) exactly like a genuinely-empty attribute,
                                # so the attribute flickered in and out of the
                                # summary between otherwise-identical calls. The
                                # attribute is DECLARED in the ontology, so it
                                # exists; annotate it as empty rather than deleting
                                # it, so an existence claim stays stable.
                                annotated.append(f"{attr_entry} [no instances]")
                            elif cnt > MAX_ENUM_CARDINALITY:
                                # High-cardinality: just show the count
                                annotated.append(f"{attr_entry} [{cnt} unique values]")
                            else:
                                annotated.append(attr_entry)
                        else:
                            annotated.append(attr_entry)
                    lines.append(f"  Attributes: {', '.join(annotated)}")
                if info["relationships"]:
                    # Keep EVERY declared relationship; annotate confirmed-empty
                    # ones instead of hiding them (ONTA-248 determinism).
                    annotated_rels = []
                    for r in sorted(info["relationships"]):
                        if (type_name, r.split(" →")[0].strip()) in empty_rels:
                            annotated_rels.append(f"{r} [no instances]")
                        else:
                            annotated_rels.append(r)
                    lines.append(f"  Relationships: {', '.join(annotated_rels)}")
                if info["functions"]:
                    lines.append(f"  Functions: {', '.join(sorted(info['functions']))}")
            summary = "\n".join(lines)
            # Log types that made it into the summary
            types_in_summary = [l.split("—")[0].replace("Type:", "").strip() for l in lines if l.startswith("Type:")]
            logger.info("ontology_summary_built", types_shown=len(types_in_summary),
                        types_active=len(active_types) if active_types else "all",
                        types_with_attrs=len(types),
                        types_empty=len(empty_types),
                        names=types_in_summary[:10])

            # Cache it
            _ontology_cache[cache_key] = (summary, time.time())
            return summary
        except Exception:
            logger.error("ontology_fetch_failed", exc_info=True)
            # Distinct from the empty-graph message: a transient fetch failure must
            # NOT be reported to the LLM as "graph is empty" (ONTA-248 A2).
            return ONTOLOGY_FETCH_ERROR

    async def _instance_graph_ontology_fallback(
        self,
        graph_uri: str,
        instance_graph: str | None,
        active_types: set[str] | None,
    ) -> tuple[str, bool] | None:
        """Build a minimal ontology summary from INSTANCE data when the schema is missing.

        Called only when the base-graph schema query yields zero types. Probes
        the instance graph directly for the types actually present and the
        predicates used on them, so a freshly-ingested KG whose schema hasn't
        been written yet can still answer a basic "list all X" query instead of
        returning the misleading "No ontology defined yet."

        Returns:
          * ``(summary, True)``  — instances exist; `summary` is a minimal
            ontology built from instance types/predicates, prefixed with a
            diagnostic telling the caller the schema isn't available yet.
          * ``(None-sentinel, False)`` i.e. ``("", False)`` — no instances found;
            caller keeps the original "No ontology defined yet." message.
          * ``None`` — probing failed; caller falls back to the default message.

        Best-effort: any error returns ``None`` so /ask never breaks on it.
        """
        target_graph = instance_graph or graph_uri
        TYPE_URI_PREFIX = "https://cograph.tech/types/"
        from cograph_client.graph.ontology_queries import type_uri, attr_uri

        try:
            # Reuse types already discovered upstream when available; otherwise
            # probe the instance graph now.
            type_leaves: set[str] = set(active_types) if active_types else set()
            if not type_leaves:
                type_query = (
                    f"SELECT DISTINCT ?type FROM <{target_graph}> "
                    f"WHERE {{ ?s <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> ?type }}"
                )
                _, type_bindings = parse_sparql_results(await self.neptune.query(type_query))
                for row in type_bindings:
                    t = row.get("type", "")
                    if t.startswith(TYPE_URI_PREFIX):
                        type_leaves.add(t[len(TYPE_URI_PREFIX):])

            if not type_leaves:
                # Genuinely empty — no instances either. Signal "no instances".
                return "", False

            # Collect the predicates actually used on each type's instances so
            # the LLM has concrete URIs to query, even without a schema. Bounded
            # per-type; failures per type are non-fatal.
            lines = [
                "NOTE: The ontology schema for this graph has not been written "
                "yet, but instance data is present. The types and predicates "
                "below were read directly from the instance data. For the full "
                "curated ontology once available, use view_ontology.",
                "",
            ]
            for leaf in sorted(type_leaves):
                lines.append(f"Type: {leaf} — URI: <{type_uri(leaf)}>")
                try:
                    pred_query = (
                        f"SELECT DISTINCT ?p FROM <{target_graph}> WHERE {{ "
                        f"?s <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
                        f"<{type_uri(leaf)}> . ?s ?p ?o }} LIMIT 100"
                    )
                    _, pred_bindings = parse_sparql_results(await self.neptune.query(pred_query))
                except Exception:
                    pred_bindings = []
                attrs: list[str] = []
                rels: list[str] = []
                for row in pred_bindings:
                    p = row.get("p", "")
                    if p.startswith(f"{TYPE_URI_PREFIX}{leaf}/attrs/"):
                        a_name = p.rsplit("/", 1)[-1]
                        attrs.append(f"{a_name} — URI: <{attr_uri(leaf, a_name)}>")
                    elif p.startswith("https://cograph.tech/onto/"):
                        r_name = p.rsplit("/", 1)[-1]
                        rels.append(f"{r_name} — predicate URI: <{p}>")
                if attrs:
                    lines.append(f"  Attributes: {', '.join(sorted(set(attrs)))}")
                if rels:
                    lines.append(f"  Relationships: {', '.join(sorted(set(rels)))}")

            return "\n".join(lines), True
        except Exception:
            logger.warning("instance_graph_ontology_fallback_failed", exc_info=True)
            return None

    @staticmethod
    def _fix_attribute_uris(sparql: str, ontology_summary: str) -> str:
        """Fix incorrect URIs in generated SPARQL using the ontology as ground truth.

        This is the post-processing safety net (Fix B). It catches URI mistakes
        the LLM makes despite the prompt telling it to copy-paste exact URIs.

        Strategy:
        1. Extract ALL valid URIs from the ontology summary (attributes + relationships)
        2. Find ALL cograph.tech URIs in the SPARQL
        3. For each URI not in the valid set, fuzzy-match against valid URIs
        4. Replace with the best match if similarity is high enough

        Common mistakes this catches:
        - <https://cograph.tech/bedrooms> → <https://cograph.tech/types/Property/attrs/bedrooms>
        - <https://cograph.tech/onto/bedrooms> → <https://cograph.tech/types/Property/attrs/bedrooms>
        - <https://cograph.tech/types/Property/attrs/property_type> → .../attrs/home_type
        - <https://cograph.tech/Property> → <https://cograph.tech/types/Property>
        """
        import re
        from difflib import SequenceMatcher

        # Step 1: Build the set of ALL valid URIs from the ontology
        valid_uris: dict[str, str] = {}  # name → full URI

        # Attribute URIs: "attr_name (type) — URI: <https://cograph.tech/types/Type/attrs/attr_name>"
        for match in re.finditer(r"URI: <(https://cograph\.tech/types/(\w+)/attrs/(\w+))>", ontology_summary):
            full_uri = match.group(1)
            attr_name = match.group(3)
            valid_uris[attr_name] = full_uri
            # Also index by type/attr for disambiguation
            valid_uris[f"{match.group(2)}/{attr_name}"] = full_uri

        # Relationship URIs: "predicate URI: <https://cograph.tech/onto/pred_name>"
        for match in re.finditer(r"predicate URI: <(https://cograph\.tech/onto/(\w+))>", ontology_summary):
            full_uri = match.group(1)
            pred_name = match.group(2)
            valid_uris[pred_name] = full_uri

        # Type URIs: "Type: TypeName — URI: <https://cograph.tech/types/TypeName>"
        for match in re.finditer(r"URI: <(https://cograph\.tech/types/(\w+))>", ontology_summary):
            full_uri = match.group(1)
            type_name = match.group(2)
            if "/attrs/" not in full_uri:  # don't overwrite attr URIs
                valid_uris[type_name] = full_uri

        valid_uri_set = set(valid_uris.values())

        # Step 2: Find and fix all cograph.tech URIs in the SPARQL
        def _fix_uri(m: re.Match) -> str:
            uri = m.group(1)

            # Already valid? Keep it.
            if uri in valid_uri_set:
                return m.group(0)

            # Skip known system URIs
            if any(uri.startswith(f"https://cograph.tech/{p}") for p in ("graphs/", "entities/", "functions/", "kgs/")):
                return m.group(0)

            # Extract the "name" part from the URI for matching
            # e.g., "https://cograph.tech/bedrooms" → "bedrooms"
            # e.g., "https://cograph.tech/onto/listed_by" → "listed_by"
            # e.g., "https://cograph.tech/types/Property/attrs/property_type" → "property_type"
            parts = uri.replace("https://cograph.tech/", "").rstrip("/").split("/")
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

        return re.sub(r"<(https://cograph\.tech/[^>]+)>", _fix_uri, sparql)

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
            r'(\?\w+)\s+a\s+(<https://cograph\.tech/)',
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
        overview_pattern = r'<https://cograph\.tech/types/Movie/attrs/overview>'
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
        from cograph_client.graph.ontology_queries import rewrite_type_predicate_to_closure
        sparql = rewrite_type_predicate_to_closure(sparql)

        # Fix 5: resolve attribute aliases (ADR 0002 §7) — a renamed attribute
        # keeps answering through its alias until backfill retires it. A None
        # or empty map (the default) leaves the query untouched.
        if alias_map:
            from cograph_client.graph.aliases import rewrite_query_attrs
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
        from cograph_client.graph.aliases import fetch_alias_map
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
        # Invalidate embeddings
        svc = get_embedding_service()
        if svc:
            svc.invalidate(graph_uri)

    async def _rephrase_via_openrouter(self, question: str, bindings: list[dict], max_rows: int | None = None) -> str:
        """Generate a 2-3 sentence narrative summary of SPARQL result bindings.

        ``max_rows`` bounds how many rows are fed to the narrative LLM (a
        deliberate sample, not the full answer — the plain-text answer in
        ``_format_answer`` carries all rows up to ANSWER_ROW_CAP). Defaults to
        OMNIX_REPHRASE_MAX_ROWS (30) so a wide result can't blow the summarizer's
        context; the truncation is already stated to the model. Now that
        generated SELECTs get a deterministic ORDER BY, this sample is stable
        across runs instead of an arbitrary slice.

        Uses Llama 3.1 8B on Cerebras (via OpenRouter) for fast, cheap rephrase.
        Fails open: returns "" on any error so the main response is never broken.
        """
        if not self._openrouter_key:
            return ""

        if max_rows is None:
            max_rows = int(os.environ.get("OMNIX_REPHRASE_MAX_ROWS", "30"))

        # Same hygiene as _format_answer: never feed internal/housekeeping
        # predicate rows (er/*, onto/norm/*, onto/batch_id, …) to the narrative
        # summarizer, or it would describe ER plumbing as business facts.
        bindings = _drop_internal_predicate_rows(bindings)

        try:
            # Build a compact tabular string from bindings
            if not bindings:
                table_str = "(no results)"
                truncation_note = ""
            else:
                rows = bindings[:max_rows]
                if rows:
                    cols = list(rows[0].keys())
                    lines = ["\t".join(cols)]
                    for row in rows:
                        lines.append("\t".join(str(row.get(c, "")) for c in cols))
                    table_str = "\n".join(lines)
                else:
                    table_str = "(no results)"
                truncation_note = (
                    f"\n(Showing {len(rows)} of {len(bindings)} total rows.)"
                    if len(bindings) > max_rows else ""
                )

            system_prompt = (
                "You are an analyst summarizing a database query result. Rules:\n"
                "- Lead with the specific count (e.g. 'Eleven founders match.').\n"
                "- If multiple rows share similar values, find the ONE row that stands out — "
                "different company, different prior company, or different category. "
                "Use that outlier as your hero example with its exact column values.\n"
                "- Keep to 2-3 sentences, max 80 words.\n"
                "- ONLY state facts visible in the rows. Never mix values from different rows.\n"
                "- Trust the row values as literal, authoritative facts. If a column has a value, "
                "that IS the answer for that column — never describe a present value as "
                "'unknown' or 'incomplete' just because it's a short code.\n"
                "- SEC filing type codes are canonical form names (e.g. D means Form D, "
                "10-K means annual report, 10-Q means quarterly, 8-K means material event, "
                "S-1 means IPO registration). State the code as-is — prefixing with 'Form' "
                "is fine; calling it unknown is not.\n"
                "- Do NOT use chatbot phrases like 'Sure!', 'Here you go', 'Great question'.\n"
                "- If the result is empty, say 'No matches found.' and stop.\n"
                "- Speak in plain English, not technical jargon."
            )

            user_prompt = (
                f"Question: {question}\n\n"
                f"Result ({len(bindings)} row{'s' if len(bindings) != 1 else ''}):\n"
                f"{table_str}{truncation_note}\n\n"
                "Summarize this result in 2-3 sentences."
            )

            t_rephrase = time.time()
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.post(
                    f"{OPENROUTER_BASE}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._openrouter_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "meta-llama/llama-3.1-8b-instruct",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "max_tokens": 300,
                        "temperature": 0.2,
                        "provider": {
                            "order": ["Cerebras", "Groq", "Nebius"],
                            "allow_fallbacks": True,
                        },
                    },
                )
                res.raise_for_status()
                data = res.json()
                narrative = data["choices"][0]["message"]["content"].strip()
            rephrase_ms = round((time.time() - t_rephrase) * 1000, 1)
            logger.info("narrative_rephrase_ok", rephrase_ms=rephrase_ms, rows=len(bindings))
            return narrative
        except Exception:
            logger.warning("narrative_rephrase_failed", exc_info=True)
            return ""

    async def _generate_sparql(self, question: str, ontology: str, graph_uri: str = "", error_feedback: str = "", examples_text: str = "") -> dict:
        prompt = build_generation_prompt(question, ontology, graph_uri, examples_text=examples_text)
        if error_feedback:
            prompt += f"\n\n{error_feedback}"

        if self._query_provider == "cerebras" and self._cerebras_key:
            return await self._generate_via_cerebras(prompt)
        if self._query_provider == "openrouter" and self._openrouter_key:
            return await self._generate_via_openrouter(prompt)
        if self._openrouter_key:
            return await self._generate_via_openrouter(prompt)
        return await self._generate_via_anthropic(prompt)

    async def _generate_via_cerebras(self, prompt: str) -> dict:
        """Generate SPARQL via Cerebras with structured output."""
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                "https://api.cerebras.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._cerebras_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._query_model,
                    "messages": [
                        {"role": "system", "content": SPARQL_GENERATION_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "max_completion_tokens": 512,
                    "temperature": 0,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "sparql_response",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "sparql": {"type": "string"},
                                    "explanation": {"type": "string"},
                                    "functions_needed": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["sparql", "explanation", "functions_needed"],
                                "additionalProperties": False,
                            },
                        },
                    },
                },
            )
            res.raise_for_status()
            data = res.json()
            return json.loads(data["choices"][0]["message"]["content"])

    async def _generate_via_openrouter(self, prompt: str) -> dict:
        """Generate SPARQL via OpenRouter (OpenAI-compatible API)."""
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._openrouter_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._query_model,
                    "models": model_chain(self._query_model),
                    "messages": [
                        {"role": "system", "content": SPARQL_GENERATION_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 1024,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "sparql_response",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "sparql": {"type": "string"},
                                    "explanation": {"type": "string"},
                                    "functions_needed": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["sparql", "explanation", "functions_needed"],
                                "additionalProperties": False,
                            },
                        },
                    },
                },
            )
            res.raise_for_status()
            data = res.json()
            text = data["choices"][0]["message"]["content"]
            # Strip code fences if present
            stripped = text.strip()
            if stripped.startswith("```"):
                lines = [l for l in stripped.split("\n") if not l.strip().startswith("```")]
                stripped = "\n".join(lines)
            return json.loads(stripped)

    async def _generate_via_anthropic(self, prompt: str) -> dict:
        """Fallback: generate SPARQL via Anthropic API."""
        message = await self.anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SPARQL_GENERATION_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "sparql": {"type": "string", "description": "The SPARQL SELECT query"},
                            "explanation": {"type": "string", "description": "Brief explanation of what the query does"},
                            "functions_needed": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of function names if computation is needed",
                            },
                        },
                        "required": ["sparql", "explanation", "functions_needed"],
                        "additionalProperties": False,
                    },
                },
            },
        )
        return json.loads(message.content[0].text)

    @staticmethod
    def _humanize_uri(uri: str) -> str:
        """Extract a human-readable name from an Omnix URI.

        Examples:
            https://cograph.tech/entities/Movie/12345 → 12345
            https://cograph.tech/types/Movie → Movie
            https://cograph.tech/entities/ConsumerComplaint/1431838 → 1431838
        """
        from urllib.parse import unquote
        path = unquote(uri.replace("https://cograph.tech/", ""))
        return path.split("/")[-1]

    async def _resolve_uri_labels(self, bindings: list[dict]) -> dict[str, str]:
        """Batch-resolve rdfs:label for all Omnix entity/type URIs in bindings.

        Returns a mapping from URI → human-readable label.
        Falls back to extracting the last URI path segment if no label is found.
        """
        # Collect all unique URIs that look like Omnix entities or types
        uris: set[str] = set()
        for row in bindings:
            for v in row.values():
                if isinstance(v, str) and (
                    v.startswith("https://cograph.tech/entities/")
                    or v.startswith("https://cograph.tech/types/")
                ):
                    uris.add(v)

        if not uris:
            return {}

        resolved: dict[str, str] = {}

        # Batch SPARQL query to fetch rdfs:label for all URIs at once
        values_clause = " ".join(f"<{u}>" for u in uris)
        label_query = (
            f"SELECT ?uri ?label WHERE {{ "
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
        uri_labels = await self._resolve_uri_labels(bindings)

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
