"""Schema-ground a parsed enrich request (attributes / scope / subset / tier).

Owns sanitizing an extracted request against the type's real schema, plus
the attribute-name normalizer and the confidence-gated soft matcher
(exact / high-similarity auto-accept; weaker unique hits need approval).

Invariants other agents must not break:
- Never silent-auto-map a weak unique suffix (sponsor → lead_sponsor).
- A pending weak map must not appear in ``attributes``.
- This module does not write the graph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from infona_client.agent.capabilities.enrich_common import _LIST_SPLIT_RE
from infona_client.agent.capabilities.enrich_types import _singularize
from infona_client.enrichment.models import EnrichmentTier


def _validate_enrich_request(
    parsed: dict,
    attr_names: list[str],
    rel_names: list[str],
    type_name: str | None = None,
) -> dict:
    """Sanitize an extracted request against the type's real schema.

    - attributes: each raw entry is first SPLIT into individual tokens (an
      extractor over multi-attribute phrasing sometimes crams a whole list — or a
      stray ``attributes:`` label — into one string, which would otherwise fuse
      into a single garbled token; see :func:`_split_attr_list`). Each token is
      normalized (a stray modifier word is dropped). Then, GROUNDED in the type's
      real schema: if ANY token names a declared attribute, keep ONLY the declared
      ones (canonical-cased) — a non-member sitting alongside real fields is a
      hallucination/garble and is dropped. If NONE match, the user is naming a
      brand-new attribute to add (e.g. "company"), so keep the clean new nouns —
      minus the target TYPE name itself, which is never a valid attribute of its
      own type (a common hallucination, e.g. "Physician" extracted for Physician).
    - scope.predicate: kept only if it resolves to a real attribute/relationship
      (case-insensitively); otherwise the scope is dropped (a bad scope would
      match nothing).
    - tier: web-fact backstop applied when missing/invalid.
    """
    known = {n.lower(): n for n in (*attr_names, *rel_names)}
    attr_lookup = {n.lower(): n for n in attr_names}

    raw_attrs = parsed.get("attributes") or []
    if isinstance(raw_attrs, str):
        raw_attrs = [raw_attrs]
    # Expand each raw entry into individual tokens, then normalize + de-dupe.
    candidates: list[str] = []
    seen: set[str] = set()
    for a in raw_attrs:
        for frag in _split_attr_list(a):
            norm = _normalize_attr(frag)
            if norm and norm.lower() not in seen:
                seen.add(norm.lower())
                candidates.append(norm)
    # Schema grounding with a confidence gate:
    #   • exact / high-similarity → auto-accept (canonical schema name)
    #   • weaker unique candidate (e.g. sponsor → lead_sponsor) → attr_approvals
    #     for an agent/user clarify (NEVER silent auto-map)
    #   • no plausible match → keep as a proposed new attribute name
    matched: list[str] = []
    seen_matched: set[str] = set()
    attr_approvals: list[dict] = []
    pending_from: set[str] = set()
    unmatched: list[str] = []
    for c in candidates:
        decision = _resolve_schema_attr(c, attr_lookup)
        if decision is None:
            if not _is_type_name(c, type_name):
                unmatched.append(c)
            continue
        if decision.needs_approval:
            attr_approvals.append(
                {
                    "from": c,
                    "to": decision.canonical,
                    "score": decision.score,
                    "reason": decision.reason,
                }
            )
            pending_from.add(c.lower())
            continue
        if decision.canonical.lower() not in seen_matched:
            seen_matched.add(decision.canonical.lower())
            matched.append(decision.canonical)
    if attr_approvals:
        # Fail-closed: pending weak maps must NOT appear in attributes (a
        # missed plan short-circuit would otherwise enrich a bogus new leaf
        # or bill paid web). Only auto-accepted schema members remain.
        attributes = matched
    elif matched:
        attributes = matched
    else:
        attributes = unmatched

    scope = parsed.get("scope")
    if isinstance(scope, dict) and scope.get("predicate") and scope.get("value"):
        pred = str(scope["predicate"]).strip()
        # Resolve against the real schema. When the schema is EMPTY (no ontology
        # available — e.g. a brand-new/uningested type) we can't validate, so we
        # keep the extracted predicate rather than silently dropping a valid scope.
        resolved = known.get(pred.lower(), pred if not known else None)
        scope = (
            {"predicate": resolved, "value": str(scope["value"]).strip()}
            if resolved
            else None
        )
    else:
        scope = None

    # Ranked/specific subset → a self-contained description + optional positive
    # int limit. Kept independent of the type schema (it is resolved later via a
    # SPARQL select, not validated against predicate names). A subset supersedes a
    # value-scope, so drop the scope when a subset is present.
    subset = parsed.get("subset")
    if isinstance(subset, dict) and str(subset.get("description") or "").strip():
        raw_limit = subset.get("limit")
        s_limit = (
            int(raw_limit)
            if isinstance(raw_limit, (int, float))
            and not isinstance(raw_limit, bool)
            and raw_limit > 0
            else None
        )
        subset = {"description": str(subset["description"]).strip(), "limit": s_limit}
        scope = None
    else:
        subset = None

    tier = parsed.get("tier")
    if tier not in {t.value for t in EnrichmentTier}:
        tier = _tier_for_attributes(attributes)

    return {
        "attributes": attributes,
        "scope": scope,
        "subset": subset,
        "tier": tier,
        "confidence_min": parsed.get("confidence_min", 0.85),
        # Non-empty → plan() must clarify before enriching. Empty list when every
        # attr was exact/high-similarity or a deliberate new leaf.
        "attr_approvals": attr_approvals,
    }


# Stray modifier / filler words an extractor must never emit as an attribute.
_STOPWORDS = {
    "current", "the", "a", "an", "their", "its", "his", "her", "missing",
    "this", "that", "these", "those", "all", "each", "every", "some", "new",
    "of", "for", "in", "on", "with",
}


# A leading "attributes:" / "field:" / "column:" label an extractor sometimes
# keeps on a crammed attribute string ("attributes: group_affiliation, npi").
# Stripped before splitting so it doesn't fuse into the first token.
_ATTR_LABEL_RE = re.compile(
    r"^\s*(?:attributes?|fields?|columns?|properties?)\s*[:=]\s*", re.IGNORECASE
)


def _split_attr_list(value) -> list[str]:
    """Split one extracted attribute entry into individual attribute tokens.

    An extractor over MULTI-attribute phrasing sometimes crams a whole list into
    a single string ("group_affiliation, board_certifications, npi") or keeps a
    stray ``attributes:`` label ("attributes: group_affiliation"). Left as one
    string, :func:`_normalize_attr` would fuse it into a single garbled token
    (e.g. ``attributes_group_affiliation``), silently collapsing four named
    fields into one bogus one. We strip a leading label and split on the same
    list delimiters the scope splitter uses, so each named field is validated on
    its own. A single clean value ("company", "group affiliation") returns as a
    lone element — ordinary single-attribute extraction is byte-for-byte
    unchanged.
    """
    if not isinstance(value, str):
        return []
    stripped = _ATTR_LABEL_RE.sub("", value.strip())
    if not stripped:
        return []
    return [p.strip() for p in _LIST_SPLIT_RE.split(stripped) if p.strip()]


def _is_type_name(candidate: str, type_name: str | None) -> bool:
    """True when ``candidate`` names the target type itself (singular/plural,
    case-insensitive) — never a valid attribute OF that type, so it is dropped as
    a hallucination (e.g. "Physician" extracted as an attribute of Physician)."""
    if not candidate or not type_name:
        return False
    return _singularize(candidate.lower()) == _singularize(type_name.lower())


def _normalize_attr(value) -> str:
    """Reduce an extracted attribute phrase to a clean predicate noun, or "".

    Strips a leading modifier ("current company" -> "company"), drops pure
    stopwords ("current" -> ""), and slugs spaces to underscores so the result
    is a usable attribute leaf name.
    """
    if not isinstance(value, str):
        return ""
    words = [w for w in re.split(r"\s+", value.strip()) if w]
    # Drop leading stopwords ("current company" -> "company").
    while words and words[0].lower() in _STOPWORDS:
        words.pop(0)
    # Stop at the first trailing stopword ("company for" -> "company").
    kept: list[str] = []
    for w in words:
        if w.lower() in _STOPWORDS:
            break
        kept.append(w)
    if not kept:
        return ""
    cleaned = "_".join(kept).strip("_-")
    return cleaned if cleaned and cleaned.lower() not in _STOPWORDS else ""


# Minimum length for a non-exact schema match. Short tokens like "id" or
# "name" would otherwise ambiguously map onto many attributes.
_SOFT_ATTR_MIN_LEN = 4
# Auto-accept without a clarify only at/above this SequenceMatcher ratio
# against the full schema leaf (typos like lead_sponsr → lead_sponsor).
_ATTR_AUTO_SIMILARITY = 0.85
# Weak (needs-approval) full-string floor — below this, ignore unless a
# structural suffix signal fires (last-token of a longer leaf).
_ATTR_WEAK_FULL_SIMILARITY = 0.70
# Score assigned to "candidate == last token of a longer leaf" (sponsor of
# lead_sponsor). Must stay BELOW auto so plan() asks before enriching.
_ATTR_SUFFIX_SCORE = 0.55


@dataclass(frozen=True)
class _AttrMatch:
    """Outcome of grounding one extracted attribute against the type schema."""

    canonical: str
    score: float
    reason: str
    needs_approval: bool


def _similarity(a: str, b: str) -> float:
    """0..1 string similarity on underscore-normalized leaves."""
    aa = (a or "").lower().replace("-", "_")
    bb = (b or "").lower().replace("-", "_")
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    return SequenceMatcher(None, aa, bb).ratio()


def _resolve_schema_attr(
    candidate: str, attr_lookup: dict[str, str]
) -> _AttrMatch | None:
    """Map an extracted attribute token onto a declared schema name.

    * **Exact** (case-insensitive) → auto-accept.
    * **High full-string similarity** (≥ ``_ATTR_AUTO_SIMILARITY``) to exactly
      one leaf → auto-accept (typos).
    * **Weak but intentional** → ``needs_approval=True`` (agent/user clarify):
        - unique structural suffix (``sponsor`` of ``lead_sponsor``), or
        - unique full-string ratio in [``_ATTR_WEAK_FULL_SIMILARITY``, auto).
    * Ambiguous / noise (e.g. ``website``↔``title`` at ~0.5) → ``None``.
    """
    if not candidate:
        return None
    key = candidate.lower()
    if key in attr_lookup:
        return _AttrMatch(
            canonical=attr_lookup[key],
            score=1.0,
            reason="exact",
            needs_approval=False,
        )
    if len(key) < _SOFT_ATTR_MIN_LEN or not attr_lookup:
        return None

    # (score, canonical, low, kind) kind ∈ {"full", "suffix"}
    scored: list[tuple[float, str, str, str]] = []
    for low, canonical in attr_lookup.items():
        full = _similarity(key, low)
        last = low.split("_")[-1] if "_" in low else low
        # Candidate is only the trailing role token of a longer leaf
        # (sponsor ⊂ lead_sponsor) — treat as structural suffix, never as a
        # "full" near-match, even if SequenceMatcher is coincidentally high.
        is_suffix = (last == key or low.endswith("_" + key)) and low != key
        if is_suffix:
            scored.append((_ATTR_SUFFIX_SCORE, canonical, low, "suffix"))
        elif full >= _ATTR_WEAK_FULL_SIMILARITY:
            scored.append((full, canonical, low, "full"))

    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1]))
    best_score, best_canonical, _best_low, best_kind = scored[0]
    # Near-tie on score → refuse to pick.
    if len(scored) > 1 and (best_score - scored[1][0]) < 0.05:
        return None
    # Shared last-token family (lead_sponsor + collaborator_sponsor): no winner.
    suffix_peers = [s for s in scored if s[3] == "suffix"]
    if best_kind == "suffix" and len(suffix_peers) > 1:
        return None

    needs_approval = best_score < _ATTR_AUTO_SIMILARITY
    reason = (
        "high_similarity"
        if not needs_approval
        else ("suffix" if best_kind == "suffix" else "weak_similarity")
    )
    return _AttrMatch(
        canonical=best_canonical,
        score=best_score,
        reason=reason,
        needs_approval=needs_approval,
    )


# Open-web / person / company facts the FREE Wikidata tier usually can't answer
# well — these should default to the paid web ``core`` tier (Parallel/Exa). Used
# only as a deterministic backstop when the LLM omits a tier.
_WEB_FACT_HINTS = {
    "company", "employer", "organization", "organisation", "website", "url",
    "homepage", "description", "bio", "summary", "reviews", "rating", "founder",
    "headquarters", "hq", "location", "address", "email", "phone", "title",
    "role", "position", "industry", "revenue", "funding", "ceo", "linkedin",
}


def _tier_for_attributes(attributes: list[str]) -> str:
    """Default tier: ``core`` (paid web) when any attribute is an open-web fact,
    else ``core`` anyway for safety — Wikidata-only ``lite`` is opt-in via the
    LLM (structured identifiers), not the silent default for a web lookup."""
    for a in attributes:
        if a.lower() in _WEB_FACT_HINTS:
            return EnrichmentTier.core.value
    # No clear structured-identifier signal → prefer the paid web tier so a
    # person/company lookup isn't silently downgraded to a Wikidata miss.
    return EnrichmentTier.core.value if attributes else EnrichmentTier.lite.value
