"""Query example bank with semantic retrieval for few-shot prompting.

Stores (question, SPARQL and/or Cypher) pairs from successful evaluations and
committed Cypher seeds (ONTA-539). At query time, retrieves the most relevant
examples via embedding similarity with anti-cheat filtering, cross-dataset
preference, and pattern diversity.

Neo4j / Cypher path (``language="cypher"``): only rows with a non-empty
``cypher`` field are ranked and formatted — SPARQL bodies never enter the
Cypher LLM prompt. Rebuild Cypher coverage with::

    python -m infona_client.nlp.cypher_example_seeds

Uses the same OpenRouter text-embedding-3-small embeddings as ontology_embeddings.py.

Implementation lives in sibling ``example_bank_*.py`` modules. Every previously
importable name is re-exported here.
"""

from __future__ import annotations

import json  # noqa: F401 — tests patch example_bank.json.dumps
import logging
import os
import re  # noqa: F401 — previously public
from dataclasses import dataclass, field  # noqa: F401 — previously public
from pathlib import Path  # noqa: F401 — previously public

import numpy as np  # noqa: F401 — previously public

# Shared embed client (ONTA-174) — model/batching/errors live in ONE place.
# Constants are re-exported for backward compatibility with existing importers.
from infona_client.nlp.embed_client import (  # noqa: F401 — re-exports
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    OPENROUTER_EMBEDDINGS_URL,
    embed_texts,
)
from infona_client.nlp.embed_client import cosine_similarity as _cosine_similarity  # noqa: F401
from infona_client.nlp.example_bank_format import (  # noqa: F401
    TARGET_GRAPH_PLACEHOLDER,
    _ANY_GRAPH_IRI_RE,
    _FROM_GRAPH_RE,
    _format_cypher_examples,
    format_examples_for_prompt,
    sanitize_example_cypher,
    sanitize_example_sparql,
)
from infona_client.nlp.example_bank_models import (  # noqa: F401
    ANTI_CHEAT_THRESHOLD,
    BENCHMARK_KG_PREFIXES,
    BLUEPRINT_ORIGIN,
    CYPHER_PATTERN_DETECTORS,
    DEFAULT_BANK_PATH,
    EVAL_REPORTS_DIR,
    HOLDOUT_V2_KGS,
    MAX_BANK_SIZE,
    PATTERN_DETECTORS,
    SAME_DATASET_MAX_SIM,
    Example,
    _HOLDOUT_V2_KGS_FALLBACK,
    _host,
    _load_holdout_v2_kgs,
    detect_pattern_tags,
    detect_pattern_tags_cypher,
    example_key,
    example_matches_kg_purge,
    example_visible_to_tenant,
    is_benchmark_kg,
    is_blueprint_origin,
    is_unscoped_blueprint_example,
    normalize_example_origin,
)
from infona_client.nlp.example_bank_retrieve import ExampleBankRetrieveMixin  # noqa: F401
from infona_client.nlp.example_bank_store import ExampleBank  # noqa: F401

logger = logging.getLogger(__name__)


# ── Singleton accessor ───────────────────────────────────────────────────

_example_bank: ExampleBank | None = None


def get_example_bank() -> ExampleBank | None:
    """Lazy-init singleton for the example bank. Returns None if no API key."""
    global _example_bank
    if _example_bank is None:
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            try:
                from infona_client.config import settings
                api_key = settings.openrouter_api_key or ""
            except Exception:
                pass
        if not api_key:
            return None
        _example_bank = ExampleBank(openrouter_api_key=api_key)
        _example_bank.load()
    return _example_bank
