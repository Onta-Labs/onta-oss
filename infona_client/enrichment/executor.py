"""Async executor for enrichment jobs.

Reads entities from Neptune, runs them through the source funnel
(lite tier = wikidata, with cache), and either stages results for
review or applies them directly based on conflict_policy.
"""


from __future__ import annotations

from infona_client.graph.iri import ENTITY_URI_PREFIX, IRI_BASE, ONTO_PRED_PREFIX, TYPE_URI_PREFIX
import asyncio
import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Optional

import structlog

from infona_client.analytics import distinct_id_for, emit
from infona_client.api_registry.enrichment import apply_registry_selection
from infona_client.api_registry.spec import AuthorityLevel
from infona_client.enrichment.cache import EnrichmentCache
from infona_client.enrichment.canonicalize import apply_canonicalizer
from infona_client.enrichment.job_store import JobStore
from infona_client.enrichment.models import (
    ConflictPolicy,
    ConflictReview,
    EnrichJob,
    EnrichScope,
    JobErrorItem,
    JobStatus,
    ProviderLog,
    RowResult,
    Verdict,
)
from infona_client.config import settings
from infona_client.pipeline.manifest import (
    HaltReasonKind,
    RunManifest,
    resolve_spend_ceiling,
)
from infona_client.pipeline.stage_trace import (
    stamp_enrichment_entities_selected,
    stamp_enrichment_run_cancelled,
    stamp_enrichment_run_failed,
    stamp_enrichment_run_finished,
    stamp_enrichment_run_started,
    stamp_enrichment_write_phase,
)
from infona_client.retrieval.cost import source_cost
from infona_client.enrichment.sources.base import (
    SourceAdapter,
    get_adapter,
    register_adapter,
)
from infona_client.enrichment.strategy import (
    AttributeStrategy,
    TypeStrategy,
    load_strategy,
    resolve_type_name,
    unknown_type_message,
)
from infona_client.enrichment.extraction import coerce_url_attribute_value
from infona_client.enrichment.tiers import get_chain
from infona_client.graph.client import NeptuneClient
from infona_client.graph.kg_writer import (
    delete_facts,
    insert_facts,
    refresh_after_write,
)
from infona_client.graph.store import resolve_optional_graph_store
from infona_client.graph.ontology_commit import commit_ontology
from infona_client.graph.ontology_queries import (
    PRIMITIVE_TYPES,
    entity_uri as _entity_uri,
)
from infona_client.models.ontology import OntologyMutation, OntologyOpKind
from infona_client.graph.provenance import (
    attr_provenance_companion_uri,
    build_attribute_provenance_companions,
    build_provenance_triples,
    legacy_attr_companion_uri,
)
from infona_client.graph.queries import (
    kg_graph_uri,
    tenant_graph_uri,
)
from infona_client.graph.suppression import is_suppressed
from infona_client.pipeline.mutations import (
    DEFAULT_RECENCY_POLICY,
    write_with_conflict_resolution,
)
from infona_client.normalization.clean import clean_value
from infona_client.resolver.models import CleanReport, ValidatedTriple
from infona_client.resolver.validator import _to_wkt_point, validate_triple

logger = structlog.stdlib.get_logger("infona.enrichment")


RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDF_PROPERTY = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"
RDFS_DOMAIN = "http://www.w3.org/2000/01/rdf-schema#domain"
# Relationship instance triples use the `…/onto/<predName>` namespace (minted in
# nlp/pipeline.py + resolver/schema_resolver.py); literal-attribute instance
# triples use the `…/types/<Type>/attrs/<name>` (attr_uri) namespace. A scope
# predicate's ontology declaration doesn't tell us which the data uses, so a
# resolved local-name maps to BOTH candidate instance IRIs. NOTE: the name the
# Explorer DISPLAYS comes from the entity's `rdfs:label` (set at ingest), which
# may differ from — or exist WITHOUT — an `…/attrs/name` literal, so a scope on a
# name/title VALUE must also match the Entity display name.
ONTO_PRED_PREFIX = f"{IRI_BASE}/onto/"
NAME_FALLBACK_ATTRS = ["name", "title", "headline"]
WORKER_POOL_SIZE = 8
PROGRESS_FLUSH_EVERY = 10

# rdfs:comment stamped on an enrichment-declared attribute so the ontology /schema
# view + Explorer can distinguish a schema slot that arrived via enrichment from
# one declared by ingest or the ontology endpoint.
ENRICH_ATTR_DESCRIPTION = "Added by enrichment job"
# Default declared range when a brand-new enriched attribute carries no values we
# can type (empty / non-numeric). The actual range is INFERRED per-attribute from
# the applied values (_infer_datatype_from_values) and, for an attribute already
# declared with a richer range by ingestion, the existing range is PRESERVED
# rather than downgraded (see _declare_attributes) — so this is only the floor.
ENRICH_ATTR_DATATYPE = "string"

# Default source-authority level for a machine refresh/scrape (ONTA-279). A
# refreshed value is routed through the P6 write-time conflict policy
# (write_with_conflict_resolution), which ranks it against the existing current
# value on the ONE shared authority scale. A generic scrape carries no explicit
# authority, so it defaults HERE to a strong-but-NOT-top machine level: it beats an
# unannotated legacy value, ties (→ recency decides) with another source_of_truth
# scrape, and — the load-bearing property — LOSES to a human ``user_assertion``
# correction (rank 0), which is exactly what keeps a user fix from being clobbered
# by a refresh (completing ONTA-281's e2e). NEVER user_assertion: that top level is
# minted only by the human-correction write path (pipeline/corrections.py).
REFRESH_AUTHORITY = AuthorityLevel.source_of_truth

# Hard ceiling on a single adapter lookup (COG-112). A misbehaving adapter — a
# stalled TCP/TLS connect, a server that dribbles keepalive bytes (which resets
# httpx's per-byte read timeout forever), or any adapter whose own client lacks a
# timeout — must NEVER hang the whole job. Without this wrapper a single
# `await adapter.lookup(...)` that never returns AND never raises strands the job
# in `running` with zero logs and no failure (the exact production symptom: logs
# stop right after the scoped SELECT, no outbound adapter HTTP, no
# enrichment_job_failed). This bound makes such a stall surface as a visible,
# retryable `enrichment_adapter_timeout` log instead (verdicts=[] → the chain
# moves on and the job completes). Generous enough to cover a slow
# multi-step adapter (e.g. wikidata's search→claims→label round-trips) while
# still failing fast relative to "forever". Overridable via the
# INFONA_ADAPTER_LOOKUP_TIMEOUT_S env var.
ADAPTER_LOOKUP_TIMEOUT_S = float(os.environ.get("INFONA_ADAPTER_LOOKUP_TIMEOUT_S", "30"))

# Cap stored per-provider error/summary messages so a chatty adapter exception
# can't bloat the job payload (it is serialized whole into the job store).
_MAX_ERROR_MSG = 300


class _ProviderTally:
    """Accumulates per-provider outcomes across a single enrichment run so the
    job can carry a ``provider_logs`` (what each provider we used did) and an
    ``error_summary`` (the potential errors, aggregated) for the run-detail view.

    Concurrency: the executor's worker pool runs cooperatively under one event
    loop and every ``record*`` mutation is synchronous (no ``await`` between read
    and write), so the plain counters here are race-free — the same property the
    existing ``job.progress`` increments rely on. No lock needed.
    """

    def __init__(self) -> None:
        self._by_provider: dict[str, ProviderLog] = {}
        # (provider, kind) -> [count, first_sample_message]
        self._errors: dict[tuple[str, str], list] = {}

    def _log(self, provider: str) -> ProviderLog:
        pl = self._by_provider.get(provider)
        if pl is None:
            pl = ProviderLog(provider=provider)
            self._by_provider[provider] = pl
        return pl

    def _bump_error(self, provider: str, kind: str, message: str) -> None:
        key = (provider, kind)
        rec = self._errors.get(key)
        if rec is None:
            self._errors[key] = [1, (message or "")[:_MAX_ERROR_MSG]]
        else:
            rec[0] += 1  # keep the first representative message

    def record_missing(self, provider: str) -> None:
        """A chain named a provider that isn't registered here (call once per
        provider per job — the caller already gates on a 'missing' set)."""
        self._log(provider).status = "skipped"
        self._bump_error(
            provider,
            "missing",
            f"provider '{provider}' is not registered on this deployment",
        )

    def record_attempt(
        self,
        provider: str,
        *,
        cache_hit: bool,
        outcome: str,  # "match" | "no_match" | "timeout" | "error"
        error_msg: Optional[str] = None,
    ) -> None:
        pl = self._log(provider)
        if cache_hit:
            pl.cache_hits += 1
        else:
            pl.attempts += 1
        if outcome == "match":
            pl.matches += 1
        elif outcome == "no_match":
            pl.no_match += 1
        elif outcome == "timeout":
            pl.timeouts += 1
            pl.last_error = (error_msg or "lookup timed out")[:_MAX_ERROR_MSG]
            self._bump_error(provider, "timeout", error_msg or "lookup timed out")
        elif outcome == "error":
            pl.errors += 1
            if error_msg:
                pl.last_error = error_msg[:_MAX_ERROR_MSG]
            self._bump_error(provider, "error", error_msg or "lookup failed")

    def to_logs(self) -> list[ProviderLog]:
        out: list[ProviderLog] = []
        for pl in self._by_provider.values():
            if pl.status != "skipped":
                if pl.matches > 0:
                    pl.status = "ok"
                elif pl.errors > 0 or pl.timeouts > 0:
                    pl.status = "error"
                else:
                    pl.status = "no_match"
            out.append(pl)
        return out

    def to_error_summary(self) -> list[JobErrorItem]:
        items = [
            JobErrorItem(provider=prov, kind=kind, message=msg, count=count)  # type: ignore[arg-type]
            for (prov, kind), (count, msg) in self._errors.items()
        ]
        items.sort(key=lambda e: e.count, reverse=True)
        return items


def _type_uri(type_name: str) -> str:
    return f"{TYPE_URI_PREFIX}{type_name}"


def _attr_uri(type_name: str, attr: str) -> str:
    return f"{TYPE_URI_PREFIX}{type_name}/attrs/{attr}"


def _strategy_version_with_instructions(
    strategy_version: str, instructions: Optional[str]
) -> str:
    """Fold optional ``instructions`` into the cache ``strategy_version`` string.

    Custom instructions can change what an agentic adapter returns, so two
    different instruction sets must NOT collide on a cached verdict. Rather than
    widen the cache key tuple (and every call site), we append a short stable
    hash of the instructions to ``strategy_version`` — a different instructions
    string yields a different key (clean miss), the same string reuses the
    cached verdict, and the no-instructions path is BYTE-FOR-BYTE the old
    ``strategy_version`` (so existing caches/keys are unchanged)."""
    if not instructions:
        return strategy_version
    digest = hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:12]
    return f"{strategy_version}+instr:{digest}"


# A well-formed http(s) IRI with none of the characters that could break out of
# a SPARQL ``<…>`` term (``<``, ``>``, ``"``, ``{``, ``}``, whitespace). The
# Pydantic validators on the request models reject bad input at the API
# boundary; this is the executor-level backstop so a malformed URI can never be
# spliced into a VALUES block (defense in depth — SPARQL injection fix #1).
_IRI_RE = re.compile(r'^https?://[^\s<>"{}]+$')


def _validate_entity_uris(entity_uris: list[str]) -> list[str]:
    """Return ``entity_uris`` unchanged, or raise ``ValueError`` if any entry is
    not a safe http(s) IRI (no ``<>"{}`` or whitespace)."""
    for u in entity_uris:
        if not isinstance(u, str) or not _IRI_RE.match(u):
            raise ValueError(f"invalid entity URI for scoped enrichment: {u!r}")
    return entity_uris


def _local_name(uri_or_value: str) -> str:
    """Last path / fragment segment of a URI; the value itself if not a URI."""
    s = uri_or_value.rstrip("/")
    if "#" in s:
        s = s.split("#")[-1]
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    return s


def _is_int(v: str) -> bool:
    """True if ``v`` parses as a plain int (optional leading sign only).

    Mirrors agent/capabilities/web_ingest_cap.py's helper — kept as a small local
    copy rather than imported so the enrichment layer takes no dependency on the
    agent layer.

    This MUST agree with the write-side validator (``resolver.validator``): its
    ``validate_value`` accepts integers as ``^-?\\d+$`` and ``coerce_value`` does
    ``int(float(v))`` — neither strips thousands separators. So we reject ``,`` and
    ``_`` groupings here too. If the inference layer declared ``xsd:integer`` for a
    comma-grouped value the validator would then REJECT (drop) it at write time, so
    a column like ``"1,234"`` must declare ``string`` and keep the value as a
    visible string literal rather than vanish."""
    if not isinstance(v, str) or "_" in v or "," in v:
        return False
    try:
        int(v)
        return True
    except (ValueError, AttributeError):
        return False


def _is_float(v: str) -> bool:
    """True if ``v`` parses as a finite float (optional leading sign only).

    Like :func:`_is_int`, this MUST agree with the write-side validator, which does
    not strip thousands separators — so we reject ``,`` and ``_`` groupings (else a
    comma value would be declared numeric and then dropped at write). Python's
    ``float()`` also parses the special tokens ``inf``/``-inf``/``infinity``/``nan``,
    none of which are real numeric data, so we reject those too. Ordinary decimals
    and scientific notation of real numbers (``8.5``, ``1e10``) still parse True."""
    if not isinstance(v, str) or "_" in v or "," in v:
        return False
    # Reject the non-finite special tokens float() accepts (inf/-inf/infinity/nan).
    cleaned = v.strip().lstrip("+-").lower()
    if cleaned in ("inf", "infinity", "nan"):
        return False
    try:
        f = float(v)
    except (ValueError, AttributeError):
        return False
    # Belt-and-suspenders: any non-finite result (should already be caught above)
    # is not a real float value.
    import math

    return math.isfinite(f)




def _is_iso_datetime(v: str) -> bool:
    """True if ``v`` parses as an ISO-8601 date or datetime via
    :meth:`datetime.fromisoformat`.

    Accepts plain dates (``2026-06-28``), datetimes (``2026-06-28T21:24:50``),
    and timezone-aware forms (``…+00:00`` and a trailing ``Z``, which Python's
    pre-3.11 ``fromisoformat`` rejects, so we normalise ``Z`` to ``+00:00``
    first). A bare integer like ``2026`` is deliberately NOT a date here — the
    caller only reaches this helper for values that already failed int/float and
    contain a date separator, so an all-integer column can never be misread as a
    date."""
    if not isinstance(v, str):
        return False
    s = v.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        datetime.fromisoformat(s)
        return True
    except ValueError:
        return False


def _entity_iri_type(value: str) -> str | None:
    """Parse the ``<TypeName>`` out of a canonical entity IRI of the form
    ``https://graph.infona.ai/entities/<TypeName>/<id>``, else None.

    Returns the bare type name (e.g. ``Manufacturer``) so the caller can decide
    whether a column of entity IRIs is a relationship to a single target type.
    Returns None for anything that is not such an IRI — a literal, a different
    URI namespace, or a malformed entities IRI missing the ``<id>`` segment."""
    if not isinstance(value, str) or not value.startswith(ENTITY_URI_PREFIX):
        return None
    rest = value[len(ENTITY_URI_PREFIX):]
    parts = rest.split("/", 1)
    # Need a non-empty <TypeName> AND a non-empty <id> segment.
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0]


def _infer_datatype_from_values(values: list[str]) -> str:
    """Cheap datatype guess from the actual enriched values for one attribute.

    Precedence (first match wins), each requiring ALL non-empty values to agree:
      1. ``integer`` — every value parses as an int.
      2. ``float`` — every value parses as a float.
      3. ``datetime`` — every value is an ISO-8601 date/datetime (checked only
         for values that failed int/float AND carry a date separator ``-``/``T``/
         ``:``, so an all-integer column like ``2026`` is never misread as a
         date). ``datetime`` is the name ``_datatype_to_xsd`` maps to
         ``xsd:dateTime``.
      4. ``geo`` — every value is a WGS84 coordinate: a WKT ``POINT(lon lat)`` or
         a ``"lat,lon"`` pair in range (the Wikidata globecoordinate form). Maps
         to ``geo:wktLiteral``; the spatio-temporal index reads it directly. The
         WGS84 range gate (lat ≤ 90, lon ≤ 180) keeps a non-coordinate ``"x,y"``
         pair from being misread, and a bare float column never reaches here
         (caught by float above).
      5. a bare ``<TypeName>`` (a RELATIONSHIP range) — every value is a
         canonical entity IRI (``…/entities/<TypeName>/<id>``) AND they all share
         the SAME ``<TypeName>``. ``_datatype_to_xsd`` maps that bare name to the
         ``types/<TypeName>`` URI (an object-property range). Mixed IRI types →
         no single range, so we fall through to string (don't guess).
      6. ``string`` — the safe floor (also for empty / all-blank).

    Date and relationship detection (E2) are now attempted because they DO
    round-trip reliably: an ISO date and a canonical entity IRI are both exact,
    machine-minted forms, unlike free-text. Mirrors web_ingest_cap._infer_datatype
    for the primitive cases."""
    vals = [str(v).strip() for v in values if v not in (None, "")]
    # A value may already carry an XSD type annotation (``<lexical>^^<xsd-uri>``,
    # the `_typed_value` convention some callers pre-apply — e.g. the enriched
    # `<attr>_verified_at` dateTime stamp). Infer from the LEXICAL form so a
    # pre-typed value classifies the same as its bare form (otherwise the trailing
    # `^^…` breaks `fromisoformat`/int/float and every typed value falls to
    # `string`, mis-declaring the column's range).
    vals = [v.rsplit("^^", 1)[0] if "^^" in v else v for v in vals]
    vals = [v for v in vals if v]
    if not vals:
        return "string"
    if all(_is_int(v) for v in vals):
        return "integer"
    if all(_is_float(v) for v in vals):
        return "float"
    # Date only for values that look temporal (carry a date separator) and are
    # not numeric — guards an all-integer column from a date false-positive.
    if all(any(c in v for c in "-T:") and _is_iso_datetime(v) for v in vals):
        return "datetime"
    # Geo: a WKT POINT or an in-range "lat,lon" pair (Wikidata globecoordinate).
    # Reached only after int/float/datetime fail, so a plain number is never a
    # coordinate here; _to_wkt_point enforces the WGS84 range.
    if all(_to_wkt_point(v) is not None for v in vals):
        return "geo"
    # Relationship: all values are entity IRIs sharing one target type.
    iri_types = [_entity_iri_type(v) for v in vals]
    if all(t is not None for t in iri_types) and len(set(iri_types)) == 1:
        return iri_types[0]  # bare <TypeName> → types/<TypeName> range
    return "string"


# Org-valued enrich leaves. Values look like strings ("Hoffmann-La Roche") so
# `_infer_datatype_from_values` would stamp xsd:string; the instance should be
# an onto/<leaf> edge to a Company (or Organization) node. Prefer a type that
# already exists in the tenant catalog; otherwise mint Company.
_ORG_ATTR_LEAVES = frozenset(
    {
        "lead_sponsor",
        "sponsor",
        "sponsor_name",
        "lead_sponsor_name",
        "manufacturer",
        "company",
        "organization",
        "employer",
        "vendor",
    }
)
_ORG_TYPE_PREFERENCE = ("Company", "Organization", "Sponsor")


def _infer_relationship_target(
    attr_name: str, declared_types: list[str] | None = None
) -> str | None:
    """If this attribute should be a relationship, return the target type leaf.

    Used when values are plain labels (org names), not entity IRIs. Does not
    fire for status/phase/nct_id — only org-like leaves or an exact type-name
    match (``company`` → existing ``Company``).
    """
    leaf = (attr_name or "").strip()
    if not leaf:
        return None
    by_lower = {n.lower(): n for n in (declared_types or []) if n}
    low = leaf.lower()
    if low in by_lower:
        return by_lower[low]
    for prefix in ("lead_", "primary_", "parent_"):
        if low.startswith(prefix):
            rest = low[len(prefix) :]
            if rest in by_lower:
                return by_lower[rest]
    if low in _ORG_ATTR_LEAVES or low.endswith("_sponsor"):
        for cand in _ORG_TYPE_PREFERENCE:
            if cand.lower() in by_lower:
                return by_lower[cand.lower()]
        return "Company"
    return None


def _safe_iri(uri: str) -> bool:
    """A concrete predicate/label IRI is safe to interpolate into ``<…>`` only if
    it carries none of the chars that could break out of the term. The resolved
    IRIs are built from ``attr_uri``/``onto/`` + an ontology-known leaf so they
    are well-formed, but this is the executor-level backstop (defense in depth)."""
    return isinstance(uri, str) and bool(_IRI_RE.match(uri))


def _instance_pred_iris_for_leaf(type_name: str, leaf: str) -> list[str]:
    """The concrete instance predicate IRIs a declared predicate ``leaf`` can use.

    A literal attribute is stored under ``…/types/<Type>/attrs/<leaf>``
    (``attr_uri``); a relationship is stored under ``…/onto/<leaf>``
    (``ONTO_PRED_PREFIX``). The ontology declaration alone doesn't pin which, so
    we match BOTH.
    """
    return [_attr_uri(type_name, leaf), f"{ONTO_PRED_PREFIX}{leaf}"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_provenance_enabled() -> bool:
    """Whether enrichment feeds the canonical companion-provenance GRAPH (ADR 0002
    §4). Gated by the SAME ``INFONA_PROVENANCE_ENABLED`` env the ingest path uses
    (default OFF) so the heavier governance/undo substrate only accrues when it is
    switched on. The always-on per-attribute DISPLAY companions
    (``*_source_url`` / ``*_verified_at``) are independent of this flag."""
    return os.environ.get("INFONA_PROVENANCE_ENABLED", "0") == "1"


def _resolve_pred_iris_from_catalog(
    type_name: str, predicate: str, attr_names: list[str]
) -> list[str]:
    """Case-insensitively resolve ``predicate`` against declared attribute names.

    Returns ``[]`` when the predicate is not declared as an ATTRIBUTE on the
    type. RELATIONSHIPS often are not declared this way — the caller unions
    this with a direct build from the input predicate so they still resolve.
    """
    want = predicate.strip().lower()
    iris: list[str] = []
    seen: set[str] = set()
    for name in attr_names:
        leaf = (name or "").strip()
        if leaf and leaf.lower() == want:
            for iri in _instance_pred_iris_for_leaf(type_name, leaf):
                if iri not in seen:
                    seen.add(iri)
                    iris.append(iri)
    return iris


def _parse_vals(vals_field: str) -> dict[str, str]:
    """Parse ?vals (predicate::value pairs joined by '||') into a dict.

    If the same predicate appears multiple times, the first one wins.
    """
    out: dict[str, str] = {}
    if not vals_field:
        return out
    for chunk in vals_field.split("||"):
        if "::" not in chunk:
            continue
        p, _, v = chunk.partition("::")
        if p and p not in out:
            out[p] = v
    return out


def _prop_key_for_leaf(leaf: str) -> str | None:
    """Entity property key for an attribute leaf, or None if reserved/unsafe."""
    from infona_client.graph.facts import (
        RESERVED_ENTITY_PROPERTY_KEYS,
        sanitize_prop_key,
    )

    raw = (leaf or "").strip()
    if not raw:
        return None
    if raw in ("name", "title", "headline"):
        return raw
    if raw in RESERVED_ENTITY_PROPERTY_KEYS:
        return None
    try:
        return sanitize_prop_key(raw)
    except Exception:  # noqa: BLE001 — skip an unsanitizable leaf
        return None


def _extract_bind_attrs(
    props: dict,
    bind_leaves,
    *,
    uri: str = "",
    label: str = "",
) -> dict[str, str]:
    """Pull ``attribute:<leaf>`` binding values out of a GraphStore property map.

    Target-attr ``vals`` never include identifier leaves (nct_id, …) because
    those are not the attributes being filled. Registry adapters still need
    them to construct the API request. When a leaf has a well-known id format
    guard (NCT) and the property is missing, the entity URI slug / label is
    tried — ingest often keys the node by that id.
    """
    from infona_client.api_registry.ids import (
        has_id_format_guard,
        normalize_attribute_binding,
    )

    out: dict[str, str] = {}
    slug = _slug_from_uri(uri) if uri else ""
    for leaf in bind_leaves or ():
        leaf_s = str(leaf or "").strip()
        if not leaf_s:
            continue
        raw = ""
        key = _prop_key_for_leaf(leaf_s)
        if key:
            val = props.get(key) if isinstance(props, dict) else None
            if val is not None and val != "":
                raw = str(val)
        if not raw and has_id_format_guard(leaf_s):
            for cand in (slug, label, str((props or {}).get("id") or "")):
                if cand and normalize_attribute_binding(leaf_s, cand):
                    raw = cand
                    break
        if not raw:
            continue
        out[leaf_s] = normalize_attribute_binding(leaf_s, raw) or raw
    return out


async def _select_entities_via_store(
    tenant_id: str,
    kg_name: str,
    type_name: str,
    attributes: list[str],
    *,
    limit: Optional[int] = None,
    scope: Optional[EnrichScope] = None,
    entity_uris: Optional[list[str]] = None,
) -> Optional[list[dict]]:
    """List enrich targets from GraphStore (ONTA-534). ``None`` = store unavailable.

    Same shape as the SPARQL SELECT path: ``{uri, label, vals}`` with ``vals``
    keyed by attribute IRI so :meth:`EnrichmentExecutor.run` is store-agnostic.
    """
    from infona_client.graph.scope import GraphScope
    from infona_client.graph.store import GraphConfigError, get_optional_graph_store

    try:
        store = get_optional_graph_store()
        session = store.session(GraphScope.for_instance(tenant_id, kg_name))
    except GraphConfigError:
        return None
    except Exception:  # noqa: BLE001
        logger.error("enrich_store_session_failed", exc_info=True)
        return None

    try:
        rows = await session.execute_template(
            "entity_list_by_type", {"primary_type": type_name}
        )
    except Exception:  # noqa: BLE001
        logger.error("enrich_store_list_failed", type_name=type_name, exc_info=True)
        return None

    summaries = [r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in rows]
    if entity_uris:
        allowed = set(entity_uris)
        summaries = [s for s in summaries if s.get("id") in allowed]

    cap = int(limit) if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit else None
    entities: list[dict] = []
    for summary in summaries:
        eid = str(summary.get("id") or "").strip()
        if not eid:
            continue
        props: dict = {}
        try:
            detail_rows = await session.execute_template("entity_detail", {"id": eid})
        except Exception:  # noqa: BLE001 — still emit the entity with no vals
            detail_rows = []
        if detail_rows:
            detail = (
                detail_rows[0].to_dict()
                if hasattr(detail_rows[0], "to_dict")
                else dict(detail_rows[0])
            )
            raw_props = detail.get("props") or {}
            if isinstance(raw_props, dict):
                props = raw_props

        if scope is not None and scope.predicate and scope.value is not None:
            want = (scope.predicate or "").strip().lower()
            have = ""
            pred_key = _prop_key_for_leaf(scope.predicate)
            if pred_key:
                have = props.get(pred_key)
            if have is None or have == "":
                for k, v in props.items():
                    if str(k).lower() == want:
                        have = v
                        break
            if have is None or have == "":
                # Display name is stored on Entity.name (rdfs:label).
                if want in {"name", "label", "title"}:
                    have = props.get("name") or summary.get("name")
            if not _values_match(str(have or ""), str(scope.value)):
                continue

        label = summary.get("name") or props.get("name") or ""
        slug = _slug_from_uri(eid)
        if not label or label == slug:
            for fb in NAME_FALLBACK_ATTRS:
                alt = props.get(fb)
                if alt:
                    label = str(alt)
                    break
        if not label:
            label = slug

        vals: dict[str, str] = {}
        for attr in attributes:
            key = _prop_key_for_leaf(attr)
            if not key:
                continue
            val = props.get(key)
            if val is None or val == "":
                continue
            if isinstance(val, list):
                val = val[0] if val else ""
            if val == "":
                continue
            vals[_attr_uri(type_name, attr)] = str(val)
            for suffix in ("source_url", "verified_at"):
                raw_c = props.get(f"{attr}_{suffix}")
                if raw_c is None or raw_c == "":
                    continue
                cite = str(raw_c[0] if isinstance(raw_c, list) else raw_c)
                vals[attr_provenance_companion_uri(type_name, attr, suffix)] = cite
                vals[legacy_attr_companion_uri(type_name, attr, suffix)] = cite

        # Stash the full property map so binding-source leaves (e.g. nct_id
        # for ClinicalTrials.gov) can be read without a residual SPARQL hop.
        # Production GraphStore is Neo4j-only; NeptuneClient.query raises
        # SparqlClientRetired and the old SPARQL bind path fail-opened to {}.
        entities.append(
            {"uri": eid, "label": str(label), "vals": vals, "props": props}
        )
        if cap is not None and len(entities) >= cap:
            break
    return entities


def _values_match(existing: str, candidate: str) -> bool:
    """Loose match: case-insensitive substring or exact equality."""
    if not existing or not candidate:
        return False
    a = existing.strip().lower()
    b = candidate.strip().lower()
    if a == b:
        return True
    return a in b or b in a


def _values_match_with_strategy(
    existing: str, candidate: str, attr_strategy: AttributeStrategy | None
) -> bool:
    """Apply canonicalizer + aliases to the existing value before matching."""
    if attr_strategy is None:
        return _values_match(existing, candidate)
    transformed = existing
    if attr_strategy.canonicalizer:
        transformed = apply_canonicalizer(attr_strategy.canonicalizer, transformed)
    # Alias dictionary: literal lookup AND match against the transformed form.
    if attr_strategy.aliases:
        if existing in attr_strategy.aliases:
            transformed = attr_strategy.aliases[existing]
        elif transformed in attr_strategy.aliases:
            transformed = attr_strategy.aliases[transformed]
    return _values_match(transformed, candidate)


class EnrichmentExecutor:
    def __init__(
        self,
        neptune_client: NeptuneClient,
        job_store: JobStore,
        cache: EnrichmentCache,
        wikidata_adapter: SourceAdapter,
    ) -> None:
        self._neptune = neptune_client
        self._jobs = job_store
        self._cache = cache
        self._wikidata = wikidata_adapter
        # Register the wikidata adapter into the global adapter registry so
        # chain-based lookups can resolve it by name. Idempotent.
        try:
            register_adapter(wikidata_adapter)
        except Exception:  # noqa: BLE001
            pass

    async def _resolve_scope_predicate_iris(
        self, tenant_id: str, type_name: str, scope: EnrichScope
    ) -> list[str]:
        """Resolve ``scope.predicate`` (a local-name) to the concrete instance
        predicate IRI(s) to match.

        The candidate IRIs are the **union** of two sources:

          1. **Ontology catalog** — match ``scope.predicate`` case-insensitively
             against the type's declared attribute names. This gives
             case-normalisation: a request ``hasLevel`` resolves to the stored
             ``haslevel`` leaf.
          2. **Direct build from the (validated) input predicate** —
             :func:`_instance_pred_iris_for_leaf` → ``…/types/<Type>/attrs/<pred>``
             and ``…/onto/<pred>``. Relationships like ``haslevel`` are stored
             under ``…/onto/<pred>`` and may not be declared as an attribute,
             so the catalog arm alone returns ``[]`` for them. The direct
             build always yields ``…/onto/<pred>`` (and the attr IRI).

        A catalog/store error logs loudly and is skipped; the direct build
        still returns so a relationship scope resolves. No SPARQL.
        """
        ontology_iris: list[str] = []
        try:
            from infona_client.graph.ontology_catalog import list_attributes
            from infona_client.graph.store import GraphConfigError

            attrs = await list_attributes(
                tenant_id=tenant_id, type_name=type_name, layer="tenant"
            )
            names = [(getattr(a, "name", None) or "").strip() for a in attrs]
            ontology_iris = _resolve_pred_iris_from_catalog(
                type_name, scope.predicate, names
            )
        except GraphConfigError:
            logger.error(
                "scope_predicate_resolve_no_store",
                tenant_id=tenant_id,
                type_name=type_name,
                predicate=scope.predicate,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "scope_predicate_resolve_failed",
                tenant_id=tenant_id,
                type_name=type_name,
                predicate=scope.predicate,
            )
        direct_iris = _instance_pred_iris_for_leaf(
            type_name, scope.predicate.strip().lower()
        )
        iris: list[str] = []
        seen: set[str] = set()
        for iri in [*ontology_iris, *direct_iris]:
            if iri not in seen and _safe_iri(iri):
                seen.add(iri)
                iris.append(iri)
        return iris

    async def count_entities(
        self,
        tenant_id: str,
        kg_name: str,
        type_name: str,
        scope: Optional[EnrichScope] = None,
        entity_uris: Optional[list[str]] = None,
    ) -> int:
        """Count entities the job will enrich.

        Whole-type by default; honors the same subset semantics as the
        GraphStore entity select (COG-112). ``entity_uris`` wins over ``scope``.

        NOTE (COG-112 non-blocking): create-job no longer calls this in the
        request path — the executor's background select resolves the matched
        subset and sets ``progress.total``. Store-only (ONTA-527): a store
        outage logs loudly and returns 0; there is no SPARQL fallback.
        """
        store_entities = await _select_entities_via_store(
            tenant_id,
            kg_name,
            type_name,
            attributes=[],
            limit=None,
            scope=scope,
            entity_uris=entity_uris,
        )
        if store_entities is None:
            logger.error(
                "enrich_count_no_store",
                tenant_id=tenant_id,
                kg_name=kg_name,
                type_name=type_name,
            )
            return 0
        return len(store_entities)

    async def select_scope_value_uris(
        self,
        tenant_id: str,
        kg_name: str,
        type_name: str,
        predicate: str,
        values: list[str],
        limit: Optional[int] = None,
    ) -> list[str]:
        """Resolve a MULTI-VALUE scope to the concrete entity IRIs it names.

        "refresh pricing for OpenAI, Google, Deepgram and ElevenLabs" (or "the
        physicians named A. Selvan, R. Mokabberi, …") is a scope over a SET of
        values, not one literal. Matching the crammed literal
        (``provided_by = "OpenAI, Google, Deepgram, ElevenLabs"``) matches zero
        rows — the reported persona-eval gap. This returns the IRIs of
        ``type_name`` entities whose ``predicate`` value (or a related node's
        label, or its own ``rdfs:label``) is a case-insensitive member of
        ``values``, so the agent can enrich EXACTLY those via ``entity_uris`` (the
        already-converged scoped-enrich path) instead of premature-clarifying to
        0 and falling into a fresh discovery.

        Reuses the SAME predicate resolution (:meth:`_resolve_scope_predicate_iris`)
        and bound-predicate matching arms as the single-value scope, so casing /
        attribute-vs-relationship / label-only names are all handled identically.
        Returns a deduped, order-preserving, ``limit``-capped list; ``[]`` on any
        failure (unresolved predicate, empty value set, Neptune error) — the caller
        fails closed rather than enriching the whole type by accident.
        """
        clean = [v.strip() for v in (values or []) if v and v.strip()]
        if not clean:
            return []
        try:
            EnrichScope(predicate=predicate, value=clean[0])
        except Exception:  # noqa: BLE001 — a bad predicate resolves to nothing
            return []
        store_entities = await _select_entities_via_store(
            tenant_id,
            kg_name,
            type_name,
            attributes=[],
            limit=None,
            scope=None,
            entity_uris=None,
        )
        if store_entities is None:
            logger.error(
                "enrich_scope_value_select_no_store",
                tenant_id=tenant_id,
                kg_name=kg_name,
                type_name=type_name,
            )
            return []
        want = (predicate or "").strip().lower()
        pred_key = _prop_key_for_leaf(predicate)
        uris: list[str] = []
        seen: set[str] = set()
        for e in store_entities:
            props = e.get("props") or {}
            candidates: list[str] = []
            if pred_key:
                raw = props.get(pred_key)
                if raw is not None and raw != "":
                    candidates.append(str(raw))
            if not candidates:
                for k, v in props.items():
                    if str(k).lower() == want and v is not None and v != "":
                        candidates.append(str(v))
                        break
            if want in {"name", "label", "title"}:
                disp = e.get("label") or props.get("name")
                if disp:
                    candidates.append(str(disp))
            if any(_values_match(c, v) for c in candidates for v in clean):
                u = e.get("uri") or ""
                if u and u not in seen:
                    seen.add(u)
                    uris.append(u)
            if limit is not None and len(uris) >= int(limit):
                break
        return uris

    async def run(self, job: EnrichJob, tenant_id: str) -> None:
        # Per-provider activity + error accumulator for this run; stamped onto the
        # job at every terminal path so the run-detail view shows which providers
        # we used and a summary of the errors hit. Defined before the try so the
        # failure path can still surface whatever was recorded before the crash.
        tally = _ProviderTally()
        # A9 Run Manifest (ONTA-273): make this enrichment run a first-class object
        # so a run halted by provider exhaustion (a 402/sustained-429 from the LLM
        # extraction backend) reaches a TERMINAL failed state with a user-visible
        # reason AND honest partial coverage ("N of M items completed before halt"),
        # instead of a silent partial. Created here if the route did not mint one;
        # settled at every terminal path below. run_id = the job id (the job IS the
        # run). Defined before the try so the failure path can halt it too.
        if job.manifest is None:
            job.manifest = RunManifest(run_id=job.id, stage="enrichment")
        manifest = job.manifest
        # A9 cost envelope (ONTA-282): stamp the HARD per-run spend ceiling before
        # work starts. A per-job override (job.spend_ceiling_usd) wins; else the
        # deployment default (config). None/0 ⇒ unlimited (unchanged behavior). The
        # per-item spend feed below (via _lookup_chain) + the check in
        # process_entity then halt the run cleanly if it crosses this envelope.
        manifest.spend_ceiling_usd = resolve_spend_ceiling(
            getattr(job, "spend_ceiling_usd", None), settings.enrich_spend_ceiling_usd
        )
        manifest.start()
        try:
            job.status = JobStatus.running
            job.started_at = _now()
            # Operator Job Trace (ONTA-387): open live P0/P2/P4/P6 for enrichment.
            stamp_enrichment_run_started(job)
            await self._jobs.update(job)

            # Resolve the target type to the tenant's canonical declared name
            # BEFORE selecting entities. The SELECT keys on ?e a <types/Name>
            # case-sensitively, so a miscased/unknown type would otherwise match
            # zero entities and this run would finish "Completed" having enriched
            # nothing (the reported no-op). Auto-correct a case-insensitive match;
            # fail fast with a clear error for a type that genuinely doesn't
            # exist. This guards EVERY caller of run() (direct enrich, schedules,
            # actions), not just the enrich route. Fail-open: when the ontology
            # read fails or declares no types (known == []) we proceed unchanged.
            canonical, known_types = await resolve_type_name(
                self._neptune, tenant_id, job.type_name
            )
            if known_types and canonical is None:
                job.status = JobStatus.failed
                job.error = unknown_type_message(job.type_name, known_types)
                job.completed_at = _now()
                job.error_summary = [JobErrorItem(kind="job", message=job.error)]
                manifest.halt(HaltReasonKind.error, job.error)
                stamp_enrichment_run_failed(job, job.error)
                await self._jobs.update(job)
                return
            if canonical and canonical != job.type_name:
                job.type_name = canonical
                await self._jobs.update(job)

            # Load ontology-driven strategy. Always returns a TypeStrategy.
            strategy = await load_strategy(self._neptune, tenant_id, job.type_name)
            # Cache-key version for this strategy. A change here auto-invalidates
            # the cache (different key -> clean miss). TODO(ADR-0005 §2): the ADR
            # wants a real strategy_version field on TypeStrategy/AttributeStrategy;
            # derive a stable string until that lands.
            strategy_version = str(getattr(strategy, "version", "v1"))
            # Fold optional custom instructions into the cache version so two
            # different instruction sets never collide on a cached verdict (an
            # agentic adapter can read job.instructions and return a different
            # value). No instructions → unchanged version (clean reuse of the
            # existing cache keys). See _strategy_version_with_instructions.
            strategy_version = _strategy_version_with_instructions(
                strategy_version, job.instructions
            )
            # Track which adapter names were missing so we warn once per job.
            missing_adapter_names: set[str] = set()

            graph_uri = kg_graph_uri(tenant_id, job.kg_name)
            # GraphStore only (ONTA-527). SPARQL HTTP is retired (ONTA-534);
            # a dual-arm that fail-opened into SparqlClientRetired is how
            # prod jobs finished 50/50 no_match in <1s. Store miss → empty
            # select, logged loudly. Tests seed MemoryGraphStore.
            store_entities = await _select_entities_via_store(
                tenant_id,
                job.kg_name,
                job.type_name,
                job.attributes,
                limit=job.limit,
                scope=job.scope,
                entity_uris=job.entity_uris,
            )
            if store_entities is None:
                logger.error(
                    "enrich_entity_select_no_store",
                    tenant_id=tenant_id,
                    kg_name=job.kg_name,
                    type_name=job.type_name,
                )
                entities = []
            else:
                entities = store_entities

            # Pre-load binding-source attributes for the `attribute:<attr>`
            # enrich_from recipe (ONTA-194 phase 3). A registry adapter can bind a
            # request param FROM another of the entity's own attributes (e.g. a
            # resolved bls_series_id feeding a FRED price lookup); its lookup()
            # reads that value from context["entity_attributes"], but nothing
            # populates it unless we pre-load it here. Additive + graceful: an
            # adapter that binds nothing (the common case) leaves entities
            # untouched and the call shape below is byte-identical.
            #
            # Design choice (step 2b): the adapter chain is resolved PER ATTRIBUTE
            # inside process_entity (per-attribute ontology strategy sources >
            # request-level job.sources override > tier default). So the precise
            # set of adapters this job could consult is the UNION of each
            # attribute's chain — reproduced ONCE here with that same precedence,
            # rather than assuming a single job-wide chain (which would MISS a
            # per-attribute strategy source) or scanning ALL registered adapters
            # (which would over-fetch leaves for adapters this job never calls). In
            # the common no-strategy case this collapses to the one job-level
            # chain. Whole block is fail-safe: any error → no bind_attrs → adapters
            # fall through exactly as before.
            bind_leaves: set[str] = set()
            try:
                chain_names: set[str] = set()
                for _attribute in job.attributes:
                    _attr_strategy = strategy.attributes.get(_attribute)
                    if _attr_strategy and _attr_strategy.sources:
                        chain_names.update(_attr_strategy.sources)
                    elif job.sources:
                        _available = [s for s in job.sources if get_adapter(s) is not None]
                        chain_names.update(_available if _available else get_chain(job.tier))
                    else:
                        chain_names.update(get_chain(job.tier))
                for _name in chain_names:
                    _ad = get_adapter(_name)
                    bind_leaves |= getattr(_ad, "binding_source_attributes", frozenset())
            except Exception:  # noqa: BLE001 - never break the job over binding setup
                bind_leaves = set()
            if bind_leaves and entities:
                _bmap: dict[str, dict[str, str]] = {}
                need_fetch: list[str] = []
                for e in entities:
                    from_props = _extract_bind_attrs(
                        e.get("props") or {},
                        bind_leaves,
                        uri=e.get("uri") or "",
                        label=e.get("label") or "",
                    )
                    if from_props:
                        _bmap[e["uri"]] = from_props
                    elif not (e.get("props") or {}):
                        # No stashed props — re-read the store for this URI.
                        need_fetch.append(e["uri"])
                if need_fetch:
                    try:
                        fetched = await self._load_binding_attrs(
                            graph_uri,
                            need_fetch,
                            job.type_name,
                            bind_leaves,
                            tenant_id=tenant_id,
                            kg_name=job.kg_name,
                        )
                    except Exception:  # noqa: BLE001
                        fetched = {}
                    for uri, attrs in (fetched or {}).items():
                        _bmap.setdefault(uri, {}).update(attrs)
                for e in entities:
                    e["bind_attrs"] = _bmap.get(e["uri"], {})

            job.progress.total = len(entities) * len(job.attributes)
            # A9 manifest: the planned item denominator (M) is one item per
            # (entity, attribute) — the same unit progress counts.
            manifest.set_total(job.progress.total)
            stamp_enrichment_entities_selected(
                job,
                entity_count=len(entities),
                item_total=job.progress.total,
            )
            await self._jobs.update(job)

            sem = asyncio.Semaphore(WORKER_POOL_SIZE)
            counter = {"n": 0}
            counter_lock = asyncio.Lock()

            async def process_entity(ent: dict) -> list[RowResult]:
                results: list[RowResult] = []
                async with sem:
                    for attribute in job.attributes:
                        # Cooperative cancellation
                        latest = await self._jobs.get(job.id)
                        if latest and latest.status == JobStatus.cancelled:
                            return results

                        existing = ent["vals"].get(_attr_uri(job.type_name, attribute))
                        # The incumbent value's provenance companions, read from the
                        # same selection (fetched via the extended in_list). Carried
                        # onto a conflict row so both sources are visible for review
                        # (ONTA-246). None when the existing value has no prior
                        # provenance (e.g. an ingested value). Dual-read (ONTA-262):
                        # the attr_meta namespace is current; the legacy attribute-
                        # namespace shape covers KGs written before the migration.
                        existing_source_url = ent["vals"].get(
                            attr_provenance_companion_uri(
                                job.type_name, attribute, "source_url"
                            )
                        ) or ent["vals"].get(
                            legacy_attr_companion_uri(
                                job.type_name, attribute, "source_url"
                            )
                        )
                        existing_verified_at = ent["vals"].get(
                            attr_provenance_companion_uri(
                                job.type_name, attribute, "verified_at"
                            )
                        ) or ent["vals"].get(
                            legacy_attr_companion_uri(
                                job.type_name, attribute, "verified_at"
                            )
                        )
                        attr_strategy = strategy.attributes.get(attribute)

                        # Strategy merge: request value wins; ontology fills gaps.
                        # confidence_min: if ontology specifies one and the
                        # request is at the default (0.85), take the ontology
                        # value. Pragmatic heuristic since EnrichRequest has no
                        # "unset" sentinel.
                        effective_confidence = job.confidence_min
                        if attr_strategy and attr_strategy.confidence_min is not None:
                            if abs(job.confidence_min - 0.85) < 1e-9:
                                effective_confidence = attr_strategy.confidence_min

                        # Adapter chain precedence (most specific wins):
                        #   1. per-attribute ontology strategy sources, then
                        #   2. the request-level job.sources override, then
                        #   3. the tier default chain.
                        # ``chain_from_tier`` marks the branches derived from
                        # get_chain(tier) — the ONLY ones whose registry lead
                        # prefix the scalable selector (ONTA-341) may reshape. An
                        # explicit strategy/job override is the user's exact chain
                        # and is never reshaped.
                        chain_from_tier = False
                        if attr_strategy and attr_strategy.sources:
                            chain = list(attr_strategy.sources)
                        elif job.sources:
                            # Request-level provider override. Keep only names
                            # that resolve to a registered adapter; if the
                            # override names ONLY unavailable providers (e.g. a
                            # premium adapter not registered on this deployment),
                            # fall back to the tier default chain rather than
                            # enriching nothing — matching the UI's "falls back
                            # to Auto if unavailable" promise. A partially-valid
                            # override uses just its available names.
                            available = [
                                s for s in job.sources if get_adapter(s) is not None
                            ]
                            if available:
                                chain = available
                            else:
                                chain = get_chain(job.tier)
                                chain_from_tier = True
                        else:
                            chain = get_chain(job.tier)
                            chain_from_tier = True

                        # ONTA-341: replace the O(N) linear self-gating registry
                        # scan with retrieve-top-K → gate → arbitrate for this
                        # (entity_type, attribute). Identity when the feature flag
                        # is OFF (default) → byte-identical chain. Only applied to
                        # tier-derived chains (never a user override), and it never
                        # raises (returns the chain unchanged on any failure).
                        if chain_from_tier and job.type_name:
                            chain = await apply_registry_selection(
                                chain,
                                job.type_name,
                                attribute,
                                cache_scope=job.tenant_id or "",
                                openrouter_key=settings.openrouter_api_key,
                            )

                        verdicts = await self._lookup_chain(
                            ent["label"],
                            attribute,
                            chain,
                            job,
                            missing_adapter_names,
                            effective_confidence,
                            strategy_version,
                            tally=tally,
                            manifest=manifest,
                            entity_attrs=ent.get("bind_attrs"),
                        )
                        best = self._pick_best(verdicts, effective_confidence)

                        action: str
                        if best is None:
                            action = "no_match"
                        elif existing is None or existing == "":
                            action = "filled"
                        elif _values_match_with_strategy(
                            existing, best.value, attr_strategy
                        ):
                            action = "verified"
                        else:
                            action = "conflict"

                        results.append(
                            RowResult(
                                entity_uri=ent["uri"],
                                attribute=attribute,
                                existing_value=existing,
                                verdict=best,
                                action=action,  # type: ignore[arg-type]
                                existing_source_url=existing_source_url,
                                existing_verified_at=existing_verified_at,
                            )
                        )

                        async with counter_lock:
                            counter["n"] += 1
                            if action == "filled":
                                job.progress.filled += 1
                            elif action == "verified":
                                job.progress.verified += 1
                            elif action == "conflict":
                                job.progress.conflicts += 1
                            elif action == "skipped":
                                job.progress.skipped += 1
                            elif action == "no_match":
                                job.progress.no_match += 1
                            job.progress.processed = counter["n"]
                            # A9 manifest: this (entity, attribute) item was
                            # handled — record it completed so a later halt can
                            # caveat exactly how many items finished before it.
                            manifest.record_completed(
                                f"{ent['uri']}#{attribute}"
                            )
                            # A9 cost envelope (ONTA-282): the item's paid adapter
                            # calls fed their spend into the manifest as they ran
                            # (see _lookup_chain). If cumulative run spend has now
                            # reached the HARD per-run ceiling, HALT CLEANLY — raise
                            # the typed CostCeilingExceeded so it propagates to the
                            # outer `except` → halt_from_exception, the SAME terminal
                            # path a 402 takes (terminal `failed`, `cost_ceiling`
                            # kind, honest partial coverage), never a silent
                            # overspend. Checked under counter_lock so exactly one
                            # worker trips it. None/0 ceiling ⇒ never trips.
                            ceiling_error = manifest.check_ceiling()
                            if ceiling_error is not None:
                                raise ceiling_error
                            if counter["n"] % PROGRESS_FLUSH_EVERY == 0:
                                await self._jobs.update(job)
                return results

            tasks = [asyncio.create_task(process_entity(e)) for e in entities]
            all_rows: list[RowResult] = []
            for t in tasks:
                rows = await t
                all_rows.extend(rows)

            # Stamp the per-provider activity log + aggregated error summary onto
            # the job now, so every terminal path below (cancelled, review,
            # applied) persists "which providers we used + the errors we hit".
            job.provider_logs = tally.to_logs()
            job.error_summary = tally.to_error_summary()

            # Re-check cancellation after work loop.
            latest = await self._jobs.get(job.id)
            if latest and latest.status == JobStatus.cancelled:
                job.status = JobStatus.cancelled
                job.completed_at = _now()
                manifest.cancel()
                stamp_enrichment_run_cancelled(job)
                await self._jobs.update(job)
                return

            # Keep conflicts AND fills/verifications in results so the cited
            # verdict (value + source_url + provenance) is retrievable via the
            # job API, not just conflicts. Skips/no-matches carry no verdict.
            job.results = [r for r in all_rows if r.action in ("conflict", "filled", "verified")]

            # One structured summary on the common terminal path (covers BOTH the
            # review and applied states below). Makes the miss count visible from
            # logs so a run that simply found nothing is distinguishable from a
            # broken pipeline. NOT emitted on the cancelled/failed early-returns.
            # Prefer the provider tally (every adapter actually attempted,
            # including no_match / timeout / error) so a chain leader that
            # returned empty (e.g. Parallel create without wait — fixed) still
            # appears in logs. Fall back to winning-verdict sources when the
            # tally is empty (should not happen after a real walk).
            sources_tried = sorted(
                {
                    pl.provider
                    for pl in (job.provider_logs or [])
                    if pl.provider and pl.status != "skipped"
                }
                or {
                    r.verdict.source
                    for r in all_rows
                    if r.verdict and getattr(r.verdict, "source", None)
                }
            )
            logger.info(
                "enrichment_job_summary",
                job_id=job.id,
                type_name=job.type_name,
                tier=job.tier.value if hasattr(job.tier, "value") else str(job.tier),
                total=job.progress.total,
                filled=job.progress.filled,
                verified=job.progress.verified,
                conflicts=job.progress.conflicts,
                no_match=job.progress.no_match,
                sources_tried=sources_tried,
            )

            # Apply phase
            policy = job.conflict_policy
            # `stage` semantics (ONTA-159): a conflict-free fill (the target field
            # was empty) has nothing to reconcile, so it is applied immediately —
            # exactly like `skip`. Only genuine value-vs-value CONFLICTS are held
            # for human review. Previously `stage` also held fills, but the review
            # surface (`GET /jobs/{id}/conflicts`) lists ONLY conflict rows, so
            # conflict-free staged fills were stranded: staged yet invisible and
            # un-approvable — a job sat "In review" with zero reviewable items.
            # So under `stage` we WRITE like `skip` (fills only) and land in
            # `review` only when there is at least one real conflict to resolve.
            has_conflicts = any(r.action == "conflict" for r in job.results)
            write_policy = (
                ConflictPolicy.skip if policy == ConflictPolicy.stage else policy
            )

            # FRESHNESS RE-STAMP (ONTA-245 F2): a `verified` row (the source
            # RE-CONFIRMS the existing value) writes NO primary value under
            # verify/skip/stage, so a decay-refresh that re-confirms a still-correct
            # value would never advance its freshness clock — defeating "verified in
            # the last N days" exactly where it matters most. Fix: for a `verified`
            # row under a refresh-appropriate policy (verify/skip/stage → write_policy
            # is verify or skip), re-emit ONLY the per-attribute provenance companions
            # (source + a fresh `_verified_at`), advancing the stamp WITHOUT rewriting
            # the unchanged primary value (no duplicate value triple). Idempotent:
            # re-asserting the same `_verified_at` predicate with a newer object simply
            # accretes a fresher stamp the NL "last N days" FILTER then matches.
            restamp_triples: list[tuple[str, str, str]] = []
            if write_policy in (ConflictPolicy.verify, ConflictPolicy.skip):
                for r in all_rows:
                    if r.action == "verified" and r.verdict is not None:
                        restamp_triples.extend(
                            self._provenance_triples(
                                r.entity_uri, job.type_name, r.attribute, r.verdict
                            )
                        )

            # `applied_attr_values` is the source of truth for "was anything
            # applied?" — the attributes (primary + provenance companions) that
            # actually received a written value under `write_policy`, mapped to
            # their values. Empty ⇒ nothing to declare or write.
            applied_attr_values = self._applied_attribute_values(all_rows, write_policy)
            # E7: resolve GraphStore once for this write batch when neo4j backend
            # is active; None keeps the Neptune SPARQL default.
            graph_store = resolve_optional_graph_store()
            if applied_attr_values:
                # Declare schema, THEN write data. Enrichment must EXTEND THE
                # ONTOLOGY (COG-112): before writing instance values, upsert the
                # ontology declaration for every attribute that actually got a
                # value (primary + its provenance companions) into the tenant
                # (ontology) graph, so an enriched attribute is first-class schema
                # — visible in the /schema view, the Explorer column schema, and
                # the Enrich dialog's predicate dropdown, not just as orphan data.
                # One idempotent upsert per attribute (not per row), each declared
                # with a range inferred from its actual applied values and never
                # downgrading an existing richer range. Runs for every write
                # policy AND for `stage`'s conflict-free fills (which now write
                # via `write_policy=skip`); only true conflicts held for review
                # declare nothing until accepted.
                #
                # `_declare_attributes` RETURNS the {attr -> resolved_datatype} map
                # it just declared, so we type each INSTANCE value with the SAME
                # datatype the attribute is DECLARED with (P1 fix): the stored
                # literal (`"92"^^xsd:integer`) now matches the declared range,
                # instead of a bare `xsd:string` literal the typed NL filters miss.
                resolved_datatypes = await self._declare_attributes(
                    tenant_id,
                    job.type_name,
                    applied_attr_values,
                    kg_name=job.kg_name,
                )
                # Canonical companion-provenance-GRAPH records (F1) for every applied
                # fill, dated from the verdict — flowed through the shared
                # insert_facts provenance seam (gated by INFONA_PROVENANCE_ENABLED).
                prov_graph_triples = self._canonical_provenance_triples(
                    [r for r in all_rows if self._row_is_applied(r, write_policy)],
                    job.type_name,
                )
                # REFRESH vs. INITIAL-FILL split (ONTA-279). A refresh (write policy
                # verify/overwrite) MUST supersede — a fresh value CLOSES the stale
                # value's validity interval and is arbitrated (authority > confidence
                # > recency) against the existing current value, so it can never
                # blind-append and can never clobber a user_assertion correction.
                # The initial-fill / skip path (write_policy=skip, from `skip`/`stage`)
                # keeps its plain conflict-free insert unchanged.
                is_refresh = write_policy in (
                    ConflictPolicy.verify,
                    ConflictPolicy.overwrite,
                )
                if is_refresh:
                    # Route each applied PRIMARY value through the P6 supersession
                    # op (consulting the suppression list); collect node-minting +
                    # display-companion triples for one shared insert.
                    companion_triples = await self._apply_refresh_writes(
                        graph_uri,
                        all_rows,
                        job.type_name,
                        write_policy,
                        resolved_datatypes,
                        job.id,
                    )
                    # F2 verified-row freshness re-stamps ride the same shared insert
                    # (verify path; empty under overwrite where verifies rewrite the
                    # value via the op).
                    write_triples = companion_triples + restamp_triples
                    if write_triples or prov_graph_triples:
                        await insert_facts(
                            self._neptune,
                            graph_uri,
                            write_triples,
                            provenance_triples=prov_graph_triples or None,
                            store=graph_store,
                        )
                    await refresh_after_write(
                        self._neptune,
                        tenant_id=tenant_id,
                        kg_name=job.kg_name,
                        affected_types=self._affected_types(
                            job.type_name, resolved_datatypes
                        ),
                    )
                else:
                    # Initial-fill / skip path — unchanged conflict-free insert.
                    # Build the instance triples USING that resolved-datatype map:
                    # primitives route through validate_triple (typed literal, or a
                    # skip on a non-conforming value); relationships write the entity
                    # IRI directly; provenance companions stay plain string literals.
                    triples = self._select_triples_for_policy(
                        all_rows, job.type_name, write_policy, resolved_datatypes
                    )
                    # Append the verified-row freshness re-stamps (F2) so a
                    # decay-refresh advances the clock in the SAME write as the fills.
                    triples.extend(restamp_triples)
                    # Single shared write path — identical to CSV/JSON ingestion
                    # (graph/kg_writer.py): batched insert, then post-write
                    # housekeeping (invalidate the NL-planning cache, re-embed the
                    # enriched type so semantic retrieval doesn't serve a stale schema
                    # embedding, and recompute the Explorer's type-stats). Only fires
                    # when something was actually applied.
                    await insert_facts(
                        self._neptune,
                        graph_uri,
                        triples,
                        provenance_triples=prov_graph_triples or None,
                        store=graph_store,
                    )
                    await refresh_after_write(
                        self._neptune,
                        tenant_id=tenant_id,
                        kg_name=job.kg_name,
                        affected_types=self._affected_types(
                            job.type_name, resolved_datatypes
                        ),
                    )
            elif restamp_triples:
                # No new fills, but a decay-refresh re-confirmed existing values:
                # write ONLY the freshness re-stamps so the clock still advances.
                # Same shared write path; no primary value is rewritten, and
                # NOTHING is declared — companions are attr_meta metadata, never
                # ontology attributes (ONTA-262; this branch used to declare
                # `_verified_at` as "first-class schema", which is exactly what
                # rendered it as a sibling column in every schema surface).
                await insert_facts(
                    self._neptune,
                    graph_uri,
                    restamp_triples,
                    store=graph_store,
                )
                await refresh_after_write(
                    self._neptune,
                    tenant_id=tenant_id,
                    kg_name=job.kg_name,
                    affected_types={job.type_name},
                )
            # `stage` with at least one real conflict stays in `review` — those
            # conflicts are now the ONLY thing the review queue holds (the fills
            # were just applied above). Everything else — a `stage` run with no
            # conflicts, or any write policy — is `applied`.
            if policy == ConflictPolicy.stage and has_conflicts:
                job.status = JobStatus.review
            else:
                job.status = JobStatus.applied
            job.completed_at = _now()
            # A9 manifest: the run finished its work (review = parked for human
            # decisions, applied = written) — a clean terminal COMPLETED.
            manifest.complete()
            # Operator Job Trace (ONTA-387): P4/P6 write-phase actions + close
            # P0/P2/P4/P6 live; skip P1/P3/P5/P7/P8/P9 with reasons.
            write_policy_s = (
                getattr(write_policy, "value", None) or str(write_policy)
            )
            stamp_enrichment_write_phase(
                job,
                write_policy=write_policy_s,
                has_conflicts=has_conflicts,
                applied=bool(applied_attr_values) or bool(restamp_triples),
            )
            stamp_enrichment_run_finished(job)
            await self._jobs.update(job)

            # Product-analytics event (ONTA-323). run() is a background task with
            # no request context, so there is no auth subject to attribute to →
            # a stable system:<tenant> distinct id (never a path-named tenant).
            # Fire-and-forget, no-op without a registered sink, never raises.
            emit(
                "enrichment_ran",
                distinct_id=distinct_id_for(None, tenant_id),
                tenant=tenant_id,
                kg=job.kg_name or "",
                type_name=job.type_name,
                tier=job.tier.value if hasattr(job.tier, "value") else str(job.tier),
                attrs_filled=job.progress.filled,
                verified=job.progress.verified,
                conflicts=job.progress.conflicts,
                sources=sources_tried,
                status=job.status.value if hasattr(job.status, "value") else str(job.status),
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("enrichment_job_failed", job_id=job.id, error=str(exc))
            job.status = JobStatus.failed
            job.error = str(exc)
            job.completed_at = _now()
            # Surface whatever providers ran (and any per-provider errors) before
            # the fatal crash, plus the crash itself as a job-level error entry.
            job.provider_logs = tally.to_logs()
            job.error_summary = tally.to_error_summary() + [
                JobErrorItem(kind="job", message=str(exc)[:_MAX_ERROR_MSG])
            ]
            # A9 manifest: terminal FAILED with the derived reason. A provider
            # exhaustion (402 billing / sustained-429) is named as such and the
            # unfinished planned items are rolled into `dropped` — so a run halted
            # mid-flight caveats partial coverage instead of a silent partial.
            landed = (
                f"{job.progress.processed} of {job.progress.total} items completed "
                "before the failure."
                if job.progress.total
                else ""
            )
            manifest.halt_from_exception(exc, landed_note=landed)
            stamp_enrichment_run_failed(job, str(exc))
            try:
                await self._jobs.update(job)
            except Exception:  # noqa: BLE001
                pass

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
        ``SparqlClientRetired`` and fail-open to ``{}``, so ClinicalTrials.gov
        (and every other ``attribute:<id>`` adapter) saw empty bindings and
        returned no_match without ever calling the API. An empty map is a real
        miss (no such props), not a query-language error. A store outage logs
        at error and returns ``{}``.
        """
        del graph_uri, type_name  # store-keyed by tenant/kg + entity id
        leaf_list = [str(x) for x in (leaves or []) if x]
        uris = _validate_entity_uris([u for u in (entity_uris or []) if u])
        if not leaf_list or not uris:
            return {}
        if not tenant_id or not kg_name:
            logger.error(
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
            logger.error(
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
            logger.error(
                "enrich_bind_attrs_no_store",
                tenant_id=tenant_id,
                kg_name=kg_name,
            )
            return {}
        except Exception:  # noqa: BLE001
            logger.exception(
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
            adapter = get_adapter(name)
            if adapter is None:
                if name not in missing:
                    missing.add(name)
                    logger.warning(
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
                timeout_s = ADAPTER_LOOKUP_TIMEOUT_S
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
                    logger.warning(
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
                    logger.warning(
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

    @staticmethod
    def _verdict_prov_string(verdict) -> str:
        """The short human citation for a verdict's `<attr>_provenance` companion:
        the source name, with the reasoning appended when present."""
        prov = (getattr(verdict, "source", None) or "")
        if getattr(verdict, "reasoning", None):
            prov = f"{prov} ({verdict.reasoning})" if prov else verdict.reasoning
        return prov

    @staticmethod
    def _verdict_as_of(verdict) -> "datetime":
        """The as-of date to stamp for a verdict — the SOURCE's real date, not the
        write time (ONTA-245 F1). Prefer ``source_published_at`` (when the page
        stated the fact), else ``retrieved_at`` (when we fetched it), else now-UTC.
        A paid adapter that carries neither degrades to now, unchanged from before."""
        return (
            getattr(verdict, "source_published_at", None)
            or getattr(verdict, "retrieved_at", None)
            or _now()
        )

    @classmethod
    def _provenance_triples(
        cls, entity_uri: str, type_name: str, attribute: str, verdict
    ) -> list[tuple[str, str, str]]:
        """Persist where + when an enriched value came from, as queryable DISPLAY
        companions (`<attr>_source_url`, `<attr>_provenance`, `<attr>_verified_at`)
        on the entity — so the citation is visible through /ask and the Explorer,
        not just in the adapter. Audit-friendly: every enriched fact carries its
        source AND a per-fact freshness stamp.

        Built via the SHARED ``build_attribute_provenance_companions``
        (graph/provenance.py) so discovery mints the identical companion shape for
        the same fact (ONTA-245 cross-rail symmetry). `<attr>_verified_at` is the
        per-fact freshness marker (unlike the per-ENTITY `onto/ingested_at`); it is
        dated from the VERDICT's real source date (``source_published_at`` /
        ``retrieved_at``, ONTA-245 F1), NOT the write time, and is written TYPED
        ``xsd:dateTime`` so the NL planner's ``NOW()``-relative FILTER matches it
        (an untyped string would be type-incompatible → silently dropped, ONTA-247).
        Full price/value history is out of scope here (a separate deferred ticket)."""
        return build_attribute_provenance_companions(
            entity_uri,
            type_name,
            attribute,
            source_url=getattr(verdict, "source_url", None) or "",
            provenance=cls._verdict_prov_string(verdict),
            verified_at=cls._verdict_as_of(verdict),
        )

    def _canonical_provenance_triples(
        self, rows_or_decisions, type_name: str
    ) -> list[tuple[str, str, str]]:
        """Canonical companion-provenance-GRAPH triples for the applied facts
        (ONTA-245 F1) — the governance/undo substrate, keyed ``sha1(s|p|o|source)``
        with ``prov:confidence`` + a real ``prov:timestamp``, flowed through the
        shared ``insert_facts(..., provenance_triples=…)`` seam (NOT a bespoke
        writer). One record per applied (entity, attribute) fact, dated from the
        verdict's real source date so re-reading provenance shows WHEN the source
        knew the fact, not when we wrote it.

        Gated by ``INFONA_PROVENANCE_ENABLED`` (the SAME env the ingest path uses),
        so the heavier substrate only accrues when governance/undo is switched on;
        the always-on per-attribute display companions above are unaffected.

        Accepts either ``RowResult`` rows (auto-apply path) or ``ConflictReview``
        decisions (review-accept path) — both expose ``entity_uri`` + ``attribute``
        and a verdict (``.verdict`` / ``.proposed``)."""
        if not _canonical_provenance_enabled():
            return []
        out: list[tuple[str, str, str]] = []
        for item in rows_or_decisions:
            verdict = getattr(item, "verdict", None) or getattr(item, "proposed", None)
            if verdict is None or not getattr(verdict, "value", None):
                continue
            source = getattr(verdict, "source", None) or ""
            if not source:
                continue
            out.extend(
                build_provenance_triples(
                    item.entity_uri,
                    _attr_uri(type_name, item.attribute),
                    verdict.value,
                    source=source,
                    confidence=float(getattr(verdict, "confidence", 1.0) or 1.0),
                    timestamp=self._verdict_as_of(verdict),
                )
            )
        return out

    @staticmethod
    def _verdict_authority(verdict) -> AuthorityLevel:
        """The source-authority level to stamp a refreshed value with (ONTA-279).

        A registry-backed / premium adapter MAY carry an explicit
        ``AuthorityLevel`` value string on the verdict (``verdict.authority``); when
        present and valid it is threaded through verbatim so a curated
        ``source_of_truth`` API outranks a weaker web scrape at the write-time
        conflict point. Otherwise a plain machine scrape defaults to
        :data:`REFRESH_AUTHORITY` — strong but never the top ``user_assertion`` slot
        (that is the human-correction path's alone).

        The ``user_assertion`` level is CLAMPED OUT unconditionally: a machine scrape
        must never be stamped as a human correction, even if an adapter/verdict
        carries ``authority="user_assertion"`` (a bug or a spoofed source). Such a
        verdict is downgraded to :data:`REFRESH_AUTHORITY`, so it can never tie or
        beat a real user fix at the arbitration point (that would let a refresh
        clobber the very correction it is supposed to preserve)."""
        raw = getattr(verdict, "authority", None)
        if raw:
            try:
                level = AuthorityLevel(raw)
            except ValueError:
                level = None
            if level is not None:
                # Never let a machine verdict claim the human-correction slot.
                if level == AuthorityLevel.user_assertion:
                    return REFRESH_AUTHORITY
                return level
        return REFRESH_AUTHORITY

    @classmethod
    def _primary_value_write(
        cls,
        entity_uri: str,
        type_name: str,
        attribute: str,
        value: str,
        datatype: str,
    ) -> Optional[tuple[str, str, list[tuple[str, str, str]]]]:
        """Split one applied value into its PRIMARY edge + any node-minting triples,
        for the ONTA-279 refresh path (which routes the primary through the P6
        conflict op rather than a blind insert).

        Reuses :meth:`_instance_triples_for_value` (the ONE value-typing +
        node-linking implementation) and splits its output: the FIRST triple is
        always the primary ``(entity, predicate, term)`` — the literal value on
        ``attrs/<leaf>`` or the relationship edge on ``onto/<leaf>`` — and the rest
        (a relationship target's ``rdf:type`` / ``rdfs:label``) are the node-minting
        companions that still ride the shared ``insert_facts``. Returns
        ``(predicate, term, node_triples)`` or ``None`` when the value produced no
        primary triple (a rejected non-conforming primitive — never clear/replace an
        incumbent for a value that won't be written)."""
        triples = cls._instance_triples_for_value(
            entity_uri, type_name, attribute, value, datatype
        )
        if not triples:
            return None
        _s, predicate, term = triples[0]
        node_triples = list(triples[1:])
        return predicate, term, node_triples

    async def _apply_refresh_writes(
        self,
        graph_uri: str,
        rows: list[RowResult],
        type_name: str,
        write_policy: ConflictPolicy,
        resolved_datatypes: dict[str, str],
        run_id: str,
    ) -> list[tuple[str, str, str]]:
        """Apply a REFRESH job's primary values through the P6 supersession op
        (ONTA-279) — the write half of "refresh supersedes, never blind-appends".

        For each row that :meth:`_row_is_applied` under ``write_policy``
        (verify → fills; overwrite → fills/conflicts/verifies), route the PRIMARY
        ``(subject, predicate, value)`` through
        :func:`pipeline.mutations.write_with_conflict_resolution` instead of a raw
        ``insert_facts`` / ``delete_facts``+``insert_facts``. That op:

          * reads the existing current value's authority back from provenance and
            arbitrates on the ONE shared policy (authority > confidence > recency >
            value) — so a machine refresh CLOSES a stale value's validity interval
            (supersession, never a hard delete) but LOSES to a ``user_assertion``
            correction (completing ONTA-281's e2e);
          * inherits the ONTA-277 resurrection semantics for free (``reopen_facts``),
            so an A→B→A oscillation lands A current again.

        Before writing, the value is checked against the STICKY suppression list
        (:func:`graph.suppression.is_suppressed`): a retracted/suppressed value is a
        no-op (a refresh must never re-acquire it), which — unlike a validity
        closure — the op's reopen cannot resurrect.

        Node-minting triples (a relationship target's type/label) and the
        per-attribute DISPLAY provenance companions are RETURNED for the caller to
        write in ONE shared ``insert_facts`` + one ``refresh_after_write``, keeping
        the companions on the converged write path.
        """
        companion_triples: list[tuple[str, str, str]] = []
        for r in rows:
            if not self._row_is_applied(r, write_policy) or r.verdict is None:
                continue
            datatype = resolved_datatypes.get(r.attribute, "string")
            primary = self._primary_value_write(
                r.entity_uri, type_name, r.attribute, r.verdict.value, datatype
            )
            if primary is None:
                # A non-conforming primitive produced no primary triple → write
                # nothing (never supersede an incumbent for a value we can't store).
                continue
            predicate, term, node_triples = primary
            # Suppression consult: a retracted/suppressed value must NOT be
            # re-acquired by a refresh (ONTA-279). Skip it entirely — no
            # supersession, no reopen, no companions — so it stays off.
            if await is_suppressed(self._neptune, graph_uri, r.entity_uri, predicate, term):
                logger.info(
                    "enrichment_refresh_value_suppressed",
                    subject=r.entity_uri,
                    predicate=predicate,
                    value=term,
                )
                continue
            # Node-minting companions (relationship target type/label) ride the
            # shared insert_facts the caller issues; write them before the edge is
            # arbitrated so the target node exists.
            companion_triples.extend(node_triples)
            # ONTA-536: prefer source_url so Assertion identity / fold matches the
            # companion citation insert (same source_discriminator → one Assertion).
            _src = (
                getattr(r.verdict, "source_url", None)
                or getattr(r.verdict, "source", "")
                or ""
            )
            await write_with_conflict_resolution(
                self._neptune,
                graph_uri,
                subject=r.entity_uri,
                predicate=predicate,
                type_name=type_name,
                value=term,
                authority=self._verdict_authority(r.verdict),
                confidence=float(r.verdict.confidence),
                source=_src,
                observed_at=self._verdict_as_of(r.verdict),
                run_id=run_id,
                reason="enrichment refresh (supersede stale value)",
                recency_policy=DEFAULT_RECENCY_POLICY,
                # This op runs PER ROW; the caller (run()'s is_refresh branch) issues
                # ONE final refresh_after_write for the touched types after the loop.
                # Deferring the per-row refresh turns a bulk refresh from ~N+1
                # housekeeping passes (Neptune query + re-embed + stats) into 1.
                refresh=False,
            )
            # ONTA-536: re-include the primary value triple so the caller's
            # companion insert_facts batch has a domain Fact for
            # fold_attr_citations_onto_facts (Assertion.source_url / verified_at).
            # Idempotent re-write — same s/p/o already landed above.
            companion_triples.append((r.entity_uri, predicate, term))
            # Per-attribute DISPLAY provenance companions (source_url / provenance /
            # verified_at) for the value we just wrote — same citations as the
            # non-refresh path, collected for one shared insert.
            companion_triples.extend(
                self._provenance_triples(r.entity_uri, type_name, r.attribute, r.verdict)
            )
        return companion_triples

    @staticmethod
    def _row_is_applied(r: RowResult, policy: ConflictPolicy) -> bool:
        """Whether a row's verdict actually contributes instance triples under
        ``policy``. Single source of truth shared by :meth:`_select_triples_for_policy`
        (which data to write) and :meth:`_applied_attribute_names` (which schema to
        declare) so the two can never drift."""
        if r.verdict is None:
            return False
        if policy == ConflictPolicy.overwrite:
            return r.action in ("filled", "conflict", "verified")
        if policy in (ConflictPolicy.verify, ConflictPolicy.skip):
            return r.action == "filled"
        return False

    @staticmethod
    def _affected_types(type_name: str, resolved_datatypes: dict[str, str]) -> set[str]:
        """Types whose embeddings + Explorer stats a post-write refresh must touch:
        the SUBJECT type PLUS the type of every node-valued attribute.

        A node-valued fill mints a target NODE
        (:meth:`_instance_triples_for_value` — e.g. ``Physician.located_in`` →
        a ``City`` node), so ``refresh_after_write`` must re-embed / re-stat that
        target TYPE too. Passing only the subject type (the old behavior) left a
        freshly-minted ``City`` node stale until ``City``'s own next write —
        the enrichment mirror of discovery's Part-3 gap. Non-primitive
        ``resolved_datatypes`` values are exactly the node-valued ranges."""
        return {type_name} | {
            dt for dt in resolved_datatypes.values() if dt not in PRIMITIVE_TYPES
        }

    def _select_triples_for_policy(
        self,
        rows: list[RowResult],
        type_name: str,
        policy: ConflictPolicy,
        resolved_datatypes: dict[str, str],
    ) -> list[tuple[str, str, str]]:
        """Build the instance triples to write for the INITIAL-FILL / skip path.

        Used only for the non-refresh write policy (``skip``, from ``skip``/``stage``
        — a conflict-free fill); a refresh (verify/overwrite) instead routes each
        primary value through the P6 supersession op (:meth:`_apply_refresh_writes`,
        ONTA-279). ``resolved_datatypes`` is the ``{attribute -> datatype}`` map
        :meth:`_declare_attributes` just declared, so each primary value is TYPED
        with the SAME datatype its attribute is DECLARED with (P1 fix). The primary
        value goes through :meth:`_instance_triples_for_value` (relationship → IRI;
        primitive → ``validate_triple`` typed literal, skipped if non-conforming);
        the citation companions (``*_source_url`` / ``*_provenance``) stay plain
        string literals exactly as before. The one typed exception is
        ``*_verified_at`` — a timestamp, not a citation string — which
        :meth:`_provenance_triples` emits as a typed ``xsd:dateTime`` literal so the
        NL planner's typed date FILTERs match it (an untyped string would be
        type-incompatible and silently drop the row)."""
        triples: list[tuple[str, str, str]] = []
        clean_report = CleanReport()  # A3 ledger: partitions every primitive fill value
        for r in rows:
            if not self._row_is_applied(r, policy):
                continue
            # Default to ``string`` if a datatype somehow wasn't resolved for this
            # attribute (defensive — _declare_attributes covers every applied attr).
            datatype = resolved_datatypes.get(r.attribute, "string")
            value_triples = self._instance_triples_for_value(
                r.entity_uri, type_name, r.attribute, r.verdict.value, datatype,
                clean_report=clean_report,
            )
            # Only stamp provenance for a value that was ACTUALLY written. A rejected
            # primitive (validate_triple → no triple) writes no primary value, so
            # emitting fresh `_source_url` / `_verified_at` here would falsely cite a
            # source on a value that was never stored. Gate the citation on the same
            # condition as the primary value (reviewer finding).
            if not value_triples:
                continue
            triples.extend(value_triples)
            # Provenance companions are user-facing citations (URLs / free text) —
            # plain string literals — EXCEPT `<attr>_verified_at`, which
            # _provenance_triples types as xsd:dateTime so typed date FILTERs match.
            triples.extend(self._provenance_triples(r.entity_uri, type_name, r.attribute, r.verdict))
        self._log_clean_report(clean_report, type_name=type_name, phase="fill")
        return triples

    def _applied_attribute_values(
        self, rows: list[RowResult], policy: ConflictPolicy
    ) -> dict[str, list[str]]:
        """The PRIMARY attribute names that ACTUALLY received a written value
        under ``policy``, mapped to the list of string VALUES applied for each —
        the set whose ontology declarations the apply step upserts so an enriched
        attribute becomes first-class schema (visible in the /schema view, the
        Explorer column schema, and the Enrich dialog's predicate dropdown).
        Attributes that found nothing are excluded so enrichment never pollutes
        the ontology with empty slots. Insertion-ordered + value-accumulating so
        the caller issues one declaration per attribute (not one per row) AND can
        infer that attribute's datatype from the actual values written.

        The provenance companions are deliberately NOT here (ONTA-262): they are
        metadata OF an attribute, minted on the attr_meta namespace and never
        declared as ontology attributes — declaring them was exactly what made
        `<attr>_provenance` / `<attr>_verified_at` render as sibling columns in
        every schema surface. Their instance triples still ride the same write
        (:meth:`_select_triples_for_policy`)."""
        out: dict[str, list[str]] = {}

        for r in rows:
            if not self._row_is_applied(r, policy):
                continue
            out.setdefault(r.attribute, []).append(r.verdict.value)
        return out

    async def _resolve_declared_datatype(
        self,
        onto_graph: str,
        type_name: str,
        attr_name: str,
        values: list[str],
        *,
        tenant_id: str | None = None,
    ) -> str:
        """Resolve the ``datatype`` to declare for one enriched attribute, never
        DOWNGRADING an existing richer range.

        Two inputs combine:
          1. The datatype INFERRED from the actual applied ``values`` (integer /
             float / string) — so a numeric enriched attribute is typed, not
             stamped ``xsd:string`` blindly.
          2. The attribute's range as ALREADY declared in the ontology. If that
             existing range is anything other than ``xsd:string`` — a richer XSD
             primitive (integer/float/dateTime) OR a relationship ``types/<X>``
             URI declared by ingestion — it is PRESERVED verbatim; enrichment must
             not clobber an ingest-inferred integer or a relationship edge down to
             a string.

        Net rule: ``existing_range if (existing_range and existing_range !=
        xsd:string) else inferred``. With no existing range, or an existing
        ``xsd:string``, the inferred datatype wins (so a brand-new attribute is
        typed correctly, and a previously-untyped string slot can be upgraded)."""
        inferred = _infer_datatype_from_values(values)
        del onto_graph  # catalog-only; SPARQL range query is retired (ONTA-527)
        declared_type_names: list[str] = []
        if not tenant_id:
            return _infer_relationship_target(attr_name) or inferred
        try:
            from infona_client.graph.ontology_catalog import list_attributes, list_types
            from infona_client.graph.store import GraphConfigError

            try:
                declared_type_names = [
                    t.name
                    for t in await list_types(tenant_id=tenant_id, layer="tenant")
                    if getattr(t, "name", None)
                ]
            except Exception:  # noqa: BLE001 — type list is advisory
                declared_type_names = []
            attrs = await list_attributes(
                tenant_id=tenant_id, type_name=type_name, layer="tenant"
            )
            match = next((a for a in attrs if a.name == attr_name), None)
            if match is not None:
                if match.kind == "relationship" and match.range_type:
                    return match.range_type
                if match.datatype and match.datatype != "string":
                    return match.datatype
            # Existing string (or no declaration) can UPGRADE to a relationship
            # when the leaf is org-valued (lead_sponsor → Company). Values are
            # labels, so inferred is "string".
            return (
                _infer_relationship_target(attr_name, declared_type_names)
                or inferred
            )
        except GraphConfigError:
            logger.error(
                "enrich_declare_range_no_store",
                type_name=type_name,
                attr=attr_name,
            )
            return (
                _infer_relationship_target(attr_name, declared_type_names)
                or inferred
            )
        except Exception:  # noqa: BLE001 — never fail a write over a range read
            logger.exception(
                "enrich_declare_range_catalog_failed",
                type_name=type_name,
                attr=attr_name,
            )
            return (
                _infer_relationship_target(attr_name, declared_type_names)
                or inferred
            )

    async def _declare_attributes(
        self,
        tenant_id: str,
        type_name: str,
        attr_values: dict[str, list[str]],
        *,
        kg_name: str | None = None,
    ) -> dict[str, str]:
        """Upsert each enrichment-applied attribute's ontology declaration into the
        TENANT (ontology) graph so it becomes first-class schema. Reuses the same
        idempotent :func:`upsert_attribute` the ontology endpoint uses
        (``rdf:Property ; rdfs:label ; rdfs:domain <Type> ; rdfs:range <…>``), one
        update per attribute. The declared ``rdfs:range`` is resolved per attribute
        by :meth:`_resolve_declared_datatype`: inferred from the actual applied
        values, but never downgrading an existing richer range. Called BEFORE the
        instance ``insert_triples`` write (declare schema, then write data) and
        inside the job's try/except so a declaration failure fails the job,
        consistent with existing behavior.

        ``attr_values`` maps each applied attribute name (primary + provenance
        companions) to the string values written for it.

        Returns the ``{attribute_name -> resolved_datatype}`` map so the caller can
        type each INSTANCE value with the SAME datatype the attribute is DECLARED
        with (P1 data-correctness fix): a numeric value must be stored as a typed
        literal (``"92"^^xsd:integer``) matching the declared integer range, not as
        a bare ``xsd:string`` literal the typed NL filters then miss. Computing the
        datatype ONCE here and reusing it for both the declaration and the value
        typing is what keeps the declared range and the stored literal in lock-step.
        The provenance companions resolve to ``string`` (URLs / free text) and are
        intentionally never typed as anything richer."""
        onto_graph = tenant_graph_uri(tenant_id)
        resolved: dict[str, str] = {}
        for name, values in attr_values.items():
            datatype = await self._resolve_declared_datatype(
                onto_graph, type_name, name, values, tenant_id=tenant_id
            )
            resolved[name] = datatype
            if datatype not in PRIMITIVE_TYPES:
                await commit_ontology(
                    self._neptune,
                    onto_graph,
                    [
                        OntologyMutation(
                            op=OntologyOpKind.UPSERT_TYPE,
                            type_name=datatype,
                        ),
                        OntologyMutation(
                            op=OntologyOpKind.UPSERT_RELATIONSHIP,
                            type_name=type_name,
                            slot_name=name,
                            target_type=datatype,
                            description=ENRICH_ATTR_DESCRIPTION,
                        ),
                    ],
                )
                if kg_name:
                    await self._promote_literal_attr_to_nodes(
                        tenant_id,
                        kg_name,
                        type_name,
                        name,
                        datatype,
                        extra_values=values,
                    )
            else:
                await commit_ontology(
                    self._neptune,
                    onto_graph,
                    [OntologyMutation(
                        op=OntologyOpKind.UPSERT_ATTRIBUTE,
                        type_name=type_name,
                        slot_name=name,
                        datatype=datatype,
                        description=ENRICH_ATTR_DESCRIPTION,
                    )],
                )
        return resolved

    async def _promote_literal_attr_to_nodes(
        self,
        tenant_id: str,
        kg_name: str,
        type_name: str,
        attr_name: str,
        target_type: str,
        *,
        extra_values: list[str] | None = None,
    ) -> None:
        """Turn already-written string values of ``attr_name`` into target nodes.

        Job c7c2c7d2 wrote ``lead_sponsor`` as attrs/lead_sponsor literals.
        After we flip the declaration to a Company relationship, those
        literals must become ``onto/lead_sponsor`` edges + Company nodes or
        Explorer keeps showing a string column. GraphStore-only — no SPARQL.
        """
        del extra_values  # values ride the subsequent insert_facts path
        try:
            from infona_client.graph.explore_store import (
                get_entity_detail,
                list_entities_by_type,
            )
            from infona_client.graph.ontology_queries import attr_uri as _attr_iri
            from infona_client.graph.store import GraphConfigError
        except Exception:  # noqa: BLE001
            return
        try:
            page = await list_entities_by_type(
                tenant_id=tenant_id,
                kg_name=kg_name,
                type_name=type_name,
                limit=200,
            )
        except GraphConfigError:
            return
        except Exception:
            logger.warning(
                "enrich_promote_list_failed",
                type_name=type_name,
                attr=attr_name,
                exc_info=True,
            )
            return
        if page is None or not page.entities:
            return
        store = resolve_optional_graph_store()
        graph_uri = kg_graph_uri(tenant_id, kg_name)
        lit_pred = _attr_iri(type_name, attr_name)
        triples: list[tuple[str, str, str]] = []
        clear: list[tuple[str, str, None]] = []
        for ent in page.entities:
            try:
                detail = await get_entity_detail(
                    tenant_id=tenant_id, kg_name=kg_name, entity_id=ent.id
                )
            except Exception:
                continue
            if detail is None:
                continue
            raw = (detail.properties or {}).get(attr_name)
            if raw is None or raw == "":
                continue
            if isinstance(raw, (list, tuple)):
                labels = [str(x) for x in raw if x]
            else:
                labels = [str(raw)]
            for label in labels:
                if label.startswith("http://") or label.startswith("https://"):
                    continue
                triples.extend(
                    self._instance_triples_for_value(
                        ent.id, type_name, attr_name, label, target_type
                    )
                )
                clear.append((ent.id, lit_pred, None))
        if triples:
            await insert_facts(self._neptune, graph_uri, triples, store=store)
        if clear:
            await delete_facts(
                self._neptune,
                graph_uri,
                triples=clear,
                reason="enrich:promote_literal_to_node",
                store=store,
            )

    @staticmethod
    def _instance_triples_for_value(
        entity_uri: str,
        type_name: str,
        attribute: str,
        value: str,
        datatype: str,
        clean_report: Optional[CleanReport] = None,
    ) -> list[tuple[str, str, str]]:
        """Build the instance triple(s) for ONE applied attribute value, typed with
        the SAME resolved ``datatype`` the attribute is DECLARED with (P1 fix).

        Two branches mirror ingestion's value-typing path
        (resolver/schema_resolver.py around 1393–1410):

        - **relationship** — ``datatype`` is NOT a primitive (it is an entity-type
          name, e.g. ``City``). If the value is ALREADY an entity IRI (a premium
          adapter that resolved it) write that edge directly. Otherwise the enriched
          value is a plain LABEL (``"San Francisco"``): resolve it to the SAME
          canonical ``entities/<Type>/<safe_id>`` URI ingestion mints and ALSO emit
          the node's ``rdf:type`` + ``rdfs:label`` — so the fact becomes ONE shared
          node across the discovery and enrichment rails, never a dangling string in
          a node-valued slot (the cross-rail correctness fix). No ``validate_triple``
          (an entity edge is not an XSD-typed literal).
        - **primitive** (string/integer/float/datetime/boolean/uri) — route the
          value through the SAME ``validate_triple`` ingestion uses so the stored
          literal is properly TYPED (``"92^^…#integer"`` → a typed literal via
          ``_escape_value``). A ``ValidatedTriple`` is written; a ``RejectedValue``
          (value can't conform/coerce to the declared range) yields NO triple — we
          skip it rather than pin a mismatched literal that the typed NL filters
          would then miss (validate_triple already logs the rejection).

        Returns ``[]`` when a primitive value is rejected; otherwise the single
        instance triple.

        ONTA-344: when a ``clean_report`` is supplied, every PRIMITIVE value is
        recorded into the A3 clean ledger (passed / transformed / dropped) — so a
        non-conforming value that yields no triple is a RECORDED ``dropped`` entry
        with a reason, not a silent skip. Additive: this changes nothing about which
        triples are written (relationship edges are not datatype-cleaned literals, so
        they are outside the clean ledger's scope and are not recorded)."""
        attr_uri_str = _attr_uri(type_name, attribute)
        # Relationship: a non-primitive datatype names an entity TYPE (a node range).
        if datatype not in PRIMITIVE_TYPES:
            # A relationship INSTANCE edge lives on the onto/<leaf> predicate — the
            # form the NL query planner emits for a type-ranged attribute
            # (nlp/prompts, ontology_embeddings) and the form discovery's PRIMARY
            # relationship writes use (schema_resolver ~969/1192). Writing the edge
            # on the attrs/<leaf> ATTRIBUTE predicate instead leaves it INVISIBLE to
            # NL queries (they traverse onto/<leaf>, with no attrs/<leaf> fallback).
            # The ontology DECLARATION stays the attrs/<leaf> property with a
            # types/<T> range (the established dual convention: attrs declares,
            # onto carries the instance); only the instance edge is onto/<leaf>.
            onto_pred = f"{IRI_BASE}/onto/{attribute}"
            # Already an entity IRI (e.g. a premium adapter that resolved it) → the
            # edge is ready as-is.
            if value.startswith("http://") or value.startswith("https://"):
                return [(entity_uri, onto_pred, value)]
            # Otherwise the enriched value is a plain LABEL. Resolve it to the SAME
            # canonical entity URI ingestion mints (entities/<Type>/<safe_id>) so the
            # same real-world thing is ONE shared node across the discovery +
            # enrichment rails, and create/type that node (idempotent INSERT) so the
            # edge is never a dangling string — closing the cross-rail divergence
            # where enrichment wrote a literal into a node-valued attribute the
            # ontology declares as a relationship. Uses the SAME shared entity_uri
            # minter discovery keys its entity URIs with (graph/ontology_queries), so
            # the URIs coincide exactly — one shared node across both rails.
            target_uri = _entity_uri(datatype, value)
            return [
                (entity_uri, onto_pred, target_uri),
                (target_uri, RDF_TYPE, _type_uri(datatype)),
                (target_uri, RDFS_LABEL, value),
            ]
        # Primitive: type the literal exactly as ingestion does. validate_triple
        # returns a ValidatedTriple (typed object) on conform/coerce, else a
        # RejectedValue (skip — never write a literal that mismatches the range).
        # Record the A3 clean outcome (passed/transformed/DROPPED) into the ledger so
        # a non-conforming value is a recorded drop, not a silent skip (ONTA-344).
        if clean_report is not None:
            clean_report.record(
                clean_value(value, datatype, entity_id=entity_uri, attribute=attribute)
            )
        validated = validate_triple(
            entity_uri,
            attr_uri_str,
            value,
            datatype,
            entity_id=entity_uri,
            attribute_name=attribute,
        )
        if isinstance(validated, ValidatedTriple):
            return [(validated.subject, validated.predicate, validated.object)]
        return []

    @staticmethod
    def _log_clean_report(report: CleanReport, *, type_name: str, phase: str) -> None:
        """Surface the A3 clean ledger for one enrichment write (ONTA-344).

        Emits the partition COUNTS, and — the point of the ledger — a structured
        record of every DROPPED value (non-conforming primitives enrichment used to
        skip silently), so a caller can see WHAT was not written and WHY. No-op when
        nothing was cleaned."""
        if report.total == 0:
            return
        counts = report.counts()
        logger.info("enrichment_clean_report", type=type_name, phase=phase, **counts)
        for fact in report.dropped:
            logger.warning(
                "enrichment_value_dropped",
                type=type_name,
                phase=phase,
                entity=fact.entity_id,
                attr=fact.attribute,
                value=fact.raw_value,
                datatype=fact.datatype,
                reason=fact.reason,
            )

    async def apply_decisions(
        self, job_id: str, decisions: list[ConflictReview]
    ) -> int:
        job = await self._jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        graph_uri = kg_graph_uri(job.tenant_id, job.kg_name)
        applied = 0  # number of accepted facts (provenance triples don't count)
        # Insertion-ordered map of applied attribute name -> the string values
        # written for it, so declarations infer the right range (and never
        # downgrade an existing one), mirroring run()'s _applied_attribute_values.
        applied_attr_values: dict[str, list[str]] = {}
        # The accepted decisions whose primary value we'll later type + write.
        accepted: list[ConflictReview] = []
        for d in decisions:
            if d.decision != "accept":
                continue
            accepted.append(d)
            # Track the PRIMARY attribute names + values actually written so we
            # declare them in the ontology, mirroring run(). Provenance companions
            # are deliberately NOT declared (ONTA-262): they are attr_meta
            # metadata, not attributes — their instance triples still ride the
            # same write below.
            applied_attr_values.setdefault(d.attribute, []).append(d.proposed.value)
            applied += 1
        if applied_attr_values:
            # Declare schema, THEN write data — accepted review decisions extend
            # the ontology too (COG-112), so the enriched attribute is first-class
            # schema, mirroring the auto-apply path in run(). The returned
            # {attr -> resolved_datatype} map types each INSTANCE value with the
            # SAME datatype the attribute is DECLARED with (P1 fix): the stored
            # literal matches the declared range instead of a bare xsd:string.
            resolved_datatypes = await self._declare_attributes(
                job.tenant_id,
                job.type_name,
                applied_attr_values,
                kg_name=job.kg_name,
            )
            # Build the instance triples USING that map: primitives route through
            # validate_triple (typed literal, or a skip on a non-conforming value);
            # relationships write the entity IRI directly; provenance companions
            # stay plain string literals.
            triples: list[tuple[str, str, str]] = []
            clean_report = CleanReport()  # A3 ledger: partition every applied primitive value
            for d in accepted:
                datatype = resolved_datatypes.get(d.attribute, "string")
                triples.extend(
                    self._instance_triples_for_value(
                        d.entity_uri, job.type_name, d.attribute, d.proposed.value, datatype,
                        clean_report=clean_report,
                    )
                )
                triples.extend(
                    self._provenance_triples(
                        d.entity_uri, job.type_name, d.attribute, d.proposed
                    )
                )
            self._log_clean_report(clean_report, type_name=job.type_name, phase="apply_decisions")
            # Canonical companion-provenance-GRAPH records (F1) for the accepted
            # decisions, dated from the verdict — same seam as the auto-apply path
            # (gated by INFONA_PROVENANCE_ENABLED).
            prov_graph_triples = self._canonical_provenance_triples(
                accepted, job.type_name
            )
            # Same shared write path as run() / ingestion (graph/kg_writer.py):
            # batched insert + post-write housekeeping (cache-invalidate,
            # re-embed the type, recompute stats). E7: GraphStore when neo4j.
            await insert_facts(
                self._neptune,
                graph_uri,
                triples,
                provenance_triples=prov_graph_triples or None,
                store=resolve_optional_graph_store(),
            )
            await refresh_after_write(
                self._neptune,
                tenant_id=job.tenant_id,
                kg_name=job.kg_name,
                affected_types=self._affected_types(job.type_name, resolved_datatypes),
            )
        job.status = JobStatus.applied
        job.completed_at = _now()
        # Operator Job Trace (ONTA-387): human accept of staged conflicts is a
        # P6 write — record an action if a live trace is present.
        try:
            from infona_client.pipeline.stage_trace import (
                StageProjectId,
                attach_recorder,
            )

            rec = attach_recorder(job)
            if rec is not None:
                rec.action(
                    StageProjectId.p6,
                    "apply_decisions",
                    detail=f"accepted={applied}",
                    meta={"accepted": applied},
                )
                rec.end(
                    StageProjectId.p6,
                    output={
                        "status": "applied",
                        "accepted": applied,
                        "source": "conflict_review",
                    },
                )
                if job.stage_trace is not None:
                    job.stage_trace.status = "applied"
        except Exception:  # pragma: no cover - never block apply on obs
            pass
        await self._jobs.update(job)
        return applied


def _slug_from_uri(uri: str) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1]
