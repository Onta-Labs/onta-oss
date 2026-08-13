"""Type-scoped semantic resolve for numeric / money-ish literal attributes.

Planning-layer helper for NL like ``price under 15`` when the ontology leaf
is ``unit_cost`` / ``list_price`` / ``amount`` (not a hard-coded 5-name list
scan over the whole ontology blob).

**Resolve order (highest wins):**

1. Exact leaf match on the subject type section (case-insensitive).
2. Normalized form (``unit cost`` ↔ ``unit_cost`` / ``unitCost``).
3. Money / numeric **family** heuristics (general concepts — price, cost,
   amount, fee, charge, rate, msrp, … — including camel/underscore variants).
4. Optional :class:`OntologyMentionIndex` attribute embeddings when the
   index is healthy **and** every allowed leaf is fully embedded (partial
   index must not invent leaves — same guard class as ONTA-537 types/rels).

**Fail closed:** when two leaves score within :data:`AMBIGUITY_MARGIN`, return
``confidence="ambiguous"`` with ``prop_key=None`` — never silent wrong prop.

Hermetic without an embedder; anti-overfit tests use synthetic type/leaf names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

# Cosine / score thresholds (aligned with ontology_mention_index spirit).
MIN_ACCEPT_SCORE = 0.42
AMBIGUITY_MARGIN = 0.04

# Safe property / attr keys only (never interpolate free text into Cypher).
_SAFE_PROP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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

# General money-ish stems (not product-specific CSV hardcodes).
# Matching is on normalized leaf tokens, not free-text substrings of ontology.
_MONEY_FAMILY_STEMS: frozenset[str] = frozenset(
    {
        "price",
        "cost",
        "amount",
        "fee",
        "charge",
        "rate",
        "msrp",
        "fare",
        "tariff",
        "premium",
        "premiums",
        "dues",
        "toll",
        "wage",
        "salary",
        "rent",
        "tuition",
        "payment",
        "payments",
        "value",  # weak alone; boosts with usd/currency neighbors
    }
)

# Strong money compounds / leaves (full-token after normalize).
# Family-only leaves like assay_cost / tuition_usd still match via stems;
# listing compounds here improves unique resolve when NL is bare price/cost.
_MONEY_STRONG_LEAVES: frozenset[str] = frozenset(
    {
        "price",
        "has_price",
        "cost",
        "has_cost",
        "unit_cost",
        "unitcost",
        "list_price",
        "listprice",
        "sale_price",
        "saleprice",
        "base_price",
        "baseprice",
        "amount",
        "value_usd",
        "valueusd",
        "msrp",
        "unit_price",
        "unitprice",
        "total_cost",
        "totalcost",
        "total_price",
        "totalprice",
        "net_price",
        "netprice",
        "gross_price",
        "grossprice",
        "fee",
        "charge",
        "rate",
        "assay_cost",
        "assaycost",
        "tuition",
        "tuition_usd",
        "tuitionusd",
        "tuition_cost",
        "tuitioncost",
    }
)

# NL cue phrases that imply a money compare even without an explicit leaf.
_MONEY_NL_CUES: frozenset[str] = frozenset(
    {
        "price",
        "prices",
        "priced",
        "cost",
        "costs",
        "costing",
        "cheaper",
        "expensive",
        "fee",
        "fees",
        "charge",
        "charges",
        "amount",
        "msrp",
        "fare",
        "rate",
        "rates",
        "payment",
        "payments",
        "tuition",
        "tuitions",
        "assay",
    }
)

# Broader numeric agg families (non-money) for ``total X of Y`` when prop missing.
_NUMERIC_GENERIC_STEMS: frozenset[str] = frozenset(
    {
        "qty",
        "quantity",
        "count",
        "score",
        "rating",
        "mileage",
        "enrollment",
        "reading",
        "weight",
        "height",
        "width",
        "length",
        "duration",
        "latency",
        "total",
        "sum",
        "average",
        "avg",
        "value",
        "number",
        "size",
        "capacity",
        "seats",
        "population",
    }
)

_TOKEN_SPLIT_RE = re.compile(r"[_\-\s]+|(?<=[a-z0-9])(?=[A-Z])")


def normalize_leaf_key(name: str) -> str:
    """Lowercase + collapse camel/underscore/hyphen to a single underscored key."""
    if not name:
        return ""
    s = name.strip()
    # Insert underscores at camel boundaries before lowercasing.
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_").lower()


def leaf_tokens(name: str) -> list[str]:
    """Tokenize a leaf or NL mention into lowercased word stems."""
    n = normalize_leaf_key(name)
    if not n:
        return []
    return [t for t in n.split("_") if t]


def is_money_family_leaf(leaf: str) -> bool:
    """True when ``leaf`` is a high-precision money/cost-shaped attribute name."""
    n = normalize_leaf_key(leaf)
    if not n:
        return False
    if n in _MONEY_STRONG_LEAVES:
        return True
    toks = leaf_tokens(leaf)
    if not toks:
        return False
    # Any token in strong money stems (price, cost, fee, …) qualifies.
    if any(t in _MONEY_FAMILY_STEMS for t in toks):
        # Avoid pure "value" without currency/money neighbor unless leaf is value_usd-ish.
        if toks == ["value"]:
            return False
        if "value" in toks and not any(
            t in _MONEY_FAMILY_STEMS - {"value"} or t in {"usd", "eur", "gbp", "currency"}
            for t in toks
        ):
            # value alone weak; value_usd / cost_value ok via other stems
            if not any(t in {"usd", "eur", "gbp", "currency"} for t in toks):
                return n in _MONEY_STRONG_LEAVES
        return True
    return False


def is_money_nl_cue(text: str) -> bool:
    """True when free text carries a money/cost cue (for compare intent)."""
    toks = re.findall(r"[A-Za-z_]+", (text or "").lower())
    return any(t in _MONEY_NL_CUES for t in toks)


_SAFE_LEAF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Dash-form catalog lines: "- unit_cost: float (literal, key=unit_cost)"
_DASH_LITERAL_LEAF_RE = re.compile(
    r"(?im)^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)\s*:"
    r".*?\b(?:literal|string|integer|float|boolean|number)\b"
)
_DASH_LITERAL_KEY_RE = re.compile(
    r"(?im)^\s*-\s*[A-Za-z_][A-Za-z0-9_]*\s*:.*\bliteral\b.*\bkey="
    r"([A-Za-z_][A-Za-z0-9_]*)"
)
# Production prompt formats (semantic retrieval / _fetch_ontology):
#   Attributes: description, sku, unit_cost
#   Attributes: name (string) — URI: <…>, unit_cost (float) — URI: <…> [no instances]
_ATTRIBUTES_LINE_RE = re.compile(r"(?im)^\s*Attributes:\s*(.+)$")


def _leaves_from_attributes_line_body(body: str) -> list[str]:
    """Extract attribute leaf names from an ``Attributes: …`` line body.

    Production ontology summaries use comma-separated attribute lists (with
    optional ``(datatype)``, ``— URI: <…>``, and ``[annotation]`` suffixes) —
    not the dash-literal catalog form. Bracket annotations may themselves
    contain commas (``[values: "a", "b"]``), so strip those first.
    """
    if not body or not body.strip():
        return []
    cleaned = body.strip()
    # Drop trailing junk after a bare "Relationships:" if a line was glued.
    cleaned = re.split(r"(?i)\bRelationships\s*:", cleaned, maxsplit=1)[0]
    # Strip [annotations] (enum values / no instances / unique counts).
    cleaned = re.sub(r"\[[^\]]*\]", "", cleaned)
    # Strip em-dash / hyphen URI suffixes.
    cleaned = re.sub(r"[—\-]\s*URI:\s*<[^>]*>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\bURI:\s*<[^>]*>", "", cleaned, flags=re.I)
    # Strip (datatype) / (literal) markers.
    cleaned = re.sub(r"\([^)]*\)", "", cleaned)
    ordered: list[str] = []
    seen: set[str] = set()
    for part in cleaned.split(","):
        tok = part.strip().strip("\"'")
        # First token of residual fragment is the leaf.
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\b", tok)
        if not m:
            continue
        leaf = m.group(1)
        if not _SAFE_LEAF_RE.match(leaf):
            continue
        key = leaf.lower()
        if key in seen:
            continue
        # Skip non-attribute filler words that sometimes leak into summaries.
        if key in {"attributes", "none", "uri", "type", "relationships"}:
            continue
        seen.add(key)
        ordered.append(leaf)
    return ordered


def _literal_leaves_from_section(section: str) -> list[str]:
    """Ordered unique literal / attribute leaves from one type block.

    Accepts both:

    * GraphStore catalog form: ``- unit_cost: float (literal, key=unit_cost)``
    * Production prompt form: ``Attributes: sku, unit_cost`` (semantic
      retrieval, ``_fetch_ontology``, instance-graph fallback)

    The production form does not tag datatype per leaf; every name on the
    Attributes line is a candidate leaf (relationships live on a separate
    ``Relationships:`` line). That is enough for money/family resolve, which
    scores by name — and is the format live ``/ask`` actually sees.
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(leaf: str) -> None:
        if not leaf or not _SAFE_LEAF_RE.match(leaf):
            return
        key = leaf.lower()
        if key in seen:
            return
        seen.add(key)
        ordered.append(leaf)

    text = section or ""
    for m in _DASH_LITERAL_LEAF_RE.finditer(text):
        _add(m.group(1))
    for m in _DASH_LITERAL_KEY_RE.finditer(text):
        _add(m.group(1))
    for m in _ATTRIBUTES_LINE_RE.finditer(text):
        for leaf in _leaves_from_attributes_line_body(m.group(1)):
            _add(leaf)
    return ordered


def literal_leaves_for_type(
    type_name: str | None,
    ontology_summary: str,
) -> list[str]:
    """Ordered unique literal leaves on ``type_name`` (preserve declaration order).

    When ``type_name`` is empty, returns de-duped leaves across the whole
    ontology summary (both dash-literal and ``Attributes:`` formats).
    """
    text = ontology_summary or ""
    if not type_name:
        return _literal_leaves_from_section(text)
    section = _ontology_section_for_type(type_name, text)
    return _literal_leaves_from_section(section)


@dataclass
class ScoredLeaf:
    leaf: str
    score: float
    reasons: tuple[str, ...] = ()


@dataclass
class NumericAttrResolve:
    """Result of type-scoped numeric/money attribute resolution."""

    mention: str
    type_name: str | None
    prop_key: str | None = None
    confidence: str = "none"  # "unique" | "ambiguous" | "none"
    candidates: list[ScoredLeaf] = field(default_factory=list)
    explanation: str = ""

    @property
    def is_unique(self) -> bool:
        return self.confidence == "unique" and bool(self.prop_key)


def _score_leaf_against_mention(
    leaf: str,
    mention: str,
    *,
    money_family: bool,
) -> ScoredLeaf | None:
    """Score one leaf vs NL mention. None when score is zero."""
    if not leaf or not _SAFE_PROP_RE.match(leaf):
        return None
    m_norm = normalize_leaf_key(mention)
    l_norm = normalize_leaf_key(leaf)
    m_toks = set(leaf_tokens(mention))
    l_toks = set(leaf_tokens(leaf))
    reasons: list[str] = []
    score = 0.0

    if m_norm and m_norm == l_norm:
        return ScoredLeaf(leaf=leaf, score=1.0, reasons=("exact",))

    # Prefix has_ / is_ stripped equality
    for prefix in ("has_", "is_", "the_"):
        if m_norm == prefix + l_norm or l_norm == prefix + m_norm:
            return ScoredLeaf(leaf=leaf, score=0.96, reasons=("exact_prefix_strip",))

    # Token Jaccard on multi-word mentions (unit cost ↔ unit_cost)
    if m_toks and l_toks:
        inter = m_toks & l_toks
        union = m_toks | l_toks
        if inter == m_toks and inter:
            # All mention tokens present in leaf
            score = max(score, 0.88 + 0.05 * min(len(inter), 2))
            reasons.append("token_cover")
        elif inter:
            j = len(inter) / max(len(union), 1)
            if j >= 0.5:
                score = max(score, 0.55 + 0.3 * j)
                reasons.append(f"token_jaccard={j:.2f}")

    money_leaf = is_money_family_leaf(leaf)
    money_mention = bool(m_toks & _MONEY_FAMILY_STEMS) or m_norm in _MONEY_STRONG_LEAVES
    if money_family or money_mention:
        if money_leaf:
            # Family hit without exact name — still strong for price↔unit_cost
            family_boost = 0.72
            # Prefer stronger compounds slightly over bare amount
            if l_norm in (
                "unit_cost",
                "list_price",
                "unit_price",
                "sale_price",
                "assay_cost",
                "tuition_usd",
                "tuition",
                "tuition_cost",
            ):
                family_boost = 0.74
            if l_norm in ("price", "cost", "has_price", "has_cost"):
                family_boost = 0.78
            # When mention is specifically "price" and leaf is unit_cost, keep
            # high but below exact price.
            if money_mention and money_leaf:
                score = max(score, family_boost)
                reasons.append("money_family")
        elif money_mention and not money_leaf:
            # Mention is money-ish but leaf is not — do not boost.
            pass

    # Generic numeric stems when not money-only
    if not money_family and m_toks & _NUMERIC_GENERIC_STEMS and l_toks & (
        _NUMERIC_GENERIC_STEMS | _MONEY_FAMILY_STEMS
    ):
        inter_g = m_toks & l_toks
        if inter_g:
            score = max(score, 0.65)
            reasons.append("numeric_stem")

    if score < MIN_ACCEPT_SCORE and score > 0:
        # Below threshold → discard
        return None
    if score <= 0:
        return None
    return ScoredLeaf(leaf=leaf, score=score, reasons=tuple(reasons) or ("heuristic",))


def _semantic_score_attrs(
    mention: str,
    leaves: Sequence[str],
    *,
    mention_index: Any,
    query_embedding: Sequence[float],
) -> dict[str, float]:
    """Return leaf → cosine when index has full attr embeddings for ``leaves``."""
    if mention_index is None or query_embedding is None:
        return {}
    # Prefer dedicated resolve_attr when present (ONTA attr kind).
    resolve_attr = getattr(mention_index, "resolve_attr", None)
    attrs_fully = getattr(mention_index, "attrs_fully_embedded", None)
    if callable(attrs_fully):
        try:
            if not attrs_fully(leaves):
                return {}
        except Exception:  # noqa: BLE001 — best-effort
            return {}
    # Rank each leaf via entry embeddings if kind=attr is available.
    get_entry = getattr(mention_index, "get_attr_entry", None)
    scores: dict[str, float] = {}
    try:
        import numpy as np
        from infona_client.nlp.embed_client import cosine_similarity

        # cosine_similarity(query 1-d, matrix n×d) — same contract as mention index.
        q = np.asarray(query_embedding, dtype=np.float32).reshape(-1)
        n = float(np.linalg.norm(q))
        if n > 0:
            q = q / n
        for leaf in leaves:
            emb = None
            if callable(get_entry):
                entry = get_entry(leaf)
                if entry is not None:
                    emb = getattr(entry, "embedding", None)
            if emb is None:
                # Fall back to scanning entries for kind=attr
                entries = getattr(mention_index, "_entries", {}) or {}
                key = f"attr:{leaf}"
                entry = entries.get(key) or entries.get(f"attr:{leaf.lower()}")
                if entry is not None:
                    emb = getattr(entry, "embedding", None)
            if emb is None:
                continue
            e = np.asarray(emb, dtype=np.float32).reshape(1, -1)
            sim = float(cosine_similarity(q, e)[0])
            if sim >= MIN_ACCEPT_SCORE:
                scores[leaf] = sim
    except Exception:  # noqa: BLE001
        return {}

    # Optional single-winner via resolve_attr (still only when fully embedded).
    if callable(resolve_attr) and not scores:
        try:
            winner = resolve_attr(
                mention,
                query_embedding=query_embedding,
                attr_names=list(leaves),
            )
            if winner and winner in leaves:
                scores[winner] = 0.85
        except Exception:  # noqa: BLE001
            pass
    return scores


def resolve_numeric_attr(
    mention: str,
    *,
    type_name: str | None = None,
    ontology_summary: str = "",
    mention_index: Any | None = None,
    query_embedding: Sequence[float] | None = None,
    money_family: bool = False,
    prefer_money: bool | None = None,
) -> NumericAttrResolve:
    """Resolve the best literal numeric leaf on ``type_name`` for ``mention``.

    When ``money_family`` / ``prefer_money`` is true (or the mention is a money
    NL cue), only money-ish leaves are considered as family fallbacks — but an
    exact leaf match always wins even if not money-shaped.
    """
    mention = (mention or "").strip()
    if not mention:
        return NumericAttrResolve(
            mention=mention,
            type_name=type_name,
            confidence="none",
            explanation="empty mention",
        )

    prefer = bool(money_family if prefer_money is None else prefer_money)
    if not prefer:
        prefer = is_money_nl_cue(mention) or bool(
            set(leaf_tokens(mention)) & _MONEY_FAMILY_STEMS
        )

    leaves = literal_leaves_for_type(type_name, ontology_summary)
    if not leaves:
        return NumericAttrResolve(
            mention=mention,
            type_name=type_name,
            confidence="none",
            explanation="no literal leaves on type",
        )

    scored: list[ScoredLeaf] = []
    for leaf in leaves:
        s = _score_leaf_against_mention(leaf, mention, money_family=prefer)
        if s is not None:
            scored.append(s)

    # Semantic boost when index is healthy/fully embedded for these leaves.
    if mention_index is not None and query_embedding is not None:
        # Prefer the declared leaf names as the candidate set for full-embed gate.
        sem = _semantic_score_attrs(
            mention,
            leaves,
            mention_index=mention_index,
            query_embedding=query_embedding,
        )
        by_leaf = {s.leaf: s for s in scored}
        for leaf, sim in sem.items():
            # Blend: semantic can introduce or boost a candidate.
            prev = by_leaf.get(leaf)
            blended = max(sim, (prev.score if prev else 0.0))
            # Slight boost when both heuristic + semantic agree.
            if prev is not None and prev.score >= MIN_ACCEPT_SCORE:
                blended = min(1.0, max(prev.score, sim) + 0.05)
            prev_reasons = prev.reasons if prev else ()
            reasons = prev_reasons + ("semantic",) if "semantic" not in prev_reasons else prev_reasons
            by_leaf[leaf] = ScoredLeaf(leaf=leaf, score=blended, reasons=reasons)
        scored = list(by_leaf.values())

    # When prefer money and we have no mention-specific scores, rank money leaves.
    if prefer and not scored:
        money_leaves = [L for L in leaves if is_money_family_leaf(L)]
        for i, leaf in enumerate(money_leaves):
            # Stable priority: price-like compounds > bare amount
            base = 0.70 - 0.01 * i
            n = normalize_leaf_key(leaf)
            if n in ("price", "has_price"):
                base = 0.80
            elif n in (
                "unit_cost",
                "list_price",
                "cost",
                "has_cost",
                "unit_price",
                "assay_cost",
                "tuition_usd",
                "tuition",
                "tuition_cost",
            ):
                base = 0.76
            scored.append(ScoredLeaf(leaf=leaf, score=base, reasons=("money_fallback",)))

    if not scored:
        return NumericAttrResolve(
            mention=mention,
            type_name=type_name,
            confidence="none",
            explanation="no candidate leaves scored",
        )

    scored.sort(key=lambda s: (-s.score, s.leaf.lower()))
    top = scored[0]
    if top.score < MIN_ACCEPT_SCORE:
        return NumericAttrResolve(
            mention=mention,
            type_name=type_name,
            confidence="none",
            candidates=scored[:5],
            explanation="top score below threshold",
        )

    if len(scored) > 1:
        second = scored[1]
        if abs(top.score - second.score) < AMBIGUITY_MARGIN:
            return NumericAttrResolve(
                mention=mention,
                type_name=type_name,
                prop_key=None,
                confidence="ambiguous",
                candidates=scored[:5],
                explanation=(
                    f"ambiguous leaves {top.leaf!r} vs {second.leaf!r} "
                    f"(scores {top.score:.3f}/{second.score:.3f})"
                ),
            )

    return NumericAttrResolve(
        mention=mention,
        type_name=type_name,
        prop_key=top.leaf,
        confidence="unique",
        candidates=scored[:5],
        explanation=f"resolved {mention!r} → {top.leaf} ({', '.join(top.reasons)})",
    )


def resolve_cost_prop(
    ontology_summary: str,
    *,
    type_name: str | None = None,
    mention: str = "price",
    mention_index: Any | None = None,
    query_embedding: Sequence[float] | None = None,
) -> str | None:
    """Type-scoped money prop resolve — upgrade over fixed candidate list scan.

    Returns the unique leaf or ``None`` when ambiguous / missing (fail closed).
    Does **not** invent a default ``price`` when the type has no money leaf.
    """
    r = resolve_numeric_attr(
        mention or "price",
        type_name=type_name,
        ontology_summary=ontology_summary,
        mention_index=mention_index,
        query_embedding=query_embedding,
        money_family=True,
    )
    if r.confidence == "unique" and r.prop_key:
        return r.prop_key
    return None


__all__ = [
    "AMBIGUITY_MARGIN",
    "MIN_ACCEPT_SCORE",
    "NumericAttrResolve",
    "ScoredLeaf",
    "is_money_family_leaf",
    "is_money_nl_cue",
    "leaf_tokens",
    "literal_leaves_for_type",
    "normalize_leaf_key",
    "resolve_cost_prop",
    "resolve_numeric_attr",
]
