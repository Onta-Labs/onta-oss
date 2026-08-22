"""Data shapes and constants for NL → ontology subgraph grounding.

Looked up on :mod:`infona_client.nlp.ontology_subgraph_match` at call time via
``_host()`` when a sibling needs a patchable name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence


def _host():
    """Call-time lookup of the public ontology_subgraph_match module.

    Tests monkeypatch names on ``infona_client.nlp.ontology_subgraph_match``.
    Sibling modules must look these up at call time so patches keep working.
    """
    from infona_client.nlp import ontology_subgraph_match as _mod

    return _mod


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
        """Render the path for the grounding text injected into the prompt.

        Deliberately NOT Cypher edge syntax. This string lands in the Cypher
        generation prompt as ``preferred_path:``, and rendering it as
        ``ClinicalTrial -[:lead_sponsor]-> Company`` demonstrated the exact
        shape the model must not emit: ``lead_sponsor`` is a ``:Property``
        name, and a lower-case relationship type cannot exist (rel types are
        UPPER_SNAKE_CASE, and relationships live on ``:Assertion`` anyway). The
        model copied the grounding hint into real Cypher and /ask answered
        "No results found." on data that has the relationship. The arrow form
        below names the same path without being valid-looking Cypher.
        """
        parts = [f"{self.domain_type} --{self.rel_attr}--> {self.range_type or '?'}"]
        for rel, rng in self.chain:
            parts.append(f"--{rel}--> {rng or '?'}")
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
        return _host().format_grounding_for_prompt(self)

    def is_unique_winner(self) -> bool:
        return self.confidence == "unique" and self.path is not None
