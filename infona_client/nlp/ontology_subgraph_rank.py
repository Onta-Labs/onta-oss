"""Rank ontology relationship paths against an NL sketch.

Looked up on :mod:`infona_client.nlp.ontology_subgraph_match` at call time via
``_host()`` when a sibling needs a patchable name.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Sequence

from infona_client.nlp.cypher_generate import (
    _SAFE_PROP_RE,
    _camel_words,
    _score_range_type_precision,
    _singularize_token,
)
from infona_client.nlp.ontology_subgraph_types import (
    NlSketch,
    OntologyPath,
    RankedPath,
    _ALL_CUES,
    _EMBED_COSINE_SCALE,
    _LOCATION_LEAF_TOKENS,
    _LOCATION_RANGE_FAMILY,
    _LOCATIVE_CUES,
    _MULTI_HOP_PENALTY,
    _PATH_AMBIGUITY_MARGIN,
    _RELATIONAL_CUES,
    _SEMANTIC_REL_BOOST,
    _STOPWORDS,
)


def _is_location_range(range_type: str | None) -> bool:
    if not range_type:
        return False
    words = _camel_words(range_type)
    compact = re.sub(r"[^a-z0-9]", "", range_type.lower())
    for w in words:
        if w in _LOCATION_RANGE_FAMILY:
            return True
        if w.endswith("s") and w[:-1] in _LOCATION_RANGE_FAMILY:
            return True
    for fam in _LOCATION_RANGE_FAMILY:
        if fam in compact and len(fam) >= 4:
            return True
    return False


def _is_location_leaf(leaf: str) -> bool:
    tokens = set(leaf.lower().split("_"))
    return bool(tokens & _LOCATION_LEAF_TOKENS)


def _mention_matches_type(mention: str, type_name: str | None) -> bool:
    if not type_name or not mention:
        return False
    m = re.sub(r"[^a-z0-9]", "", mention.lower())
    t = re.sub(r"[^a-z0-9]", "", type_name.lower())
    if not m or not t:
        return False
    m_stem = m[:-1] if m.endswith("s") and len(m) > 3 else m
    return m == t or m_stem == t or m == t + "s" or t.startswith(m_stem)


def _score_path_against_sketch(
    sketch: NlSketch,
    path: OntologyPath,
    *,
    subject_type: str | None,
) -> RankedPath:
    """Score one path with string/family heuristics. Higher = better."""
    score = 0.0
    reasons: list[str] = []

    # Domain must match resolved subject when known.
    if subject_type:
        if path.domain_type.lower() != subject_type.lower():
            return RankedPath(path=path, score=0.0, reasons=("domain_mismatch",))
        score += 2.0
        reasons.append("domain_match")

    cues = {c.lower() for c in sketch.rel_cues}
    locative = bool(cues & _LOCATIVE_CUES)
    relational = bool(cues & _RELATIONAL_CUES)
    terminal = path.terminal_range
    hop_count = path.hop_count

    # Explicit dim word: high-precision range-type or leaf match (ONTA-538).
    # Match against first hop leaf *and* terminal range (value binds at end).
    dim_hit = False
    for dim in sketch.dim_mentions:
        dim_l = dim.lower()
        sing = _singularize_token(dim_l)
        # Any hop leaf
        for leaf in path.all_rel_attrs:
            leaf_l = leaf.lower()
            leaf_tokens = set(leaf_l.split("_")) - {"has", "by", "the", "a", "an"}
            if dim_l == leaf_l or sing == leaf_l:
                score += 8.0
                reasons.append(f"leaf_exact:{dim}")
                dim_hit = True
                break
            if dim_l in leaf_tokens or sing in leaf_tokens:
                score += 6.0
                reasons.append(f"leaf_token:{dim}")
                dim_hit = True
                break
        # Prefer terminal range for value-filter plans; also allow intermediate
        # only as weak multi-hop support (scored separately).
        if terminal:
            tier = _score_range_type_precision(dim, terminal)
            if tier >= 3:
                score += 8.0
                reasons.append(f"range_exact:{dim}→{terminal}")
                dim_hit = True
            elif tier == 2:
                score += 6.0
                reasons.append(f"range_camel:{dim}→{terminal}")
                dim_hit = True
            elif tier == 1:
                score += 5.0
                reasons.append(f"range_content:{dim}→{terminal}")
                dim_hit = True

    # When an explicit dim word is present, only dim-matched paths keep their
    # score — do not award generic locative family boosts to non-matches
    # (would tie ``has_region`` with ``stored_in`` for "at site East").
    has_explicit_dim = bool(sketch.dim_mentions)

    # Locative cue without (matching) dim: prefer location-ish edges.
    # For multi-hop, score terminal range / first leaf for family signals.
    if locative and not dim_hit and not has_explicit_dim:
        if _is_location_leaf(path.rel_attr) or any(
            _is_location_leaf(r) for r in path.all_rel_attrs
        ):
            score += 4.0
            reasons.append("locative_leaf")
        if _is_location_range(terminal):
            score += 4.0
            reasons.append("locative_range")
        # Mild base for any relationship when locative + value present
        # (allows non-family names when unique).
        if sketch.value_mentions and score < 2.0:
            score += 1.0
            reasons.append("locative_any_rel")

    # Relational "with"/"having" without dim: mild preference for any edge
    # when a value is present (fixtures like "with genre X" need dim usually).
    if (
        relational
        and not locative
        and not dim_hit
        and not has_explicit_dim
        and sketch.value_mentions
    ):
        score += 1.0
        reasons.append("relational_any_rel")

    # Value present is a prerequisite for name-filter plans.
    if sketch.value_mentions:
        score += 0.5
        reasons.append("has_value")

    # Multi-hop: prefer shorter paths; only reward extra hops when sketch
    # supports the intermediate (type mention) or terminal dim is the only fit.
    if hop_count > 1:
        score -= _MULTI_HOP_PENALTY * (hop_count - 1)
        reasons.append(f"multi_hop_penalty:{hop_count}")
        # Intermediate type mentioned in NL → sketch supports the chain.
        for mid in path.intermediate_types:
            for mention in sketch.type_mentions:
                if _mention_matches_type(mention, mid):
                    score += 3.0
                    reasons.append(f"intermediate_type:{mid}")
                    break
            for dim in sketch.dim_mentions:
                if _mention_matches_type(dim, mid):
                    score += 2.0
                    reasons.append(f"intermediate_dim:{mid}")
                    break
        # Terminal type/dim already scored above via range precision.
        # Extra support: second type mention matching terminal.
        for mention in sketch.type_mentions[1:]:
            if _mention_matches_type(mention, terminal):
                score += 2.0
                reasons.append(f"terminal_type_mention:{terminal}")
                break

    return RankedPath(path=path, score=score, reasons=tuple(reasons))


def _is_async_embedder(embedder: Any) -> bool:
    """True when embedder is an async batch EmbedFn (coroutine function / async __call__)."""
    if embedder is None or not callable(embedder):
        return False
    try:
        if inspect.iscoroutinefunction(embedder):
            return True
        call = getattr(embedder, "__call__", None)
        if call is not None and inspect.iscoroutinefunction(call):
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _is_sync_embedder(embedder: Any) -> bool:
    """True when embedder is a sync batch callable (not async EmbedFn)."""
    if embedder is None or not callable(embedder):
        return False
    return not _is_async_embedder(embedder)


def _l2_normalize_list(vec: Sequence[float]) -> list[float]:
    import math

    s = math.sqrt(sum(float(x) * float(x) for x in vec))
    if s == 0.0:
        return [float(x) for x in vec]
    return [float(x) / s for x in vec]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    # Assume roughly unit; still normalize for safety.
    na = _l2_normalize_list(a)
    nb = _l2_normalize_list(b)
    return sum(x * y for x, y in zip(na, nb))


def _sketch_embed_text(sketch: NlSketch) -> str:
    """Compact text for embedding the NL sketch for **path** ranking.

    Prefer dim / locative cues over type mentions: the subject type is already
    constrained by domain match, and including type names can dominate
    keyword-style FakeEmbedders (``gadget`` steals the vector from ``depot``).
    """
    parts: list[str] = []
    # Dim words are the primary path-ranking signal (warehouse, depot, site).
    parts.extend(sketch.dim_mentions)
    if sketch.dim_mentions:
        # Reinforce dims so they win over incidental tokens.
        parts.extend(sketch.dim_mentions)
    else:
        # No explicit dim: include type mention + cue as weak path signal.
        parts.extend(sketch.type_mentions)
        parts.extend(sketch.rel_cues)
    return " ".join(parts).strip() if parts else (sketch.question or "")


def _apply_embedder_boosts(
    sketch: NlSketch,
    ranked: list[RankedPath],
    embedder: Any,
) -> list[RankedPath]:
    """Boost ranked paths with cosine(sketch, path.signature) via sync embedder."""
    if not ranked or not _is_sync_embedder(embedder):
        return ranked
    texts = [_sketch_embed_text(sketch)] + [r.path.signature() for r in ranked]
    try:
        vectors = embedder(texts)
    except Exception:  # noqa: BLE001 — degrade, never crash ranking
        return ranked
    if not isinstance(vectors, (list, tuple)) or len(vectors) != len(texts):
        return ranked
    q_vec = vectors[0]
    out: list[RankedPath] = []
    for i, rp in enumerate(ranked):
        p_vec = vectors[i + 1]
        cos = _cosine(q_vec, p_vec)
        if cos <= 0:
            out.append(rp)
            continue
        boost = _EMBED_COSINE_SCALE * cos
        out.append(
            RankedPath(
                path=rp.path,
                score=rp.score + boost,
                reasons=rp.reasons + (f"embed_cos:{cos:.3f}",),
            )
        )
    return out


def _apply_semantic_index_boosts(
    sketch: NlSketch,
    ranked: list[RankedPath],
    *,
    mention_index: Any,
    query_embedding: Sequence[float] | None,
    subject_type: str | None,
) -> list[RankedPath]:
    """Boost paths whose rel leaf matches OntologyMentionIndex.resolve_rel.

    ONTA-537 guards: only when the index is healthy and **fully embedded** for
    the candidate relationship leaves. Partial indexes are ignored (fail open
    to string/family ranking — never invent edges from a subset).
    """
    if not ranked:
        return ranked
    try:
        from infona_client.nlp.ontology_mention_index import (
            OntologyMentionIndex,
            get_resolve_context,
            lookup_query_embedding,
        )
    except Exception:  # noqa: BLE001
        return ranked

    if not isinstance(mention_index, OntologyMentionIndex):
        return ranked

    # Collect candidate first-hop leaves (and all hops) for full-embed guard.
    # ONTA-537: never rank only a partially embedded subset of allowed leaves.
    # is_healthy() is type-centric — for rel scoring we only require full
    # embedding of the *relationship* candidate set (rel-only indexes OK).
    leaves = sorted({rel for rp in ranked for rel in rp.path.all_rel_attrs})
    if not leaves:
        return ranked
    if not mention_index.rels_fully_embedded(leaves):
        return ranked

    # Query tokens: dim mentions first, then loose rel-ish words from sketch.
    tokens: list[str] = list(sketch.dim_mentions)
    # Also try common synonym tokens from question when no dim (e.g. depot).
    if not tokens:
        # Peek non-stopword tokens from the question that look like dims.
        for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sketch.question or ""):
            tl = tok.lower()
            if tl in _STOPWORDS or tl in _ALL_CUES:
                continue
            if any(_mention_matches_type(tok, m) for m in sketch.type_mentions):
                continue
            if any(v.lower() == tl for v in sketch.value_mentions):
                continue
            if _SAFE_PROP_RE.match(tok):
                tokens.append(tok)
    if not tokens:
        return ranked

    ctx = get_resolve_context()
    q_emb = query_embedding
    resolved_leaves: set[str] = set()
    for tok in tokens:
        emb = q_emb
        if emb is None:
            emb = lookup_query_embedding(tok, ctx)
        if emb is None:
            # try singular
            emb = lookup_query_embedding(_singularize_token(tok.lower()), ctx)
        if emb is None:
            continue
        # Restrict resolve_rel to leaves present on subject domain when known.
        domain_leaves = leaves
        if subject_type:
            domain_leaves = sorted(
                {
                    rel
                    for rp in ranked
                    if rp.path.domain_type.lower() == subject_type.lower()
                    for rel in rp.path.all_rel_attrs
                }
            ) or leaves
        hit = mention_index.resolve_rel(
            tok, query_embedding=emb, rel_names=domain_leaves
        )
        if hit:
            resolved_leaves.add(hit)

    if not resolved_leaves:
        return ranked

    out: list[RankedPath] = []
    for rp in ranked:
        if any(rel in resolved_leaves for rel in rp.path.all_rel_attrs):
            hits = [r for r in rp.path.all_rel_attrs if r in resolved_leaves]
            out.append(
                RankedPath(
                    path=rp.path,
                    score=rp.score + _SEMANTIC_REL_BOOST,
                    reasons=rp.reasons
                    + tuple(f"semantic_rel:{h}" for h in hits),
                )
            )
        else:
            out.append(rp)
    return out


def rank_paths(
    sketch: NlSketch,
    paths: Sequence[OntologyPath],
    *,
    subject_type: str | None = None,
    embedder: Any | None = None,
    mention_index: Any | None = None,
    query_embedding: Sequence[float] | None = None,
) -> list[RankedPath]:
    """Rank ontology paths against a sketch (highest score first).

    Ranking layers (all optional after base heuristics):

    1. **String / family** — domain match, dim/range precision, locative family
       (always on; hermetic).
    2. **Multi-hop penalty** — shorter paths preferred when scores tie; multi-hop
       only wins when terminal/intermediate evidence supports the chain.
    3. **Embedder** — optional sync batch ``embedder(texts) -> vectors``; cosine
       boost between sketch text and :meth:`OntologyPath.signature`. Async
       :data:`~infona_client.nlp.ontology_mention_index.EmbedFn` is not called
       here — pre-embed via :func:`ground_ask_plan_async` or pass
       ``query_embedding`` + index.
    4. **Semantic dim index** — optional :class:`OntologyMentionIndex` with
       ONTA-537 full-embed guards; ``resolve_rel`` boosts synonym dims
       (``depot``/``warehouse`` → ``stored_in``). Partial / unhealthy index is
       ignored (degrade, never crash).
    """
    ranked = [
        _score_path_against_sketch(sketch, p, subject_type=subject_type)
        for p in paths
    ]
    ranked = [r for r in ranked if r.score > 0]

    if embedder is not None:
        ranked = _apply_embedder_boosts(sketch, ranked, embedder)

    if mention_index is not None:
        ranked = _apply_semantic_index_boosts(
            sketch,
            ranked,
            mention_index=mention_index,
            query_embedding=query_embedding,
            subject_type=subject_type,
        )

    # Prefer higher score; on ties prefer shorter hop count (fail-closed still
    # applies via _unique_winner when scores remain within margin).
    ranked.sort(
        key=lambda r: (
            -r.score,
            r.path.hop_count,
            r.path.rel_attr,
            r.path.domain_type,
            r.path.describe(),
        )
    )
    return ranked


def _unique_winner(
    ranked: Sequence[RankedPath],
) -> tuple[RankedPath | None, str]:
    """Return (winner, confidence). Fail closed on near-ties."""
    if not ranked:
        return None, "none"
    top = ranked[0]
    if top.score <= 0:
        return None, "none"
    if len(ranked) == 1:
        return top, "unique"
    second = ranked[1]
    # Same score band or within margin → ambiguous (never silent wrong edge).
    if abs(top.score - second.score) <= _PATH_AMBIGUITY_MARGIN:
        return None, "ambiguous"
    return top, "unique"
