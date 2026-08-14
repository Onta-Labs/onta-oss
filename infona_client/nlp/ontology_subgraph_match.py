"""NL sketch → ontology subgraph match → grounded /ask plan (planning layer).

Grounding spine for questions like ``how many widgets in east``:

1. **Extract** an :class:`NlSketch` (type mentions, values, locative/rel cues, intent).
2. **Enumerate** ontology relationship paths (1-hop and multi-hop chains).
3. **Rank** paths against the sketch (location-ish cues, range-type synonyms,
   optional embedder cosine + :class:`OntologyMentionIndex` semantic dims).
4. **Ground** a structured :class:`GroundedAskPlan` for the LLM prompt
   (preferred ADR 0013 template + safe params) — never silent free-form Cypher.

Implementation lives in sibling ``ontology_subgraph_*.py`` modules. Every
previously importable name is re-exported here.

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

from infona_client.graph.rdfs_helpers import (  # noqa: F401 — public re-exports
    TEMPLATE_ENTITIES_OF_TYPE,
    TEMPLATE_ENTITIES_OF_TYPE_COUNT,
    TEMPLATE_RELATED_ENTITY_NAME_FILTER,
    type_names_with_subclasses,
)
from infona_client.nlp.cypher_generate import (  # noqa: F401 — public re-exports
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
from infona_client.nlp.ontology_subgraph_ground import (  # noqa: F401
    _maybe_async_none,
    _safe_params,
    ground_ask_plan,
    ground_ask_plan_async,
)
from infona_client.nlp.ontology_subgraph_paths import (  # noqa: F401
    _clamp_max_hops,
    _outbound_edges,
    candidate_ontology_paths,
)
from infona_client.nlp.ontology_subgraph_prompt import format_grounding_for_prompt
from infona_client.nlp.ontology_subgraph_rank import (  # noqa: F401
    _apply_embedder_boosts,
    _apply_semantic_index_boosts,
    _cosine,
    _is_async_embedder,
    _is_location_leaf,
    _is_location_range,
    _is_sync_embedder,
    _l2_normalize_list,
    _mention_matches_type,
    _score_path_against_sketch,
    _sketch_embed_text,
    _unique_winner,
    rank_paths,
)
from infona_client.nlp.ontology_subgraph_sketch import (  # noqa: F401
    _BARE_TYPE_RE,
    _COUNT_PREFIX_RE,
    _LIST_PREFIX_RE,
    _SKETCH_PATH_RE,
    _TRAILING_PUNCT_RE,
    _dedupe_keep,
    extract_nl_sketch,
)
from infona_client.nlp.ontology_subgraph_types import (  # noqa: F401
    AsyncEmbedFn,
    GroundedAskPlan,
    NlSketch,
    OntologyPath,
    RankedPath,
    SyncEmbedBatchFn,
    _ALL_CUES,
    _DEFAULT_MAX_HOPS,
    _EMBED_COSINE_SCALE,
    _HARD_MAX_HOPS,
    _LOCATION_LEAF_TOKENS,
    _LOCATION_RANGE_FAMILY,
    _LOCATIVE_CUES,
    _MULTI_HOP_PENALTY,
    _PATH_AMBIGUITY_MARGIN,
    _RELATIONAL_CUES,
    _SAFE_PARAM_KEYS,
    _SEMANTIC_REL_BOOST,
    _STOPWORDS,
    _host,
)

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
