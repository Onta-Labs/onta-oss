"""NL sketch → ontology subgraph match → grounded /ask plan (planning layer).

Grounding spine for questions like ``how many widgets in east``:

1. **Extract** an :class:`NlSketch` (type mentions, values, locative/rel cues, intent).
2. **Enumerate** ontology relationship paths (domain type —rel→ range type).
3. **Rank** paths against the sketch (location-ish cues, range-type synonyms).
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

See also: :mod:`infona_client.nlp.cypher_generate` (type / rel resolve),
ADR 0013 templates (``related_entity_name_filter``, ``entities_of_type_count``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

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
    """One ontology relationship edge: domain --rel_attr--> range."""

    domain_type: str
    rel_attr: str
    range_type: str | None = None

    def as_tuple(self) -> tuple[str, str, str | None]:
        return (self.domain_type, self.rel_attr, self.range_type)

    def describe(self) -> str:
        rng = self.range_type or "?"
        return f"{self.domain_type} -[:{self.rel_attr}]-> {rng}"


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
# 2. Candidate ontology paths
# ---------------------------------------------------------------------------


def candidate_ontology_paths(
    ontology_summary: str,
    type_names: Sequence[str] | None = None,
    *,
    domain_type: str | None = None,
) -> list[OntologyPath]:
    """Enumerate relationship paths from the ontology summary text.

    Only **relationship** edges (not pure literals). When ``domain_type`` is
    set, only that type's outbound edges are returned.
    """
    names = (
        list(type_names)
        if type_names is not None
        else extract_type_names_from_ontology(ontology_summary)
    )
    if domain_type:
        names = [domain_type]
    if not names:
        # Still try to parse every Type: block from the summary.
        names = extract_type_names_from_ontology(ontology_summary)

    paths: list[OntologyPath] = []
    seen: set[tuple[str, str, str | None]] = set()
    for tname in names:
        section = _ontology_section_for_type(tname, ontology_summary)
        if not section:
            continue
        for leaf, range_type in _relationship_specs_in_section(section):
            if not leaf or not _SAFE_PROP_RE.match(leaf):
                continue
            key = (tname, leaf, range_type)
            if key in seen:
                continue
            seen.add(key)
            paths.append(
                OntologyPath(
                    domain_type=tname,
                    rel_attr=leaf,
                    range_type=range_type,
                )
            )
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


def _score_path_against_sketch(
    sketch: NlSketch,
    path: OntologyPath,
    *,
    subject_type: str | None,
) -> RankedPath:
    """Score one path. Higher = better. Reasons are for tests / prompt."""
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

    # Explicit dim word: high-precision range-type or leaf match (ONTA-538).
    dim_hit = False
    for dim in sketch.dim_mentions:
        # Leaf name / has_ / tokens
        dim_l = dim.lower()
        sing = _singularize_token(dim_l)
        leaf_l = path.rel_attr.lower()
        leaf_tokens = set(leaf_l.split("_")) - {"has", "by", "the", "a", "an"}
        if dim_l == leaf_l or sing == leaf_l:
            score += 8.0
            reasons.append(f"leaf_exact:{dim}")
            dim_hit = True
        elif dim_l in leaf_tokens or sing in leaf_tokens:
            score += 6.0
            reasons.append(f"leaf_token:{dim}")
            dim_hit = True
        # Range-type precision (no substring Website←site)
        if path.range_type:
            tier = _score_range_type_precision(dim, path.range_type)
            if tier >= 3:
                score += 8.0
                reasons.append(f"range_exact:{dim}→{path.range_type}")
                dim_hit = True
            elif tier == 2:
                score += 6.0
                reasons.append(f"range_camel:{dim}→{path.range_type}")
                dim_hit = True
            elif tier == 1:
                score += 5.0
                reasons.append(f"range_content:{dim}→{path.range_type}")
                dim_hit = True

    # When an explicit dim word is present, only dim-matched paths keep their
    # score — do not award generic locative family boosts to non-matches
    # (would tie ``has_region`` with ``stored_in`` for "at site East").
    has_explicit_dim = bool(sketch.dim_mentions)

    # Locative cue without (matching) dim: prefer location-ish edges.
    if locative and not dim_hit and not has_explicit_dim:
        if _is_location_leaf(path.rel_attr):
            score += 4.0
            reasons.append("locative_leaf")
        if _is_location_range(path.range_type):
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

    return RankedPath(path=path, score=score, reasons=tuple(reasons))


def rank_paths(
    sketch: NlSketch,
    paths: Sequence[OntologyPath],
    *,
    subject_type: str | None = None,
    embedder: Any | None = None,  # reserved for future semantic ranking
) -> list[RankedPath]:
    """Rank ontology paths against a sketch (highest score first).

    ``embedder`` is optional and currently unused (string / family heuristics
    only); reserved for ONTA-537-style cosine ranking when an index is ready.
    """
    del embedder  # reserved
    ranked = [
        _score_path_against_sketch(sketch, p, subject_type=subject_type)
        for p in paths
    ]
    ranked = [r for r in ranked if r.score > 0]
    ranked.sort(key=lambda r: (-r.score, r.path.rel_attr, r.path.domain_type))
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
    # Also fail if both share the same range type at high score with different leaves
    # (two edges to Site) — already covered by margin when scores equal.
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
) -> GroundedAskPlan | None:
    """End-to-end: sketch → paths → rank → safe plan (or fail-closed).

    Returns ``None`` only when the question/ontology is empty or no subject
    type can be resolved. Ambiguous path ranking still returns a plan with
    ``confidence="ambiguous"`` and ``path=None`` so the LLM can see the shortlist.
    """
    del mention_index, query_embedding  # reserved for semantic resolve
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

    # Path ranking for related-entity name filter shapes.
    paths = candidate_ontology_paths(
        ontology_summary, names, domain_type=subject
    )
    # If subject has no outbound edges, also try all paths whose domain matches
    # resolved type (already filtered). Empty → no path.
    ranked = rank_paths(sketch, paths, subject_type=subject)
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
        # Locative + value but no scoring path: if exactly one outbound edge,
        # accept it only when locative (unique edge is a strong signal).
        is_locative = bool(
            {c.lower() for c in sketch.rel_cues} & _LOCATIVE_CUES
        )
        if is_locative and value and len(paths) == 1:
            only = paths[0]
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

    # Grounded related-entity name filter (list or count intent).
    # Intent "count" still targets related_entity_name_filter params; the prompt
    # tells the LLM to COUNT matching subjects (no separate count template yet).
    params = _safe_params(
        {
            "type_names": expanded,
            "rel_attr": path.rel_attr,
            "target_name": value,
            "limit": DEFAULT_LIST_LIMIT,
        }
    )
    intent = sketch.intent if sketch.intent != "unknown" else "list"
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
        if plan.template:
            lines.append(f"  preferred_template: {plan.template}")
        if plan.params:
            # Show only safe keys already filtered.
            param_bits = []
            for k in sorted(plan.params.keys()):
                param_bits.append(f"{k}={plan.params[k]!r}")
            lines.append(f"  template_params: {{{', '.join(param_bits)}}}")
        if plan.intent == "count" and plan.template == TEMPLATE_RELATED_ENTITY_NAME_FILTER:
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
    "rank_paths",
]
