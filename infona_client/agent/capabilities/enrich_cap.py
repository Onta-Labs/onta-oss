"""Enrichment capability — with clean-before-enrich composition.

Public facade. Implementation lives in sibling ``enrich_*.py`` modules.
Every previously importable name is re-exported here.

Reuses the existing enrichment engine (no reimplementation):

* ``plan`` parses the NL instruction into the existing :class:`EnrichRequest`
  shape (attributes + optional scope ``predicate=value`` + tier + confidence).
  THEN it detects a prerequisite: if the **scope predicate's target values are
  composite** (un-normalized — a delimiter shows up in the sampled target
  labels), scoping by ``value`` would MISS the rows packed inside a composite
  cell (e.g. scope ``speaks=Persian`` misses an entity whose ``speaks`` points
  at ``English__Persian``). In that case it emits a NORMALIZE step FIRST (reusing
  :class:`NormalizeCapability.plan` so the cleanup logic isn't duplicated) and
  sets the enrich step's ``depends_on`` to it. Returns ``[normalize_step?,
  enrich_step]``. No writes.

* ``execute`` runs the enrichment as a background job, building the EXACT same
  :class:`EnrichJob` + ``EnrichmentExecutor.run`` the ``/enrich/jobs`` route
  builds (strong-ref ``_spawn`` so the task can't be GC'd). Returns an ack.

The agent never calls the ``/enrich`` HTTP route — it drives the executor + job
store directly via the same primitives.

Invariants other agents must not break:
- Writes stay on ``insert_facts`` / ``refresh_after_write`` (the executor, not
  this facade). Instance edges on ``onto/<leaf>``; entity IRIs via ``entity_uri``.
- Monkeypatched names live on THIS module. Siblings look them up at call time
  via ``_host()``.
"""

from __future__ import annotations

import asyncio  # noqa: F401 — previously importable
import json  # noqa: F401
import re  # noqa: F401
import uuid  # noqa: F401
from dataclasses import dataclass  # noqa: F401
from datetime import datetime, timezone  # noqa: F401
from difflib import SequenceMatcher  # noqa: F401
from typing import Optional  # noqa: F401

import structlog  # noqa: F401

from infona_client.agent.capabilities.normalize_cap import NormalizeCapability
from infona_client.agent.kg_scope import SCOPE_REQUIRE
from infona_client.agent.registry import AgentContext, PlanStep  # noqa: F401
from infona_client.enrichment.models import (  # noqa: F401
    EnrichJob,
    EnrichScope,
    EnrichmentTier,
    JobStatus,
)
from infona_client.pipeline.stage_trace import stamp_enrichment_job_created  # noqa: F401
from infona_client.enrichment.tier_router import (  # noqa: F401
    DEFAULT_CONFIDENCE_MIN as _DEFAULT_CONFIDENCE_MIN,
)
from infona_client.enrichment.tier_router import (  # noqa: F401
    WEB_CONFIDENCE_MIN as _WEB_CONFIDENCE_MIN,
)
from infona_client.enrichment.tier_router import (  # noqa: F401
    resolve_chain_cost as _resolve_chain_cost,
)
from infona_client.graph.queries import kg_graph_uri, tenant_graph_uri  # noqa: F401
from infona_client.normalization.inference import (  # noqa: F401 — monkeypatch surface
    list_type_schema,
    sample_predicate_values,
)
from infona_client.resolver.llm_router import PRIMARY_MODEL, openrouter_chat  # noqa: F401
from infona_client.web_sources.url_extract import extract_urls  # noqa: F401

from infona_client.agent.capabilities.enrich_clarify import (  # noqa: F401
    _attr_match_clarify_step,
    _discover_option,
    _no_match_clarify_step,
    _no_value_match_clarify_step,
    _subset_clarify_step,
)
from infona_client.agent.capabilities.enrich_common import (  # noqa: F401
    _COMPOSITE_DELIMS,
    _DEFAULT_PLAN_LIMIT,
    _LIST_SPLIT_RE,
    _SUBSET_MAX,
    _bg_tasks,
    _host,
    _spawn,
    _split_scope_values,
    logger,
)
from infona_client.agent.capabilities.enrich_cost import (  # noqa: F401
    _REGISTRY_SOURCE_LABELS,
    _coerce_tier,
    _confidence_note,
    _estimate_cost,
    _registry_covers_safe,
    _source_clause,
)
from infona_client.agent.capabilities.enrich_execute import EnrichExecuteMixin
from infona_client.agent.capabilities.enrich_extract import (  # noqa: F401
    _ATTR_TRIGGER,
    _EXTRACT_SYSTEM,
    _EXTRACT_USER_TEMPLATE,
    _SCOPE_REL,
    _SCOPE_VERB_LEMMA,
    _WITH_ATTRS,
    _extract_enrich_request,
    _parse_enrich_instruction,
    _parse_json_object,
)
from infona_client.agent.capabilities.enrich_intent import (  # noqa: F401
    _REFRESH_RE,
    _REPLACE_GOAL_RE,
    _REPLACE_VERB_RE,
    _default_conflict_policy,
    _looks_composite,
    _looks_like_overwrite,
    _looks_like_refresh,
    _overwrite_conflict_policy,
    _refresh_conflict_policy,
)
from infona_client.agent.capabilities.enrich_plan import EnrichPlanMixin
from infona_client.agent.capabilities.enrich_types import (  # noqa: F401
    _WORD_RE,
    _camel_words,
    _first_phrase_index,
    _list_types,
    _match_type_in_text,
    _resolve_target_type,
    _singularize,
    _tokenize_for_match,
    _type_match_index,
)
from infona_client.agent.capabilities.enrich_validate import (  # noqa: F401
    _ATTR_AUTO_SIMILARITY,
    _ATTR_LABEL_RE,
    _ATTR_SUFFIX_SCORE,
    _ATTR_WEAK_FULL_SIMILARITY,
    _SOFT_ATTR_MIN_LEN,
    _STOPWORDS,
    _WEB_FACT_HINTS,
    _AttrMatch,
    _is_type_name,
    _normalize_attr,
    _resolve_schema_attr,
    _similarity,
    _split_attr_list,
    _tier_for_attributes,
    _validate_enrich_request,
)


class EnrichCapability(EnrichPlanMixin, EnrichExecuteMixin):
    name = "enrich"
    # ONTA-428: enrichment FILLS attributes on entities that already exist. A
    # nonexistent graph resolves an empty scope, so the run would burn (paid)
    # provider calls or report success over zero rows. ONTA-426: an omitted name
    # previously fell through to the tenant BASE graph, writing enriched values
    # into a graph the user never named. The planner gates both.
    kg_scope_policy = SCOPE_REQUIRE

    def __init__(self, normalize: NormalizeCapability | None = None) -> None:
        # Reuse the normalize capability to BUILD the prerequisite step so the
        # clean-before-enrich logic lives in exactly one place.
        self._normalize = normalize or NormalizeCapability()

    def describe(self) -> str:
        return (
            "Fill in or verify missing attributes on a type by looking them up "
            "from external sources (enrichment). Use for 'enrich', 'fill in', "
            "'look up', 'find the <attribute> for <type>' requests, optionally "
            "scoped (e.g. 'for managers', 'who speak Persian')."
        )
