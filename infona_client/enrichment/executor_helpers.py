"""Pure helpers for the enrichment executor.

Call-time ``_host()`` lookups keep monkeypatches on ``executor`` working
(``logger``, ``insert_facts``, ``refresh_after_write``, ``openrouter_chat``,
``_spawn``, adapters).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from infona_client.enrichment.canonicalize import apply_canonicalizer
from infona_client.enrichment.executor_const import (
    ONTO_PRED_PREFIX,
    _ORG_ATTR_LEAVES,
    _ORG_TYPE_PREFERENCE,
)
from infona_client.enrichment.strategy import AttributeStrategy
from infona_client.graph.iri import ENTITY_URI_PREFIX, TYPE_URI_PREFIX
from infona_client.resolver.validator import _to_wkt_point


def _host():
    """Call-time lookup of the public ``executor`` module.

    Tests monkeypatch names on ``infona_client.enrichment.executor``
    (``insert_facts``, ``refresh_after_write``, ``delete_facts``, ``logger``,
    ``get_adapter``, ``ADAPTER_LOOKUP_TIMEOUT_S``, ``openrouter_chat``,
    ``_spawn``). Sibling modules must look these up at call time.
    """
    from infona_client.enrichment import executor as _mod

    return _mod


def _type_uri(type_name: str) -> str:
    return f"{TYPE_URI_PREFIX}{type_name}"


def _attr_uri(type_name: str, attr: str) -> str:
    return f"{TYPE_URI_PREFIX}{type_name}/attrs/{attr}"


def _strategy_version_with_instructions(
    strategy_version: str, instructions: str | None
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
    import os

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


def _slug_from_uri(uri: str) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1]
