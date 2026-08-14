"""Type-name matching against the planning ontology summary.

Never drop THIS-KG populated types from planning context — matching
here only *selects* a type, it does not filter the prompt catalog.
"""

from __future__ import annotations

import re
from typing import Any

_TRAILING_PUNCT_RE = re.compile(r"[?!.\s]+$")

# Strip trailing plural / noise words for type matching.
_NOISE_RE = re.compile(
    r"(?i)\b(?:entities|records|rows|items|entries|instances|of|the|a|an)\b"
)
_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9_]+")

# Types listed in ontology summary lines like "Type: Person".
# Do NOT match bare "- attr" lines (those are attributes, not types).
_TYPE_LINE_RE = re.compile(
    r"(?im)^\s*Type:\s*([A-Za-z][A-Za-z0-9_]*)\b"
)

# Safe property / attr keys only (never interpolate free text into Cypher).
_SAFE_PROP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 200


def extract_type_names_from_ontology(ontology_summary: str) -> list[str]:
    """Best-effort type leaves from the SPARQL-era ontology summary text."""
    names: list[str] = []
    seen: set[str] = set()
    for m in _TYPE_LINE_RE.finditer(ontology_summary or ""):
        name = m.group(1)
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        names.append(name)
    return names



_TYPE_ACTIVITY_RE = re.compile(
    r"(?im)^\s*Type:\s*([A-Za-z][A-Za-z0-9_]*)\b(?:\s*\((\d+)\s+entities?\)|\s*\[no instances\])?"
)


def extract_type_activity_from_ontology(ontology_summary: str) -> dict[str, int]:
    """Map type leaf → instance count hint from ontology summary text.

    ``[no instances]`` → 0; ``(N entities)`` → N; bare ``Type: X`` → -1 (unknown).
    Used to prefer types that actually have data in this KG over empty tenant
    leftovers (e.g. Product with 0 vs InventoryItem with 6).
    """
    out: dict[str, int] = {}
    for m in _TYPE_ACTIVITY_RE.finditer(ontology_summary or ""):
        name = m.group(1)
        if m.group(0).lower().endswith("[no instances]"):
            out[name] = 0
        elif m.group(2) is not None:
            out[name] = int(m.group(2))
        else:
            out.setdefault(name, -1)
    return out


def _normalize_type_token(text: str) -> str:
    t = _NOISE_RE.sub(" ", text or "")
    t = _NON_ALNUM_RE.sub("", t.strip())
    return t


# Common irregular plurals → singular for type matching / guessing.
_IRREGULAR_SINGULAR = {
    "people": "person",
    "men": "man",
    "women": "woman",
    "children": "child",
    "mice": "mouse",
}


def _singularize_token(token: str) -> str:
    """Best-effort English singular for type matching (general, not domain-specific)."""
    t = (token or "").lower()
    if not t:
        return t
    if t in _IRREGULAR_SINGULAR:
        return _IRREGULAR_SINGULAR[t]
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith(("sses", "ches", "shes", "xes")):
        return t[:-2]
    if t.endswith("s") and not t.endswith("ss") and len(t) > 2:
        return t[:-1]
    return t


def _camel_words(name: str) -> list[str]:
    """Split PascalCase / snake_case type leaves into lower words."""
    if not name:
        return []
    s = re.sub(r"[_\-]+", " ", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return [w.lower() for w in s.split() if w]


def _label_alternatives(label: str) -> list[str]:
    """Split multi-option NL labels: 'inventory items or SKUs' → candidates.

    Keeps full phrase first so exact multi-word matches still win, then each
    ``or`` / ``/`` / comma alternative.
    """
    raw = (label or "").strip()
    if not raw:
        return []
    parts = re.split(r"(?i)\s+(?:or|/)\s+|,\s*", raw)
    out: list[str] = []
    seen: set[str] = set()
    for p in [raw, *parts]:
        p = p.strip()
        if not p:
            continue
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _score_type_match(label: str, type_name: str) -> int:
    """Score how well a free-text label matches one ontology type leaf.

    Higher is better. 0 = no match. Prefer longer / more specific types so
    compound CamelCase leaves beat short accidental tokens.
    """
    if not label or not type_name:
        return 0
    needle = _normalize_type_token(label).lower()
    if not needle:
        return 0
    tl = type_name.lower()
    tl_compact = re.sub(r"[^a-z0-9]", "", tl)
    sing = _singularize_token(needle)
    words = _camel_words(type_name)
    head = words[-1] if words else tl_compact
    score = 0

    if needle == tl or needle == tl_compact:
        score = 1000
    elif sing == tl or sing == tl_compact:
        score = 950
    elif words and " ".join(words) == needle.replace("_", " "):
        score = 900
    elif words and " ".join(words) == sing:
        score = 890
    elif head and (needle == head or sing == head) and len(head) >= 3:
        # "trials" → ClinicalTrial (head noun of CamelCase compound)
        score = 800 + min(len(head), 40)
    elif len(sing) >= 4 and sing in tl_compact and len(sing) >= max(4, len(tl_compact) // 2):
        # Needle singular is a substantial substring of the type (not type ⊂ needle).
        score = 500 + min(len(sing), 40)
    elif len(needle) >= 4 and needle in tl_compact and len(needle) >= max(4, len(tl_compact) // 2):
        score = 400 + min(len(needle), 40)
    else:
        # Word-token overlap with CamelCase parts (require content match ≥3 chars)
        label_tokens = {
            _singularize_token(_normalize_type_token(w))
            for w in re.split(r"[^a-zA-Z0-9]+", label)
            if w and _normalize_type_token(w)
        }
        label_tokens = {t for t in label_tokens if len(t) >= 3}
        type_tokens = set(words) | {tl_compact}
        overlap = label_tokens & type_tokens
        if overlap:
            score = 300 + 20 * len(overlap) + max(len(t) for t in overlap)

    if score <= 0:
        return 0
    # Tie-break: longer type names win (more specific compound leaves).
    return score * 100 + min(len(type_name), 99)


def match_type_name(
    label: str,
    type_names: list[str],
    ontology_summary: str = "",
) -> str | None:
    """Match a free-text label to an ontology type leaf (case-insensitive).

    General matching (no domain hard-codes):

    * exact / singular / plural
    * CamelCase head-noun (``trials`` → ``ClinicalTrial`` / ``TrialRun``)
    * multi-alternative labels (``A or B``)
    * singularized containment and token overlap
    * **prefer types with instances in this KG** over empty tenant leftovers
      (``Product [no instances]`` loses to ``InventoryItem (6 entities)``)

    Returns None on no match or when two distinct types score equally best
    (ambiguous) so callers can fall through to the LLM instead of inventing
    a type and counting zero.
    """
    if not label or not type_names:
        return None

    activity = extract_type_activity_from_ontology(ontology_summary)
    best: dict[str, int] = {}
    for alt in _label_alternatives(label):
        for t in type_names:
            s = _score_type_match(alt, t)
            if s <= 0:
                continue
            # Demote empty types so "products" does not bind a zero-row Product
            # leftover when InventoryItem has data in this KG.
            act = activity.get(t, -1)
            if act == 0:
                s = max(1, s // 20)
            elif act > 0:
                s = s + 2000  # strong preference for types present in-KG
            prev = best.get(t, 0)
            if s > prev:
                best[t] = s

    if not best:
        return None

    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    top_type, top_score = ranked[0]
    # Ambiguous: two different types within the same score band.
    if len(ranked) > 1:
        second_type, second_score = ranked[1]
        if second_type != top_type and second_score // 100 == top_score // 100:
            return None
    return top_type


def guess_type_name(label: str) -> str | None:
    """PascalCase type guess when ontology is empty (tests / bootstrap)."""
    raw = _normalize_type_token(label)
    if not raw:
        return None
    lower = raw.lower()
    if lower in _IRREGULAR_SINGULAR:
        stem = _IRREGULAR_SINGULAR[lower]
        return stem[0].upper() + stem[1:]
    # Prefer last content token for multi-word labels ("inventory skus" → Sku)
    parts = [p for p in re.split(r"[^a-zA-Z0-9]+", raw) if p]
    if parts:
        raw = parts[-1]
        lower = raw.lower()
    guess = raw
    if guess.islower():
        guess = guess[0].upper() + guess[1:]
        if guess.endswith("s") and len(guess) > 1:
            guess = guess[:-1]
    elif guess.endswith("s") and len(guess) > 1 and guess[:-1].istitle():
        # Books → Book when already title-ish
        if guess[-2].islower():
            guess = guess[:-1]
    return guess or None


def resolve_type_name(
    label: str,
    type_names: list[str] | None,
    ontology_summary: str = "",
    *,
    mention_index: Any | None = None,
    query_embedding: Any | None = None,
    require_semantic: bool = False,
) -> str | None:
    """Resolve a free-text type mention to an ontology leaf.

    When the ontology is known (non-empty ``type_names`` / summary types),
    **never invent** a PascalCase guess — a miss returns ``None`` so fixtures
    fall through to the LLM instead of counting a non-existent type as 0.

    Prefer types that have instances in this KG. Binding an empty leftover
    type (tenant pollution: ``Product [no instances]`` while ``InventoryItem``
    has rows) yields silent wrong zeros — better to fall through to the LLM
    which sees the full schema.

    **Semantic path (ONTA-537):** when ``mention_index`` is an
    :class:`~infona_client.nlp.ontology_mention_index.OntologyMentionIndex`
    with **every allowed candidate embedded**
    (:meth:`~OntologyMentionIndex.types_fully_embedded`) and a
    ``query_embedding`` is provided, rank by cosine + hierarchy + instance
    prior. A partial index (only some types embedded) skips semantic entirely
    so a product synonym cannot bind an unrelated embedded leaf.

    Activity and hierarchy are **call-local only** — this function never
    mutates the process-global index (``merge_activity`` / ``set_hierarchy``).

    **Fail-closed is opt-in:** ``require_semantic=True`` or
    ``INFONA_REQUIRE_SEMANTIC_RESOLVE=1`` without a ready index / embed config
    raises
    :class:`~infona_client.nlp.ontology_mention_index.EmbedConfigError`.
    Default remains best-effort (semantic when ready, else string). Until
    cold-start reindex is solid, production should leave the env flag off.
    """
    from infona_client.nlp.ontology_mention_index import (
        EmbedConfigError,
        OntologyMentionIndex,
        get_process_mention_index,
        get_resolve_context,
        lookup_query_embedding,
    )

    ctx = get_resolve_context()
    if mention_index is None and ctx is not None and ctx.mention_index is not None:
        mention_index = ctx.mention_index
    if not require_semantic and ctx is not None and ctx.require_semantic:
        require_semantic = True
    if query_embedding is None and ctx is not None:
        query_embedding = lookup_query_embedding(label, ctx)

    index = mention_index if mention_index is not None else get_process_mention_index()

    names = (
        list(type_names)
        if type_names is not None
        else extract_type_names_from_ontology(ontology_summary)
    )

    # require_semantic: need index + every candidate embedded + query vector.
    if require_semantic:
        if (
            not isinstance(index, OntologyMentionIndex)
            or not index.types_fully_embedded(names if names else None)
            or query_embedding is None
        ):
            # Allow tests that pass require_semantic without a query vector to
            # fail early when the index itself is missing/unhealthy; if names
            # is empty, invent is forbidden below.
            if not isinstance(index, OntologyMentionIndex) or not index.is_healthy():
                raise EmbedConfigError(
                    "NL type resolution with require_semantic / "
                    "INFONA_REQUIRE_SEMANTIC_RESOLVE needs a healthy ontology "
                    "mention embed index (set INFONA_OPENROUTER_API_KEY / "
                    "OPENROUTER_API_KEY, reindex types, or inject "
                    "OntologyMentionIndex for tests). Fail-closed is opt-in."
                )
            if names and not index.types_fully_embedded(names):
                raise EmbedConfigError(
                    "Partial ontology mention index: not every allowed type "
                    "candidate is embedded. Refusing semantic resolve under "
                    "require_semantic (would risk binding a wrong leaf)."
                )
            if query_embedding is None and isinstance(index, OntologyMentionIndex):
                # Index present but no vector for this mention — miss, not invent.
                if names:
                    return None

    if not names:
        # Bootstrap / empty ontology only — invent PascalCase for hermetic tests.
        # Semantic path still refuses invent when require_semantic.
        if require_semantic:
            return None
        return guess_type_name(label)

    activity = extract_type_activity_from_ontology(ontology_summary)

    # Call-local hierarchy from ontology text (do not set_hierarchy on index).
    hierarchy_overlay: dict[str, str] | None = None
    if ontology_summary:
        from infona_client.graph.rdfs_helpers import (
            extract_subclass_map_from_ontology,
        )

        smap = extract_subclass_map_from_ontology(ontology_summary)
        if smap:
            hierarchy_overlay = smap

    # Semantic resolve only when every allowed candidate is embedded.
    if (
        isinstance(index, OntologyMentionIndex)
        and index.types_fully_embedded(names)
        and query_embedding is not None
    ):
        hit = index.resolve_type(
            label,
            query_embedding=query_embedding,
            activity=activity or None,
            type_names=names,
            hierarchy=hierarchy_overlay,
        )
        if hit is not None:
            return hit
        # Semantic miss: do not invent; string path only when not require_semantic
        # (fast path for exact/plural — still subject to empty-leftover rules).
        if require_semantic:
            return None

    # String heuristics (fast path / hermetic fixtures / partial index).
    active = [t for t in names if activity.get(t, -1) > 0]
    if active:
        hit = match_type_name(label, active, ontology_summary=ontology_summary)
        if hit is not None:
            return hit
        # No active-type match. Avoid binding a known-empty type for fixtures.
        empty_hit = match_type_name(label, names, ontology_summary=ontology_summary)
        if empty_hit is not None and activity.get(empty_hit) == 0:
            return None
        return empty_hit

    return match_type_name(label, names, ontology_summary=ontology_summary)


async def resolve_type_name_async(
    label: str,
    type_names: list[str] | None,
    ontology_summary: str = "",
    *,
    mention_index: Any | None = None,
    require_semantic: bool = False,
    embed_fn: Any | None = None,
) -> str | None:
    """Async resolve: embed ``label`` then :func:`resolve_type_name` (ONTA-537).

    Prefer this when a mention index may be ready. Inject ``embed_fn`` in
    hermetic tests (FakeEmbedder); omit to use the OpenRouter client
    (raises :class:`EmbedConfigError` when ``require_semantic`` and no key).
    """
    from infona_client.nlp.ontology_mention_index import (
        EmbedConfigError,
        OntologyMentionIndex,
        get_process_mention_index,
        openrouter_embed_fn,
    )

    index = mention_index if mention_index is not None else get_process_mention_index()
    names = (
        list(type_names)
        if type_names is not None
        else extract_type_names_from_ontology(ontology_summary)
    )
    ready = isinstance(index, OntologyMentionIndex) and index.types_fully_embedded(
        names if names else None
    )
    use_semantic = require_semantic or ready
    if not use_semantic:
        return resolve_type_name(
            label, type_names, ontology_summary, require_semantic=require_semantic
        )

    if not ready:
        if require_semantic:
            raise EmbedConfigError(
                "require_semantic set but ontology mention index is missing "
                "or partial (not every allowed type candidate is embedded)."
            )
        return resolve_type_name(label, type_names, ontology_summary)

    fn = embed_fn
    if fn is None:
        fn = openrouter_embed_fn()
    vecs = await fn([label.strip()])
    if not vecs:
        if require_semantic:
            return None
        return resolve_type_name(label, type_names, ontology_summary)
    return resolve_type_name(
        label,
        type_names,
        ontology_summary,
        mention_index=index,
        query_embedding=vecs[0],
        require_semantic=require_semantic,
    )


