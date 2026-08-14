"""Shared NL pipeline helpers and process-scoped caches.

Invariant (other agents): never drop THIS-KG populated types from planning
context; money-leaf hard-bind is unique-resolve only.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

from infona_client.graph.iri import ENTITY_URI_PREFIX, IRI_BASE
from infona_client.nlp.pipeline_llm import ANSWER_ROW_CAP  # noqa: F401 — mixin re-export

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

