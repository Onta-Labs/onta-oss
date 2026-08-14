"""Web-discovery capability — find a NEW set of records on the web and ingest them.

Public type: :class:`WebIngestCapability`. Implementation lives in sibling
``web_ingest_*.py`` modules (plan / fetch / project / write). Every previously
importable name is re-exported here.

Invariants other agents must not break:
- Writes go through ``insert_facts`` / ``refresh_after_write`` (resolver ingest
  seams). Do not fork a second write path.
- Relationship instance edges on ``onto/<leaf>``. Entity IRIs via ``entity_uri``.
- Retrieval stays on ``infona_client/retrieval/`` + registered providers.
- BYOR: OSS registers no default fetcher. ``plan`` degrades to "not enabled"
  until a provider is registered.
"""
from __future__ import annotations

import asyncio
import os

import structlog

from infona_client.agent.kg_scope import SCOPE_CREATE
from infona_client.graph.kg_writer import refresh_after_write
from infona_client.graph.ontology_queries import entity_uri  # noqa: F401 — mint only via this
from infona_client.api_registry import route_query  # noqa: F401 — tests patch this
from infona_client.normalization.inference import list_type_schema  # noqa: F401
from infona_client.resolver.llm_router import (  # noqa: F401
    OPENROUTER_BASE,
    PRIMARY_MODEL,
    openrouter_chat,
)

logger = structlog.stdlib.get_logger("infona.agent.web_ingest")

_bg_tasks: set[asyncio.Task] = set()

# Rows requested for the cheap plan-time sample (preview + datatype inference).
_SAMPLE_ROWS = 8
_PREVIEW_SAMPLE = 5
_PREVIEW_SOURCES = 5
_DEFAULT_PLAN_CAP = max(1, int(os.environ.get("INFONA_DISCOVERY_DEFAULT_CAP", "50")))
_SAMPLE_BUDGET_S = float(os.environ.get("INFONA_WEB_SAMPLE_BUDGET_S", "22"))
_SHAPE_BUDGET_S = float(os.environ.get("INFONA_WEB_SHAPE_BUDGET_S", "15"))
_RUN_TIMEOUT_S = float(os.environ.get("INFONA_DISCOVERY_RUN_TIMEOUT_S", "600"))
_PREVIEW_GATE_USD = float(os.environ.get("INFONA_WEB_PREVIEW_GATE_USD", "0.50"))
_DISCOVERY_SOFT_EXTRACT = (
    os.environ.get("INFONA_DISCOVERY_SOFT_EXTRACT", "1") != "0"
)
_DISCOVERY_STRUCTURED_FASTPATH = (
    os.environ.get("INFONA_DISCOVERY_STRUCTURED_FASTPATH", "1") != "0"
)
_DISCOVERY_INGEST_SUBBATCH = max(
    1, int(os.environ.get("INFONA_DISCOVERY_INGEST_SUBBATCH", "5"))
)
_DISCOVERY_FANOUT_WARN_RATIO = float(
    os.environ.get("INFONA_DISCOVERY_FANOUT_WARN_RATIO", "2.0")
)


async def _refresh_after_discovery_write(client, **kwargs):
    """Shared post-write housekeeping. Mixins call this (or ``refresh_after_write``
    via this module) so tests that patch ``web_ingest_cap.refresh_after_write``
    keep working. Do not fork a second refresh."""
    return await refresh_after_write(client, **kwargs)


# Mixins after host names so ``import web_ingest_cap as _wic`` sees flags / logger.
from infona_client.agent.capabilities.web_ingest_text import (  # noqa: E402,F401
    _LEAD_FILLER,
    _META_FRAMING_RE,
    _answer_step,
    _as_list,
    _clean_query,
    _current_request,
    _dedupe,
    _explicit_user_fields,
    _explicit_user_type,
    _parse_json_object,
    _pascal,
    _slug,
    _snap_to_declared,
    _strip_inline_annotations,
    _strip_type_stopwords,
    _singularize,
)
from infona_client.agent.capabilities.web_ingest_fetch import (  # noqa: E402,F401
    SOURCE_URL_ATTR,
    _MAX_REQUEST_TRACES_PER_PROVIDER,
    _attach_source_urls,
    _emit_source_bundle,
    _host,
    _merge_registry_ensemble,
    _platforms,
    _provider_secret_refs,
    _provider_tier,
    _rebuild_registry_sources,
    _record_locate_trace,
    _record_provider_skip,
    _record_requests,
    _registry_card,
    _registry_route,
    _row_source_url,
    _spawn,
    _tenant_catalog,
)
from infona_client.agent.capabilities.web_ingest_project import (  # noqa: E402,F401
    StructuralGateResult,
    _chunk_rows,
    _drop_suppressed_rows,
    _group_rows_by_source_url,
    _screen_a1_rows,
    _structural_identity_keys,
    apply_post_a1_structural_gates,
)
from infona_client.agent.capabilities.web_ingest_plan_enum import (  # noqa: E402,F401
    _DEDUPE_SIGNAL_COLS,
    _MAX_SUBQUERIES,
    _authoritative_list_query,
    _dedupe_rows,
    _dedupe_rows_with_source_urls,
    _ensure_enumeration_partition,
    _expand_enumeration_ensemble,
    _is_enumeration_ask,
    _norm_key_part,
    _norm_subqueries,
    _row_key,
    _synthesize_enumeration_subqueries,
)
from infona_client.agent.capabilities.web_ingest_plan_spec import (  # noqa: E402,F401
    _DEGRADED_NOTE,
    _DEFAULT_CORE_CAP,
    _FALLBACK_CORE_MAX,
    _SPEC_SYSTEM,
    _clarify_step,
    _core_attrs,
    _fallback_spec,
    _norm_query_kind,
    _normalize_spec,
    _resolve_spec,
)
from infona_client.agent.capabilities.web_ingest_plan_preview import (  # noqa: E402,F401
    _build_resolver,
    _empty_sample_message,
    _estimate_cost,
    _estimate_cost_multi,
    _flat_shape,
    _paid_call_count,
    _preview_shape,
    _preview_summary,
    _provider_context,
    _step_cost,
)
from infona_client.agent.capabilities.web_ingest_job import (  # noqa: E402,F401
    _build_stage_contracts,
    _fail_billing_job,
    _fail_job,
    _finalize_stage_trace_failed,
    _finish_job,
)
from infona_client.agent.capabilities.web_ingest_plan import (  # noqa: E402
    WebIngestPlanMixin,
)
from infona_client.agent.capabilities.web_ingest_execute import (  # noqa: E402
    WebIngestExecuteMixin,
)


class WebIngestCapability(WebIngestPlanMixin, WebIngestExecuteMixin):
    """Discover a NEW set of records from the web and ingest them.

    Orchestrator only — plan / fetch / project / write live in sibling modules.
    """

    name = "web_ingest"
    # Discovery MINTS records that do not exist yet, so a missing target graph
    # is a legitimate cold start (``ensure_kg_registered``). The planner does
    # not refuse this rail (ONTA-428); ``plan`` surfaces the create on the card.
    kg_scope_policy = SCOPE_CREATE
