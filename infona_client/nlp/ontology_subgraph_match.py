"""NL sketch → ontology subgraph match → grounded /ask plan (planning layer).

Grounding spine for questions like ``how many widgets in east``:

1. **Extract** an :class:`NlSketch` (type mentions, values, locative/rel cues, intent).
2. **Enumerate** ontology relationship paths (1-hop and multi-hop chains).
3. **Rank** paths against the sketch (location-ish cues, range-type synonyms,
   optional embedder cosine + :class:`OntologyMentionIndex` semantic dims).
4. **Ground** a structured :class:`GroundedAskPlan` for the LLM prompt
   (preferred ADR 0013 template + safe params) — never silent free-form Cypher.

**Product rules:**

* User-facing ``/ask`` always uses the LLM for final Cypher generation.
  This module only supplies **structured context** (prompt injection / plan
  metadata). It must **not** short-circuit the LLM path.
* **Fail closed** on ambiguity (two equally good edges to Site) → return a
  plan with ``confidence="ambiguous"`` / ``None`` winner so the binder does
  not pick a silent wrong edge.
* Anti-overfit: synthetic type names only in tests; ranking uses **general**
  location/rel families and range-type precision (ONTA-538), not persona CSV
  column hardcodes.
* Multi-hop (default max 2 hops, hard cap 3): prompt-only grounding when no
  multi-hop ADR 0013 template exists — never invent raw Cypher in params.
* Embedder / mention-index ranking is **optional**; absent → hermetic
  string/family heuristics only (no network, no crash).

See also: :mod:`infona_client.nlp.cypher_generate` (type / rel resolve),
:mod:`infona_client.nlp.ontology_mention_index` (ONTA-537 full-embed guards),
ADR 0013 templates (``related_entity_name_filter``, ``entities_of_type_count``).
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from infona_client.graph.rdfs_helpers import (
    TEMPLATE_ENTITIES_OF_TYPE,
    TEMPLATE_ENTITIES_OF_TYPE_COUNT,
    TEMPLATE_RELATED_ENTITY_NAME_FILTER,
    type_names_with_subclasses,
)
from infona_client.nlp.cypher_generate import (
    DEFAULT_LIST_LIMIT,
    _SAFE_PROP_RE,
    _camel_words,
    _ontology_section_for_type,
    _relationship_specs_in_section,
    _score_range_type_precision,
    _singularize_token,
    extract_type_names_from_ontology,
    resolve_type_name,
)

# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

# Safe template param keys only — never free-text Cypher fragments.
_SAFE_PARAM_KEYS = frozenset(
    {
        "type_names",
        "rel_attr",
        "target_name",
        "limit",
        "after_id",
        "from_types",
        "to_types",
        "prop_key",
        "prop_value",
    }
)

# Prepositions / relational cues that signal "subject linked via rel to value".
_LOCATIVE_CUES = frozenset(
    {
        "in",
        "at",
        "from",
        "near",
        "inside",
        "within",
        "into",
        "on",
    }
)
_RELATIONAL_CUES = frozenset(
    {
        "with",
        "having",
        "of",
        "via",
        "by",
        "for",
    }
)
_ALL_CUES = _LOCATIVE_CUES | _RELATIONAL_CUES

# Generic English tokens that are never type/value mentions.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "all",
        "any",
        "some",
        "of",
        "to",
        "and",
        "or",
        "are",
        "is",
        "was",
        "were",
        "be",
        "been",
        "do",
        "does",
        "did",
        "we",
        "you",
        "they",
        "i",
        "me",
        "my",
        "our",
        "their",
        "there",
        "here",
        "how",
        "many",
        "much",
        "what",
        "which",
        "who",
        "where",
        "when",
        "why",
        "count",
        "number",
        "total",
        "list",
        "show",
        "find",
        "get",
        "please",
        "me",
        "entities",
        "records",
        "rows",
        "items",
        "entries",
        "instances",
        "exist",
        "exists",
        "have",
        "has",
        "that",
        "this",
        "those",
        "these",
        "limit",
    }
)

# General location-ish range content words / leaf tokens (not domain-specific
# CSV columns — "warehouse" as a *concept family*, not a hard-coded success path).
_LOCATION_RANGE_FAMILY = frozenset(
    {
        "site",
        "location",
        "place",
        "facility",
        "plant",
        "warehouse",
        "store",
        "building",
        "region",
        "area",
        "zone",
        "wing",
        "room",
        "floor",
        "depot",
        "hub",
        "center",
        "centre",
        "office",
        "campus",
        "city",
        "country",
        "state",
        "address",
    }
)
_LOCATION_LEAF_TOKENS = frozenset(
    {
        "stored",
        "store",
        "located",
        "locate",
        "location",
        "site",
        "at",
        "in",
        "region",
        "place",
        "facility",
        "warehouse",
        "based",
        "housed",
        "belongs",
        "belonging",
    }
)

_TRAILING_PUNCT_RE = re.compile(r"[?!.\s]+$")
_COUNT_PREFIX_RE = re.compile(
    r"(?ix)^"
    r"(?:how\s+many|count(?:\s+the|\s+of)?|number\s+of|total(?:\s+number\s+of)?)"
    r"\s+"
)
_LIST_PREFIX_RE = re.compile(
    r"(?ix)^"
    r"(?:list|show(?:\s+me)?|get|find|what\s+are|which)"
    r"\s+"
)
# "<type(s)> <cue> [the] [<dim>] <value...>"
_SKETCH_PATH_RE = re.compile(
    r"(?ix)^"
    r"(?P<label>.+?)\s+"
    r"(?P<cue>in|at|from|near|inside|within|into|on|with|having|of|via|by|for)\s+"
    r"(?:the\s+)?"
    r"(?:(?P<dim>[A-Za-z_][A-Za-z0-9_]*)\s+)?"
    r"[\"']?(?P<value>.+?)[\"']?"
    r"$"
)
# Bare count / list without path: "how many widgets" / "list widgets"
_BARE_TYPE_RE = re.compile(
    r"(?ix)^"
    r"(?:(?:are\s+there|do\s+we\s+have|exist)\s+)?"
    r"(?P<label>.+?)"
    r"(?:\s+(?:are\s+there|do\s+we\s+have|exist|in\s+the\s+\w+))?"
    r"$"
)

# Ambiguity margin: top-2 within this absolute score gap → no unique winner.
_PATH_AMBIGUITY_MARGIN = 0.5

# Multi-hop defaults.
_DEFAULT_MAX_HOPS = 2
_HARD_MAX_HOPS = 3
# Mild penalty per extra hop so direct 1-hop wins when both are valid.
_MULTI_HOP_PENALTY = 1.5
# Cosine boost scale when an embedder is present (added to base score).
_EMBED_COSINE_SCALE = 4.0
# Semantic resolve_rel boost when dim synonym maps to a path leaf.
_SEMANTIC_REL_BOOST = 7.0


# Sync batch embedder: texts → vectors (hermetic tests). Async EmbedFn lives
# on ontology_mention_index and is accepted via ground_ask_plan_async.
SyncEmbedBatchFn = Callable[[Sequence[str]], list[list[float]]]
# Async EmbedFn re-export style (optional import avoided at module load).
AsyncEmbedFn = Callable[[Sequence[str]], Awaitable[list[list[float]]]]


@dataclass(frozen=True)
class NlSketch:
    """Lightweight entity/relation sketch extracted from NL (no ontology bind)."""

    question: str
    intent: str  # "count" | "list" | "unknown"
    type_mentions: tuple[str, ...] = ()
    value_mentions: tuple[str, ...] = ()
    rel_cues: tuple[str, ...] = ()
    dim_mentions: tuple[str, ...] = ()  # optional explicit dim words (site, region)


@dataclass(frozen=True)
class OntologyPath:
    """Ontology relationship path: 1-hop domain--rel-->range, or multi-hop chain.

    1-hop (default)::

        OntologyPath(domain_type="Widget", rel_attr="stored_in", range_type="Site")

    Multi-hop (e.g. Part -[:stored_in]→ Bin -[:in_facility]→ Facility)::

        OntologyPath(
            domain_type="Part",
            rel_attr="stored_in",
            range_type="Bin",
            chain=(("in_facility", "Facility"),),
        )

    ``chain`` holds additional hops after the first edge as
    ``(rel_attr, range_type)`` pairs. Empty ``chain`` ⇒ 1-hop.
    """

    domain_type: str
    rel_attr: str
    range_type: str | None = None
    chain: tuple[tuple[str, str | None], ...] = ()

    def as_tuple(self) -> tuple[str, str, str | None]:
        """First-edge triple (back-compat for 1-hop callers / tests)."""
        return (self.domain_type, self.rel_attr, self.range_type)

    @property
    def hop_count(self) -> int:
        return 1 + len(self.chain)

    @property
    def terminal_range(self) -> str | None:
        if self.chain:
            return self.chain[-1][1]
        return self.range_type

    @property
    def intermediate_types(self) -> tuple[str, ...]:
        """Range types of non-terminal edges (empty for 1-hop)."""
        if not self.chain:
            return ()
        out: list[str] = []
        if self.range_type:
            out.append(self.range_type)
        for _rel, rng in self.chain[:-1]:
            if rng:
                out.append(rng)
        return tuple(out)

    @property
    def all_rel_attrs(self) -> tuple[str, ...]:
        rels = [self.rel_attr]
        for rel, _rng in self.chain:
            rels.append(rel)
        return tuple(rels)

    def describe(self) -> str:
        parts = [f"{self.domain_type} -[:{self.rel_attr}]-> {self.range_type or '?'}"]
        for rel, rng in self.chain:
            parts.append(f"-[:{rel}]-> {rng or '?'}")
        return " ".join(parts)

    def signature(self) -> str:
        """Stable embed text for path ranking (domain rel range …)."""
        bits = [self.domain_type, self.rel_attr, self.range_type or ""]
        for rel, rng in self.chain:
            bits.extend([rel, rng or ""])
        return " ".join(b for b in bits if b)


@dataclass(frozen=True)
class RankedPath:
    path: OntologyPath
    score: float
    reasons: tuple[str, ...] = ()


@dataclass
class GroundedAskPlan:
    """Structured grounding for the LLM (or allowlisted template params).

    When ``confidence == "unique"`` and a path is present, ``params`` holds
    **safe** template bindings only (no free Cypher). When ambiguous / none,
    ``ranked_paths`` may still carry a shortlist for the prompt.

    Multi-hop unique winners are **prompt-only** (no multi-hop ADR 0013
    template yet): ``template`` stays ``None``; ``path.describe()`` carries
    the chain the LLM must follow.
    """

    question: str
    intent: str
    sketch: NlSketch
    subject_type: str | None = None
    path: OntologyPath | None = None
    value: str | None = None
    template: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    confidence: str = "none"  # "unique" | "ambiguous" | "none"
    ranked_paths: list[RankedPath] = field(default_factory=list)
    explanation: str = ""

    def to_prompt_block(self) -> str:
        """Format as LLM prompt context (never executable Cypher)."""
        return format_grounding_for_prompt(self)

    def is_unique_winner(self) -> bool:
        return self.confidence == "unique" and self.path is not None


# ---------------------------------------------------------------------------
# 1. Extract NL sketch
# ---------------------------------------------------------------------------


def extract_nl_sketch(question: str) -> NlSketch:
    """Extract type / value / rel-cue mentions and intent from free-text NL.

    Pure string heuristics — no ontology lookup. Messy casing / plurals kept
    as raw mentions for later resolve.
    """
    raw = _TRAILING_PUNCT_RE.sub("", (question or "").strip())
    if not raw:
        return NlSketch(question=question or "", intent="unknown")

    intent = "unknown"
    body = raw
    if _COUNT_PREFIX_RE.match(body):
        intent = "count"
        body = _COUNT_PREFIX_RE.sub("", body, count=1).strip()
        body = re.sub(
            r"(?i)\s+(?:are\s+there|do\s+we\s+have|exist)\s*$",
            "",
            body,
        ).strip()
    elif _LIST_PREFIX_RE.match(body):
        intent = "list"
        body = _LIST_PREFIX_RE.sub("", body, count=1).strip()
        body = re.sub(r"(?i)^(all|the)\s+", "", body).strip()
    else:
        # Bare "widgets in east" → list-ish without explicit verb
        if re.search(
            r"(?i)\b(?:in|at|from|with|having|of|near|inside|within)\b",
            body,
        ):
            intent = "list"

    type_mentions: list[str] = []
    value_mentions: list[str] = []
    rel_cues: list[str] = []
    dim_mentions: list[str] = []

    m = _SKETCH_PATH_RE.match(body)
    if m:
        label = (m.group("label") or "").strip()
        cue = (m.group("cue") or "").strip().lower()
        dim = (m.group("dim") or "").strip()
        value = _TRAILING_PUNCT_RE.sub("", (m.group("value") or "").strip())
        # If dim captured but value is empty / cue-like, don't invent.
        if label:
            type_mentions.append(label)
        if cue:
            rel_cues.append(cue)
        if dim and _SAFE_PROP_RE.match(dim) and dim.lower() not in _STOPWORDS:
            # Dim might actually be the whole value for single-token "in East"
            # when pattern greedily took dim — if value is multi-word keep both;
            # if value looks empty we already require value group.
            dim_mentions.append(dim)
        if value:
            # When dim was captured, value is the rest; when not, value may be
            # "site East" (dim word still in value) — peel first token if it
            # looks like a dim and remainder remains.
            value_mentions.append(value)
            # "site East" → dim=site, value=East when no dim group (optional
            # dim only matches when a second token exists).
            parts = value.split()
            if (
                not dim
                and len(parts) >= 2
                and _SAFE_PROP_RE.match(parts[0])
                and parts[0].lower() not in _STOPWORDS
            ):
                dim_mentions.append(parts[0])
                value_mentions[-1] = " ".join(parts[1:]).strip()
    else:
        m2 = _BARE_TYPE_RE.match(body)
        if m2:
            label = (m2.group("label") or "").strip()
            # Drop trailing noise
            label = re.sub(
                r"(?i)\s+(?:are\s+there|do\s+we\s+have|exist)$",
                "",
                label,
            ).strip()
            if label:
                type_mentions.append(label)

    # De-dupe while preserving order; drop stopword-only labels.
    type_mentions = _dedupe_keep(
        t for t in type_mentions if t and t.lower() not in _STOPWORDS
    )
    value_mentions = _dedupe_keep(v for v in value_mentions if v)
    dim_mentions = _dedupe_keep(
        d for d in dim_mentions if d and d.lower() not in _STOPWORDS
    )
    rel_cues = _dedupe_keep(rel_cues)

    return NlSketch(
        question=raw,
        intent=intent,
        type_mentions=tuple(type_mentions),
        value_mentions=tuple(value_mentions),
        rel_cues=tuple(rel_cues),
        dim_mentions=tuple(dim_mentions),
    )


def _dedupe_keep(items) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        key = x.lower() if isinstance(x, str) else str(x)
        if key in seen:
            continue
        seen.add(key)
        out.append(x)
    return out


# ---------------------------------------------------------------------------
# 2. Candidate ontology paths (1-hop + multi-hop)
# ---------------------------------------------------------------------------


def _clamp_max_hops(max_hops: int | None) -> int:
    n = _DEFAULT_MAX_HOPS if max_hops is None else int(max_hops)
    if n < 1:
        n = 1
    return min(n, _HARD_MAX_HOPS)


def _outbound_edges(
    ontology_summary: str,
    type_names: Sequence[str],
) -> dict[str, list[tuple[str, str | None]]]:
    """type → list of (rel_attr, range_type) for relationship edges only."""
    graph: dict[str, list[tuple[str, str | None]]] = {}
    for tname in type_names:
        section = _ontology_section_for_type(tname, ontology_summary)
        if not section:
            continue
        edges: list[tuple[str, str | None]] = []
        seen: set[tuple[str, str | None]] = set()
        for leaf, range_type in _relationship_specs_in_section(section):
            if not leaf or not _SAFE_PROP_RE.match(leaf):
                continue
            key = (leaf, range_type)
            if key in seen:
                continue
            seen.add(key)
            edges.append(key)
        if edges:
            graph[tname] = edges
    return graph


def candidate_ontology_paths(
    ontology_summary: str,
    type_names: Sequence[str] | None = None,
    *,
    domain_type: str | None = None,
    max_hops: int = _DEFAULT_MAX_HOPS,
) -> list[OntologyPath]:
    """Enumerate relationship paths from the ontology summary text.

    Only **relationship** edges (not pure literals). When ``domain_type`` is
    set, only paths starting at that type are returned.

    Enumerates 1-hop edges and multi-hop chains up to ``max_hops`` (default 2,
    hard-capped at 3). Multi-hop walks require typed intermediate ranges so
    the next hop can be resolved from the ontology graph.
    """
    names = (
        list(type_names)
        if type_names is not None
        else extract_type_names_from_ontology(ontology_summary)
    )
    if not names:
        names = extract_type_names_from_ontology(ontology_summary)

    hop_limit = _clamp_max_hops(max_hops)
    graph = _outbound_edges(ontology_summary, names)

    start_types = [domain_type] if domain_type else list(graph.keys())
    # If domain_type was requested but not in graph keys (no edges), still empty.
    if domain_type and domain_type not in graph:
        # Case-insensitive lookup
        lower_map = {k.lower(): k for k in graph}
        resolved = lower_map.get(domain_type.lower())
        start_types = [resolved] if resolved else []

    paths: list[OntologyPath] = []
    seen: set[tuple] = set()

    def _add(path: OntologyPath) -> None:
        key = (
            path.domain_type,
            path.rel_attr,
            path.range_type,
            path.chain,
        )
        if key in seen:
            return
        seen.add(key)
        paths.append(path)

    for start in start_types:
        if not start or start not in graph:
            continue
        # BFS: state = (current_type, list of (rel, range) hops so far)
        # hop length 1..hop_limit
        stack: list[tuple[str, list[tuple[str, str | None]]]] = []
        for rel, rng in graph[start]:
            stack.append((rng or "", [(rel, rng)]))
            _add(
                OntologyPath(
                    domain_type=start,
                    rel_attr=rel,
                    range_type=rng,
                    chain=(),
                )
            )

        while stack:
            cur_type, hops = stack.pop()
            if len(hops) >= hop_limit:
                continue
            if not cur_type:
                continue
            # Resolve current type for outbound edges (case-insensitive).
            next_key = cur_type if cur_type in graph else None
            if next_key is None:
                lower_map = {k.lower(): k for k in graph}
                next_key = lower_map.get(cur_type.lower())
            if next_key is None:
                continue
            for rel, rng in graph[next_key]:
                new_hops = hops + [(rel, rng)]
                first_rel, first_rng = new_hops[0]
                rest = tuple(new_hops[1:])
                _add(
                    OntologyPath(
                        domain_type=start,
                        rel_attr=first_rel,
                        range_type=first_rng,
                        chain=rest,
                    )
                )
                if len(new_hops) < hop_limit and rng:
                    stack.append((rng, new_hops))

    return paths


# ---------------------------------------------------------------------------
# 3. Rank paths
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 4. Ground plan
# ---------------------------------------------------------------------------


def _safe_params(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only allowlisted keys; validate prop-like strings."""
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in _SAFE_PARAM_KEYS:
            continue
        if k in ("rel_attr", "prop_key") and isinstance(v, str):
            if not _SAFE_PROP_RE.match(v):
                continue
        if k == "type_names":
            if not isinstance(v, (list, tuple)):
                continue
            cleaned = [t for t in v if isinstance(t, str) and t]
            if not cleaned:
                continue
            out[k] = list(cleaned)
            continue
        if k == "target_name":
            if not isinstance(v, str) or not v.strip():
                continue
            # Value is a param, never interpolated into Cypher — allow free text.
            out[k] = v.strip()
            continue
        if k == "limit":
            try:
                n = int(v)
            except (TypeError, ValueError):
                n = DEFAULT_LIST_LIMIT
            out[k] = max(1, min(n, 200))
            continue
        out[k] = v
    return out


def ground_ask_plan(
    question: str,
    ontology_summary: str,
    *,
    type_names: Sequence[str] | None = None,
    mention_index: Any | None = None,
    query_embedding: Any | None = None,
    embedder: Any | None = None,
    max_hops: int = _DEFAULT_MAX_HOPS,
) -> GroundedAskPlan | None:
    """End-to-end: sketch → paths → rank → safe plan (or fail-closed).

    Returns ``None`` only when the question/ontology is empty or no subject
    type can be resolved. Ambiguous path ranking still returns a plan with
    ``confidence="ambiguous"`` and ``path=None`` so the LLM can see the shortlist.

    Optional ``mention_index`` / ``query_embedding`` / sync ``embedder`` feed
    ranking only — never skip the LLM. Multi-hop unique winners are
    prompt-only (no multi-hop allowlisted template).
    """
    q = (question or "").strip()
    if not q:
        return None
    if not (ontology_summary or "").strip():
        return None

    names = (
        list(type_names)
        if type_names is not None
        else extract_type_names_from_ontology(ontology_summary)
    )
    if not names:
        return None

    sketch = extract_nl_sketch(q)

    # Resolve subject type from first type mention (no invent).
    subject: str | None = None
    for mention in sketch.type_mentions:
        hit = resolve_type_name(mention, names, ontology_summary)
        if hit is not None:
            subject = hit
            break

    if subject is None and not sketch.type_mentions:
        # No type mention extracted — cannot ground.
        return GroundedAskPlan(
            question=q,
            intent=sketch.intent,
            sketch=sketch,
            confidence="none",
            explanation="No type mention extracted from NL.",
        )
    if subject is None:
        return GroundedAskPlan(
            question=q,
            intent=sketch.intent,
            sketch=sketch,
            confidence="none",
            explanation=(
                f"Type mention(s) {list(sketch.type_mentions)!r} did not resolve "
                f"to a known ontology type."
            ),
        )

    expanded = type_names_with_subclasses(
        subject, ontology_summary=ontology_summary, include_subclasses=True
    )
    value = sketch.value_mentions[0] if sketch.value_mentions else None

    # No relational cue / value → bare type count or list.
    if not sketch.rel_cues and not value:
        if sketch.intent == "count":
            return GroundedAskPlan(
                question=q,
                intent="count",
                sketch=sketch,
                subject_type=subject,
                path=None,
                value=None,
                template=TEMPLATE_ENTITIES_OF_TYPE_COUNT,
                params=_safe_params({"type_names": expanded}),
                confidence="unique",
                explanation=f"Count entities of type {subject}.",
            )
        if sketch.intent == "list":
            return GroundedAskPlan(
                question=q,
                intent="list",
                sketch=sketch,
                subject_type=subject,
                path=None,
                value=None,
                template=TEMPLATE_ENTITIES_OF_TYPE,
                params=_safe_params(
                    {
                        "type_names": expanded,
                        "after_id": None,
                        "limit": DEFAULT_LIST_LIMIT,
                    }
                ),
                confidence="unique",
                explanation=f"List entities of type {subject}.",
            )
        return GroundedAskPlan(
            question=q,
            intent=sketch.intent,
            sketch=sketch,
            subject_type=subject,
            confidence="none",
            explanation="Subject type resolved but no intent/path to ground.",
        )

    # Path ranking for related-entity name filter shapes (1-hop + multi-hop).
    paths = candidate_ontology_paths(
        ontology_summary,
        names,
        domain_type=subject,
        max_hops=max_hops,
    )
    ranked = rank_paths(
        sketch,
        paths,
        subject_type=subject,
        embedder=embedder,
        mention_index=mention_index,
        query_embedding=query_embedding,
    )
    winner, confidence = _unique_winner(ranked)

    # Explicit dim that failed to rank any path → do not invent.
    if sketch.dim_mentions and not ranked:
        return GroundedAskPlan(
            question=q,
            intent=sketch.intent if sketch.intent != "unknown" else "list",
            sketch=sketch,
            subject_type=subject,
            path=None,
            value=value,
            confidence="none",
            ranked_paths=[],
            explanation=(
                f"Dim mention(s) {list(sketch.dim_mentions)!r} matched no "
                f"relationship on {subject}."
            ),
        )

    if confidence == "ambiguous":
        return GroundedAskPlan(
            question=q,
            intent=sketch.intent if sketch.intent != "unknown" else "list",
            sketch=sketch,
            subject_type=subject,
            path=None,
            value=value,
            template=None,
            params={},
            confidence="ambiguous",
            ranked_paths=list(ranked[:5]),
            explanation=(
                f"Ambiguous ontology paths from {subject} for cues "
                f"{list(sketch.rel_cues)!r} / dims {list(sketch.dim_mentions)!r}; "
                f"no unique winner (fail closed)."
            ),
        )

    if winner is None or confidence == "none":
        # Locative + value but no scoring path: if exactly one outbound 1-hop
        # edge, accept it only when locative (unique edge is a strong signal).
        is_locative = bool(
            {c.lower() for c in sketch.rel_cues} & _LOCATIVE_CUES
        )
        one_hop = [p for p in paths if p.hop_count == 1]
        if is_locative and value and len(one_hop) == 1:
            only = one_hop[0]
            winner = RankedPath(
                path=only,
                score=2.0,
                reasons=("unique_outbound_edge",),
            )
            ranked = [winner]
            confidence = "unique"
        else:
            return GroundedAskPlan(
                question=q,
                intent=sketch.intent if sketch.intent != "unknown" else "list",
                sketch=sketch,
                subject_type=subject,
                path=None,
                value=value,
                confidence="none",
                ranked_paths=list(ranked[:5]),
                explanation=(
                    f"No ontology path ranked for {subject} with "
                    f"cues={list(sketch.rel_cues)!r}."
                ),
            )

    path = winner.path
    if not value:
        # Path without value — hop-ish; leave template unset for free-form LLM.
        return GroundedAskPlan(
            question=q,
            intent=sketch.intent if sketch.intent != "unknown" else "list",
            sketch=sketch,
            subject_type=subject,
            path=path,
            value=None,
            template=None,
            params=_safe_params({"type_names": expanded}),
            confidence="unique",
            ranked_paths=list(ranked[:5]),
            explanation=f"Preferred path {path.describe()} but no value to bind.",
        )

    intent = sketch.intent if sketch.intent != "unknown" else "list"

    # Multi-hop: prompt-only (no multi-hop ADR 0013 template). Do not invent
    # raw Cypher; do not stuff multi-hop into single-hop related_entity params.
    if path.hop_count > 1:
        expl = (
            f"{'Count' if intent == 'count' else 'Find'} {subject} entities "
            f"via multi-hop path {path.describe()} "
            f"with related name {value!r} on terminal "
            f"{path.terminal_range or '?'}. "
            f"(No multi-hop allowlisted template — follow preferred_path in Cypher.)"
        )
        return GroundedAskPlan(
            question=q,
            intent=intent,
            sketch=sketch,
            subject_type=subject,
            path=path,
            value=value,
            template=None,
            params=_safe_params(
                {
                    "type_names": expanded,
                    "target_name": value,
                    "limit": DEFAULT_LIST_LIMIT,
                }
            ),
            confidence="unique",
            ranked_paths=list(ranked[:5]),
            explanation=expl,
        )

    # 1-hop: grounded related-entity name filter (list or count intent).
    params = _safe_params(
        {
            "type_names": expanded,
            "rel_attr": path.rel_attr,
            "target_name": value,
            "limit": DEFAULT_LIST_LIMIT,
        }
    )
    expl = (
        f"{'Count' if intent == 'count' else 'Find'} {subject} entities "
        f"related via {path.rel_attr} to name {value!r}"
        + (f" (range {path.range_type})" if path.range_type else "")
        + "."
    )
    return GroundedAskPlan(
        question=q,
        intent=intent,
        sketch=sketch,
        subject_type=subject,
        path=path,
        value=value,
        template=TEMPLATE_RELATED_ENTITY_NAME_FILTER,
        params=params,
        confidence="unique",
        ranked_paths=list(ranked[:5]),
        explanation=expl,
    )


async def ground_ask_plan_async(
    question: str,
    ontology_summary: str,
    *,
    type_names: Sequence[str] | None = None,
    mention_index: Any | None = None,
    query_embedding: Any | None = None,
    embedder: Any | None = None,
    max_hops: int = _DEFAULT_MAX_HOPS,
) -> GroundedAskPlan | None:
    """Async variant: materialize embeddings from async ``EmbedFn`` then ground.

    When ``embedder`` is an async batch function, embeds the sketch text and
    each candidate path signature once, then ranks with a sync cosine table
    (no further network). When embedder is sync or absent, delegates to
    :func:`ground_ask_plan`.
    """
    q = (question or "").strip()
    if not q or not (ontology_summary or "").strip():
        return await _maybe_async_none(
            ground_ask_plan(
                question,
                ontology_summary,
                type_names=type_names,
                mention_index=mention_index,
                query_embedding=query_embedding,
                embedder=embedder if _is_sync_embedder(embedder) else None,
                max_hops=max_hops,
            )
        )

    # Async EmbedFn path: precompute vectors for sketch + path signatures.
    if embedder is not None and _is_async_embedder(embedder):
        names = (
            list(type_names)
            if type_names is not None
            else extract_type_names_from_ontology(ontology_summary)
        )
        sketch = extract_nl_sketch(q)
        subject: str | None = None
        for mention in sketch.type_mentions:
            hit = resolve_type_name(mention, names or [], ontology_summary)
            if hit is not None:
                subject = hit
                break
        paths = (
            candidate_ontology_paths(
                ontology_summary,
                names,
                domain_type=subject,
                max_hops=max_hops,
            )
            if subject
            else []
        )
        sketch_text = _sketch_embed_text(sketch)
        texts = [sketch_text] + [p.signature() for p in paths]
        try:
            vectors = await embedder(texts)
        except Exception:  # noqa: BLE001
            vectors = None
        sync_map: dict[str, list[float]] = {}
        q_emb = query_embedding
        if (
            isinstance(vectors, (list, tuple))
            and vectors
            and len(vectors) == len(texts)
        ):
            q_emb = q_emb if q_emb is not None else vectors[0]
            for i, p in enumerate(paths):
                sync_map[p.signature()] = list(vectors[i + 1])
            sketch_vec = list(vectors[0])
            dim = len(sketch_vec) or 1

            def _sync_batch(batch: Sequence[str]) -> list[list[float]]:
                out: list[list[float]] = []
                for t in batch:
                    if t == sketch_text:
                        out.append(list(sketch_vec))
                    elif t in sync_map:
                        out.append(list(sync_map[t]))
                    else:
                        out.append([0.0] * dim)
                return out

            return ground_ask_plan(
                question,
                ontology_summary,
                type_names=type_names,
                mention_index=mention_index,
                query_embedding=q_emb,
                embedder=_sync_batch,
                max_hops=max_hops,
            )
        # Async embed failed — fall through without embedder.
        return ground_ask_plan(
            question,
            ontology_summary,
            type_names=type_names,
            mention_index=mention_index,
            query_embedding=query_embedding,
            embedder=None,
            max_hops=max_hops,
        )

    return ground_ask_plan(
        question,
        ontology_summary,
        type_names=type_names,
        mention_index=mention_index,
        query_embedding=query_embedding,
        embedder=embedder if _is_sync_embedder(embedder) else None,
        max_hops=max_hops,
    )


async def _maybe_async_none(plan: GroundedAskPlan | None) -> GroundedAskPlan | None:
    return plan


def format_grounding_for_prompt(plan: GroundedAskPlan | None) -> str:
    """Render a grounded plan as prompt context for the Cypher LLM.

    Returns empty string when there is nothing useful to inject.
    """
    if plan is None:
        return ""
    lines: list[str] = [
        "Ontology grounding (structured hint — prefer these when confident;",
        "still emit valid Cypher / allowlisted template JSON; do not invent types):",
        f"  intent: {plan.intent}",
    ]
    if plan.subject_type:
        lines.append(f"  subject_type: {plan.subject_type}")
    if plan.sketch.rel_cues:
        lines.append(f"  rel_cues: {', '.join(plan.sketch.rel_cues)}")
    if plan.sketch.dim_mentions:
        lines.append(f"  dim_mentions: {', '.join(plan.sketch.dim_mentions)}")
    if plan.value:
        lines.append(f"  related_entity_name: {plan.value!r}")
    lines.append(f"  confidence: {plan.confidence}")

    if plan.confidence == "unique" and plan.path is not None:
        lines.append(f"  preferred_path: {plan.path.describe()}")
        if plan.path.hop_count > 1:
            lines.append(f"  path_hops: {plan.path.hop_count}")
            lines.append(
                "  note: multi-hop path — emit Cypher that traverses each "
                "relationship in preferred_path in order; bind the related "
                "entity name filter on the **terminal** node "
                f"({plan.path.terminal_range or '?'}). There is no multi-hop "
                "allowlisted template; do not invent intermediate types."
            )
            if plan.intent == "count":
                lines.append(
                    "  note: intent is COUNT — return count of subjects matching "
                    "this multi-hop related-entity name filter."
                )
        if plan.template:
            lines.append(f"  preferred_template: {plan.template}")
        if plan.params:
            # Show only safe keys already filtered.
            param_bits = []
            for k in sorted(plan.params.keys()):
                param_bits.append(f"{k}={plan.params[k]!r}")
            lines.append(f"  template_params: {{{', '.join(param_bits)}}}")
        if (
            plan.intent == "count"
            and plan.template == TEMPLATE_RELATED_ENTITY_NAME_FILTER
        ):
            lines.append(
                "  note: intent is COUNT — return count of subjects matching "
                "this related-entity name filter (do not return the unfiltered "
                "type total; do not filter a literal property with the value)."
            )
        elif plan.intent == "count" and plan.template == TEMPLATE_ENTITIES_OF_TYPE_COUNT:
            lines.append(
                "  note: prefer entities_of_type_count with the given type_names."
            )
    elif plan.confidence == "ambiguous":
        lines.append(
            "  note: multiple ontology paths score equally — do NOT silently "
            "pick one edge; prefer a clarifying query shape or the strongest "
            "schema-supported reading. Shortlist:"
        )
        for i, rp in enumerate(plan.ranked_paths[:5], 1):
            lines.append(
                f"    {i}. {rp.path.describe()} (score={rp.score:.1f}; "
                f"{', '.join(rp.reasons) or '—'})"
            )
    else:
        if plan.ranked_paths:
            lines.append("  candidate_paths:")
            for i, rp in enumerate(plan.ranked_paths[:5], 1):
                lines.append(f"    {i}. {rp.path.describe()} (score={rp.score:.1f})")
        if plan.explanation:
            lines.append(f"  note: {plan.explanation}")

    if plan.explanation and plan.confidence == "unique":
        lines.append(f"  explanation: {plan.explanation}")

    return "\n".join(lines) + "\n"


__all__ = [
    "GroundedAskPlan",
    "NlSketch",
    "OntologyPath",
    "RankedPath",
    "candidate_ontology_paths",
    "extract_nl_sketch",
    "format_grounding_for_prompt",
    "ground_ask_plan",
    "ground_ask_plan_async",
    "rank_paths",
]
