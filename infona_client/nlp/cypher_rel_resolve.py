"""Relationship-attribute resolve against the ontology summary.

Relationship INSTANCE edges live on ``onto/<leaf>``; this module only
*names* the leaf for a fixture, it does not write.
"""

from __future__ import annotations

import re
from typing import Any

from infona_client.nlp.cypher_types import (
    _SAFE_PROP_RE,
    _camel_words,
    _normalize_type_token,
    _singularize_token,
    resolve_type_name,
)

def _ontology_section_for_type(type_name: str, ontology_summary: str) -> str:
    """Return the Type: block for ``type_name`` (or full text if not found)."""
    text = ontology_summary or ""
    if not type_name:
        return text
    m = re.search(
        rf"(?ims)Type:\s*{re.escape(type_name)}\b.*?(?=^Type:|\Z)",
        text,
    )
    return m.group(0) if m else text


def _relationship_specs_in_section(section: str) -> list[tuple[str, str | None]]:
    """Parse relationship leaves + optional range types from a type section.

    Accepts both hand-written colon form and production
    ``format_schema_types_for_cypher`` form::

        - has_phase: relationship → Phase
        - has_phase -> Phase (relationship, key=has_phase)

    Returns ``(leaf, range_type_or_None)`` in section order, de-duped by leaf.
    """
    specs: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    patterns = (
        # Production: "- name -> Range (relationship, key=name)"
        r"(?im)^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)\s*->\s*"
        r"([A-Za-z_][A-Za-z0-9_]*)?\s*\([^)]*\brelationship\b"
        r"(?:,\s*key=([A-Za-z_][A-Za-z0-9_]*))?",
        # Colon form: "- name: relationship → Range" / "- name: relationship -> Range"
        r"(?im)^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
        r".*?\brelationship\b\s*(?:→|->)?\s*([A-Za-z_][A-Za-z0-9_]*)?",
    )
    for pat in patterns:
        for m in re.finditer(pat, section or ""):
            # Production: g1=name, g2=range, g3=key; colon: g1=name, g2=range
            key_or_name = (
                m.group(3)
                if m.lastindex and m.lastindex >= 3 and m.group(3)
                else m.group(1)
            )
            range_type = m.group(2) if m.lastindex and m.lastindex >= 2 else None
            if range_type and range_type.lower() in {
                "relationship",
                "literal",
                "string",
                "integer",
                "float",
                "boolean",
            }:
                range_type = None
            if not key_or_name:
                continue
            key = key_or_name.lower()
            if key in seen:
                continue
            seen.add(key)
            specs.append((key_or_name, range_type or None))
    return specs


def _relationship_leaves_in_section(section: str) -> list[str]:
    """Parse relationship attribute leaves from a type ontology section."""
    return [leaf for leaf, _rng in _relationship_specs_in_section(section)]


def _literal_leaves_in_section(section: str) -> set[str]:
    """Literal attribute leaves declared on a type section (lowercased).

    Shares production ``Attributes:`` + catalog dash-literal parsing with
    :func:`infona_client.nlp.numeric_attr_resolve.literal_leaves_for_type` so
    promoted-dim / money resolve sees the same leaves live ``/ask`` does.
    """
    from infona_client.nlp.numeric_attr_resolve import _literal_leaves_from_section

    return {leaf.lower() for leaf in _literal_leaves_from_section(section or "")}


# Generic CamelCase suffixes stripped when matching range **content** tokens
# (warehouse ↔ WarehouseNode). Bare suffix words must not bind any *Node type.
_RANGE_GENERIC_SUFFIXES = frozenset(
    {"node", "entity", "type", "object", "record", "item", "class"}
)


def _score_range_type_precision(rel_word: str, range_type: str) -> int:
    """High-precision range-type match tier for promoted-dim resolve.

    Returns 0 (no match) or a positive tier (higher = better). Deliberately
    does **not** reuse :func:`_score_type_match` — that scorer's substring /
    weak word-overlap tiers (``site``⊂``Website``, ``state``⊂``Statement``,
    ``form``⊂``Platform``) are too fuzzy for stealing a relationship leaf.

    Tiers (unique winner is decided on tier alone — no length bonus):

    * **3** — exact / singular full type name (``site`` ↔ ``Site``)
    * **2** — camel / multi-word join equals needle
      (``warehouse node`` ↔ ``WarehouseNode``)
    * **1** — content token of the range after stripping generic suffixes
      (``warehouse`` ↔ ``WarehouseNode``, ``phase`` ↔ ``PhaseNode``)

    Rejected: any substring-of-type / weak token-overlap tier.
    """
    if not rel_word or not range_type:
        return 0
    needle = _normalize_type_token(rel_word).lower()
    if not needle:
        return 0
    sing = _singularize_token(needle)
    tl = range_type.lower()
    tl_compact = re.sub(r"[^a-z0-9]", "", tl)
    words = _camel_words(range_type)

    # Tier 3: exact / singular full type name (incl. compact CamelCase).
    if needle in (tl, tl_compact) or sing in (tl, tl_compact):
        return 3

    raw_sp = re.sub(r"[_\-]+", " ", (rel_word or "").strip().lower())
    raw_sp = re.sub(r"\s+", " ", raw_sp).strip()
    sing_sp = " ".join(_singularize_token(w) for w in raw_sp.split() if w)

    # Tier 2: full camel-word join (spaces / underscores) equals needle.
    # Compact form (``warehousenode``) is already tier 3 via ``tl_compact``.
    if words:
        joined = " ".join(words)
        if joined in (raw_sp, sing_sp) or "".join(words) in (needle, sing):
            return 2

    # Tier 1: content tokens excluding generic Node/Entity/Type suffixes.
    content = [w for w in words if w not in _RANGE_GENERIC_SUFFIXES]
    if not content:
        return 0
    for tok in content:
        if len(tok) >= 3 and (needle == tok or sing == tok):
            return 1
    content_join = " ".join(content)
    if content_join in (raw_sp, sing_sp) or "".join(content) in (needle, sing):
        return 1
    return 0


def _literal_forms_block_range(rel_l: str, literal_leaves: set[str]) -> bool:
    """True when ``rel_l`` collides with a declared literal (incl. plurals).

    If the type still has literal leaf ``site``, both ``site`` and ``sites``
    must refuse range steal so equality keeps the literal prop.
    """
    if not rel_l or not literal_leaves:
        return False
    sing = _singularize_token(rel_l)
    lit_forms: set[str] = set()
    for lit in literal_leaves:
        lit_forms.add(lit)
        lit_forms.add(_singularize_token(lit))
    return rel_l in lit_forms or sing in lit_forms


def _resolve_via_range_type(
    rel_word: str,
    specs: list[tuple[str, str | None]],
    *,
    literal_leaves: set[str],
) -> str | None:
    """Map a dim word to a relationship leaf via the **range type** name.

    Hermetic ONTA-538 path: after CSV promotion, users still say the column /
    entity-type name ("site", "warehouse") even when the edge leaf is a verb
    (``stored_in``). Prefer a unique high-precision range-type hit; fall
    through (``None``) when ambiguous, when the word is also a declared
    **literal** on the type (incl. plural), or when only a fuzzy substring
    would match (literal equality / LLM owns those shapes — do not steal).
    """
    rel_l = (rel_word or "").strip().lower()
    if not rel_l:
        return None
    # If the NL word (or its singular) is a literal attribute on this type,
    # do not rebind via range (prefer equality / LLM rather than a confident
    # wrong related filter). Plural ``sites`` blocks when literal is ``site``.
    if _literal_forms_block_range(rel_l, literal_leaves):
        return None

    # (tier, leaf) — uniqueness is on **tier only**, never score*100+len.
    # Longer *Node types must not beat shorter peers at the same precision.
    scored: list[tuple[int, str]] = []
    for leaf, range_type in specs:
        if not range_type:
            continue
        tier = _score_range_type_precision(rel_word, range_type)
        if tier > 0:
            scored.append((tier, leaf))
    if not scored:
        return None
    best_tier = max(t for t, _ in scored)
    # Unique leaf at best tier — multiple ranges at the same tier fall through
    # (e.g. bare ``node`` must not pick WarehouseNode over PhaseNode via len).
    runners = list(dict.fromkeys(leaf for t, leaf in scored if t == best_tier))
    if len(runners) != 1:
        return None
    return runners[0]


def _resolve_relationship_attr(
    rel_word: str,
    *,
    type_name: str,
    ontology_summary: str,
) -> str | None:
    """Map a free-text dimension word to a relationship leaf on the type.

    Only returns leaves that the ontology marks as **relationships** on the
    subject type. No bare-text fallback onto literals.

    Resolution order (ONTA-538 / ONTA-537):

    1. Exact / ``has_`` / ``_by`` leaf name (string)
    2. Underscore-token equality on the leaf (``phase`` ⊂ ``has_phase``)
    3. **Range-type** name match (``site`` → ``stored_in → Site``) — hermetic
    4. **Semantic** :meth:`OntologyMentionIndex.resolve_rel` when fully embedded
       (e.g. ``warehouse`` → ``stored_in``) — ONTA-537

    Ambiguous multi-leaf hits return ``None`` so fixtures fall through rather
    than emit a confident empty related-entity filter.
    """
    rel = (rel_word or "").strip()
    if not rel or not _SAFE_PROP_RE.match(rel):
        return None
    section = _ontology_section_for_type(type_name, ontology_summary)
    specs = _relationship_specs_in_section(section)
    leaves = [leaf for leaf, _rng in specs]
    if not leaves:
        return None

    rel_l = rel.lower()
    sing = _singularize_token(rel_l)
    by_lower = {leaf.lower(): leaf for leaf in leaves}

    # Exact / has_ / _by first (no substring guessing).
    for cand in (rel_l, sing, f"has_{rel_l}", f"has_{sing}", f"{rel_l}_by", f"{sing}_by"):
        if cand in by_lower:
            return by_lower[cand]

    # Underscore-token equality only (avoid author ⊂ has_authority).
    token_hits = [
        leaf
        for leaf in leaves
        if rel_l in (set(leaf.lower().split("_")) - {"has", "by", "the", "a", "an"})
        or sing in (set(leaf.lower().split("_")) - {"has", "by", "the", "a", "an"})
    ]
    if len(token_hits) == 1:
        return token_hits[0]
    if len(token_hits) > 1:
        return None  # ambiguous leaf tokens — fall through

    # Range-type hermetic path (promoted dim columns still spoken as type names).
    range_hit = _resolve_via_range_type(
        rel,
        specs,
        literal_leaves=_literal_leaves_in_section(section),
    )
    if range_hit is not None:
        return range_hit

    # Semantic synonym path (e.g. "warehouse" → stored_in) under same guards
    # as type resolve: full candidate embedding set + query vector.
    from infona_client.nlp.ontology_mention_index import (
        OntologyMentionIndex,
        get_process_mention_index,
        get_resolve_context,
        lookup_query_embedding,
    )

    ctx = get_resolve_context()
    index = (
        ctx.mention_index
        if ctx is not None and ctx.mention_index is not None
        else get_process_mention_index()
    )
    if not isinstance(index, OntologyMentionIndex):
        return None
    if not index.rels_fully_embedded(leaves):
        return None
    query_embedding = lookup_query_embedding(rel, ctx)
    if query_embedding is None:
        # Also try singular form seed from pipeline phrase list.
        query_embedding = lookup_query_embedding(sing, ctx)
    if query_embedding is None:
        return None
    return index.resolve_rel(
        rel,
        query_embedding=query_embedding,
        rel_names=leaves,
    )


def _attr_is_relationship(attr: str, type_name: str, ontology_summary: str) -> bool:
    """True when ontology marks ``attr`` as a relationship on the type."""
    if not attr:
        return False
    leaves = {
        x.lower()
        for x in _relationship_leaves_in_section(
            _ontology_section_for_type(type_name, ontology_summary)
        )
    }
    return attr.lower() in leaves


